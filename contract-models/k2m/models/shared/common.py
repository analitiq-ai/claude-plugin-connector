"""
Shared constants, helpers, and base models for authored Pydantic schemas.

All five authored resources (connector, connection, pipeline, stream,
endpoint) share the same alias/semver grammar, the same display-name and
tags rules, and the same authoring-error classification. Keeping the
implementation here removes drift risk: a change to the alias or tag policy
lands in one place.

Each resource module supplies its own `SERVER_MANAGED_FIELDS` set (because
the field names differ per resource) and uses `make_authored_top_level_check`
and `make_strip_managed_fields` to bind it.

Spec cross-references:
- the published Analitiq schema documentation
- the published Analitiq schema documentation
- the published Analitiq schema documentation (contracts are CLOSED —
  no `x-*` keys allowed at any authored object level)
"""
from __future__ import annotations

import os
import re
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

# --- Schema URL base --------------------------------------------------------
#
# `DOMAIN` is the canonical environment variable Terraform stamps into every
# Lambda (see `base_env_vars` in the deploy configuration) and that local /
# CI rendering pipelines source from the deploy configuration. Required — no
# fallback; an unset `DOMAIN` is a deploy / dev-env misconfiguration that
# should fail loud at import time rather than silently pin per-resource
# `Literal[$schema]` fields to a wrong domain.
SCHEMA_DOMAIN = os.environ["DOMAIN"]
SCHEMA_BASE_URL = f"https://schemas.{SCHEMA_DOMAIN}"


def schema_url_for(resource: str) -> str:
    """Build the published schema URL for a resource (e.g. `pipeline`, `api-endpoint`)."""
    return f"{SCHEMA_BASE_URL}/{resource}/latest.json"


def schema_url_pattern(resource: str) -> str:
    """Regex matching the published schema URL for `resource` on any environment host.

    The `$schema` a document advertises is an informational pointer that must
    stay valid across environments. A connector authored against the canonical
    `https://schemas.analitiq.ai/<resource>/latest.json` URL is copied verbatim
    into per-run bundles and validated by the engine against the *environment's*
    schema (served at `schemas.analitiq.work` / `.dev` / `.ai`). Pinning the
    field to a single host (a `Literal`) makes a `.ai`-authored document fail
    validation against the `.work`/`.dev` schema, so the published contract
    accepts any `schemas.analitiq.<tld>` host instead.
    """
    return rf"^https://schemas\.analitiq\.[a-z]+/{re.escape(resource)}/latest\.json$"


# --- Patterns ----------------------------------------------------------------

ALIAS_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"

# Spec: the published Analitiq schema documentation §Semver.
SEMVER_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

CRON_PATTERN = r"^cron\(.+\)$"


# --- Length constants -------------------------------------------------------

DISPLAY_NAME_MIN = 1
DISPLAY_NAME_MAX = 120
DESCRIPTION_MAX = 2000
TAG_MIN_LEN = 1
TAG_MAX_LEN = 64
TAGS_MAX = 50


# Anchored "no leading/trailing whitespace" — the declarative mirror of
# `validate_display_name` / `validate_tags` for schemas that must carry the
# constraint on the wire (request contracts, where the FE validates a payload
# BEFORE sending and the contract must never approve what the gate rejects).
# `[\s\S]` instead of `.` so interior newlines don't defeat the anchors.
# The mirror is exact for all realistically-authorable input; ECMA `\s` and
# Python's `str.strip()` disagree only on U+001C-001F/U+0085 (FE-side
# under-reject; the imperative gate validators stay authoritative) and
# U+FEFF (benign FE-side over-reject).
NO_EDGE_WHITESPACE_PATTERN = r"^\S(?:[\s\S]*\S)?$"


# --- Annotated newtypes -----------------------------------------------------

Tag = Annotated[
    str, StringConstraints(min_length=TAG_MIN_LEN, max_length=TAG_MAX_LEN)
]

# Tag for request-contract surfaces: same bounds as `Tag` plus the trim
# constraint declared so it renders into the JSON contract. Read models keep
# plain `Tag` (their values already passed the write gate).
TrimmedTag = Annotated[
    str,
    StringConstraints(
        min_length=TAG_MIN_LEN,
        max_length=TAG_MAX_LEN,
        pattern=NO_EDGE_WHITESPACE_PATTERN,
    ),
]

# For cross-artifact references whose shape is owned by the runtime (pipeline
# stream/destination IDs, stream connection refs, etc.). Rejects empty and
# whitespace-only strings; engines resolve the actual shape at runtime.
NonEmptyStr = Annotated[
    str, StringConstraints(min_length=1, pattern=r"\S")
]


# --- Field-shape validators -------------------------------------------------

def validate_display_name(v: str | None) -> str | None:
    """Reject leading/trailing whitespace beyond the declarative length checks."""
    if v is None:
        return v
    if v != v.strip():
        raise ValueError("display_name must not have leading or trailing whitespace")
    return v


