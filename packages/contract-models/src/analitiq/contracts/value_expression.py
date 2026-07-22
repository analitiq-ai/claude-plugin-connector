"""Shared value-expression and transport-header resolution.

Implements the connector schema contract's resolution rules (the value-expression
grammar and the connector Header Resolution rules) in one place so every caller
resolves refs/templates/functions and merges transport headers identically.

Context shape (nested, scoped — never a flat merge):

    {
        "connection": {"parameters": {...}, "selections": {...}, "discovered": {...}},
        "secrets": {...},
        "auth": {...},
        "runtime": {"oauth": {...}, ...},
    }

`${a.b.c}` placeholders walk this nested structure; bare `${name}` falls back
to a flat lookup in `secrets` then the top-level context (the legacy shape
some connector specs still use).

`build_resolution_context` assembles this shape from a stored connection
record and its resolved secret set; the `auth` scope is the opaque OAuth
token payload stored verbatim under `OAUTH_TOKEN_PAYLOAD_KEY` in the
connection's secret blob.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

LOG = logging.getLogger(__name__)

# Reserved entry in the connection secret blob holding the opaque OAuth
# token payload — the provider's token response stored verbatim, per the
# connection schema contract ("providers return different fields and there
# is no reliable universal taxonomy"). Contract-declared secrets stay flat
# next to it; this key never gets a `secret_refs` pointer and never leaks
# into the `secrets` resolution scope.
OAUTH_TOKEN_PAYLOAD_KEY = "__oauth_token_payload"

_TEMPLATE_RE = re.compile(r"\$\{([^}]+)\}")

# The four object forms the value-expression contract defines. A dict bearing
# any of these keys is an expression node; a dict bearing none is structural
# JSON. A structural object that legitimately needs one of these field names
# can be authored as `{"literal": {...}}`, which passes the wrapped value
# through verbatim.
_EXPRESSION_KEYS = ("template", "function", "literal", "ref")

# The complete value-expression resolution-scope vocabulary (spec: §Value
# Expressions). A `ref` path and every `${...}` template placeholder addresses
# one of these leading scopes. `build_resolution_context` populates the
# connection-phase subset (connection/secrets/auth/runtime); endpoint resolution
# adds the rest (connector/stream/state/request/response). This is the single
# source of the scope vocabulary — the contract-model validators import it.
RESOLUTION_SCOPES: tuple[str, ...] = (
    "connector",
    "connection",
    "secrets",
    "auth",
    "stream",
    "state",
    "runtime",
    "request",
    "response",
)


def _is_expression_node(value: Any) -> bool:
    """True when a value is a value-expression object form (not structural JSON)."""
    return isinstance(value, dict) and any(k in value for k in _EXPRESSION_KEYS)


def iter_expression_strings(node: Any) -> Iterator[tuple[str, str]]:
    """Yield ``('ref'|'template', value)`` for every *resolvable* expression
    string in ``node``, mirroring ``resolve_template_deep``'s dispatch and
    boundaries so validators parse expressions exactly as the resolver does: a
    bare string is a template; a ``literal`` subtree is opaque data; a
    ``function``'s ``input`` is a nested expression while its ``map`` is a data
    table. Lets the contract models enforce value-expression rules without
    re-implementing this grammar."""
    if isinstance(node, str):
        yield ("template", node)
        return
    if isinstance(node, dict):
        # Dispatch on the first key the resolver honours, in `_EXPRESSION_KEYS` order.
        if "template" in node:
            if isinstance(node["template"], str):
                yield ("template", node["template"])
            return
        if "function" in node:
            yield from iter_expression_strings(node.get("input"))
            return
        if "literal" in node:
            return
        if "ref" in node:
            if isinstance(node["ref"], str):
                yield ("ref", node["ref"])
            return
        for value in node.values():
            yield from iter_expression_strings(value)
        return
    if isinstance(node, list):
        for item in node:
            yield from iter_expression_strings(item)


def template_placeholders(template: str) -> list[str]:
    """Return the resolution key of every `${...}` placeholder in `template`,
    stripped exactly as the resolver strips it before lookup. A template with no
    placeholders (a plain literal string) returns an empty list."""
    return [match.strip() for match in _TEMPLATE_RE.findall(template)]


def build_resolution_context(
    *,
    connection: dict[str, Any],
    secrets: dict[str, Any] | None,
    connector: dict[str, Any] | None = None,
    platform_secrets: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the scoped resolution context (module-docstring shape) from a
    connection record and its resolved secret set.

    The opaque OAuth token payload stored under `OAUTH_TOKEN_PAYLOAD_KEY`
    becomes the `auth` scope: every payload field resolves as
    `auth.<field>`, and the whole payload additionally nests as
    `auth.token_response.<field>` per the connection schema contract. The
    reserved key is removed from the `secrets` scope. (A provider field
    literally named `token_response` is shadowed by the nesting and stays
    reachable as `auth.token_response.token_response`.)

    When `connector` and `platform_secrets` are given, contract inputs with
    `source: platform` are projected into the scope their declared
    `storage` names — `connection.parameters` or, for credential material
    (the default), `secrets` — so templates like
    `basic_auth(connection.parameters.client_id, secrets.client_secret)`
    resolve for platform-provisioned credentials. Values already present in
    the connection record / secret set win over the projection.
    """
    secret_scope = dict(secrets or {})
    payload = secret_scope.pop(OAUTH_TOKEN_PAYLOAD_KEY, None) or {}

    parameters = dict(connection.get("parameters") or {})
    selections = dict(connection.get("selections") or {})
    discovered = dict(connection.get("discovered") or {})

    if connector and platform_secrets:
        inputs = ((connector.get("connection_contract") or {}).get("inputs")) or {}
        for name, spec in inputs.items():
            if not isinstance(spec, dict) or spec.get("source") != "platform":
                continue
            if name not in platform_secrets:
                continue
            if spec.get("storage") == "connection.parameters":
                parameters.setdefault(name, platform_secrets[name])
            else:
                secret_scope.setdefault(name, platform_secrets[name])

    context: dict[str, Any] = {
        "connection": {
            "parameters": parameters,
            "selections": selections,
            "discovered": discovered,
        },
        "secrets": secret_scope,
        "auth": {**payload, "token_response": dict(payload)},
    }
    if runtime is not None:
        context["runtime"] = dict(runtime)
    return context


