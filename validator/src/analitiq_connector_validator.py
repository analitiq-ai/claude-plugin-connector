#!/usr/bin/env python3
"""Validate an Analitiq connector or endpoint document.

Layer 1: JSON Schema validation against the published schema URL (Draft 2020-12).
Layer 2: Semantic validators encoding rules that JSON Schema can't express.

Output: a single Diagnostics JSON object on stdout. Exit 0 iff `passed` is true.

Schemas are fetched from the published host (schemas.analitiq.ai), and
authored documents declare the same host in their `$schema` field — the
`$schema` const inside each schema locks that.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

CONNECTOR_SCHEMA_URL = "https://schemas.analitiq.ai/connector/latest.json"

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:
    print(
        json.dumps(
            {
                "passed": False,
                "findings": [
                    {
                        "validator": "json-schema",
                        "severity": "error",
                        "path": "",
                        "message": f"Missing dependency: {exc}. Install with `pip install jsonschema`.",
                    }
                ],
            }
        )
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Schema fetch + cache
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".cache" / "analitiq" / "schemas"


def fetch_schema(url: str, cache: bool = True) -> dict:
    """Fetch a JSON schema from URL with atomic disk cache.

    Parses the JSON response *before* writing to disk so a malformed
    response can never poison the cache. Writes via a temp file +
    `os.replace` so a Ctrl-C mid-write leaves no truncated cache file.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(url.encode()).hexdigest()[:16]
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if cache and cache_path.exists():
        return json.loads(cache_path.read_text())
    with urllib.request.urlopen(url, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"schema fetch returned HTTP {resp.status} for {url}")
        body = resp.read().decode()
    schema = json.loads(body)
    tmp_path = cache_path.with_suffix(".tmp")
    tmp_path.write_text(body)
    os.replace(tmp_path, cache_path)
    return schema


# ---------------------------------------------------------------------------
# Diagnostics helpers
# ---------------------------------------------------------------------------

VALIDATOR_IDS = {
    "json-schema",
    "reserved-field",
    "expression-resolver",
    "phase-resolvability",
    "transport-ref",
    "dsn-binding",
    "auth-shape",
    "tls-consistency",
    "type-map-coverage",
    "type-map-rule",
    "type-map-write-coverage",
    "endpoint-annotations",
    "endpoint-filename",
}


def finding(
    validator: str,
    severity: str,
    path: str,
    message: str,
    rule_doc: str | None = None,
) -> dict:
    # Use explicit `raise` (not `assert`) so the invariant checks survive
    # `python -O`, which strips assertions. A silently-emitted finding with
    # an unregistered validator id or unknown severity would defeat the
    # whole crash-handler contract.
    if validator not in VALIDATOR_IDS:
        raise ValueError(f"unknown validator id: {validator!r}")
    if severity not in ("error", "warning"):
        raise ValueError(f"unknown severity: {severity!r}")
    out = {
        "validator": validator,
        "severity": severity,
        "path": path,
        "message": message,
    }
    if rule_doc:
        out["rule_doc"] = rule_doc
    return out


# ---------------------------------------------------------------------------
# Layer 1 — JSON Schema validation
# ---------------------------------------------------------------------------


def layer1_jsonschema(document: dict, schema: dict) -> list[dict]:
    """Run Draft 2020-12 validation, mapping each error to a finding."""
    validator = Draft202012Validator(schema)
    findings: list[dict] = []
    for err in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        path = "/" + "/".join(str(p) for p in err.absolute_path)
        findings.append(
            finding(
                "json-schema",
                "error",
                path,
                err.message,
                rule_doc="https://schemas.analitiq.ai/",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Layer 2 — Semantic validators
# ---------------------------------------------------------------------------

RESERVED_FIELDS = {
    "created_at",
    "updated_at",
}

KNOWN_SCOPES = {
    "secrets",
    "connection.parameters",
    "connection.selections",
    "connection.discovered",
    "auth",
    "runtime",
    "stream",
}

KNOWN_FUNCTIONS = {
    "basic_auth",
    "jwt_sign",
    "url_encode",
}

# The closed DSN-binding encoding vocabulary is owned by the published
# connector schema ($defs/DsnBinding/properties/encoding). Derive it from the
# live schema so this validator never drifts from the contract; the literal
# below is the offline fallback and documents the expected set.
_FALLBACK_ENCODINGS = {
    "raw",
    "host",
    "url_userinfo",
    "url_path_segment",
    "url_query_key",
    "url_query_value",
}


def _enum_at(schema: dict, *path: str) -> set[str] | None:
    """Return the `enum` set at a `$defs`/properties path, or None if absent."""
    node: Any = schema
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    enum = node.get("enum") if isinstance(node, dict) else None
    return set(enum) if isinstance(enum, list) else None


def known_encodings() -> tuple[frozenset[str], bool]:
    """Closed DSN-binding `encoding` enum, read from the live connector schema.

    Returns `(enum, derived_from_live)`. `derived_from_live` is False when the
    enum fell back to `_FALLBACK_ENCODINGS` — either the schema host was
    unreachable (offline / infra) or the enum is no longer at the expected
    pointer (the contract moved). The caller surfaces that as a `dsn-binding`
    warning rather than silently validating against a possibly-stale copy.

    Only the expected fetch/parse failures are absorbed; a bug in `_enum_at`
    or any other unexpected error propagates so the per-validator crash
    handler reports it loudly instead of masquerading as "offline".
    """
    try:
        schema = fetch_schema(CONNECTOR_SCHEMA_URL)
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        RuntimeError,
        UnicodeDecodeError,
    ):
        return frozenset(_FALLBACK_ENCODINGS), False
    derived = _enum_at(schema, "$defs", "DsnBinding", "properties", "encoding")
    if derived:
        return frozenset(derived), True
    return frozenset(_FALLBACK_ENCODINGS), False


def check_reserved_fields(doc: dict) -> list[dict]:
    findings = []
    if not isinstance(doc, dict):
        return findings
    for field in RESERVED_FIELDS:
        if field in doc:
            findings.append(
                finding(
                    "reserved-field",
                    "error",
                    f"/{field}",
                    f"Reserved server-managed field '{field}' must not appear in authored documents.",
                    rule_doc="connectors/connector-schema-parameterization.md#server-managed-and-reserved-fields",
                )
            )
    return findings


def _walk(node: Any, path: str = ""):
    """Yield (path, node) pairs for every nested object in the document."""
    if isinstance(node, dict):
        yield path or "/", node
        for k, v in node.items():
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}/{i}")


_EXPRESSION_KEYS = ("ref", "template", "literal", "function")


def _is_value_expression(node: Any) -> str | None:
    """Return the expression kind, a malformation sentinel, or None.

    Return values:
    - `"ref"` / `"template"` / `"function"` — corresponding key present
      with a string value (the legal value-expression shapes).
    - `"literal"` — `literal` key present (any value type is valid per
      the contract; `literal` payloads are opaque to the validator).
    - `"malformed-ref"` / `"malformed-template"` / `"malformed-function"`
      — corresponding key present but value is non-string.
    - `"multi-keyed"` — more than one of the four expression keys is
      present. The contract requires exactly one (`oneOf`); Layer 1
      rejects multi-keyed nodes, but `--semantic-only` would otherwise
      let them through by silently picking the first kind in iteration
      order.
    - `None` — node is not a value expression (no expression keys, or
      not a dict).
    """
    if not isinstance(node, dict):
        return None
    present = [k for k in _EXPRESSION_KEYS if k in node]
    if not present:
        return None
    if len(present) > 1:
        return "multi-keyed"
    kind = present[0]
    if kind == "literal":
        return "literal"
    return kind if isinstance(node[kind], str) else f"malformed-{kind}"


_SINGLE_TOKEN_SCOPES = {s for s in KNOWN_SCOPES if "." not in s}
_MULTI_TOKEN_SCOPE_HEADS = {s.split(".", 1)[0] for s in KNOWN_SCOPES if "." in s}

# Response-extraction namespace. The `response` scope reads from the provider's
# HTTP *response* (the record selector, pagination cursors/links, and
# `stop_when`/`success_when` predicates), as opposed to the request-side
# `KNOWN_SCOPES` above which *construct* the outgoing request. Per the published
# value-expression contract (`shared/value-expression-parameterization.md`, the
# "context scopes" table) `response` is a first-class scope alongside the
# request-side ones; its sub-paths (`response.body.*`, `response.headers.*`, …)
# all hang off this single head.
#
# It is kept DELIBERATELY OUT of `KNOWN_SCOPES`: it is legal ONLY at
# response-extraction sites (see `_is_response_extraction_path`) and must stay
# rejected in request-construction slots. Folding it into the global set would
# trade the old false positive (flagging a spec-mandated `response.body`) for a
# false negative (silently accepting a header that references the
# not-yet-existent response). The gating is positional.
#
# The contract's scope table also lists `request`, `connector`, and `state`;
# those are not yet exercised by any authored artifact, so they are left out
# until a real use appears (they would need the same position-aware handling).
_RESPONSE_SCOPE_HEADS = {"response"}

# An endpoint document is a tree of operations; value expressions under an
# operation's `response` or `pagination` subtree extract from the provider's
# response, so the `response.*` namespace is valid there and only there. The
# alternation mirrors the api-endpoint contract's two operation shapes:
#   - `operations.read`  — a single object carrying `response` and `pagination`;
#   - `operations.write` — a mode-keyed map (`insert`/`upsert`), each mode an
#     object carrying `response` (writes have no `pagination`).
# So read response refs sit at `/operations/read/{response,pagination}/…` and
# write response refs one level deeper at `/operations/write/<mode>/response/…`.
# Anchored at `/operations/…` so a request slot — including a request body field
# that happens to be named "response"/"pagination" — is never mistaken for a
# response-extraction site.
_RESPONSE_EXTRACTION_PATH = re.compile(
    r"^/operations/(?:read/(?:response|pagination)|write/[^/]+/response)(?:/|$)"
)


def _is_response_extraction_path(path: str) -> bool:
    """True when `path` points inside an operation's response-extraction region.

    Response-extraction sites are an operation's `response` subtree (and, for
    read operations, `pagination`), where value expressions read from the
    provider's response — the record selector, pagination cursor/link, and the
    `stop_when` / `success_when` predicates — rather than constructing the
    request. The `response.*` namespace (`_RESPONSE_SCOPE_HEADS`) is permitted
    here and nowhere else.
    """
    return bool(_RESPONSE_EXTRACTION_PATH.match(path))


def _scope_is_known(dotted: str, *, response_ok: bool = False) -> bool:
    """Decide whether a dotted path targets a known scope.

    For single-token scopes (`secrets`, `auth`, `runtime`, `stream`),
    the head alone is enough. For multi-token scope heads like
    `connection`, the *two-token* prefix must be one of the registered
    scopes — `connection.bogus.x` is rejected.

    `response_ok` is set only at response-extraction sites (see
    `_is_response_extraction_path`); it additionally accepts the
    response-extraction namespace (`response.*`), which is a validation
    error everywhere else.
    """
    head_one = dotted.split(".", 1)[0]
    if response_ok and head_one in _RESPONSE_SCOPE_HEADS:
        return True
    if head_one in _SINGLE_TOKEN_SCOPES:
        return True
    if head_one in _MULTI_TOKEN_SCOPE_HEADS:
        head_two = ".".join(dotted.split(".", 2)[:2])
        return head_two in KNOWN_SCOPES
    return False


def _known_scopes_display(response_ok: bool) -> list[str]:
    """Sorted scope names for an unknown-scope finding, position-aware."""
    scopes = set(KNOWN_SCOPES)
    if response_ok:
        scopes |= _RESPONSE_SCOPE_HEADS
    return sorted(scopes)


def check_expressions(doc: dict) -> list[dict]:
    findings: list[dict] = []
    ref_pattern = re.compile(r"^([a-z_]+(?:\.[a-z_]+)*)(?:\.[A-Za-z0-9_-]+)*$")
    # `[^}]*` (not `+`) so an empty `${}` is captured and flagged below; with
    # `+` it matched nothing and slipped through to corrupt the value at runtime.
    template_var = re.compile(r"\$\{([^}]*)\}")
    for path, node in _walk(doc):
        kind = _is_value_expression(node)
        if not kind:
            continue
        # Value expressions under an operation's response/pagination subtree
        # extract from the provider's response, so the response-extraction
        # namespace (`response.*`) is valid there; everywhere else it stays
        # rejected.
        response_ok = _is_response_extraction_path(path)
        if kind == "multi-keyed":
            present = sorted(k for k in _EXPRESSION_KEYS if k in node)
            findings.append(
                finding(
                    "expression-resolver",
                    "error",
                    path,
                    f"value expression must declare exactly one of {list(_EXPRESSION_KEYS)}; got {present}.",
                    rule_doc="shared/value-expression-parameterization.md",
                )
            )
            continue
        if kind.startswith("malformed-"):
            expr_kind = kind.removeprefix("malformed-")
            bad_value = node[expr_kind]
            findings.append(
                finding(
                    "expression-resolver",
                    "error",
                    path,
                    f"{expr_kind!r} must be a string; got {type(bad_value).__name__}.",
                    rule_doc="shared/value-expression-parameterization.md",
                )
            )
            continue
        if kind == "ref":
            ref = node["ref"]
            scope_match = ref_pattern.match(ref)
            if not scope_match:
                findings.append(
                    finding(
                        "expression-resolver",
                        "error",
                        path,
                        f"ref '{ref}' is not a valid dotted path.",
                        rule_doc="shared/value-expression-parameterization.md",
                    )
                )
                continue
            if not _scope_is_known(ref, response_ok=response_ok):
                findings.append(
                    finding(
                        "expression-resolver",
                        "error",
                        path,
                        f"ref '{ref}' targets unknown scope. Known scopes: {_known_scopes_display(response_ok)}.",
                        rule_doc="shared/value-expression-parameterization.md",
                    )
                )
        elif kind == "template":
            for var in template_var.findall(node["template"]):
                if not var.strip():
                    findings.append(
                        finding(
                            "expression-resolver",
                            "error",
                            path,
                            "empty template variable '${}' resolves to nothing at runtime.",
                            rule_doc="shared/value-expression-parameterization.md",
                        )
                    )
                elif not _scope_is_known(var, response_ok=response_ok):
                    findings.append(
                        finding(
                            "expression-resolver",
                            "error",
                            path,
                            f"template variable '${{{var}}}' targets unknown scope. "
                            f"Known scopes: {_known_scopes_display(response_ok)}.",
                            rule_doc="shared/value-expression-parameterization.md",
                        )
                    )
            # A `${` with no closing `}` is not extracted by the loop above, so
            # it would otherwise pass silently and survive as a literal at
            # runtime. Literal `{`/`}` are legitimate here (e.g. a JSON body
            # template), so only a dangling `${` opener is flagged — detected as
            # a `${` left in the string after the well-formed vars are removed.
            if "${" in template_var.sub("", node["template"]):
                findings.append(
                    finding(
                        "expression-resolver",
                        "error",
                        path,
                        "unclosed template variable: '${' without a closing '}' survives as a literal at runtime.",
                        rule_doc="shared/value-expression-parameterization.md",
                    )
                )
        elif kind == "function":
            fn = node["function"]
            if fn not in KNOWN_FUNCTIONS:
                findings.append(
                    finding(
                        "expression-resolver",
                        "error",
                        path,
                        f"function '{fn}' is not in the registered catalog: {sorted(KNOWN_FUNCTIONS)}.",
                        rule_doc="shared/value-expression-parameterization.md",
                    )
                )
    return findings


def check_transport_refs(doc: dict) -> list[dict]:
    findings: list[dict] = []
    if "transports" not in doc:
        findings.append(
            finding(
                "transport-ref",
                "error",
                "/transports",
                "transports is required on connector documents; Layer 1 enforces this — rerun without `--semantic-only` to see the schema error.",
                rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
            )
        )
        return findings
    transports = doc["transports"]
    if not isinstance(transports, dict):
        findings.append(
            finding(
                "transport-ref",
                "error",
                "/transports",
                f"transports must be an object; got {type(transports).__name__}.",
                rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
            )
        )
        return findings
    transport_keys = set(transports.keys())
    default = doc.get("default_transport")
    if default is not None and default not in transport_keys:
        findings.append(
            finding(
                "transport-ref",
                "error",
                "/default_transport",
                f"default_transport '{default}' is not defined in transports {sorted(transport_keys)}.",
                rule_doc="connectors/connector-schema-parameterization.md#transport-selection",
            )
        )
    for path, node in _walk(doc):
        if not isinstance(node, dict) or "transport_ref" not in node:
            continue
        ref = node["transport_ref"]
        if not isinstance(ref, str):
            findings.append(
                finding(
                    "transport-ref",
                    "error",
                    f"{path}/transport_ref",
                    f"transport_ref must be a string; got {type(ref).__name__}.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-selection",
                )
            )
            continue
        if ref not in transport_keys:
            findings.append(
                finding(
                    "transport-ref",
                    "error",
                    f"{path}/transport_ref",
                    f"transport_ref '{ref}' is not defined in transports {sorted(transport_keys)}.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-selection",
                )
            )
    return findings