def validate_tags(v: list[str] | None) -> list[str] | None:
    """Reject whitespace-padded tags and duplicates; per-item length is declarative."""
    if v is None:
        return v
    seen: set[str] = set()
    duplicates: list[str] = []
    for tag in v:
        if tag != tag.strip():
            raise ValueError("tags must not have leading or trailing whitespace")
        if tag in seen:
            duplicates.append(tag)
        seen.add(tag)
    if duplicates:
        raise ValueError(
            f"tags must not contain duplicates: {sorted(set(duplicates))!r}"
        )
    return v


# --- Unknown-key classification --------------------------------------------
#
# Authored payloads must reject unknown keys outright. Pydantic's
# `extra="forbid"` emits one error per unknown key with no special handling
# for server-managed fields, so we run our own pre-check on dict input to
# aggregate "server-managed sent by client" + "unknown field" violations
# into a single ValueError with a clear message pointing at the spec doc.

def declared_field_names(cls: type[BaseModel]) -> set[str]:
    names: set[str] = set(cls.model_fields.keys())
    for info in cls.model_fields.values():
        if info.alias:
            names.add(info.alias)
    return names


def make_authored_top_level_check(
    server_managed: frozenset[str], spec_doc: str
) -> Callable[[type[BaseModel], Any], Any]:
    """Build a top-level authored-payload check for a resource.

    The returned function aggregates two classes of authoring error
    (server-managed-fields sent by client, unknown top-level keys) into a
    single ValueError. Raising early short-circuits Pydantic's own
    `extra="forbid"` so callers see one consolidated message per request.
    """

    def _check(cls: type[BaseModel], data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        msgs: list[str] = []
        sent_managed = sorted(server_managed & data.keys())
        if sent_managed:
            msgs.append(
                f"server-managed fields {sent_managed!r} must not be sent by clients "
                f"(see {spec_doc} §Server-Managed Fields)"
            )

        declared = declared_field_names(cls) | server_managed
        unknown = sorted(str(k) for k in data if k not in declared)
        if unknown:
            msgs.append(f"unknown fields {unknown!r}")

        if msgs:
            raise ValueError("; ".join(msgs))
        return data

    return _check


def make_strip_managed_fields(
    server_managed: frozenset[str],
) -> Callable[[Any], Any]:
    """Build a `strip_server_managed_fields(payload)` helper bound to one resource.

    Lambdas use this on save/update/patch routes where the routing key
    arrives out-of-band (path param) but a client might still resend it in
    the body — typically because the client re-posted the result of a prior
    GET. Stripping before validation lets the natural "GET → modify → save"
    workflow succeed without manual field-pruning by every consumer.

    Non-dict inputs pass through unchanged so callers can chain this with
    other validators that surface their own type-mismatch errors.
    """

    def _strip(payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        return {k: v for k, v in payload.items() if k not in server_managed}

    return _strip


# --- Strict base for authored sub-models -----------------------------------

class StrictModel(BaseModel):
    """Base for authored sub-models. Rejects all unknown keys.

    `x-*` extension keys are NOT allowed; the authored contract is closed.
    Per the published Analitiq schema documentation, provider
    extensions must use first-class fields rather than `x-*` smuggling.
    """

    model_config = ConfigDict(extra="forbid")


# --- Read-contract corrupted-row placeholders --------------------------------


def validation_error_summary(e: ValidationError) -> str:
    """Client-safe one-line summary of a pydantic ValidationError.

    `str(e)` embeds `input_value=...` — the offending row's contents, which
    for connections can include raw secret material persisted by a buggy
    writer (the exact row class that fails read-contract validation). This
    summary keeps loc + msg only, so it is safe for wire `error` fields,
    5xx messages, and CloudWatch logs. Same discipline as
    `k2m.web._validation_error_to_detail`, which omits `input` for the
    400-path detail envelope.
    """
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in e.errors(include_url=False, include_input=False)
    )


class CorruptedPlaceholderBase(BaseModel):
    """Shared base for the read contracts' corrupted-row placeholders.

    Owns the `_corrupted` discriminator, the client-safe `error` reason, and
    the serialization rule: placeholders follow the same absent-never-null
    wire policy as healthy records — an identity field that could not be
    read is OMITTED, not null. This keeps a placeholder's shape identical
    whether it ships top-level (a degraded list row) or nested inside
    another record's sidecar (where the parent's `exclude_none` dump would
    strip nulls anyway). `CorruptedPipelinePlaceholder` overrides `wire()`
    for its legacy null-stuffed shape.

    Resource placeholders subclass this with their identity fields.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    corrupted: Literal[True] = Field(
        alias="_corrupted",
        description="Corrupted-row discriminator — REQUIRED on every placeholder.",
    )
    error: str = Field(..., description="Client-safe reason the row was degraded.")

    def wire(self) -> dict:
        """Placeholder wire payload — unreadable identity fields are omitted."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    @classmethod
    def from_validation_error(cls, e: ValidationError, **identity: Any):
        """Build a placeholder from a read-contract ValidationError.

        Sanitizes the reason via `validation_error_summary` (never `str(e)`)
        and null-coalesces non-string identity values so a degrade path can
        never crash on a row whose id field itself is corrupt.
        """
        safe_identity = {
            k: (v if isinstance(v, str) else None) for k, v in identity.items()
        }
        return cls(
            corrupted=True,
            error=f"read-contract validation failed: {validation_error_summary(e)}",
            **safe_identity,
        )