def resolve_path(path: str, context: dict[str, Any]) -> Any | None:
    """Walk a dotted reference path against the nested context.

    Returns None when any segment is missing or non-mappable. Empty strings,
    dicts, and lists count as unresolved (an unsubmitted optional input reads
    the same as a missing one).
    """
    cur: Any = context
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    if cur is None or cur == "" or cur == {} or cur == []:
        return None
    return cur


def _lookup_placeholder(key: str, context: dict[str, Any]) -> Any | None:
    """Resolve a single `${...}` placeholder key, or None when unresolved.

    Dotted refs walk the nested context; bare names fall back to a flat
    lookup of `context['secrets']` then the top-level context.
    """
    val = resolve_path(key, context)
    if val is not None:
        return val
    secrets = context.get("secrets") or {}
    if isinstance(secrets, dict) and secrets.get(key) is not None:
        return secrets[key]
    return context.get(key)


def _has_unresolved_placeholder(template: str, context: dict[str, Any]) -> bool:
    """True when any `${...}` placeholder in `template` fails to resolve.

    Lets the direct-template callers (URL, body) treat an unresolved
    placeholder as a hard error / drop instead of silently substituting "",
    using the same detection as `_resolve_function_input`.
    """
    return any(
        _lookup_placeholder(m.group(1).strip(), context) is None
        for m in _TEMPLATE_RE.finditer(template)
    )


def resolve_template_string(template: str, context: dict[str, Any]) -> str:
    """Resolve `${ref.path}` and bare `${name}` tokens inside a string.

    Unresolved placeholders substitute "" with a WARNING.
    """

    def _sub(m: "re.Match[str]") -> str:
        key = m.group(1).strip()
        val = _lookup_placeholder(key, context)
        if val is None:
            LOG.warning("value-expression: unresolved placeholder ${%s}", key)
            return ""
        return str(val)

    return _TEMPLATE_RE.sub(_sub, template)


