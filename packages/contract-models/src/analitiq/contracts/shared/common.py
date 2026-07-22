"""
Shared constants, helpers, and base models for authored Pydantic schemas.

All five authored resources (connector, connection, pipeline, stream,
endpoint) share the same slug/semver grammar and the same display-name and
tags rules. Keeping the implementation here removes drift risk: a change to
the id, display-name, or tag policy lands in one place.

Authored contracts are CLOSED: no `x-*` keys are allowed at any authored
object level. Each model declares exactly what an author may write, so
anything Analitiq assigns is not merely rejected — it cannot be expressed.
"""
from __future__ import annotations

import os
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

# --- Schema URL base --------------------------------------------------------
#
# `DOMAIN` is the canonical environment variable the deploy stamps into every
# runtime, and that local / CI rendering sources from the deploy
# configuration. Required — no
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


# Identifier slug shared by `connector_id` and `endpoint_id`: lowercase
# alphanumeric, `_`/`-` inside, must not start with a separator. One grammar,
# so a change to the id policy lands in one place.
SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"

# SemVer 2.0.0, including optional prerelease and build-metadata parts.
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


# --- Strict base for authored sub-models -----------------------------------

class StrictModel(BaseModel):
    """Base for authored sub-models. Rejects all unknown keys.

    `x-*` extension keys are NOT allowed; the authored contract is closed.
    Provider extensions must use first-class fields rather than `x-*`
    smuggling.
    """

    model_config = ConfigDict(extra="forbid")


# --- Shared retry/error-handling behavior ----------------------------------

# JSON-Schema conditional rule shared by the pipeline and stream error-handling
# blocks: when `max_retries == 0`, `retry_delay_seconds` must be 0 or null.
# Defined once so the two blocks' published schemas cannot drift.
_RETRY_ERROR_HANDLING_CONDITIONAL_RULES: dict[str, Any] = {
    "allOf": [
        {
            "if": {
                "properties": {"max_retries": {"const": 0}},
                "required": ["max_retries"],
            },
            "then": {
                "properties": {
                    "retry_delay_seconds": {"oneOf": [{"const": 0}, {"type": "null"}]}
                }
            },
        },
    ],
    "additionalProperties": False,
}


class RetryErrorHandlingBase(StrictModel):
    """Shared error-handling contract for the pipeline and stream blocks.

    Owns the whole `max_retries` / `retry_delay_seconds` retry contract — the
    field bounds, the defaulting behavior, the cross-field rule, and the shared
    JSON-Schema conditional rules — so the two blocks cannot drift. A subclass
    re-declares a field only to attach a public description (the pipeline block
    documents its fields; the stream block does not); the stream block inherits
    the fields verbatim.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_RETRY_ERROR_HANDLING_CONDITIONAL_RULES,
    )

    strategy: Literal["fail", "dlq", "skip"] = Field(default="dlq")
    max_retries: int = Field(default=3, ge=0, le=5)
    retry_delay_seconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_retry_fields(self) -> "RetryErrorHandlingBase":
        if self.max_retries == 0 and self.retry_delay_seconds not in (None, 0):
            raise ValueError(
                "retry_delay_seconds must be omitted or 0 when max_retries is 0"
            )
        return self

    @model_validator(mode="after")
    def _default_retry_delay(self) -> "RetryErrorHandlingBase":
        # Fill the effective default here, never by mutating the input dict in a
        # `mode="before"` validator: that marks the key as provided, corrupting
        # the one signal consumers use to tell author-set from defaulted (#938).
        # `retry_delay_seconds is None` means the author omitted it (or sent
        # null); after assigning, discard it from the field-set — pydantic's
        # `__setattr__` records the assignment, and the injected default must
        # not read as author input. The `0 if max_retries == 0 else 5` value is
        # consistent with the cross-field rule above regardless of validator
        # order (an author-supplied delay under `max_retries == 0` is rejected
        # there; the only value ever injected for that case is 0).
        if self.retry_delay_seconds is None:
            self.retry_delay_seconds = 0 if self.max_retries == 0 else 5
            self.__pydantic_fields_set__.discard("retry_delay_seconds")
        return self


# --- Read-contract corrupted-row placeholders --------------------------------


def validation_error_summary(e: ValidationError) -> str:
    """Client-safe one-line summary of a pydantic ValidationError.

    `str(e)` embeds `input_value=...` — the offending row's contents, which
    for connections can include raw secret material persisted by a buggy
    writer (the exact row class that fails read-contract validation). This
    summary keeps loc + msg only, so it is safe for wire `error` fields,
    5xx messages, and server logs.
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