def check_dsn_bindings(doc: dict) -> list[dict]:
    findings: list[dict] = []
    transports = doc.get("transports", {})
    if not isinstance(transports, dict):
        return findings  # `check_transport_refs` already emitted the structural error
    encodings, encodings_from_live = known_encodings()
    offline_encoding_warned = False
    # `[^}]*` (not `+`) so an empty `{}` is captured and flagged below; with
    # `+` it matched nothing and slipped through to corrupt the DSN URL at
    # runtime (same bug class as the `${}` value-expression/type-map sites).
    placeholder_re = re.compile(r"\{([^}]*)\}")
    for tname, tspec in transports.items():
        if not isinstance(tspec, dict):
            findings.append(
                finding(
                    "dsn-binding",
                    "error",
                    f"/transports/{tname}",
                    f"transport entry must be an object; got {type(tspec).__name__}.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                )
            )
            continue
        if "dsn" not in tspec:
            continue  # dsn is optional for some transport types (e.g. http)
        dsn = tspec["dsn"]
        if not isinstance(dsn, dict):
            findings.append(
                finding(
                    "dsn-binding",
                    "error",
                    f"/transports/{tname}/dsn",
                    f"dsn must be an object; got {type(dsn).__name__}.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                )
            )
            continue
        if "kind" not in dsn:
            findings.append(
                finding(
                    "dsn-binding",
                    "error",
                    f"/transports/{tname}/dsn",
                    "dsn.kind is required (must be 'url_template'); Layer 1 enforces this — rerun without `--semantic-only` to see the schema error.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                )
            )
            continue
        dsn_kind = dsn["kind"]
        if dsn_kind != "url_template":
            # Unknown / typo'd dsn.kind (Layer 1 catches via enum; surface
            # under `--semantic-only` so an author with a typo doesn't get
            # a silent green).
            findings.append(
                finding(
                    "dsn-binding",
                    "error",
                    f"/transports/{tname}/dsn/kind",
                    f"dsn.kind must be 'url_template' (the only kind defined today); got {dsn_kind!r}.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                )
            )
            continue
        path_prefix = f"/transports/{tname}/dsn"
        template = dsn.get("template", "")
        if not isinstance(template, str):
            # A non-string template would crash `re.findall` below; surface
            # explicitly instead of letting the top-level per-validator
            # guard report it as a generic "validator crashed" finding.
            findings.append(
                finding(
                    "dsn-binding",
                    "error",
                    f"{path_prefix}/template",
                    f"template must be a string; got {type(template).__name__}.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                )
            )
            continue
        bindings = dsn.get("bindings", {})
        if not isinstance(bindings, dict):
            # A non-dict bindings would silently produce misdirected
            # "placeholder has no matching binding" errors; surface the
            # root cause instead. Layer 1 catches via type.
            findings.append(
                finding(
                    "dsn-binding",
                    "error",
                    f"{path_prefix}/bindings",
                    f"bindings must be an object; got {type(bindings).__name__}.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                )
            )
            continue
        raw_placeholders = placeholder_re.findall(template)
        if any(not ph.strip() for ph in raw_placeholders):
            findings.append(
                finding(
                    "dsn-binding",
                    "error",
                    f"{path_prefix}/template",
                    "empty placeholder '{}' has no binding name and resolves to nothing at runtime.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                )
            )
        # Braces are reserved for `{placeholder}` markers in a DSN template, so
        # any `{` or `}` left after the well-formed markers are removed is an
        # unclosed/unbalanced brace (e.g. `{abc` with no closer) that would
        # otherwise pass silently and corrupt the connection string at runtime.
        residue = placeholder_re.sub("", template)
        if "{" in residue or "}" in residue:
            findings.append(
                finding(
                    "dsn-binding",
                    "error",
                    f"{path_prefix}/template",
                    "template has an unbalanced or unclosed brace outside a well-formed '{placeholder}', which corrupts the connection string at runtime.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                )
            )
        placeholders = {ph for ph in raw_placeholders if ph.strip()}
        binding_keys = set(bindings.keys())
        for ph in placeholders - binding_keys:
            findings.append(
                finding(
                    "dsn-binding",
                    "error",
                    f"{path_prefix}/template",
                    f"placeholder '{{{ph}}}' has no matching binding.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                )
            )
        for bk in binding_keys - placeholders:
            findings.append(
                finding(
                    "dsn-binding",
                    "warning",
                    f"{path_prefix}/bindings/{bk}",
                    f"binding '{bk}' is not referenced by the template.",
                    rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                )
            )
        # bindings is guaranteed dict here — the earlier non-dict guard returns.
        for bk, bspec in bindings.items():
            if not isinstance(bspec, dict):
                findings.append(
                    finding(
                        "dsn-binding",
                        "error",
                        f"{path_prefix}/bindings/{bk}",
                        f"binding must be an object with 'value' and 'encoding'; got {type(bspec).__name__}.",
                        rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                    )
                )
                continue
            enc = bspec.get("encoding")
            if enc is None:
                continue
            if not encodings_from_live and not offline_encoding_warned:
                findings.append(
                    finding(
                        "dsn-binding",
                        "warning",
                        f"{path_prefix}/bindings/{bk}/encoding",
                        "DSN-binding `encoding` enum could not be derived from the live "
                        "connector schema (host unreachable or contract moved); validated "
                        "against the offline fallback set. Re-run with network access to "
                        f"verify against {CONNECTOR_SCHEMA_URL}.",
                        rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                    )
                )
                offline_encoding_warned = True
            if enc not in encodings:
                findings.append(
                    finding(
                        "dsn-binding",
                        "error",
                        f"{path_prefix}/bindings/{bk}/encoding",
                        f"encoding '{enc}' is not in the closed enum {sorted(encodings)}.",
                        rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
                    )
                )
    return findings


def check_auth_shape(doc: dict) -> list[dict]:
    findings: list[dict] = []
    if "auth" not in doc:
        return findings  # absence is owned by Layer 1 (required key)
    auth = doc["auth"]
    if not isinstance(auth, dict):
        findings.append(
            finding(
                "auth-shape",
                "error",
                "/auth",
                f"auth must be an object with a `type` key; got {type(auth).__name__}.",
                rule_doc="connectors/connector-schema-parameterization.md#authentication",
            )
        )
        return findings
    atype = auth.get("type")
    if atype == "oauth2_authorization_code":
        for required in ("authorize", "token_exchange"):
            if required not in auth:
                findings.append(
                    finding(
                        "auth-shape",
                        "error",
                        f"/auth/{required}",
                        f"oauth2_authorization_code requires '{required}'.",
                        rule_doc="connectors/connector-schema-parameterization.md#authentication",
                    )
                )
    elif atype == "oauth2_client_credentials":
        if "token_exchange" not in auth:
            findings.append(
                finding(
                    "auth-shape",
                    "error",
                    "/auth/token_exchange",
                    "oauth2_client_credentials requires 'token_exchange'.",
                    rule_doc="connectors/connector-schema-parameterization.md#authentication",
                )
            )
        if "authorize" in auth:
            findings.append(
                finding(
                    "auth-shape",
                    "error",
                    "/auth/authorize",
                    "oauth2_client_credentials must omit 'authorize'.",
                    rule_doc="connectors/connector-schema-parameterization.md#authentication",
                )
            )
    elif atype == "none":
        for forbidden in ("authorize", "token_exchange", "refresh"):
            if forbidden in auth:
                findings.append(
                    finding(
                        "auth-shape",
                        "error",
                        f"/auth/{forbidden}",
                        f"auth.type 'none' must not declare '{forbidden}'.",
                        rule_doc="connectors/connector-schema-parameterization.md#authentication",
                    )
                )
    return findings


def check_tls_consistency(doc: dict) -> list[dict]:
    findings: list[dict] = []
    cc = doc.get("connection_contract")
    if not isinstance(cc, dict):
        return findings
    inputs = cc.get("inputs", {})
    if not isinstance(inputs, dict):
        return findings
    ssl_mode = inputs.get("ssl_mode")
    if ssl_mode is None:
        return findings  # no ssl_mode input declared; nothing to check
    if not isinstance(ssl_mode, dict):
        # ssl_mode declared but as a flat value instead of an input descriptor
        # (e.g. `"ssl_mode": "verify-full"` instead of
        # `"ssl_mode": {"source": "...", ..., "enum": [...]}`). Surface
        # explicitly; Layer 1 catches the type but `--semantic-only` bypasses.
        findings.append(
            finding(
                "tls-consistency",
                "error",
                "/connection_contract/inputs/ssl_mode",
                f"ssl_mode input must be an object descriptor (with `source`, `phase`, `enum`, …); got {type(ssl_mode).__name__}.",
                rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
            )
        )
        return findings
    enum = ssl_mode.get("enum")
    if enum is None:
        return findings
    if not isinstance(enum, list):
        # A non-list enum (e.g. `"verify-full"` instead of `["verify-full"]`)
        # would iterate character-by-character via the `any(...)` below,
        # silently mis-deciding the TLS branch. Layer 1 catches this — emit
        # explicitly under `--semantic-only`.
        findings.append(
            finding(
                "tls-consistency",
                "error",
                "/connection_contract/inputs/ssl_mode/enum",
                f"ssl_mode.enum must be a list; got {type(enum).__name__}.",
                rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
            )
        )
        return findings
    # The ssl_mode vocabulary is connector-defined (libpq-shaped systems
    # declare verify-ca/verify-full; MySQL/MariaDB declare
    # VERIFY_CA/VERIFY_IDENTITY) — normalize before matching so every
    # certificate-verification mode triggers the CA-input requirement.
    _verification_modes = {"verify-ca", "verify-full", "verify-identity"}
    requires_ca = any(
        isinstance(v, str)
        and v.lower().replace("_", "-") in _verification_modes
        for v in enum
    )
    has_ca_input = "ssl_ca_certificate" in inputs
    if requires_ca and not has_ca_input:
        findings.append(
            finding(
                "tls-consistency",
                "error",
                "/connection_contract/inputs",
                "ssl_mode allows a certificate-verification mode (verify-ca/verify-full/VERIFY_CA/VERIFY_IDENTITY) but ssl_ca_certificate input is not declared.",
                rule_doc="connectors/connector-schema-parameterization.md#transport-contracts",
            )
        )
    return findings


# Lifecycle phase ordering. Anything available in an earlier phase is also
# available in later ones. Index = phase rank (higher = later).
_PHASE_ORDER = ["pre_auth", "auth", "post_auth", "active"]


def _phase_le(a: str, b: str) -> bool:
    """Return True iff phase `a` is reachable in phase `b` (a runs no later than b)."""
    try:
        return _PHASE_ORDER.index(a) <= _PHASE_ORDER.index(b)
    except ValueError:
        return False


# Closed `runtime.*` set per `shared/lifecycle-phases.md`.
_GENERIC_RUNTIME_KEYS = {"run_id", "current_time", "batch_size"}
# Operation-local subkeys that can only be referenced inside an endpoint
# operation (request/response/pagination/cursor expressions). Connector-level
# templates cannot reach them. The validator does not currently walk endpoint
# operation templates, so any reference to these keys at the sites we *do*
# walk (transports, auth ops, post-auth ops) is an error.
_OPERATION_LOCAL_RUNTIME_KEYS = {"pagination"}
# `runtime.pagination.*` is itself a closed set per the spec.
_PAGINATION_RUNTIME_KEYS = {"offset"}
_OAUTH_RUNTIME_KEYS = {"code", "state", "redirect_uri", "pkce_verifier"}


_INDEXED_STORAGE = ("connection.parameters", "secrets")


def _index_inputs(doc: dict) -> tuple[dict[str, dict], list[dict]]:
    """Map storage-scoped reference path -> input record, for declared inputs.

    Keys are like `connection.parameters.host` and `secrets.password`.
    Values carry the `phase` so the resolvability check can assert it.

    Returns `(index, warnings)`. Warnings flag inputs the index dropped
    silently for shape reasons — non-dict spec, unknown `storage`, or
    unknown `phase`. Without these, downstream refs would surface
    misdirected "input is not declared" errors when the real problem
    is the input declaration itself.
    """
    out: dict[str, dict] = {}
    warnings: list[dict] = []
    cc = doc.get("connection_contract")
    if not isinstance(cc, dict):
        return out, warnings
    inputs = cc.get("inputs") or {}
    if not isinstance(inputs, dict):
        return out, warnings
    for name, spec in inputs.items():
        if not isinstance(spec, dict):
            warnings.append(
                finding(
                    "phase-resolvability",
                    "warning",
                    f"/connection_contract/inputs/{name}",
                    f"inputs.{name} must be an object; got {type(spec).__name__}.",
                    rule_doc="shared/lifecycle-phases.md",
                )
            )
            continue
        storage = spec.get("storage")
        phase = spec.get("phase", "pre_auth")
        if phase not in _PHASE_ORDER:
            warnings.append(
                finding(
                    "phase-resolvability",
                    "warning",
                    f"/connection_contract/inputs/{name}",
                    f"inputs.{name} declares phase {phase!r}, which is outside the closed phase enum {_PHASE_ORDER}. Refs to this input will surface as phase-mismatch errors at the referring site — fix the declaration here.",
                    rule_doc="shared/lifecycle-phases.md",
                )
            )
            continue
        if storage in _INDEXED_STORAGE:
            out[f"{storage}.{name}"] = {"phase": phase, "input_name": name, "via": "input"}
        else:
            # Storage value the resolver doesn't index — `None` (key absent),
            # a typo (`connection.parameter` singular), an unknown but legal
            # value (`connection.selections` lives elsewhere), or a future
            # enum addition. Surface so downstream "not declared" errors
            # don't misattribute. The post-auth-outputs sibling helper uses
            # the same "not in valid set" treatment; keeping these parallel
            # avoids silent drops on missing-storage inputs.
            warnings.append(
                finding(
                    "phase-resolvability",
                    "warning",
                    f"/connection_contract/inputs/{name}",
                    f"inputs.{name} declares storage {storage!r}; the input resolver indexes only {list(_INDEXED_STORAGE)}. Refs to this input will fail as 'not declared' — add or correct the `storage` field.",
                    rule_doc="shared/lifecycle-phases.md",
                )
            )
    return out, warnings


def _index_post_auth_outputs(doc: dict) -> tuple[dict[str, dict], list[dict]]:
    """Map produced reference paths to their post-auth output, plus warnings.

    Returns `(index, warnings)`. The index keys are the produced reference
    paths — derived as `storage + "." + <output key>` (e.g. the output
    `api_domain` with `storage: "connection.discovered"` produces
    `connection.discovered.api_domain`). This is the durable path that refs
    and `required_for_activation` target.

    The produced path is NOT `value_path`. Per the published `PostAuthOutput`
    schema, `value_path` (and `label_path` / `options_path`) is a
    **response-extraction path** — the field read out of the
    `options_request` / `discovery_request` response (e.g. an option object's
    `"id"`), not the materialized `connection.*` reference. Indexing by it,
    or requiring it to start with the `storage` prefix, contradicts the
    contract and mis-flags correctly-authored `user_selection` outputs.

    Warnings flag malformed entries:

    - non-dict spec entry (`{"foo": "bar-string"}`-style mistakes),
    - `storage` outside the allowed enum (`connection.discovered` /
      `connection.selections` / `secrets`) — these drop from the index,
    - missing / non-string / empty `value_path` (the response-extraction
      path is required and `minLength: 1` per the schema). The entry is
      still indexed by its produced path so downstream refs resolve; the
      warning surfaces the unusable extraction path on its own.

    Without surfacing these, downstream refs would emit misdirected
    "not declared" errors when the real fault is in the output
    declaration itself.
    """
    warnings: list[dict] = []
    out: dict[str, dict] = {}
    cc = doc.get("connection_contract")
    if not isinstance(cc, dict):
        return out, warnings
    post_auth = cc.get("post_auth_outputs") or {}
    if not isinstance(post_auth, dict):
        return out, warnings
    valid_storage = {"connection.discovered", "connection.selections", "secrets"}
    for name, spec in post_auth.items():
        if not isinstance(spec, dict):
            warnings.append(
                finding(
                    "phase-resolvability",
                    "warning",
                    f"/connection_contract/post_auth_outputs/{name}",
                    f"post_auth_outputs.{name} must be an object; got {type(spec).__name__}.",
                    rule_doc="shared/lifecycle-phases.md",
                )
            )
            continue
        storage = spec.get("storage")
        value_path = spec.get("value_path")
        if storage not in valid_storage:
            warnings.append(
                finding(
                    "phase-resolvability",
                    "warning",
                    f"/connection_contract/post_auth_outputs/{name}",
                    f"post_auth_outputs.{name} has unrecognized storage {storage!r}.",
                    rule_doc="shared/lifecycle-phases.md",
                )
            )
            continue
        if not isinstance(value_path, str) or not value_path:
            warnings.append(
                finding(
                    "phase-resolvability",
                    "warning",
                    f"/connection_contract/post_auth_outputs/{name}",
                    (
                        f"post_auth_outputs.{name} has a missing or empty value_path; "
                        f"it must be a non-empty response-extraction path (the field read "
                        f"out of the discovery/options response): {value_path!r}"
                    ),
                    rule_doc="shared/lifecycle-phases.md",
                )
            )
            # Still index the produced path: the reference path is derived from
            # storage + key, independent of value_path, so refs to it resolve.
        out[f"{storage}.{name}"] = {"storage": storage, "output_name": name}
    return out, warnings


def _ref_phase_problem(
    dotted: str,
    phase: str,
    auth_op: str | None,
    auth_type: str | None,
    input_idx: dict[str, dict],
    output_idx: dict[str, dict],
) -> str | None:
    """Return a human-readable error for `dotted` referenced in `phase`, or None.

    Handles every top-level scope (`runtime`, `auth`, `stream`, `state`,
    `connection`, `secrets`) directly — no caller-side OR-chaining.

    `auth_op` is one of "authorize", "token_exchange", "refresh", or None.
    """
    head = dotted.split(".", 1)[0]
    if head == "runtime":
        return _runtime_phase_problem(dotted, auth_op, auth_type)
    if head == "auth":
        if not _phase_le("post_auth", phase):
            return f"'auth.*' is not available before post_auth (current phase: {phase})."
        return None
    if head == "stream":
        if not _phase_le("active", phase):
            return f"'stream.*' is only available in the active phase (current phase: {phase})."
        return None
    if head == "state":
        if not _phase_le("active", phase):
            return f"'state.*' is only available in the active phase (current phase: {phase})."
        return None
    if head in ("secrets", "connection"):
        return _connection_or_secrets_phase_problem(dotted, phase, input_idx, output_idx)
    return None


def _runtime_phase_problem(
    dotted: str,
    auth_op: str | None,
    auth_type: str | None,
) -> str | None:
    parts = dotted.split(".", 2)
    if len(parts) < 2:
        return "'runtime' must be followed by a key (e.g. 'runtime.run_id')."
    sub = parts[1]
    if sub in _GENERIC_RUNTIME_KEYS:
        return None  # generic runtime is always available
    if sub in _OPERATION_LOCAL_RUNTIME_KEYS:
        sub_key = parts[2].split(".", 1)[0] if len(parts) >= 3 else ""
        if sub == "pagination" and sub_key not in _PAGINATION_RUNTIME_KEYS:
            return (
                f"'runtime.pagination.{sub_key}' is not in the closed set "
                f"{sorted(_PAGINATION_RUNTIME_KEYS)}."
            )
        # The validator does not currently walk endpoint operation templates,
        # so any reference at sites we *do* walk is out-of-context.
        return (
            f"'runtime.{sub}.*' is operation-local; "
            "it can only be referenced inside an endpoint operation template."
        )
    if sub == "oauth":
        if auth_type != "oauth2_authorization_code":
            return (
                f"'runtime.oauth.*' is only available when auth.type is "
                f"'oauth2_authorization_code' (current: {auth_type!r})."
            )
        oauth_key = parts[2].split(".", 1)[0] if len(parts) >= 3 else ""
        if oauth_key not in _OAUTH_RUNTIME_KEYS:
            return f"'runtime.oauth.{oauth_key}' is not in the closed set {sorted(_OAUTH_RUNTIME_KEYS)}."
        if auth_op == "refresh":
            return "'runtime.oauth.*' must not be referenced inside auth.refresh."
        if oauth_key == "code" and auth_op != "token_exchange":
            return f"'runtime.oauth.code' is only available inside auth.token_exchange (current op: {auth_op!r})."
        if auth_op not in ("authorize", "token_exchange"):
            return "'runtime.oauth.*' is only available in auth.authorize and auth.token_exchange."
        return None
    return f"'runtime.{sub}' is not in the registered closed set."


def _connection_or_secrets_phase_problem(
    dotted: str,
    phase: str,
    input_idx: dict[str, dict],
    output_idx: dict[str, dict],
) -> str | None:
    """Resolve refs into connection.parameters / connection.* / secrets."""
    head = dotted.split(".", 1)[0]
    if head == "secrets":
        primary = ".".join(dotted.split(".", 2)[:2])  # `secrets.password`
        record = input_idx.get(primary)
        if record is not None:
            if not _phase_le(record["phase"], phase):
                return (
                    f"'{primary}' is declared in phase '{record['phase']}' "
                    f"and is not available in '{phase}'."
                )
            return None
        if primary in output_idx:
            if not _phase_le("post_auth", phase):
                return f"'{primary}' is produced post-auth and is not available in '{phase}'."
            return None
        return f"'{primary}' is not declared as an input nor produced by a post_auth_output."
    if head != "connection":
        return None
    sub = dotted.split(".", 2)
    if len(sub) < 2:
        return "'connection' must be followed by a sub-scope."
    scope = ".".join(sub[:2])  # `connection.parameters` etc
    primary = ".".join(sub[:3]) if len(sub) >= 3 else scope  # `connection.parameters.host`
    if scope == "connection.parameters":
        record = input_idx.get(primary)
        if record is None:
            return f"'{primary}' is not declared in connection_contract.inputs."
        if not _phase_le(record["phase"], phase):
            return (
                f"'{primary}' is declared in phase '{record['phase']}' "
                f"and is not available in '{phase}'."
            )
        return None
    if scope in ("connection.discovered", "connection.selections"):
        if not _phase_le("post_auth", phase):
            return f"'{scope}.*' is only available from post_auth onward (current phase: {phase})."
        if primary not in output_idx:
            return f"'{primary}' is not produced by any post_auth_output."
        return None
    return None


def _walk_refs_with_phase(
    container: Any,
    base_path: str,
    phase: str,
    auth_op: str | None,
    auth_type: str | None,
    input_idx: dict[str, dict],
    output_idx: dict[str, dict],
) -> list[dict]:
    """Walk a sub-tree, validating every ref/template var against the phase model."""
    findings: list[dict] = []
    # `[^}]*` mirrors `check_expressions`; an empty `${}` is reported there as
    # an expression-resolver error, so this phase walker just skips it (below).
    template_var = re.compile(r"\$\{([^}]*)\}")
    for path, node in _walk(container, base_path):
        if not isinstance(node, dict):
            continue
        ref = node.get("ref")
        if isinstance(ref, str):
            problem = _ref_phase_problem(ref, phase, auth_op, auth_type, input_idx, output_idx)
            if problem:
                findings.append(
                    finding(
                        "phase-resolvability",
                        "error",
                        path,
                        f"ref '{ref}': {problem}",
                        rule_doc="shared/lifecycle-phases.md",
                    )
                )
        tmpl = node.get("template")
        if isinstance(tmpl, str):
            for var in template_var.findall(tmpl):
                if not var.strip():
                    continue
                problem = _ref_phase_problem(var, phase, auth_op, auth_type, input_idx, output_idx)
                if problem:
                    findings.append(
                        finding(
                            "phase-resolvability",
                            "error",
                            path,
                            f"template '${{{var}}}': {problem}",
                            rule_doc="shared/lifecycle-phases.md",
                        )
                    )
    return findings


def check_phase_resolvability(doc: dict) -> list[dict]:
    """Validate every templated reference against `shared/lifecycle-phases.md`.

    Builds two indexes — declared inputs (from connection_contract.inputs)
    and produced outputs (from connection_contract.post_auth_outputs) —
    then walks the document at known phase-anchored sites and asserts each
    ref/template var targets a scope available in that phase.

    Anchored sites and their phases:

    | Site | Phase | Auth-op context |
    |---|---|---|
    | `auth.authorize.*` | auth | authorize |
    | `auth.token_exchange.*` | auth | token_exchange |
    | `auth.refresh.*` | post_auth | refresh |
    | `auth.test.*` | active | None |
    | `connection_contract.post_auth_outputs.*.options_request` | post_auth | None |
    | `connection_contract.post_auth_outputs.*.discovery_request` | post_auth | None |
    | `transports.*` | varies — assumed `active` (most permissive) by default |

    `auth.refresh` is modeled at `post_auth`-equivalent scope availability
    (rather than the spec table's `auth` phase) because it runs *after*
    the in-flight authorization-code workflow has completed, so persisted
    `auth.access_token` / `auth.refresh_token` are accessible. The spec's
    "no runtime.oauth.* inside refresh" rule is preserved via the
    `auth_op="refresh"` context flag.

    For transports we conservatively validate against the `active` phase.
    Transport phase inference (assigning each transport its earliest
    phase based on which auth/discovery/data ops reference it) is a
    deeper analysis tracked separately.

    Also emits a warning when a `post_auth_outputs` entry is malformed
    (bad storage, or a missing/empty `value_path` response-extraction
    path), and an error when
    `connection_contract.inputs`, `connection_contract.post_auth_outputs`,
    or `transports` is present-but-non-object (the index helpers would
    otherwise silently coerce to {} and produce misdirected "not
    declared" diagnostics).
    """
    findings: list[dict] = []
    if not isinstance(doc, dict):
        return findings
    # `auth` is required by Layer 1; under `--semantic-only` a non-dict here
    # would silently collapse to {} and skip all auth-op resolvability checks
    # without telling the author why. On connector docs `check_auth_shape`
    # surfaces the structural problem separately (it's connector-only); for
    # any non-connector callers this validator just skips its auth-op walk
    # safely.
    auth_raw = doc.get("auth")
    auth = auth_raw if isinstance(auth_raw, dict) else {}
    auth_type = auth.get("type")
    # Shape checks for the structures the index helpers silently coerce to
    # {} on non-dict. Without these, downstream "ref X is not declared"
    # errors would misattribute the root cause when `inputs` is actually a
    # list/string. Layer 1 catches each via type; surface explicitly under
    # `--semantic-only`.
    cc_for_shape = doc.get("connection_contract")
    if isinstance(cc_for_shape, dict):
        inputs_shape = cc_for_shape.get("inputs")
        if inputs_shape is not None and not isinstance(inputs_shape, dict):
            findings.append(
                finding(
                    "phase-resolvability",
                    "error",
                    "/connection_contract/inputs",
                    f"inputs must be an object; got {type(inputs_shape).__name__}.",
                    rule_doc="shared/lifecycle-phases.md",
                )
            )
        post_auth_shape = cc_for_shape.get("post_auth_outputs")
        if post_auth_shape is not None and not isinstance(post_auth_shape, dict):
            findings.append(
                finding(
                    "phase-resolvability",
                    "error",
                    "/connection_contract/post_auth_outputs",
                    f"post_auth_outputs must be an object; got {type(post_auth_shape).__name__}.",
                    rule_doc="shared/lifecycle-phases.md",
                )
            )
    transports_shape = doc.get("transports")
    if transports_shape is not None and not isinstance(transports_shape, dict):
        findings.append(
            finding(
                "phase-resolvability",
                "error",
                "/transports",
                f"transports must be an object; got {type(transports_shape).__name__}.",
                rule_doc="shared/lifecycle-phases.md",
            )
        )

    input_idx, input_warnings = _index_inputs(doc)
    findings.extend(input_warnings)
    output_idx, malformed = _index_post_auth_outputs(doc)
    findings.extend(malformed)

    # Auth ops. Phase assignment notes:
    # - authorize / token_exchange run in the auth phase proper.
    # - refresh runs *after* the in-flight authorization-code workflow has
    #   completed, so it has access to persisted auth state (auth.access_token,
    #   auth.refresh_token). We model that as post_auth-level scope
    #   availability while keeping the spec's "no runtime.oauth.* in refresh"
    #   rule via the auth_op context flag.
    # - test runs against an established connection; treat as active.
    if isinstance(auth, dict):
        for op_name, op_phase in [
            ("authorize", "auth"),
            ("token_exchange", "auth"),
            ("refresh", "post_auth"),
            ("test", "active"),
        ]:
            op = auth.get(op_name)
            if isinstance(op, dict):
                findings.extend(
                    _walk_refs_with_phase(
                        op,
                        f"/auth/{op_name}",
                        phase=op_phase,
                        auth_op=op_name if op_name != "test" else None,
                        auth_type=auth_type,
                        input_idx=input_idx,
                        output_idx=output_idx,
                    )
                )

    # Post-auth output ops
    cc = doc.get("connection_contract")
    post_auth = cc.get("post_auth_outputs") if isinstance(cc, dict) else None
    post_auth = post_auth or {}
    if isinstance(post_auth, dict):
        for name, spec in post_auth.items():
            if not isinstance(spec, dict):
                continue
            for op_name in ("options_request", "discovery_request"):
                op = spec.get(op_name)
                if isinstance(op, dict):
                    findings.extend(
                        _walk_refs_with_phase(
                            op,
                            f"/connection_contract/post_auth_outputs/{name}/{op_name}",
                            phase="post_auth",
                            auth_op=None,
                            auth_type=auth_type,
                            input_idx=input_idx,
                            output_idx=output_idx,
                        )
                    )

    # Transports — conservatively validated against the active phase.
    transports = doc.get("transports") or {}
    if isinstance(transports, dict):
        for tname, tspec in transports.items():
            if not isinstance(tspec, dict):
                continue
            findings.extend(
                _walk_refs_with_phase(
                    tspec,
                    f"/transports/{tname}",
                    phase="active",
                    auth_op=None,
                    auth_type=auth_type,
                    input_idx=input_idx,
                    output_idx=output_idx,
                )
            )

    return findings


# `[^}]*` (not `+`) so an empty `${}` is captured and flagged by
# `check_type_map_rules`; with `+` it matched nothing and survived into the
# rendered DDL as a literal `${}`.
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")
_NARROWING_ARROW_TYPES = {"Object", "List"}

# Bare-marker `arrow_type` values: the `authored_shape_type` enum in the
# published `canonical-types.json` (and accepted by the `arrow_type` pattern
# in `api-endpoint/latest.json`, which lists `Object|List|Json` alongside the
# scalar and parameterized forms). Each carries a sibling-key contract on its
# endpoint field node that the JSON Schema layer does NOT mechanically enforce
# (`JsonSchemaPropertyNode` is `additionalProperties: true` and only couples
# `arrow_type` ↔ `native_type`), so the semantic layer enforces it — recursively
# — from both `check_endpoint_annotations` (endpoint validated directly) and
# `check_type_map_coverage` (sibling endpoints during connector validation):
#   - `Object` — declares a known inner shape; REQUIRES a non-empty
#     `properties` map, FORBIDS `items`.
#   - `List`   — declares a known element shape; REQUIRES an `items` field
#     spec (a sub-schema), FORBIDS `properties`.
#   - `Json`   — opaque pass-through; FORBIDS both `properties` and `items`.
# These are distinct from the parameterized `Struct<…>` / `List<…>` forms,
# which carry their inner types inline and take no siblings (the exact-set
# membership test below excludes them — do NOT loosen it to a prefix match).
_BARE_MARKER_ARROW_TYPES = {"Object", "List", "Json"}

# Schemaless / structured-container native types. A read-map rule whose
# `native` is one of these (or a parameterized container such as
# `array<object>` / `struct<...>` / `map<...>`) MUST render a container
# canonical (`_CONTAINER_CANONICAL_HEADS`), never a scalar like `Utf8`: a
# scalar may round-trip the bytes but throws away the shape the canonical is
# meant to describe. Kept connector-agnostic here, not in any one connector's
# spec. Compared UPPERCASE (read matchers normalize to uppercase).
_SCHEMALESS_CONTAINER_NATIVES = {
    "JSON",
    "JSONB",
    "VARIANT",
    "OBJECT",
    "ARRAY",
    "MAP",
    "STRUCT",
    "RECORD",
    "HSTORE",
    "SUPER",
}
# Leading tokens of parameterized container natives (`ARRAY<...>` etc.).
_CONTAINER_NATIVE_HEADS = {"ARRAY", "STRUCT", "MAP", "LIST", "OBJECT"}
# Canonical Arrow heads that preserve structure — the acceptable render
# targets for the natives above. `Json` is the standard read-map target;
# `Object` / `List` are the endpoint-only narrowings; `Struct` / `Map` /
# `List` cover typed containers (`List<Int64>`, `Struct<...>`).
_CONTAINER_CANONICAL_HEADS = {
    "Json",
    "Object",
    "List",
    "LargeList",
    "FixedSizeList",
    "Struct",
    "Map",
}
_ECMA_NAMED_GROUP = re.compile(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>")
# Catches all non-ECMA `(?P…` regex extensions: Python stdlib's named-group
# declaration `(?P<name>…)` and backreference `(?P=name)`, plus the
# `regex`-library recursive-call `(?P>name)`. None are valid ECMA-262, so all
# three are rejected by `check_type_map_rules`.
_PYTHON_REGEX_FEATURE = re.compile(r"\(\?P[<=>]")


def _to_python_regex(pattern: str) -> str:
    """Translate ECMA-262 `(?<name>…)` named groups to Python's `(?P<name>…)`.

    The published type-map schema documents ECMA-262 regex syntax;
    Python's `re` module only accepts the `(?P<…>)` spelling, so the
    validator translates the well-defined named-group form before
    compiling. Anonymous groups (`(...)`) and non-capturing (`(?:…)`)
    are passed through unchanged. The `(?P[<=>]…)` extensions are
    contract violations flagged separately by `check_type_map_rules`:
    `(?P<…>)` declarations and `(?P=…)` backreferences are Python
    stdlib syntax, `(?P>…)` recursive calls are PyPI `regex`-library
    syntax — none are valid ECMA-262. This helper is for compilation,
    not enforcement.
    """
    return _ECMA_NAMED_GROUP.sub(r"(?P<\1>", pattern)


# On-disk sibling filenames under `{connector_id}/definition/`. The read map
# (native → Arrow) is required for api and database kinds; the write map
# (Arrow → native DDL render rules) is required for database kinds only.
# The pre-split filename is rejected with a migration finding.
_READ_MAP_FILENAME = "type-map-read.json"
_WRITE_MAP_FILENAME = "type-map-write.json"
_LEGACY_MAP_FILENAME = "type-map.json"

# Per-direction (matcher key, render key) for type-map rules. Read maps match
# on `native` and render `canonical`; write maps invert: they match on
# `canonical` (which may be a regex with named captures) and render `native`
# (which may carry `${name}` substitutions backed by those captures).
_DIRECTION_KEYS = {
    "read": ("native", "canonical"),
    "write": ("canonical", "native"),
}

# Representative probes for the write-direction canonical vocabulary
# (dip-registry-connector-packages.md, authoring rule 8). Each entry is
# (family label, probe canonical); a write map should resolve every probe.
# Gaps are warnings, not errors — a dialect may deliberately leave a family
# unmapped and take over rendering via a `render_column_type` override
# (BigQuery's NUMERIC/BIGNUMERIC precision-range arithmetic is the
# canonical example).
_WRITE_VOCABULARY_PROBES: tuple[tuple[str, str], ...] = (
    ("Boolean", "Boolean"),
    ("Int8", "Int8"),
    ("Int16", "Int16"),
    ("Int32", "Int32"),
    ("Int64", "Int64"),
    ("UInt8", "UInt8"),
    ("UInt16", "UInt16"),
    ("UInt32", "UInt32"),
    ("UInt64", "UInt64"),
    ("Float16", "Float16"),
    ("Float32", "Float32"),
    ("Float64", "Float64"),
    ("Decimal128(p, s)", "Decimal128(38, 9)"),
    ("Utf8", "Utf8"),
    ("LargeUtf8", "LargeUtf8"),
    ("Json", "Json"),
    ("Binary", "Binary"),
    ("LargeBinary", "LargeBinary"),
    ("FixedSizeBinary(n)", "FixedSizeBinary(16)"),
    ("Date32", "Date32"),
    ("Date64", "Date64"),
    ("Time", "Time64(MICROSECOND)"),
    ("Timestamp (bare)", "Timestamp(MICROSECOND)"),
    ("Timestamp (tz)", "Timestamp(MICROSECOND, UTC)"),
)


def _strip_regex_meta(pattern: str) -> str:
    """Strip named-group declarations and non-literal escapes from a regex.

    What remains of the pattern after `(?<name>` declarations and the
    recognized class/anchor escapes (`\\d`, `\\s`, `\\b`, …) are removed
    is (approximately) the literal text the pattern must match. Any
    OTHER escaped character (`\\(`, `\\.`) is a literal, so the
    backslash is dropped but the character is kept. (Unknown
    lowercase-letter escapes like `\\q` cannot reach this check at all:
    Python's `re` rejects them at the compile gate, which runs first
    and errors out.) Used by the uppercase-pattern check: lowercase
    letters surviving this strip are literal matches that can never
    fire against the engine's UPPERCASED native strings.
    """
    without_groups = _ECMA_NAMED_GROUP.sub("(", pattern)
    # Class/anchor/whitespace escapes (plus \x/\u prefixes of hex and
    # unicode escapes) are regex machinery, not literals — drop them.
    without_class_escapes = re.sub(r"\\[dDsSwWbBAZfnrtvux0]", "", without_groups)
    # Everything else escaped is a literal character — keep it.
    return re.sub(r"\\(.)", r"\1", without_class_escapes)


def _load_sibling_type_map(tm_path: Path) -> tuple[list | None, list[dict]]:
    """Read, parse, and shape-check a sibling type-map file.

    Returns `(doc, findings)`: `doc` is the parsed top-level array on
    success and `None` when the file is unreadable, unparsable, or not a
    non-empty array — in which case `findings` carries the corresponding
    `type-map-coverage` error naming the file.
    """
    try:
        tm_doc = json.loads(tm_path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, [
            finding(
                "type-map-coverage",
                "error",
                "/",
                f"sibling {tm_path.name} could not be read or parsed ({exc}).",
                rule_doc="shared/type-maps.md",
            )
        ]
    if not isinstance(tm_doc, list) or not tm_doc:
        return None, [
            finding(
                "type-map-coverage",
                "error",
                "/",
                f"sibling {tm_path.name} must be a non-empty array of rules.",
                rule_doc="shared/type-maps.md",
            )
        ]
    return tm_doc, []


def check_type_map_coverage(doc: dict, doc_path: Path | None = None) -> list[dict]:
    """Validate connector ↔ sibling type-map coverage and consistency.

    API and database kinds require a sibling `type-map-read.json`
    (native → Arrow, non-empty array) per `shared/type-maps.md`; database
    kinds additionally require a sibling `type-map-write.json` (Arrow →
    native render rules). API kinds must NOT ship a write map. A
    pre-split `type-map.json` sibling is an error with a migration
    pointer. The validator emits an error when a required file is
    missing, unreadable, or empty. Storage kinds (file / s3 / stdout)
    follow a separate branch where the siblings are optional but, when
    present, are still rule-checked.

    For database connectors, presence of read rules is sufficient at
    author time — runtime discovery reconciles natives against the
    user's database; the write map is additionally probed against the
    canonical vocabulary (warnings via `type-map-write-coverage`). For
    API connectors, the validator walks sibling endpoint files and
    asserts every typed field's `(native_type, arrow_type)` pair
    resolves via the sibling `type-map-read.json`, rendering templated
    canonicals (named-capture substitution) before comparison. The
    `Object` / `List` markers are accepted as narrowings of a
    `Json`-resolved rule (the endpoint has declared the inner shape via
    `properties` / `items`).

    `doc_path` is the absolute path to the connector document on disk;
    used to locate the sibling type-map files and `endpoints/`
    directory. When omitted, the check is skipped (the validator was
    invoked without a filesystem-anchored connector).
    """
    findings: list[dict] = []
    if not isinstance(doc, dict):
        return findings
    # Distinguish a real connector document from one that just has a stray
    # `kind` field (e.g. an api-endpoint with `kind` accidentally added).
    # An author who fills in `kind` only — no transports / connection_contract
    # / default_transport / auth — is in an ambiguous state: it could be a
    # malformed connector OR a stray-kind endpoint. `check_transport_refs`
    # will surface its own "transports is required" error which is the
    # canonical signal that something connector-shaped is missing fields.
    # Demanding sibling type-map files here would be misleading on a doc
    # that turns out to be an endpoint, so we skip when none of the four
    # sentinel keys is present.
    if not any(k in doc for k in ("transports", "connection_contract", "default_transport", "auth")):
        return findings
    if doc_path is None:
        findings.append(
            finding(
                "type-map-coverage",
                "warning",
                "/",
                "type-map coverage skipped: validator was invoked without a filesystem-anchored document path; sibling type-map files cannot be located.",
                rule_doc="shared/type-maps.md",
            )
        )
        return findings
    kind = doc.get("kind")
    if kind is None:
        findings.append(
            finding(
                "type-map-coverage",
                "warning",
                "/kind",
                "type-map coverage skipped: connector has no 'kind' discriminator (Layer 1 should have caught this).",
                rule_doc="shared/type-maps.md",
            )
        )
        return findings
    if kind not in ("api", "database", "file", "s3", "stdout"):
        # Unknown / non-string `kind`. Layer 1 rejects via the closed enum;
        # under `--semantic-only` we surface it so the coverage policy
        # selection (storage skip vs. api/db enforcement) isn't silently
        # mis-routed.
        findings.append(
            finding(
                "type-map-coverage",
                "warning",
                "/kind",
                f"type-map coverage skipped: connector 'kind' is not in the closed enum (got {kind!r}). Layer 1 enforces this.",
                rule_doc="shared/type-maps.md",
            )
        )
        return findings
    # Pre-split filename. The read map was renamed `type-map.json` →
    # `type-map-read.json` when the write direction split out into its own
    # file; a sibling still carrying the old name will never be read by the
    # engine. Applies to every kind (a stale file is equally dead under a
    # storage kind).
    legacy_path = doc_path.parent / _LEGACY_MAP_FILENAME
    if legacy_path.is_file():
        findings.append(
            finding(
                "type-map-coverage",
                "error",
                "/",
                f"legacy sibling {_LEGACY_MAP_FILENAME} found; the read map is now {_READ_MAP_FILENAME} "
                f"(database connectors additionally ship {_WRITE_MAP_FILENAME}). Rename the file and re-validate.",
                rule_doc="shared/type-maps.md",
            )
        )

    read_path = doc_path.parent / _READ_MAP_FILENAME
    write_path = doc_path.parent / _WRITE_MAP_FILENAME

    if kind not in ("api", "database"):
        # storage kinds (file/s3/stdout) are accepted by the schema but not
        # yet executed by the engine, so no per-kind coverage contract
        # applies. If sibling map files exist, surface read / parse / shape /
        # rule errors anyway so authors who ship type maps ahead of engine
        # support don't get a silent pass on a broken file. Absence of the
        # siblings is allowed for storage kinds (unlike api/db).
        for sibling_path, direction in ((read_path, "read"), (write_path, "write")):
            if not sibling_path.is_file():
                continue
            tm_doc, load_findings = _load_sibling_type_map(sibling_path)
            findings.extend(load_findings)
            if tm_doc is not None:
                findings.extend(_safe_check_type_map_rules(tm_doc, direction=direction))
        return findings

    if not read_path.is_file():
        findings.append(
            finding(
                "type-map-coverage",
                "error",
                "/",
                f"connector requires sibling {_READ_MAP_FILENAME} (native → Arrow rules); file is missing.",
                rule_doc="shared/type-maps.md",
            )
        )
        return findings

    tm_doc, load_findings = _load_sibling_type_map(read_path)
    findings.extend(load_findings)
    if tm_doc is None:
        return findings

    # Surface rule-shape errors from the sibling (broken regex, Python-syntax,
    # duplicates, etc.) at connector-validation time, not just when the
    # validator is invoked directly against the map file. Use the safe wrapper
    # so a crash in the inner validator can't discard the in-progress
    # `findings` list we've accumulated.
    findings.extend(_safe_check_type_map_rules(tm_doc, direction="read"))

    if kind == "database":
        # Database connectors ship the write direction as a sibling
        # `type-map-write.json`: Arrow → native DDL render rules consumed by
        # `dialect.render_column_type`. Rule-shape errors surface the same
        # way as for the read map; vocabulary gaps (rule 8) are warnings.
        if not write_path.is_file():
            findings.append(
                finding(
                    "type-map-coverage",
                    "error",
                    "/",
                    f"database connector requires sibling {_WRITE_MAP_FILENAME} (Arrow → native render rules); file is missing.",
                    rule_doc="shared/type-maps.md",
                )
            )
            return findings
        write_doc, load_findings = _load_sibling_type_map(write_path)
        findings.extend(load_findings)
        if write_doc is not None:
            findings.extend(_safe_check_type_map_rules(write_doc, direction="write"))
            findings.extend(_write_map_vocabulary_findings(write_doc))
        return findings

    # API connectors are read-only at the type-map layer: the write direction
    # is a database-package concept (DDL rendering). A write map on an api
    # connector would never be consumed and signals a misunderstood contract.
    if write_path.is_file():
        findings.append(
            finding(
                "type-map-coverage",
                "error",
                "/",
                f"api connector must not ship a sibling {_WRITE_MAP_FILENAME}; the write direction applies to database connectors only.",
                rule_doc="shared/type-maps.md",
            )
        )

    endpoint_dir = doc_path.parent / "endpoints"
    if not endpoint_dir.is_dir():
        findings.append(
            finding(
                "type-map-coverage",
                "error",
                "/",
                f"api connector requires a sibling 'endpoints/' directory at {endpoint_dir.name}/; directory is missing.",
                rule_doc="shared/type-maps.md",
            )
        )
        return findings
    endpoint_files = sorted(endpoint_dir.glob("*.json"))
    if not endpoint_files:
        findings.append(
            finding(
                "type-map-coverage",
                "error",
                "/",
                "api connector's 'endpoints/' directory contains no *.json files; at least one endpoint is required.",
                rule_doc="shared/type-maps.md",
            )
        )
        return findings

    for ep_path in endpoint_files:
        try:
            ep_doc = json.loads(ep_path.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            findings.append(
                finding(
                    "type-map-coverage",
                    "error",
                    "/",
                    f"endpoint file '{ep_path.name}' could not be read or parsed ({exc}); coverage analysis cannot proceed.",
                    rule_doc="shared/type-maps.md",
                )
            )
            continue
        # A parsed-but-non-dict sibling (`[]`, a string, a number) would crash
        # the `_collect_asymmetric_pairs` walker below on its unguarded
        # `.get("operations")` — the dispatcher would catch it but mislabel it
        # a validator bug AND abort the loop, leaving every remaining sibling
        # unchecked. Shape-gate here so it surfaces as a clean per-file error
        # and the loop continues.
        if not isinstance(ep_doc, dict):
            findings.append(
                finding(
                    "type-map-coverage",
                    "error",
                    "/",
                    f"endpoint file '{ep_path.name}' is not a JSON object (got {type(ep_doc).__name__}); "
                    "cannot analyze its filename or annotations.",
                    rule_doc="shared/type-maps.md",
                )
            )
            continue
        # The endpoint file's basename must equal `{endpoint_id}.json` (the
        # engine's on-disk lookup key). Enforced here on every sibling, in the
        # same per-endpoint loop as the asymmetric-pair / marker walkers, so a
        # connector-level run catches it; the standalone `check_endpoint_filename`
        # covers an endpoint validated by itself. The parity is positional —
        # findings surface under the `endpoint-filename` id (not
        # `type-map-coverage` like the sibling walkers), so don't realign the id.
        findings.extend(_endpoint_filename_findings(ep_doc, ep_path.name))
        for problem_pointer, problem_kind in _collect_asymmetric_pairs(ep_doc):
            if problem_kind == "asymmetric":
                findings.append(
                    finding(
                        "type-map-coverage",
                        "error",
                        "/",
                        f"endpoint '{ep_path.name}' field at {problem_pointer} declares exactly one of native_type / arrow_type; both are required per the api-endpoint schema.",
                        rule_doc="shared/type-maps.md",
                    )
                )
            elif problem_kind == "both_non_string":
                findings.append(
                    finding(
                        "type-map-coverage",
                        "error",
                        "/",
                        f"endpoint '{ep_path.name}' field at {problem_pointer} declares native_type / arrow_type with non-string value(s); both must be strings per the api-endpoint schema.",
                        rule_doc="shared/type-maps.md",
                    )
                )
            elif problem_kind == "param_annotations":
                findings.append(
                    finding(
                        "type-map-coverage",
                        "error",
                        "/",
                        f"endpoint '{ep_path.name}' param at {problem_pointer} carries native_type / arrow_type; the api-endpoint schema's Param forbids both — a param is a request input, typed only by its `type` field. Remove the annotations.",
                        rule_doc="shared/type-maps.md",
                    )
                )
            elif problem_kind == "non_dict_subtree":
                findings.append(
                    finding(
                        "type-map-coverage",
                        "warning",
                        "/",
                        f"endpoint '{ep_path.name}' sub-tree at {problem_pointer} is not a JSON object; the asymmetric-pair walker could not recurse here, so any annotations beneath it are not checked. Edit the endpoint to make that sub-tree a JSON object, or rerun without `--semantic-only` to see the Layer 1 schema error.",
                        rule_doc="shared/type-maps.md",
                    )
                )
        for problem_pointer, problem_kind in _collect_marker_sibling_violations(ep_doc):
            findings.append(
                finding(
                    "type-map-coverage",
                    "error",
                    "/",
                    f"endpoint '{ep_path.name}' field at {problem_pointer} {_marker_sibling_message(problem_kind)}",
                    rule_doc="endpoints/api-endpoint-schema-parameterization.md",
                )
            )
        for native, arrow, pointer in _collect_endpoint_native_arrow_pairs(ep_doc):
            rendered = _render_canonical(native, tm_doc)
            site = f"{ep_path.name}{pointer}"
            if rendered is None:
                findings.append(
                    finding(
                        "type-map-coverage",
                        "error",
                        "/",
                        f"native_type {native!r} at {site} has no matching rule in sibling {_READ_MAP_FILENAME}.",
                        rule_doc="shared/type-maps.md",
                    )
                )
                continue
            if rendered == arrow:
                continue
            if rendered == "Json" and arrow in _NARROWING_ARROW_TYPES:
                continue
            findings.append(
                finding(
                    "type-map-coverage",
                    "error",
                    "/",
                    (
                        f"native_type {native!r} at {site} resolves to {rendered!r} "
                        f"via sibling {_READ_MAP_FILENAME} but endpoint declares arrow_type={arrow!r}."
                    ),
                    rule_doc="shared/type-maps.md",
                )
            )
    return findings


def _safe_check_type_map_rules(doc: Any, direction: str = "read") -> list[dict]:
    """Call `check_type_map_rules` with a crash guard.

    Used when one accumulating validator (e.g. `check_type_map_coverage`)
    cross-dispatches to another. The top-level dispatch loop in
    `run_semantic_validators` already wraps each validator call in
    try/except, but cross-dispatch happens inside a validator that is
    itself accumulating its own findings into a local list. An uncaught
    exception in the inner call would discard the caller's in-progress
    findings — so we catch here and convert to a structured finding.
    """
    try:
        return check_type_map_rules(doc, direction=direction)
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        return [
            finding(
                "type-map-rule",
                "error",
                "/",
                f"sibling type-map rule validation crashed ({type(exc).__name__}: {exc}); coverage analysis continued with partial findings. This is a validator bug — please report.",
                rule_doc="shared/type-maps.md",
            )
        ]


def _leading_type_token(value: str) -> str:
    """First run of identifier characters in a type string, UPPERCASE."""
    m = re.search(r"[A-Za-z_]+", value)
    return m.group(0).upper() if m else ""


def _native_is_schemaless_container(native: str, match: str) -> bool:
    """True when a read-map `native` denotes a schemaless / structured
    container: a known token (JSON, JSONB, VARIANT, OBJECT, ARRAY, MAP,
    STRUCT, …), a parameterized container (`array<...>`, `struct<...>`,
    `map<...>`), or a SQL array-suffix spelling (`integer[]`, regex `.*\\[\\]$`).
    Regex matchers are stripped of their meta first."""
    probe = _strip_regex_meta(native) if match == "regex" else native
    head = _leading_type_token(probe)
    if head in _SCHEMALESS_CONTAINER_NATIVES:
        return True
    if "<" in probe and head in _CONTAINER_NATIVE_HEADS:
        return True
    # SQL array-suffix: `integer[]` (exact) or a read-map regex like `.*\[\]$`.
    return probe.replace("\\", "").rstrip("$").endswith("[]")


def _canonical_head(canonical: str) -> str:
    """Leading PascalCase Arrow type name (`Json` from `Json`, `List` from
    `List<Int64>`); empty when the value opens with a `${...}` substitution."""
    m = re.match(r"\s*([A-Za-z][A-Za-z0-9]*)", canonical)
    return m.group(1) if m else ""


def check_type_map_rules(
    doc: Any,
    doc_path: Path | None = None,
    *,
    direction: str | None = None,
) -> list[dict]:
    """Validate self-contained rules in a type-map document.

    Runs against a top-level array (the on-disk shape of
    `type-map-read.json` / `type-map-write.json`). The two directions
    share the rule shape but invert which key is the matcher and which
    is rendered: read maps match on `native` and render `canonical`;
    write maps match on `canonical` and render `native`. `direction`
    selects the orientation explicitly; when omitted it is derived from
    `doc_path` (filename `type-map-write.json` → write, anything else →
    read, matching the on-disk contract).

    Enforces, beyond what JSON Schema covers (key names below follow
    the read orientation; swap native/canonical for write maps):

    - Rules missing required key(s) (`match`, `native`, `canonical`)
      emit a warning and are skipped — Layer 1 owns the schema error.
    - Rules with unknown / legacy key(s) (anything outside `{match,
      native, canonical}`) emit a warning; the canonical case is a
      partial migration that left a `method` key behind.
    - `match` outside the closed `{"exact", "regex"}` enum errors out
      (including `null`, typos, or future schema additions the
      validator pre-dates).
    - `match: "exact"` rules must not use `${...}` substitution in the
      render-side value (those are regex-only).
    - The matcher-side value must be a string — non-string values would
      silently shadow valid rules at runtime.
    - `match: "regex"` rules' matcher must compile as a valid regex
      (regardless of whether the render side is templated).
    - `match: "regex"` rules must use ECMA-262 named-group syntax
      `(?<name>…)`; non-ECMA `(?P[<=>]…)` extensions (Python stdlib's
      `(?P<…>)` / `(?P=…)`, PyPI `regex`-library's `(?P>…)`) are
      contract violations.
    - `match: "regex"` rules referencing `${name}` on the render side
      must define a matching named capture group `(?<name>…)` in the
      matcher.
    - Read-direction regex matchers are evaluated against UPPERCASED,
      whitespace-collapsed native strings; lowercase literals left in
      the pattern after stripping group declarations and escapes can
      never match, so they warn (capture group names stay lowercase).
    - The render-side value must be a string — non-string values
      silently inert a rule at runtime.
    - Duplicate (match, matcher) pairs are flagged as warnings —
      first-match-wins makes later duplicates unreachable.

    Other layout rules (top-level type, required keys, minItems ≥ 1) are
    enforced by the published `type-map-read/latest.json` (read) and
    `type-map-write/latest.json` (write) schemas in Layer 1.
    """
    findings: list[dict] = []
    if not isinstance(doc, list):
        return findings
    if direction is None:
        direction = (
            "write"
            if doc_path is not None and doc_path.name == _WRITE_MAP_FILENAME
            else "read"
        )
        # Direction is a filesystem contract. When the filename is neither
        # recognized map name, read semantics are assumed — surface that
        # assumption instead of silently mis-validating a write map (whose
        # write-direction checks, incl. vocabulary coverage, would otherwise
        # vanish and whose many-to-one render rules would false-positive as
        # duplicates).
        if doc_path is not None and doc_path.name not in (
            _READ_MAP_FILENAME,
            _WRITE_MAP_FILENAME,
        ):
            findings.append(
                finding(
                    "type-map-rule",
                    "warning",
                    "/",
                    f"rule direction defaulted to 'read': filename {doc_path.name!r} is neither {_READ_MAP_FILENAME!r} nor {_WRITE_MAP_FILENAME!r}. If this is a write map, validate it under its on-disk filename — write-direction checks (incl. vocabulary coverage) did not run.",
                    rule_doc="shared/type-maps.md",
                )
            )
    matcher_key, render_key = _DIRECTION_KEYS[direction]
    seen: set[tuple[Any, Any]] = set()
    for i, rule in enumerate(doc):
        if not isinstance(rule, dict):
            findings.append(
                finding(
                    "type-map-rule",
                    "warning",
                    f"/{i}",
                    f"rule entry is not an object (got {type(rule).__name__}); skipped from semantic checks. Layer 1 should have rejected this.",
                    rule_doc="shared/type-maps.md",
                )
            )
            continue
        match = rule.get("match")
        matcher_value = rule.get(matcher_key)
        render_value = rule.get(render_key)
        # Required keys. Layer 1 enforces `required: [match, native, canonical]`;
        # under `--semantic-only` an omitted key would silently shadow downstream
        # validation. Treat key-absent here (`not in`). Explicit `null` per
        # key is handled separately by the per-key gates below:
        #   - `match: null` → caught by the closed-enum gate below (null is
        #     not in {"exact","regex"}, so it's flagged as an unknown value).
        #   - matcher `null` with `match in {"exact","regex"}` → caught by the
        #     non-string-matcher gate below.
        #   - render-side `null` → caught by the non-string-render gate at
        #     the bottom of the loop.
        missing = [k for k in ("match", "native", "canonical") if k not in rule]
        if missing:
            findings.append(
                finding(
                    "type-map-rule",
                    "warning",
                    f"/{i}",
                    f"rule is missing required key(s) {missing}; Layer 1 enforces this — rerun without `--semantic-only` to see the schema error. The rule will not match any native at runtime.",
                    rule_doc="shared/type-maps.md",
                )
            )
            continue
        # Unknown / legacy keys. Catches partial migrations where the author
        # added `match` but forgot to remove the legacy `method`, plus any
        # other stray key. Layer 1 rejects via `additionalProperties: false`;
        # under `--semantic-only` we surface it ourselves so the author sees
        # the cleanup pointer.
        unknown_keys = sorted(set(rule.keys()) - {"match", "native", "canonical"})
        if unknown_keys:
            hint = (
                " (rename `method` → `match`)"
                if "method" in unknown_keys
                else ""
            )
            findings.append(
                finding(
                    "type-map-rule",
                    "warning",
                    f"/{i}",
                    f"rule has unknown key(s) {unknown_keys}{hint}; the schema rejects additional properties.",
                    rule_doc="shared/type-maps.md",
                )
            )
        # `match` is a closed enum. Without this gate, an unknown value would
        # be silently shadowed by `_render_canonical` (which only checks
        # `== "exact"` / `== "regex"`) — the rule would never resolve any
        # native, making a future-schema rule the validator pre-dates
        # invisible to the author. Explicit `null` is covered by Layer 1 via
        # the enum constraint (Layer 2 catches it through the missing-key
        # check above only if the key is absent — for a present `null` value
        # the rule falls through here and the per-`match` branches below
        # don't fire, so the rule resolves nothing at runtime).
        if match not in ("exact", "regex"):
            findings.append(
                finding(
                    "type-map-rule",
                    "error",
                    f"/{i}/match",
                    f"match must be one of 'exact' | 'regex'; got {match!r}.",
                    rule_doc="shared/type-maps.md",
                )
            )
            continue
        # The matcher-side value must be a string for both `exact` and
        # `regex`. Without this gate, a `regex` rule with a `null` matcher
        # skips the regex compile branch (gated on isinstance) and an `exact`
        # rule never matches — the rule silently shadows valid rules at
        # runtime.
        if match in ("exact", "regex") and not isinstance(matcher_value, str):
            findings.append(
                finding(
                    "type-map-rule",
                    "warning",
                    f"/{i}/{matcher_key}",
                    f"{matcher_key} must be a string for {match!r} rules; got {type(matcher_value).__name__}. Layer 1 enforces this — rerun without `--semantic-only` to see the schema error. The rule will not match anything at runtime.",
                    rule_doc="shared/type-maps.md",
                )
            )
            continue
        # Dedupe set. Layer 1 should already reject non-string values, but
        # `--semantic-only` bypasses Layer 1, so the dedupe set must tolerate
        # unhashable rule values. Tuple construction is always safe; only
        # `hash()` (called by `in` / `add`) can raise TypeError. When it does,
        # emit a warning so the un-checkable rule isn't a silent skip.
        key: tuple[Any, Any] = (match, matcher_value)
        try:
            if key in seen:
                findings.append(
                    finding(
                        "type-map-rule",
                        "warning",
                        f"/{i}",
                        f"duplicate rule for (match={match!r}, {matcher_key}={matcher_value!r}); first-match-wins makes later duplicates unreachable.",
                        rule_doc="shared/type-maps.md",
                    )
                )
            else:
                seen.add(key)
        except TypeError:
            findings.append(
                finding(
                    "type-map-rule",
                    "warning",
                    f"/{i}",
                    f"rule's (match, {matcher_key}) key is not hashable; dedupe analysis skipped for this entry. Layer 1 should have rejected non-primitive values.",
                    rule_doc="shared/type-maps.md",
                )
            )

        # Regex compile + Python-syntax checks run BEFORE the render-string
        # gate so that a broken regex with a non-string render value still
        # surfaces as an error instead of being silently swallowed. Note: a
        # regex rule with a non-string matcher is short-circuited by the
        # non-string-matcher gate above and never reaches this block, so the
        # only way here is `match == "regex" and isinstance(matcher_value, str)`.
        if match == "regex" and isinstance(matcher_value, str):
            # Contract: ECMA-262 syntax only. Python-only `(?P<name>…)`
            # declarations, `(?P=name)` backreferences, and `(?P>name)`
            # recursive calls are all contract violations — none have
            # ECMA-262 equivalents.
            if _PYTHON_REGEX_FEATURE.search(matcher_value):
                findings.append(
                    finding(
                        "type-map-rule",
                        "error",
                        f"/{i}/{matcher_key}",
                        f"{matcher_key} uses Python-only '(?P…)' regex syntax; the contract requires ECMA-262 (use '(?<name>…)' for named groups).",
                        rule_doc="shared/type-maps.md",
                    )
                )
                continue
            try:
                compiled = re.compile(_to_python_regex(matcher_value))
            except re.error as exc:
                findings.append(
                    finding(
                        "type-map-rule",
                        "error",
                        f"/{i}/{matcher_key}",
                        f"{matcher_key} is not a valid regex ({exc}).",
                        rule_doc="shared/type-maps.md",
                    )
                )
                continue
            # Read-direction matchers are evaluated against UPPERCASED,
            # whitespace-collapsed native type strings (the engine
            # normalizes before matching; exact rules are normalized
            # automatically). A lowercase literal left in the pattern can
            # therefore never match — the rule is dead. Capture group
            # names stay lowercase and are stripped before the check, as
            # are backslash escapes (`\d`, `\s`, …). Write-direction
            # matchers run against PascalCase canonicals and are exempt.
            if direction == "read" and re.search(r"[a-z]", _strip_regex_meta(matcher_value)):
                findings.append(
                    finding(
                        "type-map-rule",
                        "warning",
                        f"/{i}/{matcher_key}",
                        f"regex {matcher_key} patterns are matched against UPPERCASED, whitespace-collapsed native types; lowercase literals in {matcher_value!r} can never match. Author the pattern uppercase (named capture group names stay lowercase).",
                        rule_doc="shared/type-maps.md",
                    )
                )
        else:
            compiled = None  # type: ignore[assignment]

        if not isinstance(render_value, str):
            # Layer 1 enforces string values; under `--semantic-only` a
            # non-string render value (`null`, list, etc.) would silently
            # propagate to the first-match renderer, which also skips
            # non-string values — meaning the rule never resolves anything
            # at runtime. Surface it explicitly.
            findings.append(
                finding(
                    "type-map-rule",
                    "warning",
                    f"/{i}/{render_key}",
                    f"{render_key} must be a string; got {type(render_value).__name__}. Layer 1 enforces this — rerun without `--semantic-only` to see the schema error. The rule will not resolve anything at runtime.",
                    rule_doc="shared/type-maps.md",
                )
            )
            continue
        placeholders = _PLACEHOLDER_RE.findall(render_value)
        if any(not name.strip() for name in placeholders):
            findings.append(
                finding(
                    "type-map-rule",
                    "error",
                    f"/{i}/{render_key}",
                    f"{render_key}={render_value!r} contains an empty ${{}} placeholder, which renders to nothing.",
                    rule_doc="shared/type-maps.md",
                )
            )
            continue
        # A `${` with no closing `}` is not captured above; it would otherwise
        # pass silently and render as a literal. Only the dangling `${` opener
        # is flagged (a bare `{`/`}` may be valid in a native DDL type).
        if "${" in _PLACEHOLDER_RE.sub("", render_value):
            findings.append(
                finding(
                    "type-map-rule",
                    "error",
                    f"/{i}/{render_key}",
                    f"{render_key}={render_value!r} has an unclosed '${{' (missing closing '}}'), which renders as a literal.",
                    rule_doc="shared/type-maps.md",
                )
            )
            continue
        # Schemaless / structured-container natives must resolve to a CONTAINER
        # canonical (Json / Object / List / Struct / Map), never a scalar like
        # Utf8 — collapsing a structured value to a scalar silently drops its
        # shape. Read direction only (native is the matcher, canonical the
        # render); the canonical *head* is checked so `List<Int64>` etc. pass.
        if direction == "read" and _native_is_schemaless_container(
            matcher_value, match
        ):
            head = _canonical_head(render_value)
            if head and head not in _CONTAINER_CANONICAL_HEADS:
                findings.append(
                    finding(
                        "type-map-rule",
                        "error",
                        f"/{i}/{render_key}",
                        f"native {matcher_value!r} is a schemaless/structured container but resolves to scalar canonical {render_value!r}; map it to a container canonical (`Json`, or `Object`/`List` for endpoint narrowings) so its structure is not lost.",
                        rule_doc="shared/type-maps.md",
                    )
                )
                continue
        if match == "exact" and placeholders:
            findings.append(
                finding(
                    "type-map-rule",
                    "error",
                    f"/{i}/{render_key}",
                    f"exact rules must not use ${{...}} substitution; got {render_key}={render_value!r}.",
                    rule_doc="shared/type-maps.md",
                )
            )
            continue
        if compiled is None:
            continue
        if placeholders:
            capture_names = set(compiled.groupindex.keys())
            for name in placeholders:
                if name not in capture_names:
                    findings.append(
                        finding(
                            "type-map-rule",
                            "error",
                            f"/{i}/{render_key}",
                            f"{render_key} references ${{{name}}} but {matcher_key} has no matching (?<{name}>…) capture group.",
                            rule_doc="shared/type-maps.md",
                        )
                    )
    return findings


def _normalize_native(value: str) -> str:
    """Normalize a native type string the way the engine does before
    matching read-map rules: UPPERCASE with runs of whitespace collapsed
    to a single space."""
    return re.sub(r"\s+", " ", value.strip()).upper()


def _first_match_render(
    value: str,
    rules: list[Any],
    matcher_key: str,
    render_key: str,
    normalize: Callable[[str], str] | None = None,
) -> str | None:
    """Apply first-match-wins; return the rendered value or None.

    Direction-agnostic core shared by `_render_canonical` (read maps:
    match `native`, render `canonical`) and `_render_native` (write
    maps: match `canonical`, render `native`). For regex rules with
    named capture groups, substitutes `${name}` placeholders on the
    render side with the captured value. Returns None when no rule
    matches.

    `normalize` mirrors the engine's pre-match normalization: read maps
    match against UPPERCASED, whitespace-collapsed natives, with exact
    matchers normalized the same way; write maps match canonicals
    verbatim (PascalCase is case-significant).

    Broken regex rules (re.error from `_to_python_regex` output) are
    skipped silently here — `check_type_map_rules` runs first via the
    cross-validator wiring in `check_type_map_coverage` and surfaces
    those as `type-map-rule` errors, so they don't slip through unseen.
    """
    probe = normalize(value) if normalize else value
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = rule.get("match")
        matcher_value = rule.get(matcher_key)
        render_value = rule.get(render_key)
        if not isinstance(matcher_value, str) or not isinstance(render_value, str):
            continue
        if match == "exact":
            target = normalize(matcher_value) if normalize else matcher_value
            if target == probe:
                return render_value
        elif match == "regex":
            try:
                m = re.fullmatch(_to_python_regex(matcher_value), probe)
            except re.error:
                continue
            if not m:
                continue
            groups = m.groupdict()

            def _sub(placeholder: re.Match) -> str:
                name = placeholder.group(1)
                if name not in groups:
                    # Placeholder name absent from matcher captures —
                    # leave the literal `${name}` so the downstream
                    # mismatch surfaces visibly.
                    return placeholder.group(0)
                value = groups[name]
                # An unmatched alternation captures None; render as empty
                # string rather than the literal placeholder so an empty
                # match in render position is unambiguous.
                return value if value is not None else ""

            return _PLACEHOLDER_RE.sub(_sub, render_value)
    return None


def _render_canonical(native: str, rules: list[Any]) -> str | None:
    """Read direction: first-match a native type, render the canonical.

    The native probe and exact matchers are normalized (UPPERCASE,
    whitespace-collapsed) before comparison, mirroring the engine.
    """
    return _first_match_render(
        native, rules, "native", "canonical", normalize=_normalize_native
    )


def _render_native(canonical: str, rules: list[Any]) -> str | None:
    """Write direction: first-match a canonical type, render the native DDL."""
    return _first_match_render(canonical, rules, "canonical", "native")


def _write_map_vocabulary_findings(rules: list[Any]) -> list[dict]:
    """Probe a write map against the canonical vocabulary (rule 8).

    Every probe in `_WRITE_VOCABULARY_PROBES` should resolve to a native
    DDL render through the write map. Gaps are a single grouped warning
    (not an error): a dialect may deliberately leave a family unmapped
    and take over rendering via a `render_column_type` override.
    """
    missing = [
        family
        for family, probe in _WRITE_VOCABULARY_PROBES
        if _render_native(probe, rules) is None
    ]
    if not missing:
        return []
    return [
        finding(
            "type-map-write-coverage",
            "warning",
            "/",
            (
                f"write map does not resolve canonical famil{'y' if len(missing) == 1 else 'ies'}: "
                f"{', '.join(missing)}. The write direction should cover the full canonical "
                "vocabulary; leave a family unmapped only when the connector's dialect "
                "deliberately takes over its rendering via a render_column_type override."
            ),
            rule_doc="shared/type-maps.md",
        )
    ]


def check_type_map_write_coverage(doc: Any, doc_path: Path | None = None) -> list[dict]:
    """Vocabulary coverage for a standalone `type-map-write.json` document.

    Runs only when the validated document's filename is the write map —
    direction is a filesystem contract, not a document-shape one (read
    and write maps share the rule shape). The same probe also runs from
    `check_type_map_coverage` when a database connector is validated
    with its siblings; this validator covers the case where the
    orchestrator validates the write map file by itself.
    """
    if doc_path is None or doc_path.name != _WRITE_MAP_FILENAME:
        return []
    if not isinstance(doc, list) or not doc:
        return []
    return _write_map_vocabulary_findings(doc)


def _collect_endpoint_native_arrow_pairs(endpoint_doc: dict) -> list[tuple[str, str, str]]:
    """Walk an api-endpoint document and return a list of (native_type, arrow_type, json_pointer) tuples.

    Both `native_type` and `arrow_type` are required-paired annotations on
    typed field schemas per the published api-endpoint contract; the
    walker recurses into JSON-Schema-shaped sub-trees (properties / items
    / *Of). Operation `params` are never collected — the contract forbids
    annotations on them. Fields with only one of the pair
    are NOT collected here — `_collect_asymmetric_pairs` surfaces those
    as separate findings.
    """
    out: list[tuple[str, str, str]] = []
    operations = endpoint_doc.get("operations") or {}
    if not isinstance(operations, dict):
        return out

    read = operations.get("read")
    if isinstance(read, dict):
        _walk_endpoint_op(read, "/operations/read", schema_field="response", out=out)

    write = operations.get("write")
    if isinstance(write, dict):
        # Layer 1 (api-endpoint/latest.json) rejects modes outside
        # {"insert", "upsert"}, but iterate defensively so this walker
        # stays correct if the schema later widens the enum.
        for mode, mode_op in write.items():
            if not isinstance(mode_op, dict):
                continue
            _walk_endpoint_op(
                mode_op, f"/operations/write/{mode}", schema_field="input", out=out
            )
    return out


def _collect_asymmetric_pairs(endpoint_doc: dict) -> list[tuple[str, str]]:
    """Return a list of (pointer, kind) tuples for typed-field annotation problems.

    `kind` is one of:
    - `"asymmetric"` — exactly one of `native_type` / `arrow_type` is declared.
    - `"both_non_string"` — both annotations present but at least one is not a string.
    - `"param_annotations"` — a param carries `native_type` and/or
      `arrow_type`. The published `Param` def forbids both (a param is a
      request input, never a materialized column; its `type` enum is its
      only typing): a Layer 1 error re-surfaced here so it is caught
      under `--semantic-only` and on sibling endpoints (which Layer 1
      never sees).
    - `"non_dict_subtree"` — a sub-tree at any structural level (operations,
      read, write, response/input, schema, params, properties, items,
      combiners) is not a dict and the walker could not recurse into it.
      The walker emits this on every run; under default validation it
      complements Layer 1's error, under `--semantic-only` it's the only
      signal.

    The api-endpoint schema's `JsonSchemaPropertyNode` uses
    `dependentRequired` to enforce the pair at Layer 1. This walker
    provides defense-in-depth that also visits a superset of nodes —
    the well-formed coverage walker (`_walk_endpoint_op`) silently
    skips non-dict subtrees with `isinstance` gates; this one records
    them so the gap is visible.
    """
    out: list[tuple[str, str]] = []
    operations = endpoint_doc.get("operations")
    if operations is None:
        # No operations at all — Layer 1 catches the missing required key,
        # nothing to walk.
        return out
    if not isinstance(operations, dict):
        out.append(("/operations", "non_dict_subtree"))
        return out
    if "read" in operations:
        read = operations["read"]
        if isinstance(read, dict):
            _walk_endpoint_op_for_asymmetric(read, "/operations/read", schema_field="response", out=out)
        else:
            out.append(("/operations/read", "non_dict_subtree"))
    if "write" in operations:
        write = operations["write"]
        if isinstance(write, dict):
            # Iterate defensively — Layer 1 fixes write modes to {insert, upsert},
            # but the walker stays correct if the schema later widens the enum.
            for mode, mode_op in write.items():
                if not isinstance(mode_op, dict):
                    out.append((f"/operations/write/{mode}", "non_dict_subtree"))
                    continue
                _walk_endpoint_op_for_asymmetric(
                    mode_op, f"/operations/write/{mode}", schema_field="input", out=out
                )
        else:
            out.append(("/operations/write", "non_dict_subtree"))
    return out


def _walk_endpoint_op_for_asymmetric(
    op: dict, base_pointer: str, *, schema_field: str, out: list[tuple[str, str]]
) -> None:
    """Collect annotation-pair problems from one endpoint operation.

    Uses the `if "key" in op` / `else: append non_dict_subtree` pattern
    throughout to distinguish "key absent" (valid; nothing to walk) from
    "key present but wrong type" (warn). Collapsing this to `.get()` +
    `isinstance(..., dict)` would silently regress the non_dict_subtree
    warnings the walker is designed to surface under `--semantic-only`.
    """
    if schema_field in op:
        body = op[schema_field]
        if isinstance(body, dict):
            if "schema" in body:
                schema = body["schema"]
                if isinstance(schema, dict):
                    _walk_jsonschema_asymmetric(schema, f"{base_pointer}/{schema_field}/schema", out)
                else:
                    out.append((f"{base_pointer}/{schema_field}/schema", "non_dict_subtree"))
        else:
            out.append((f"{base_pointer}/{schema_field}", "non_dict_subtree"))
    if "params" in op:
        params = op["params"]
        if isinstance(params, dict):
            for pname, pspec in params.items():
                pointer = f"{base_pointer}/params/{pname}"
                if not isinstance(pspec, dict):
                    out.append((pointer, "non_dict_subtree"))
                    continue
                if "native_type" in pspec or "arrow_type" in pspec:
                    out.append((pointer, "param_annotations"))
        else:
            out.append((f"{base_pointer}/params", "non_dict_subtree"))


_JSONSCHEMA_MAP_KEYWORDS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
_JSONSCHEMA_LIST_KEYWORDS = ("prefixItems", "allOf", "anyOf", "oneOf")
_JSONSCHEMA_SINGLE_KEYWORDS = (
    "contains", "additionalProperties", "propertyNames",
    "unevaluatedItems", "unevaluatedProperties",
    "not", "if", "then", "else",
)


def _walk_jsonschema_asymmetric(node: Any, pointer: str, out: list[tuple[str, str]]) -> None:
    """Recurse through a JSON Schema, surfacing annotation-pair problems and
    non-dict sub-trees.

    Mirrors `_walk_jsonschema_pairs`'s keyword coverage so the asymmetric
    walker visits a superset of nodes: every recursive `JsonSchemaPropertyNode`
    keyword is checked here, and any present-but-wrong-type sub-tree gets a
    `non_dict_subtree` emission (the well-formed walker silently skips
    those — this one records them).

    Uses the membership-check pattern (`if "key" in node` then explicit
    isinstance dispatch with non_dict_subtree on the else branch) so that
    "key absent" (valid) is distinguished from "key present but wrong
    type" (warn).
    """
    if not isinstance(node, dict):
        out.append((pointer, "non_dict_subtree"))
        return
    _check_annotation_pair(node, pointer, out)
    # Map-keyed (sub-schema per key) — non-dict container is a structural error.
    for keyword in _JSONSCHEMA_MAP_KEYWORDS:
        if keyword in node:
            sub = node[keyword]
            if isinstance(sub, dict):
                for k, v in sub.items():
                    _walk_jsonschema_asymmetric(v, f"{pointer}/{keyword}/{k}", out)
            else:
                out.append((f"{pointer}/{keyword}", "non_dict_subtree"))
    # List-keyed (sub-schema per index) — non-list container is structural.
    for keyword in _JSONSCHEMA_LIST_KEYWORDS:
        if keyword in node:
            sub = node[keyword]
            if isinstance(sub, list):
                for i, v in enumerate(sub):
                    _walk_jsonschema_asymmetric(v, f"{pointer}/{keyword}/{i}", out)
            else:
                out.append((f"{pointer}/{keyword}", "non_dict_subtree"))
    # `items` special: single schema, tuple-list, or boolean (Draft 2020-12
    # allows boolean schemas wherever a sub-schema is accepted).
    if "items" in node:
        items = node["items"]
        if isinstance(items, dict):
            _walk_jsonschema_asymmetric(items, f"{pointer}/items", out)
        elif isinstance(items, list):
            for i, v in enumerate(items):
                _walk_jsonschema_asymmetric(v, f"{pointer}/items/{i}", out)
        elif isinstance(items, bool):
            pass  # boolean schema — valid, nothing to recurse into
        else:
            out.append((f"{pointer}/items", "non_dict_subtree"))
    # Single-schema keywords. Per Draft 2020-12, these accept either a
    # JSON Schema object OR a boolean (`additionalProperties: false` is the
    # canonical strict-schema idiom). Booleans are valid-but-non-recursive;
    # only non-dict/non-bool values are structural errors.
    for keyword in _JSONSCHEMA_SINGLE_KEYWORDS:
        if keyword in node:
            sub = node[keyword]
            if isinstance(sub, dict):
                _walk_jsonschema_asymmetric(sub, f"{pointer}/{keyword}", out)
            elif isinstance(sub, bool):
                continue  # boolean schema — valid, nothing to recurse into
            else:
                out.append((f"{pointer}/{keyword}", "non_dict_subtree"))


def _check_annotation_pair(node: dict, pointer: str, out: list[tuple[str, str]]) -> None:
    """Emit asymmetric / non-string-both findings for one node's pair.

    Returns early (no finding) when NEITHER `native_type` nor `arrow_type`
    is present — an unannotated node is valid JSON Schema and the
    coverage walker simply doesn't collect it. Only nodes that attempted
    to declare the pair (one or both keys present) are checked here.
    """
    has_native_key = "native_type" in node
    has_arrow_key = "arrow_type" in node
    if not has_native_key and not has_arrow_key:
        return
    if has_native_key ^ has_arrow_key:
        out.append((pointer, "asymmetric"))
        return
    # Both keys present — must both be strings.
    native_is_str = isinstance(node.get("native_type"), str)
    arrow_is_str = isinstance(node.get("arrow_type"), str)
    if not (native_is_str and arrow_is_str):
        out.append((pointer, "both_non_string"))


def _walk_endpoint_op(
    op: dict, base_pointer: str, *, schema_field: str, out: list[tuple[str, str, str]]
) -> None:
    """Collect typed-field pairs from one endpoint operation.

    `schema_field` is `"response"` for read ops (records + response
    schema) and `"input"` for each write-mode op. Only
    `<schema_field>.schema` is walked (recursively, as JSON Schema).
    `params` are NOT a coverage source: the published `Param` def is
    `additionalProperties: false` with a plain JSON `type` enum — a
    param is a request input, never a materialized column, so it
    carries no native/arrow annotations (`_walk_endpoint_op_for_asymmetric`
    flags any that appear).
    Half-typed fields (only one of `native_type` / `arrow_type`) and
    non-string-both pairs are NOT collected here; they're surfaced
    instead by `_walk_endpoint_op_for_asymmetric` /
    `_check_annotation_pair`. The two walkers are intentionally
    separate: this one collects WELL-FORMED pairs for coverage
    analysis; the asymmetric walker collects MALFORMED annotation
    sites for direct error reporting. The asymmetric walker visits a
    superset of this walker's sub-trees (it additionally checks
    `params`); within the schema trees both visit the same nodes but
    emit different finding categories, so silently dropping a
    malformed pair here is correct — the asymmetric walker has
    already flagged it.
    """
    body = op.get(schema_field)
    if isinstance(body, dict):
        schema = body.get("schema")
        if isinstance(schema, dict):
            _walk_jsonschema_pairs(schema, f"{base_pointer}/{schema_field}/schema", out)


def _walk_jsonschema_pairs(node: Any, pointer: str, out: list[tuple[str, str, str]]) -> None:
    """Recurse through a JSON Schema, collecting (native_type, arrow_type, pointer).

    Recurses through every recursive keyword in the published
    `JsonSchemaPropertyNode` shape: `properties`, `patternProperties`,
    `$defs`, `definitions`, `dependentSchemas` (maps); `prefixItems`,
    `allOf`, `anyOf`, `oneOf` (lists); `items`, `contains`,
    `additionalProperties`, `propertyNames`, `unevaluatedItems`,
    `unevaluatedProperties`, `not`, `if`, `then`, `else` (single).
    A pair is collected at every node carrying both annotations as
    strings. Does NOT resolve `$ref` — referenced sub-schemas annotated
    only at the ref-target are walked at the target's site (under
    `$defs` / `definitions`), not at the referring site.
    """
    if not isinstance(node, dict):
        return
    native = node.get("native_type")
    arrow = node.get("arrow_type")
    if isinstance(native, str) and isinstance(arrow, str):
        out.append((native, arrow, pointer))
    _recurse_jsonschema(node, pointer, lambda child, child_ptr: _walk_jsonschema_pairs(child, child_ptr, out))


def _recurse_jsonschema(
    node: dict, pointer: str, visit: Callable[[Any, str], None]
) -> None:
    """Shared recursion across all `JsonSchemaPropertyNode` recursive keywords.

    Keyword sets (`_JSONSCHEMA_MAP_KEYWORDS`, `_JSONSCHEMA_LIST_KEYWORDS`,
    `_JSONSCHEMA_SINGLE_KEYWORDS`) are defined near the asymmetric walker
    and shared so the two walkers stay in lockstep — adding a new
    recursive keyword in one place updates both.

    `visit(child, child_pointer)` is called for each recursive child the
    api-endpoint contract recognizes. This is the well-formed walker's
    entry; the asymmetric walker uses the keyword sets directly so it
    can emit `non_dict_subtree` on present-but-wrong-type containers.
    """
    for keyword in _JSONSCHEMA_MAP_KEYWORDS:
        sub = node.get(keyword)
        if isinstance(sub, dict):
            for k, v in sub.items():
                visit(v, f"{pointer}/{keyword}/{k}")
    for keyword in _JSONSCHEMA_LIST_KEYWORDS:
        sub = node.get(keyword)
        if isinstance(sub, list):
            for i, v in enumerate(sub):
                visit(v, f"{pointer}/{keyword}/{i}")
    # `items` is special: single schema (Draft 2020-12) or tuple-list
    # (Draft 4 / 7). Both forms are accepted by the api-endpoint contract.
    items = node.get("items")
    if isinstance(items, dict):
        visit(items, f"{pointer}/items")
    elif isinstance(items, list):
        for i, v in enumerate(items):
            visit(v, f"{pointer}/items/{i}")
    for keyword in _JSONSCHEMA_SINGLE_KEYWORDS:
        sub = node.get(keyword)
        if isinstance(sub, dict):
            visit(sub, f"{pointer}/{keyword}")


# Per-marker sibling-key violation kinds → human-readable message tails. The
# pointer is prefixed at emission time. Keys are the `kind` strings emitted by
# `_check_marker_siblings`.
_MARKER_SIBLING_MESSAGES = {
    "object_requires_properties": (
        'declares arrow_type "Object" but has no non-empty `properties` map; '
        "the `Object` marker requires `properties` describing the inner field shape "
        "(use `Json` for an opaque object with no declared shape)."
    ),
    "object_forbids_items": (
        'declares arrow_type "Object" with an `items` sibling; `items` belongs to the '
        "`List` marker, not `Object`."
    ),
    "list_requires_items": (
        'declares arrow_type "List" but has no `items` field spec; the `List` marker '
        "requires `items` to be a sub-schema describing the element shape (a boolean / "
        "null / scalar does not count — use `Json` for an opaque array with no declared "
        "element shape)."
    ),
    "list_forbids_properties": (
        'declares arrow_type "List" with a `properties` sibling; `properties` belongs to '
        "the `Object` marker, not `List`."
    ),
    "json_forbids_properties": (
        'declares arrow_type "Json" with a `properties` sibling; `Json` is opaque and '
        "takes no inner declaration — use `Object` + `properties` to declare a shape."
    ),
    "json_forbids_items": (
        'declares arrow_type "Json" with an `items` sibling; `Json` is opaque and takes '
        "no inner declaration — use `List` + `items` to declare an element shape."
    ),
}


def _marker_sibling_message(kind: str) -> str:
    """Message tail for a marker sibling-key violation `kind`.

    Uses `.get` with a generic fallback rather than a direct subscript so a
    future `kind` added to `_check_marker_siblings` without a matching
    `_MARKER_SIBLING_MESSAGES` entry degrades to a still-useful finding instead
    of raising `KeyError`. A raise here would unwind `check_type_map_coverage`
    and collapse every coverage finding for the connector into one synthetic
    crash finding (cf. the `_check_type_map_rules_guarded` precedent for the
    same hazard). `test_marker_message_keys_cover_every_emitted_kind` keeps the
    mapping complete for all currently-emitted kinds.
    """
    return _MARKER_SIBLING_MESSAGES.get(
        kind, f"violates the bare-marker arrow_type sibling-key contract (kind: {kind})."
    )


def _check_marker_siblings(node: dict, arrow: str, pointer: str, out: list[tuple[str, str]]) -> None:
    """Emit sibling-key violations for one node carrying a bare-marker arrow_type.

    Enforces the `authored_shape_type` contract the JSON Schema layer leaves
    open (see `_BARE_MARKER_ARROW_TYPES`): `Object` requires a non-empty
    `properties` map and forbids `items`; `List` requires an `items` sub-schema
    and forbids `properties`; `Json` forbids both. Nodes whose `arrow_type` is
    a scalar or a parameterized container (`Struct<…>`, `List<Int64>`, …) carry
    no sibling contract and are ignored here.

    The require checks demand the sibling actually be a sub-schema, not merely
    present: `Object` needs a truthy `properties` dict, `List` needs `items`
    to be a sub-schema — a dict (the api-endpoint contract's single-schema
    form) or, defensively, a non-empty Draft-4/7 tuple list (the contract types
    `items` as a single object, so a tuple is itself a Layer 1 error, but the
    walkers tolerate it). A boolean / null / scalar / empty-list `items` does
    NOT satisfy `List` — none can describe an element shape, so it is reported
    as missing.
    """
    if arrow == "Object":
        props = node.get("properties")
        if not (isinstance(props, dict) and props):
            out.append((pointer, "object_requires_properties"))
        if "items" in node:
            out.append((pointer, "object_forbids_items"))
    elif arrow == "List":
        items = node.get("items")
        if not (isinstance(items, dict) or (isinstance(items, list) and items)):
            out.append((pointer, "list_requires_items"))
        if "properties" in node:
            out.append((pointer, "list_forbids_properties"))
    elif arrow == "Json":
        if "properties" in node:
            out.append((pointer, "json_forbids_properties"))
        if "items" in node:
            out.append((pointer, "json_forbids_items"))


def _walk_jsonschema_markers(node: Any, pointer: str, out: list[tuple[str, str]]) -> None:
    """Recurse a JSON Schema, collecting bare-marker sibling-key violations.

    Reuses `_recurse_jsonschema` so it visits exactly the recursive
    `JsonSchemaPropertyNode` keywords the well-formed and asymmetric walkers
    do — children inside `properties` / `items` are themselves marker-checked,
    matching the contract's "recursive" rule.

    Like the other well-formed walkers, `_recurse_jsonschema` only descends
    `isinstance`-gated containers, so a recursive keyword that is present but
    the wrong type (e.g. `anyOf` authored as a dict) is silently skipped here —
    any marker beneath it goes unchecked. That gap is covered by the
    co-running `_collect_asymmetric_pairs` (a `non_dict_subtree` warning) and,
    in default validation, by Layer 1 (a schema error). Both emission sites
    run the asymmetric walker alongside this one; a future caller that does not
    would lose the wrong-type signal.
    """
    if not isinstance(node, dict):
        return
    arrow = node.get("arrow_type")
    if isinstance(arrow, str) and arrow in _BARE_MARKER_ARROW_TYPES:
        _check_marker_siblings(node, arrow, pointer, out)
    _recurse_jsonschema(node, pointer, lambda child, child_ptr: _walk_jsonschema_markers(child, child_ptr, out))


def _walk_op_for_markers(op: dict, base_pointer: str, schema_field: str, out: list[tuple[str, str]]) -> None:
    """Collect bare-marker sibling-key violations from one endpoint operation.

    Visits the operation's `<schema_field>.schema` JSON-Schema tree (recursive),
    matching the sub-tree `_walk_endpoint_op` walks for coverage. `params` are
    not visited: the published `Param` def forbids `arrow_type`, so there is
    no marker to check — an annotated param is flagged wholesale by the
    asymmetric walker's `param_annotations` finding.
    """
    body = op.get(schema_field)
    if isinstance(body, dict) and isinstance(body.get("schema"), dict):
        _walk_jsonschema_markers(body["schema"], f"{base_pointer}/{schema_field}/schema", out)


def _collect_marker_sibling_violations(endpoint_doc: dict) -> list[tuple[str, str]]:
    """Return `(json_pointer, kind)` tuples for bare-marker sibling-key
    violations across an api-endpoint document's response/input schemas.

    Walks the same `operations.read.response.schema` /
    `operations.write.<mode>.input.schema` sub-trees the coverage walker
    visits (`params` carry no annotations and are not marker-checked).
    """
    out: list[tuple[str, str]] = []
    operations = endpoint_doc.get("operations")
    if not isinstance(operations, dict):
        return out
    read = operations.get("read")
    if isinstance(read, dict):
        _walk_op_for_markers(read, "/operations/read", "response", out)
    write = operations.get("write")
    if isinstance(write, dict):
        # Iterate defensively — Layer 1 fixes write modes to {insert, upsert},
        # but stay correct if the schema later widens the enum.
        for mode, mode_op in write.items():
            if isinstance(mode_op, dict):
                _walk_op_for_markers(mode_op, f"/operations/write/{mode}", "input", out)
    return out


def check_endpoint_annotations(doc: Any) -> list[dict]:
    """Run the asymmetric / non-string / param-annotation / non-dict-subtree
    pair walker on an endpoint document validated directly.

    The same walker runs from `check_type_map_coverage` when a connector
    is validated (it walks sibling endpoints). When the orchestrator
    validates an endpoint file by itself with `--schema-url=api-endpoint/latest.json`,
    Layer 1 catches malformed annotation pairs via `dependentRequired`
    and annotated params via the `Param` def's `additionalProperties:
    false`; under `--semantic-only` Layer 1 is bypassed, so this validator
    surfaces the same findings the coverage walker would have emitted
    when invoked from a parent connector.

    Also enforces the bare-marker sibling-key contract
    (`_BARE_MARKER_ARROW_TYPES`): `Object` → `properties`, `List` → `items`,
    `Json` → neither. The JSON Schema layer accepts the marker but leaves the
    sibling keys unconstrained, so the semantic layer (this validator, and
    `type-map-coverage` for connector-level runs) is the only thing that
    catches an `Object` with no `properties` or a `Json` carrying an inner
    declaration.
    """
    findings: list[dict] = []
    if not isinstance(doc, dict):
        return findings
    for problem_pointer, problem_kind in _collect_asymmetric_pairs(doc):
        if problem_kind == "asymmetric":
            findings.append(
                finding(
                    "endpoint-annotations",
                    "error",
                    "/",
                    f"endpoint field at {problem_pointer} declares exactly one of native_type / arrow_type; both are required per the api-endpoint schema.",
                    rule_doc="shared/type-maps.md",
                )
            )
        elif problem_kind == "both_non_string":
            findings.append(
                finding(
                    "endpoint-annotations",
                    "error",
                    "/",
                    f"endpoint field at {problem_pointer} declares native_type / arrow_type with non-string value(s); both must be strings per the api-endpoint schema.",
                    rule_doc="shared/type-maps.md",
                )
            )
        elif problem_kind == "param_annotations":
            findings.append(
                finding(
                    "endpoint-annotations",
                    "error",
                    "/",
                    f"endpoint param at {problem_pointer} carries native_type / arrow_type; the api-endpoint schema's Param forbids both — a param is a request input, typed only by its `type` field. Remove the annotations.",
                    rule_doc="shared/type-maps.md",
                )
            )
        elif problem_kind == "non_dict_subtree":
            findings.append(
                finding(
                    "endpoint-annotations",
                    "warning",
                    "/",
                    f"endpoint sub-tree at {problem_pointer} is not a JSON object; the asymmetric-pair walker could not recurse here, so any annotations beneath it are not checked. Edit the endpoint to make that sub-tree a JSON object, or rerun without `--semantic-only` to see the Layer 1 schema error.",
                    rule_doc="shared/type-maps.md",
                )
            )
    for problem_pointer, problem_kind in _collect_marker_sibling_violations(doc):
        findings.append(
            finding(
                "endpoint-annotations",
                "error",
                "/",
                f"endpoint field at {problem_pointer} {_marker_sibling_message(problem_kind)}",
                rule_doc="endpoints/api-endpoint-schema-parameterization.md",
            )
        )
    return findings


# The engine locates an API endpoint on disk as
# `{connector_id}/definition/endpoints/{endpoint_id}.json` — the filename is the
# lookup key, derived from the document's `endpoint_id`. The published
# api-endpoint schema constrains `endpoint_id` (required, `^[a-z0-9][a-z0-9_-]*$`)
# but cannot see the filename, so a file whose basename disagrees with its
# `endpoint_id` is invisible to Layer 1 yet broken at runtime (the engine and
# the on-disk file diverge). The semantic layer is the only place this is
# catchable; enforced from both `check_endpoint_filename` (endpoint validated
# directly) and `check_type_map_coverage` (sibling endpoints during connector
# validation), mirroring the endpoint-annotations split.
def _endpoint_filename_findings(ep_doc: Any, filename: str) -> list[dict]:
    """Findings for an api-endpoint whose file basename ≠ `{endpoint_id}.json`.

    `filename` is the endpoint file's basename (e.g. `"users.json"`). Returns
    `[]` when the document is not a dict (a defensive guard — the connector
    walk shape-gates non-dict siblings before calling, and the standalone
    caller passes a dict) or when the names already agree.

    A missing / non-string `endpoint_id` is NOT a silent pass — it warns. The
    filename↔id equality is the only thing this check exists to verify, so an
    unusable id means the check is *prevented*, not satisfied. Layer 1 owns the
    hard required/pattern error, but it never runs on sibling endpoints (only
    the top-level CLI document is schema-validated) nor under `--semantic-only`,
    so a bare `return []` here would let exactly the file this check targets —
    one with no resolvable on-disk name — pass green. Mirrors the
    `doc_path is None` / `kind is None` "skipped" warnings.
    """
    if not isinstance(ep_doc, dict):
        return []
    endpoint_id = ep_doc.get("endpoint_id")
    if not isinstance(endpoint_id, str):
        return [
            finding(
                "endpoint-filename",
                "warning",
                "/endpoint_id",
                f"endpoint filename check skipped for {filename!r}: endpoint_id is "
                "absent or non-string, so the basename cannot be compared to the "
                "expected '{endpoint_id}.json'. Layer 1 owns the hard required/pattern "
                "error, but it never runs on sibling endpoints during connector "
                "validation, nor under `--semantic-only` — validate this endpoint file "
                "directly (without `--semantic-only`) to surface the schema error.",
                rule_doc="endpoints/api-endpoint-schema-parameterization.md",
            )
        ]
    expected = f"{endpoint_id}.json"
    if filename == expected:
        return []
    return [
        finding(
            "endpoint-filename",
            "error",
            "/endpoint_id",
            f"endpoint file is named {filename!r} but its endpoint_id is {endpoint_id!r}; "
            f"the file must be named {expected!r} — the engine locates an endpoint as "
            "endpoints/{endpoint_id}.json, so a divergent filename is unreachable at runtime.",
            rule_doc="endpoints/api-endpoint-schema-parameterization.md",
        )
    ]


def check_endpoint_filename(doc: Any, doc_path: Path | None = None) -> list[dict]:
    """Enforce that an api-endpoint file's basename equals `{endpoint_id}.json`.

    Runs only for api-endpoint documents (the dispatcher gates this via
    `_ENDPOINT_ONLY`). When `doc_path` is None the validator was invoked
    without a filesystem anchor, so the basename can't be compared — emit a
    warning rather than a silent pass, mirroring `check_type_map_coverage`'s
    "no document path" warning. The same equality is also enforced on every
    sibling endpoint during connector-level validation (in
    `check_type_map_coverage`), so connector runs cover it too.
    """
    findings: list[dict] = []
    if not isinstance(doc, dict):
        return findings
    if doc_path is None:
        findings.append(
            finding(
                "endpoint-filename",
                "warning",
                "/",
                "endpoint filename check skipped: validator was invoked without a "
                "filesystem-anchored document path; the file basename cannot be compared "
                "to endpoint_id.",
                rule_doc="endpoints/api-endpoint-schema-parameterization.md",
            )
        )
        return findings
    findings.extend(_endpoint_filename_findings(doc, doc_path.name))
    return findings


SEMANTIC_VALIDATORS: dict[str, Callable[..., list[dict]]] = {
    "reserved-field": check_reserved_fields,
    "expression-resolver": check_expressions,
    "transport-ref": check_transport_refs,
    "dsn-binding": check_dsn_bindings,
    "auth-shape": check_auth_shape,
    "tls-consistency": check_tls_consistency,
    "phase-resolvability": check_phase_resolvability,
    "type-map-coverage": check_type_map_coverage,
    "type-map-rule": check_type_map_rules,
    "type-map-write-coverage": check_type_map_write_coverage,
    "endpoint-annotations": check_endpoint_annotations,
    "endpoint-filename": check_endpoint_filename,
}

# Validators that accept an optional `doc_path` second positional argument.
# `type-map-rule` and `type-map-write-coverage` use it to derive the rule
# direction from the on-disk filename (`type-map-write.json` → write);
# `endpoint-filename` uses it to compare the file's basename to `endpoint_id`.
_PATH_AWARE_VALIDATORS = {"type-map-coverage", "type-map-rule", "type-map-write-coverage", "endpoint-filename"}
_CONNECTOR_ONLY = {"transport-ref", "dsn-binding", "auth-shape", "tls-consistency", "type-map-coverage"}
_TYPE_MAP_ONLY = {"type-map-rule", "type-map-write-coverage"}
_ENDPOINT_ONLY = {"endpoint-annotations", "endpoint-filename"}

# Module-load invariant: every dispatched validator id MUST be registered in
# VALIDATOR_IDS. The crash-handler in `run_semantic_validators` calls
# `finding(vid, ...)`, which validates `vid` via `raise ValueError` (see
# `finding()`). Without this invariant, a dispatched-but-unregistered `vid`
# would propagate that `ValueError` uncaught past the per-validator guard.
# Using `raise RuntimeError` rather than `assert` so the check fails fast
# at module load even under `python -O` (which strips assertions).
# `_safe_check_type_map_rules` passes a literal `"type-map-rule"` id and is
# already safe by construction; this invariant protects the dispatcher's
# `vid`-tagged crash finding only.
_unregistered = set(SEMANTIC_VALIDATORS.keys()) - VALIDATOR_IDS
if _unregistered:
    raise RuntimeError(
        f"SEMANTIC_VALIDATORS contains ids not registered in VALIDATOR_IDS: "
        f"{sorted(_unregistered)}"
    )
del _unregistered


def is_connector_doc(doc: Any) -> bool:
    """Detect a connector-shaped document for dispatch purposes.

    Permissive: any dict with `kind` qualifies. Layer 1 owns the
    required-key errors for substructures; the connector-only
    validators each emit their own error when a substructure is
    *present but malformed*. `transport-ref` additionally surfaces the
    *absent-transports* case so the rest of the dispatch isn't quietly
    skipped under `--semantic-only`. The other connector-only
    validators (`dsn-binding`, `auth-shape`, `tls-consistency`,
    `type-map-coverage`) rely on Layer 1 for required-key surfacing
    and would no-op on absence under `--semantic-only` — that's
    intentional, with `transport-ref`'s absent-transports finding
    serving as the canonical "this isn't a complete connector" signal.
    """
    return isinstance(doc, dict) and "kind" in doc


def is_endpoint_doc(doc: Any) -> bool:
    """Detect an api-endpoint document for dispatch purposes.

    True when `doc` is a dict carrying `operations` (the api-endpoint
    discriminator under the shared endpoint contract) and crucially
    does NOT carry `kind` (which would route it to the connector
    dispatch).

    Used so that when the orchestrator validates an api-endpoint file
    directly under `--semantic-only`, the asymmetric-pair walker still
    runs and surfaces malformed `native_type`/`arrow_type` annotations
    that Layer 1 would have caught.

    Database endpoints are deliberately NOT routed here: they have
    `endpoint_id` but no `operations`, and they carry annotations on
    `columns[]` instead of inside `operations.*`. The walker
    (`_collect_asymmetric_pairs`) only knows the api-endpoint shape, so
    routing database endpoints would produce zero findings — a silent
    pass that would advertise coverage the validator chain doesn't
    deliver. Database-endpoint shape validation is out of scope for
    this plugin (DB endpoints are produced at runtime by the
    connector's resource_discovery).
    """
    if not isinstance(doc, dict):
        return False
    if "kind" in doc:
        return False
    return "operations" in doc


def is_type_map_doc(doc: Any) -> bool:
    """Detect a type-map document (`type-map-read.json` / `type-map-write.json`).

    True when `doc` is a list AND at least one element looks like a rule
    (a dict). This dispatches `check_type_map_rules` on any plausible
    type-map shape — including arrays with malformed rules — so the
    validator surfaces shape errors instead of silently skipping the
    document. Layer 1 catches `minItems: 1`; the per-rule structural
    constraints are enforced by `check_type_map_rules` itself.

    Returns False for `[]` (no rules to validate; Layer 1 owns the
    minItems error) and for non-list documents.
    """
    if not isinstance(doc, list) or not doc:
        return False
    return any(isinstance(r, dict) for r in doc)


def _looks_like_legacy_type_map(doc: Any) -> bool:
    """Heuristic: detect legacy type-map shapes (object wrappers,
    `native_to_arrow.rules`, or `method`-keyed rules) so authors who
    haven't migrated get an explicit warning instead of a silent pass.

    Three legacy shapes are recognized:

    1. Wrapped object: `{"native_to_arrow": {"rules": [...]}}`.
    2. Rules-keyed object: `{"rules": [...]}` (a different drift —
       authors who mistook the file format).
    3. Top-level list of rules using the renamed `method` key
       (e.g. `[{"method": "exact", ...}]`). This is the most common
       transcription of the old shape because the on-disk container
       was already a list; only the rule key name changed in the
       migration. Catching this case inside `is_type_map_doc` /
       `check_type_map_rules` would surface as "match must be exact
       or regex" per-rule errors — actionable but doesn't tell the
       author "rename `method` to `match`". The heuristic gives them
       that explicit pointer.
    """
    # Variant 1 + 2: object shapes. Skip if the dict looks like a connector
    # (kept in sync with `is_connector_doc`: any dict with `kind`) or an
    # endpoint (has `endpoint_id` or `operations`). Connector docs are
    # covered by the embedded-`type_maps` hint below; endpoint docs are
    # covered by Layer 1's `additionalProperties` rule only (no dedicated
    # Layer 2 hint, since endpoints don't carry type-map shapes today).
    if isinstance(doc, dict):
        looks_like_other_artifact = (
            "kind" in doc
            or "endpoint_id" in doc
            or "operations" in doc
        )
        if looks_like_other_artifact:
            return False
        if isinstance(doc.get("native_to_arrow"), dict):
            return True
        rules = doc.get("rules")
        if isinstance(rules, list) and rules and any(
            isinstance(r, dict) and ("native" in r or "method" in r) for r in rules
        ):
            return True
        return False
    # Variant 3: top-level list whose rule entries use `method` (legacy key)
    # without `match` (current key). A list with both is treated as a transient
    # author error and routed through `check_type_map_rules` (which will warn).
    if isinstance(doc, list) and doc:
        any_legacy_only = any(
            isinstance(r, dict) and "method" in r and "match" not in r for r in doc
        )
        return any_legacy_only
    return False


def run_semantic_validators(doc: Any, doc_path: Path | None = None) -> list[dict]:
    findings: list[dict] = []
    is_ep = is_endpoint_doc(doc)
    # Top-level shape guard. A scalar (str/number/bool/null) or a list whose
    # elements aren't dicts cannot be any known published artifact — running
    # the dispatch table on it would silently produce zero findings under
    # `--semantic-only` (Layer 1 would catch it, but Layer 1 may be bypassed).
    # Surface this explicitly so a green pass on garbage is impossible.
    if not isinstance(doc, (dict, list)):
        findings.append(
            finding(
                "json-schema",
                "error",
                "/",
                f"document root must be a JSON object or array; got {type(doc).__name__}.",
            )
        )
        return findings
    if isinstance(doc, list) and doc and not any(isinstance(r, dict) for r in doc):
        findings.append(
            finding(
                "json-schema",
                "error",
                "/",
                f"document root is a list with no object entries (sample types: {[type(r).__name__ for r in doc[:3]]}…); no published artifact has this shape. Mixed lists (at least one object) fall through to type-map dispatch.",
            )
        )
        return findings
    is_conn = is_connector_doc(doc)
    is_tm = is_type_map_doc(doc)
    # Dict roots that don't look like any recognized artifact (connector,
    # api-endpoint, or pre-migration type-map shape) get a warning so
    # `--semantic-only` can't silently green-light an unrecognized shape.
    # The most common case is a DB endpoint document (`endpoint_id` +
    # `columns[]` but no `operations`) — explicitly out of scope for this
    # plugin per CLAUDE.md, but the silent pass would be misleading. The
    # legacy-shape hint emitted below covers connector docs and standalone
    # type-map shapes; this warning covers everything else.
    if (
        isinstance(doc, dict)
        and not is_conn
        and not is_ep
        and not _looks_like_legacy_type_map(doc)
        and "endpoint_id" not in doc  # let DB endpoints fall through with a specific hint below
    ):
        findings.append(
            finding(
                "json-schema",
                "warning",
                "/",
                "document does not look like any recognized artifact (connector / api-endpoint / type-map). No semantic validators apply; rerun without `--semantic-only` so Layer 1 can identify the shape.",
            )
        )
    elif isinstance(doc, dict) and "endpoint_id" in doc and not is_ep:
        # `is_endpoint_doc` excludes any dict with `kind`, so this branch also
        # catches the pathological "connector with stray `endpoint_id`" case.
        # That's a tolerable cost — Layer 1 catches the structural issue and
        # under `--semantic-only` `check_transport_refs` will fire its own
        # absent-/malformed-transports error alongside. The common case here
        # is a real DB endpoint document (`endpoint_id` + `columns[]`, no
        # `kind`, no `operations`), which is out of plugin scope.
        findings.append(
            finding(
                "json-schema",
                "warning",
                "/",
                "document carries `endpoint_id` without `operations` (looks like a database-endpoint). Database endpoints are produced at runtime by `resource_discovery` and are not authored by this plugin — rerun against the published `database-endpoint/latest.json` schema for Layer 1 validation.",
            )
        )
    # Empty top-level array. Today only type-map files have a list root, so
    # this nearly always means an empty type-map; phrase the warning to be
    # informative without presuming intent. The warning fires from any
    # Layer 2 run (not only `--semantic-only`); under default validation
    # it complements Layer 1's `minItems` error rather than replacing it.
    if isinstance(doc, list) and not doc:
        findings.append(
            finding(
                "type-map-rule",
                "warning",
                "/",
                "document root is an empty array. If this is meant to be a type map (type-map-read.json / type-map-write.json), it must contain at least one rule (Layer 1's `minItems` rule); no other published artifact has a list root.",
                rule_doc="shared/type-maps.md",
            )
        )
    # Migration hint for legacy type-map shapes. Fires from any Layer 2
    # run; under default validation it complements the Layer 1 error
    # (`additionalProperties` for embedded `type_maps`, schema rejection for
    # legacy wrappers); under `--semantic-only` it's the only signal. Two
    # paths trigger it:
    # - Standalone documents that look like the old shape (object wrappers,
    #   or top-level lists keyed by the legacy `method`).
    # - Connector documents that still carry an embedded `type_maps` block.
    if _looks_like_legacy_type_map(doc) or (
        is_conn and isinstance(doc, dict) and "type_maps" in doc
    ):
        findings.append(
            finding(
                "type-map-rule",
                "warning",
                "/",
                "document looks like a pre-migration type-map shape (embedded `type_maps` inside connector.json, object wrapper such as `native_to_arrow.rules` or `{rules: [...]}`, OR a top-level list using the legacy `method` rule key). The current contract is standalone `{connector_id}/definition/type-map-read.json` (native → Arrow; plus `type-map-write.json` for database connectors) holding a top-level JSON array of `{match, native, canonical}` rules — extract any embedded block to those siblings, rename `method` → `match`, and unwrap any object container. See `shared/type-maps.md`.",
                rule_doc="shared/type-maps.md",
            )
        )
    for vid, fn in SEMANTIC_VALIDATORS.items():
        if vid in _CONNECTOR_ONLY and not is_conn:
            continue
        if vid in _TYPE_MAP_ONLY and not is_tm:
            continue
        if vid in _ENDPOINT_ONLY and not is_ep:
            continue
        # Per-validator try/except so a crash in one doesn't discard the
        # findings other validators have already produced. The synthetic
        # crash finding tags the offending validator id so the orchestrator
        # can route it.
        try:
            if vid in _PATH_AWARE_VALIDATORS:
                findings.extend(fn(doc, doc_path))
            else:
                findings.extend(fn(doc))
        except Exception as exc:  # noqa: BLE001 — last-resort guard
            # Tag the finding with the failing validator id so the orchestrator
            # can route it correctly. Every dispatched `vid` is guaranteed to
            # be in VALIDATOR_IDS (registered at module load).
            findings.append(
                finding(
                    vid,
                    "error",
                    "",
                    f"validator {vid!r} crashed unexpectedly ({type(exc).__name__}: {exc}); other validators continued. This is a validator bug — please report.",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an Analitiq connector or endpoint document.")
    parser.add_argument("--schema-url", help="Published schema URL to validate against. Required unless --semantic-only (Layer 1 needs it to fetch; Layer 2 does not).")
    parser.add_argument("--document", required=True, help="Path to JSON document to validate.")
    parser.add_argument("--semantic-only", action="store_true", help="Skip Layer 1 JSON Schema validation.")
    parser.add_argument("--json-only", action="store_true", help="Skip Layer 2 semantic validators.")
    parser.add_argument("--no-cache", action="store_true", help="Bypass schema disk cache.")
    args = parser.parse_args()

    if args.semantic_only and args.json_only:
        parser.error("--semantic-only and --json-only are mutually exclusive (would skip all validation).")

    if not args.semantic_only and not args.schema_url:
        parser.error("--schema-url is required unless --semantic-only is given (Layer 1 fetches and validates against it).")

    document_path = Path(args.document)
    try:
        document = json.loads(document_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "findings": [
                        finding("json-schema", "error", "", f"Cannot read document: {exc}")
                    ],
                }
            )
        )
        return 1

    findings: list[dict] = []

    if not args.semantic_only:
        try:
            schema = fetch_schema(args.schema_url, cache=not args.no_cache)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError, RuntimeError, UnicodeDecodeError) as exc:
            print(
                json.dumps(
                    {
                        "passed": False,
                        "findings": [
                            finding("json-schema", "error", "", f"Cannot fetch schema {args.schema_url}: {exc}")
                        ],
                    }
                )
            )
            return 1
        findings.extend(layer1_jsonschema(document, schema))

    if not args.json_only:
        # Per-validator try/except is inside `run_semantic_validators` itself
        # so a crashing validator becomes one synthetic finding while every
        # other validator's findings survive. No outer wrap here — it would
        # mask the partial results.
        findings.extend(run_semantic_validators(document, doc_path=document_path.resolve()))

    passed = all(f["severity"] != "error" for f in findings)
    print(json.dumps({"passed": passed, "findings": findings}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