def resolve_template_deep(obj: Any, context: dict[str, Any]) -> Any:
    """Recursively resolve value expressions nested in dicts/lists.

    Expression-form dicts (`template`/`function`/`literal`/`ref`) dispatch
    through `resolve_value_expression` rather than serializing the raw
    expression dict onto the wire. An expression node that resolves to None —
    unresolved, or an authored `{"literal": null}`, which this layer cannot
    tell apart — drops its dict field / list item with a WARNING, mirroring
    how the header path drops an unresolved header. Plain structural
    dicts/lists recurse, and non-string scalars (including authored JSON
    nulls) are returned unchanged, so a JSON body keeps its native structure
    and types.
    """
    if isinstance(obj, str):
        return resolve_template_string(obj, context)
    if _is_expression_node(obj):
        resolved = resolve_value_expression(obj, context)
        if resolved is None:
            # No enclosing field to drop here — without this line a missed
            # top-level `ref`/`literal` would null the whole payload silently.
            LOG.warning(
                "value-expression: top-level expression resolved to None"
            )
        return resolved
    if isinstance(obj, dict):
        resolved_dict: dict[str, Any] = {}
        for key, value in obj.items():
            if _is_expression_node(value):
                resolved = resolve_value_expression(value, context)
                if resolved is None:
                    LOG.warning(
                        "value-expression: dropping field %r — expression resolved to None",
                        key,
                    )
                    continue
                resolved_dict[key] = resolved
            else:
                resolved_dict[key] = resolve_template_deep(value, context)
        return resolved_dict
    if isinstance(obj, list):
        resolved_list: list[Any] = []
        for value in obj:
            if _is_expression_node(value):
                resolved = resolve_value_expression(value, context)
                if resolved is None:
                    LOG.warning(
                        "value-expression: dropping list item — expression resolved to None"
                    )
                    continue
                resolved_list.append(resolved)
            else:
                resolved_list.append(resolve_template_deep(value, context))
        return resolved_list
    return obj


def _resolve_function_input(value: Any, context: dict[str, Any]) -> Any | None:
    """Resolve a function's `input` expression to a value or None (unresolved).

    Template forms (bare strings or `{"template": ...}`) substitute "" for
    unresolved placeholders, which would make the function encode garbage —
    empty or partial (`"Bearer ${missing}"` → `"Bearer "`). Any placeholder
    that fails to resolve marks the whole input unresolved. Placeholder-free
    strings and `{"literal": ...}` are explicit values and pass through.
    """
    if isinstance(value, dict) and "template" in value:
        template = value["template"]
    else:
        template = value
    if isinstance(template, str) and _has_unresolved_placeholder(template, context):
        return None
    return resolve_value_expression(value, context)


def resolve_function(spec: dict, context: dict[str, Any]) -> Any | None:
    """Resolve a registered value-expression function to a concrete value.

    Implements the `implemented` functions from the value-expression
    contract. Unknown / `planned` functions return None so the caller drops
    the field rather than serializing an unresolved expression.
    """
    name = spec.get("function")
    if name == "basic_auth":
        inp = spec.get("input") or {}
        username = _resolve_function_input(inp.get("username"), context)
        password = _resolve_function_input(inp.get("password"), context)
        if username is None or password is None:
            # A missing/typo'd credential input must drop the header (return
            # None) rather than send `Basic base64(":")` with partial/blank
            # credentials — surface the misconfig instead of masking it.
            LOG.warning(
                "value-expression: basic_auth dropped — unresolved %s input",
                "username" if username is None else "password",
            )
            return None
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return f"Basic {token}"
    if name == "base64_encode":
        value = _resolve_function_input(spec.get("input"), context)
        if value is None:
            # An absent/typo'd/unresolvable input must drop the field (return
            # None) rather than silently encode "" — same rationale as
            # basic_auth above.
            LOG.warning("value-expression: base64_encode dropped — unresolved input")
            return None
        return base64.b64encode(str(value).encode()).decode()
    if name == "url_encode":
        value = _resolve_function_input(spec.get("input"), context)
        if value is None:
            LOG.warning("value-expression: url_encode dropped — unresolved input")
            return None
        return quote(str(value), safe=spec.get("safe", ""))
    if name == "lookup":
        value = _resolve_function_input(spec.get("input"), context)
        if value is None:
            LOG.warning("value-expression: lookup dropped — unresolved input")
            return None
        if not isinstance(value, (str, int, float, bool)):
            # Map keys are scalars; a ref resolving to a dict/list is a
            # connector authoring error. Drop it instead of letting the
            # (unhashable) membership test below raise TypeError.
            LOG.warning(
                "value-expression: lookup dropped — non-scalar input of type %s",
                type(value).__name__,
            )
            return None
        mapping = spec.get("map") or {}
        if value not in mapping:
            # Distinct from the unresolved-input case above: the input
            # resolved fine but the connector's map has no entry for it.
            LOG.warning("value-expression: lookup dropped — key %r not in map", value)
            return None
        return mapping[value]
    LOG.error("value-expression: unsupported function %r", name)
    return None


