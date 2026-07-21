"""
Connection contract.

The shape of an authored connection document — what you write, and what
Analitiq validates on ingest. Source of the published `connection/latest.json`.

- `ConnectionAuthored` — base carrying the metadata fields
  (`display_name`, `description`, `connector_id`, `tags`).
- `ConnectionStoredMaps` — mixin carrying the maps a connection is made of:
  `parameters` / `selections` / `discovered` / `secret_refs`.
- `ConnectionInput` — the authored document. Declares exactly what an author
  may write, so anything Analitiq assigns cannot be expressed here, and
  `extra="forbid"` rejects it like any other unknown key.

`connection_id` is optional: an externally-authored connection may supply its
own UUID, and one is assigned when omitted.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from analitiq.contracts.shared.common import (
    DESCRIPTION_MAX,
    DISPLAY_NAME_MAX,
    DISPLAY_NAME_MIN,
    NO_EDGE_WHITESPACE_PATTERN,
    TAGS_MAX,
    TrimmedTag,
    schema_url_for,
    validate_display_name,
    validate_tags,
)
from analitiq.contracts.shared.types import UUID_PATTERN

CONNECTION_SCHEMA_URL = schema_url_for("connection")


# --- Secret-shape detection (heuristic guards on non-secret maps) ---

# Names that look secret in any field that should hold non-secret context
# (`parameters`, `selections`, `discovered`). Spec: §Secret Storage Materialization
# — secret material lives in the secret store and is referenced via `secret_refs`.
_NON_SECRET_FIELD_KEY_RE = re.compile(
    r"(?:^|_)(?:access|refresh|id|bearer|oauth|auth|session|sas|api)_?token$"
    r"|(?:^|_)(?:api|access|secret|signing|private|encryption|account|service_account)_?key$"
    r"|cert_(?:pem|key)$"
    r"|(?:^|_)assertion$"
    r"|(?:^|_)secret(?:_|$)"
    r"|(?:^|_)password(?:_|$)"
    r"|(?:^|_)(?:pwd|pat|pkey|creds)(?:_|$)"
    r"|(?:^|_)passwd(?:_|$)"
    r"|(?:^|_)client_secret(?:_|$)"
    r"|(?:^|_)credentials?(?:_|$)"
    r"|(?:^|_)connection_string(?:_|$)",
    re.IGNORECASE,
)
_NON_SECRET_FIELD_KEY_EXACT = frozenset({
    "secret", "password", "passwd", "pwd", "key", "token", "credential",
    "credentials", "creds", "pat", "pkey", "assertion",
})


def _looks_secret(key: str) -> bool:
    return (
        key.lower() in _NON_SECRET_FIELD_KEY_EXACT
        or _NON_SECRET_FIELD_KEY_RE.search(key) is not None
    )


def _to_case_insensitive_ecma(pattern: str) -> str:
    """Inline case-insensitivity for a JSON-Schema `pattern`.

    ECMA `pattern` (JSON Schema) has no inline `(?i)` flag, so mirror the runtime
    `re.IGNORECASE` by expanding each ASCII letter to a two-character class
    (`s` -> `[Ss]`). The source patterns use letters only inside keyword literals,
    never inside structural constructs, so the transform is lossless.
    """
    return re.sub(
        r"[A-Za-z]", lambda m: f"[{m.group().upper()}{m.group().lower()}]", pattern
    )


# ECMA-262 mirror of `_looks_secret`, derived from the SAME Python sources: the
# unanchored `.search()` regex (case-folded) OR any bare EXACT member (anchored,
# case-folded — the unanchored regex does not match a bare `token`/`key`). The
# pattern lives only in json_schema_extra; pydantic-core (rust regex) never
# compiles it, and the runtime keeps using the compiled Python objects.
# `test_secret_key_ecma_pattern_matches_runtime` cross-checks the derivation
# against `_looks_secret` over a corpus.
#
# Caveat: that guard compiles the pattern with Python `re`, so it verifies the
# DERIVATION logic, not ECMA-262 engine semantics. The two engines differ on `$`
# before a trailing newline (Python matches, ECMA does not), so a pathological
# map key ending in "\n" can be blocked by the runtime yet slip the published
# pattern. The runtime `_looks_secret` gate stays authoritative — a leaked key
# still fails on ingest — so external consumers get best-effort structural
# coverage, not a security boundary. `_to_case_insensitive_ecma` is also lossless
# only because the source patterns use letters solely in keyword literals; a
# future `\w`/`[a-z]` in structural position would need re-checking.
_SECRET_KEY_ECMA_PATTERN: str = "|".join(
    [_to_case_insensitive_ecma(_NON_SECRET_FIELD_KEY_RE.pattern)]
    + [
        rf"^{_to_case_insensitive_ecma(key)}$"
        for key in sorted(_NON_SECRET_FIELD_KEY_EXACT)
    ]
)

# Shared `propertyNames` fragment for the non-secret maps (`parameters` /
# `selections` / `discovered`): every key must NOT look secret.
_NON_SECRET_KEYS_SCHEMA: dict[str, Any] = {
    "propertyNames": {"not": {"pattern": _SECRET_KEY_ECMA_PATTERN}}
}


# secret_refs values must look like a *reference* into a secret store, never raw
# secret material. This PUBLIC set is cloud-free — every scheme resolves in the
# open-source engine without any Analitiq cloud service:
#   env:VAR                                — value from an environment variable
#   file:./path                            — value from a local file (relative)
#   s3://<bucket>/<key>                    — object in S3 / an S3-compatible store
#   arn:aws:secretsmanager:<region>:<acct>:secret:<id>   — the user's own AWS
#   arn:aws:ssm:<region>:<acct>:parameter/<id>           — the user's own AWS
#   ssm:/path/to/parameter                 — the user's own SSM parameter
#   sidecar:<name>                         — entry <name> in the connection's
#                                            local credentials secrets file
#
# Every value carries an EXPLICIT scheme, so a bare token (a pasted raw secret)
# is rejected — secret material never belongs in `secret_refs`. The `sidecar:`
# key part accepts ANY characters, so every credentials-file key is representable
# (credentials keys are unconstrained); the required `sidecar:` prefix keeps it
# distinguishable from raw material and from the vault schemes below.
#
# Analitiq may materialize a reference into a storage-specific pointer on save.
# Those pointer schemes are not part of this contract and are not accepted here.
#
# Note: this regex is structural (does the value look like a known reference
# scheme?), not security-validating. Resolvers MUST canonicalize the path and
# scope-check it before fetching, since the regex does not prevent traversal
# sequences such as `..` inside the path component.
_PUBLIC_SECRET_REF_SCHEMES = (
    r"env:[A-Za-z_][A-Za-z0-9_]*"
    r"|file:[A-Za-z0-9_.][A-Za-z0-9_./\-]*"
    r"|s3://[A-Za-z0-9._\-]+/[A-Za-z0-9_./\-]+"
    r"|arn:aws:secretsmanager:[A-Za-z0-9\-]+:\d+:secret:[A-Za-z0-9/_\-+=.@]+"
    r"|arn:aws:ssm:[A-Za-z0-9\-]+:\d+:parameter/[A-Za-z0-9_./\-]+"
    r"|ssm:/[A-Za-z0-9_./\-]+"
    r"|sidecar:.+"
)
SECRET_REF_VALUE_PATTERN = rf"^(?:{_PUBLIC_SECRET_REF_SCHEMES})$"
SecretRefValue = Annotated[str, StringConstraints(pattern=SECRET_REF_VALUE_PATTERN)]


# --- Authored shape ---


class ConnectionAuthored(BaseModel):
    """Authored connection metadata.

    The storage maps (`parameters` / `selections` / `discovered` /
    `secret_refs`) are part of the authored shape and live on
    `ConnectionStoredMaps`.
    """

    schema_url: Literal[CONNECTION_SCHEMA_URL] | None = Field(
        default=None,
        alias="$schema",
        description="Connection schema URL (optional in API payloads, required for standalone files).",
    )

    display_name: str | None = Field(
        default=None,
        min_length=DISPLAY_NAME_MIN,
        max_length=DISPLAY_NAME_MAX,
        pattern=NO_EDGE_WHITESPACE_PATTERN,
        description="User-facing connection label (1-120 chars, no leading/trailing whitespace).",
    )
    description: str | None = Field(
        default=None, max_length=DESCRIPTION_MAX, description="User-facing summary."
    )

    connector_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Identifier of the connector this connection configures. "
            "Non-empty string; the registry assigns connector identifiers "
            "(UUID or slug — not constrained at the schema level)."
        ),
    )

    tags: list[TrimmedTag] | None = Field(
        default=None,
        max_length=TAGS_MAX,
        json_schema_extra={"uniqueItems": True},
        description="Grouping/search labels (max 50, each 1-64 chars, unique, trimmed).",
    )

    @field_validator("display_name")
    @classmethod
    def _validate_display_name_field(cls, v: str | None) -> str | None:
        return validate_display_name(v)

    @field_validator("tags")
    @classmethod
    def _validate_tags_field(cls, v: list[str] | None) -> list[str] | None:
        return validate_tags(v)


class ConnectionStoredMaps(BaseModel):
    """The maps a connection is made of.

    Mixed into `ConnectionInput`: `parameters` (authored values),
    `selections` (post-auth choices), `discovered` (values the connector
    reports back), and `secret_refs` (pointers to where secrets live).
    """

    parameters: dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra=_NON_SECRET_KEYS_SCHEMA,
        description=(
            "Non-secret submitted values keyed by connection-contract input key. "
            "Per-key value vocabularies (enums, formats, etc.) are authored "
            "on the owning connector's `connection_contract.inputs.<key>` and "
            "enforced server-side on save."
        ),
    )
    selections: dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra=_NON_SECRET_KEYS_SCHEMA,
        description=(
            "Durable user-selected post-auth values keyed by post-auth output key. "
            "Spec: §Post-Auth Outputs."
        ),
    )
    discovered: dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra=_NON_SECRET_KEYS_SCHEMA,
        description=(
            "Durable provider-discovered non-secret values keyed by post-auth "
            "output key. Spec: §Post-Auth Outputs."
        ),
    )
    secret_refs: dict[str, SecretRefValue] = Field(
        default_factory=dict,
        description=(
            "Opaque secret-store references keyed by connection-contract input "
            "or post-auth output key. Every value carries an explicit cloud-free "
            "scheme: `env:<VAR>`, `file:<./path>`, `s3://<bucket>/<key>`, "
            "`arn:aws:secretsmanager:<region>:<acct>:secret:<id>`, "
            "`arn:aws:ssm:<region>:<acct>:parameter/<id>`, `ssm:/<path>`, or "
            "`sidecar:<name>` (entry `<name>` in the connection's local "
            "credentials secrets file). Spec: §Secret Storage Materialization."
        ),
    )

    @model_validator(mode="after")
    def _validate_no_secret_keys(self) -> "ConnectionStoredMaps":
        _validate_non_secret_maps(
            parameters=self.parameters,
            selections=self.selections,
            discovered=self.discovered,
        )
        return self


def _validate_non_secret_maps(
    *,
    parameters: dict[str, Any] | None,
    selections: dict[str, Any] | None,
    discovered: dict[str, Any] | None = None,
) -> None:
    """Enforce secret-shaped-key rules on the non-secret maps.

    Spec: §Secret Storage Materialization — non-secret maps must not contain
    secret material; refs go in `secret_refs`.

    Scope: this check inspects map *keys* only (e.g. rejects `"password"`,
    `"api_key"`). It does NOT scan values, so a raw secret stored under an
    innocuous key (e.g. `{"region": "AKIA..."}`) will pass. Value-shape
    scanning is out of scope for v1.

    Per-key value vocabularies (e.g. `ssl_mode`) are authored on the
    owning connector at `connection_contract.inputs.<key>` (for
    values landing in `parameters` / `secret_refs`) or
    `connection_contract.post_auth_outputs.<key>` (for values landing
    in `selections` / `discovered`), and validated server-side on save —
    not here. Imposing a single canonical enum at
    the connection-contract level would conflict with driver-native
    vocabularies (e.g. MySQL's `PREFERRED` vs libpq's `prefer`).

    Called from `ConnectionStoredMaps._validate_no_secret_keys`.
    """
    for field_name, payload in (
        ("parameters", parameters),
        ("selections", selections),
        ("discovered", discovered),
    ):
        if not payload:
            continue
        leaked = sorted(k for k in payload if _looks_secret(str(k)))
        if leaked:
            raise ValueError(
                f"{field_name} must not contain secret-shaped keys {leaked}; "
                "move them to secret storage and reference via `secret_refs` "
                "(spec: §Secret Storage Materialization)"
            )


class ConnectionInput(ConnectionAuthored, ConnectionStoredMaps):
    """Strict API input variant for POST/PUT bodies.

    Source of the published `connection/latest.json`. It declares exactly what
    an author may write, so anything Analitiq assigns is unrepresentable here
    and `extra="forbid"` rejects it like any other unknown key.

    `connection_id` is an optional authored identifier (RFC-4122 UUID) so
    externally-authored connection definitions can supply their own UUID;
    the service assigns one when the create payload omits it.

    The storage maps inherited from `ConnectionStoredMaps`
    (`parameters` / `selections` / `discovered` / `secret_refs`) are the
    authored shape: each value goes in the bucket that says what it is.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    connection_id: str | None = Field(
        default=None,
        pattern=UUID_PATTERN,
        description=(
            "Connection UUID (RFC-4122). Optional in authored definitions so "
            "external publishers can assign their own identifier; "
            "service-assigned on ingest when omitted."
        ),
    )