def resolve_value_expression(value: Any, context: dict[str, Any]) -> Any | None:
    """Resolve any value-expression node (string or object form).

    Object forms: `{template}`, `{function}`, `{ref}`, `{literal}`. Returns
    None for an unresolvable/unsupported node so header/field callers can drop
    it instead of emitting a raw dict. Dicts with none of the four recognised
    keys also emit a WARNING before returning None.
    """
    if isinstance(value, dict):
        if "template" in value:
            return resolve_template_string(value["template"], context)
        if "function" in value:
            return resolve_function(value, context)
        if "literal" in value:
            return value["literal"]
        if "ref" in value:
            return resolve_path(str(value["ref"]).strip(), context)
        LOG.warning("value-expression: unrecognised object form (keys: %s)", list(value))
        return None
    if isinstance(value, str):
        return resolve_template_string(value, context)
    return value


def resolve_header_value(name: str, value: Any, context: dict[str, Any]) -> str | None:
    """Resolve a single header value to its outgoing string form.

    Returns None — so the caller drops the header rather than putting a raw
    dict / unresolved expression on the wire — when the value-expression
    cannot be resolved (e.g. an unknown function or unsupported object form).
    """
    resolved = resolve_value_expression(value, context)
    if resolved is None:
        LOG.warning(
            "Dropping header %r: unresolved or unsupported value-expression", name
        )
        return None
    return str(resolved)


def is_json_content_type(content_type: str | None) -> bool:
    """True when a Content-Type selects JSON body encoding.

    Normalizes away parameters (`; charset=utf-8`) and casing so
    `application/json`, `Application/JSON`, and
    `application/json; charset=utf-8` all select the JSON path.
    """
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


def select_transport(connector: dict, template: dict) -> dict | None:
    """Pick the operation's transport: explicit `transport_ref`, else the
    connector's `default_transport`. Returns None when neither resolves.
    """
    transports = connector.get("transports") or {}
    ref = template.get("transport_ref") or connector.get("default_transport")
    return transports.get(ref) if ref else None


def resolve_transport_base_url(
    transport: dict | None, context: dict[str, Any] | None = None
) -> str:
    """A transport's `base_url` as a string the caller's URL builder can join
    with the operation path, accepting either a literal string or a
    value-expression object — mirroring how the engine's transport factory
    resolves `base_url` before building request URLs.

    A literal string (which may still bear `${...}` template placeholders) is
    returned unchanged, for the caller's existing placeholder resolution. An
    object form is normalized to that same string shape:

      * `{template}` → its raw template string, so the caller's own `${...}`
        resolution (and its unresolved-placeholder handling) runs identically
        to a bare-string `base_url`. No `context` required — this is the form
        the connector schema documents for a per-connection host.
      * `{literal}` → its wrapped string, verbatim. No `context` required — a
        literal is context-free.
      * `{ref}` / `{function}` → resolved against `context` to a concrete,
        non-empty string here. `context` is required for these two forms; a
        caller that resolves URLs against a different/flat context (e.g.
        oauth-start) passes none and so supports the literal + `{template}`
        forms, failing loud on `{ref}`/`{function}` rather than emitting a
        malformed URL.

    Returns "" when the transport declares no `base_url`.
    """
    base = (transport or {}).get("base_url")
    if not isinstance(base, dict):
        return base or ""
    template = base.get("template")
    if isinstance(template, str):
        return template
    if "template" in base:
        # A `{template}` node whose value isn't a string is malformed — surface
        # a clean error rather than the raw TypeError `resolve_template_string`
        # would raise on a non-string.
        raise ValueError(
            f"base_url value-expression has a non-string template: {base!r}"
        )
    if "literal" in base:
        # A literal is context-free — resolve it without requiring a context, so
        # a `{"literal": "https://…"}` base_url works in oauth-start too. It must
        # still be a non-empty URL string.
        literal = base["literal"]
        if not isinstance(literal, str) or not literal:
            raise ValueError(
                f"base_url literal must be a non-empty string: {base!r}"
            )
        return literal
    if context is None:
        raise ValueError(
            f"base_url value-expression {base!r} requires a resolution context"
        )
    resolved = resolve_value_expression(base, context)
    if not isinstance(resolved, str) or not resolved:
        raise ValueError(
            f"base_url value-expression did not resolve to a URL string: {base!r}"
        )
    return resolved


def resolve_operation_url(
    operation: dict, context: dict[str, Any], *, transport: dict | None
) -> str:
    """Resolve an operation's request URL.

    An explicit `url` wins; otherwise the (already selected) transport's
    `base_url` — a literal or a value-expression, resolved via
    `resolve_transport_base_url` — is joined with the operation `path`. An
    operation declaring neither `url` nor `path` resolves to "" — a transport
    alone does not define an endpoint — so the caller can fail (or fall back)
    with its own context-specific handling. Placeholders resolve per the
    template rules.
    """
    url = operation.get("url")
    if not url:
        path = operation.get("path") or ""
        if not path:
            return ""
        base = resolve_transport_base_url(transport, context)
        url = base.rstrip("/") + "/" + path.lstrip("/") if base else path
    if _has_unresolved_placeholder(url, context):
        # A request URL with a missing segment is unconstructable — fail loud
        # instead of silently emitting a malformed URL (".../${id}/x" -> "...//x").
        raise ValueError(f"operation URL has unresolved placeholder(s): {url!r}")
    return resolve_template_string(url, context)


def apply_operation_content_type(headers: dict[str, str], operation: dict) -> str:
    """Apply the operation-level `content_type` shortcut to a built header
    map (mutating it) and return the effective Content-Type.

    The shortcut overrides any inherited Content-Type; otherwise an
    inherited header wins; else the historical form default so legacy
    string bodies still get urlencoded. Matched case-insensitively.
    """
    operation_ct = operation.get("content_type")
    if operation_ct:
        for k in list(headers):
            if k.lower() == "content-type":
                del headers[k]
        headers["Content-Type"] = operation_ct
    elif not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    return next(
        (v for k, v in headers.items() if k.lower() == "content-type"),
        "application/x-www-form-urlencoded",
    )


def resolve_body_template(
    raw_template: str, *, content_type: str, context: dict[str, Any], label: str
) -> Any:
    """Resolve the string value of an operation `body.template` into a payload.

    The parse is keyed on `content_type` so the same connector contract works
    for both wire formats the value-expression spec sanctions:

    * `application/json` — parse the JSON *structure* first, then resolve
      placeholders recursively (nested objects/arrays included), so a
      substituted secret containing `"`/`\\` can't break the JSON. Raises
      `ValueError` on an invalid JSON body template.
    * form-encoded (default) — split on literal `&` first, then resolve each
      value, so secrets containing `&` are not truncated.
    """
    if is_json_content_type(content_type):
        try:
            parsed = json.loads(raw_template)
        except json.JSONDecodeError as e:
            raise ValueError(f"{label}: invalid JSON body template: {e}") from e
        return resolve_template_deep(parsed, context)

    payload: dict[str, Any] = {}
    for pair in raw_template.split("&"):
        if not pair:
            continue
        if "=" not in pair:
            LOG.error("%s template: ignoring malformed pair %r", label, pair)
            continue
        k, raw_v = pair.split("=", 1)
        if _has_unresolved_placeholder(raw_v, context):
            # Drop the field rather than emit `k=` with an empty/partial value —
            # mirrors how headers and deep JSON fields drop on unresolved input.
            LOG.warning("%s template: dropping field %r — unresolved placeholder", label, k)
            continue
        payload[k] = resolve_template_string(raw_v, context)
    return payload


def build_effective_headers(
    template: dict,
    context: dict[str, Any],
    *,
    transport: dict | None,
    transport_defaults: dict | None,
) -> dict[str, str]:
    """Build the outgoing header map per the connector schema contract.

    Order (§Header Resolution):
      1. `transport_defaults.headers`
      2. `transports.<transport_ref>.headers`
      3. operation `headers_remove` (drops inherited names only)
      4. operation `headers`

    Header names are matched case-insensitively for override and removal. The
    casing of the latest merge layer to set a name is the casing sent on the
    wire; `headers_remove` does not re-establish casing on its own.
    """
    effective: dict[str, str] = {}
    casing: dict[str, str] = {}

    def _merge(layer: dict | None) -> None:
        for name, value in (layer or {}).items():
            resolved = resolve_header_value(name, value, context)
            if resolved is None:
                continue
            lk = name.lower()
            prior = casing.pop(lk, None)
            if prior is not None and prior != name:
                effective.pop(prior, None)
            effective[name] = resolved
            casing[lk] = name

    _merge((transport_defaults or {}).get("headers"))
    _merge((transport or {}).get("headers"))
    for name in template.get("headers_remove") or []:
        if not isinstance(name, str):
            LOG.warning(
                "Ignoring non-string headers_remove entry of type %s",
                type(name).__name__,
            )
            continue
        prior = casing.pop(name.lower(), None)
        if prior is not None:
            effective.pop(prior, None)
    _merge(template.get("headers"))
    return effective
