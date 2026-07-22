"""
Pipeline models and validators (schema v1).

Schedules are authored as a `cron(...)` wrapper with an IANA timezone; the
lifecycle `status` is one of draft/active/inactive. The authored contract is
closed — no `x-*` keys.

Classes:
- `PipelineAuthored` — private base carrying authored fields shared by every variant.
- `PipelineInput` — the authored document, and the source of the published
  `pipeline/latest.json`.
- `PipelinePatch` — partial-update body for PATCH routes.
"""
from __future__ import annotations

import re
from typing import Any, Literal
from zoneinfo import available_timezones

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from analitiq.contracts.shared.advisory import AdvisoryValidated
from analitiq.contracts.shared.common import (
    CRON_PATTERN,
    DESCRIPTION_MAX,
    DISPLAY_NAME_MAX,
    DISPLAY_NAME_MIN,
    NO_EDGE_WHITESPACE_PATTERN,
    NonEmptyStr,
    RetryErrorHandlingBase,
    StrictModel,
    TAGS_MAX,
    TrimmedTag,
    schema_url_for,
    validate_display_name,
    validate_tags,
)
from analitiq.contracts.shared.types import UUID_PATTERN
PIPELINE_SCHEMA_URL = schema_url_for("pipeline")




class PipelineConnections(AdvisoryValidated, StrictModel):
    """Connection set available to every stream in the pipeline."""

    source: NonEmptyStr = Field(
        ...,
        description=(
            "Source connection reference. Typically a versioned connection "
            "ID (e.g. 'uuid_v1'); the schema does not enforce a specific "
            "shape — engines resolve the reference at runtime."
        ),
        examples=["00000000-0000-4000-8000-000000000001_v1"],
    )

    destinations: list[NonEmptyStr] = Field(
        ...,
        min_length=1,
        description=(
            "Non-empty list of unique destination connection references. "
            "A destination reference may equal `source`. The schema accepts "
            "any non-empty string; engines resolve references at runtime."
        ),
        examples=[["00000000-0000-4000-8000-000000000002_v1"]],
        json_schema_extra={"uniqueItems": True},
    )


_SCHEDULE_CONDITIONAL_RULES: dict[str, Any] = {
    "allOf": [
        {
            "if": {"properties": {"type": {"const": "manual"}}, "required": ["type"]},
            "then": {
                "not": {
                    "anyOf": [
                        {"required": ["interval_minutes"]},
                        {"required": ["cron_expression"]},
                    ]
                }
            },
        },
        {
            "if": {"properties": {"type": {"const": "interval"}}, "required": ["type"]},
            "then": {
                "required": ["interval_minutes"],
                "not": {"required": ["cron_expression"]},
            },
        },
        {
            "if": {"properties": {"type": {"const": "cron"}}, "required": ["type"]},
            "then": {
                "required": ["cron_expression"],
                "not": {"required": ["interval_minutes"]},
            },
        },
    ],
    "additionalProperties": False,
}


class Schedule(StrictModel):
    """Pipeline schedule."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_SCHEDULE_CONDITIONAL_RULES,
    )

    type: Literal["manual", "interval", "cron"] = Field(
        default="manual", description="Schedule type"
    )
    timezone: str = Field(default="UTC", description="IANA timezone name")
    # Plain `int` (not `CoerceInt`) so Pydantic emits `minimum: 1` on the
    # published JSON Schema. `CoerceInt`'s `BeforeValidator` causes the JSON
    # Schema generator to fall back to `ge: 1`, which JSON Schema 2020-12
    # ignores — leaving external validators with a weaker contract than the
    # runtime. Persisted Decimal coercion isn't needed here because the field
    # is populated from JSON request bodies, where it always arrives as `int`.
    interval_minutes: int | None = Field(
        default=None, ge=1, description="Positive integer minutes (interval schedule only)"
    )
    cron_expression: str | None = Field(
        default=None,
        pattern=CRON_PATTERN,
        description=(
            "Cron wrapper string matching "
            "'^cron\\(.+\\)$' (cron schedule only; e.g. 'cron(0 6 * * ? *)')"
        ),
    )

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        if v not in available_timezones():
            raise ValueError(
                f"Invalid timezone: {v}. Use IANA timezone names "
                "(e.g., 'UTC', 'America/New_York')."
            )
        return v

    @model_validator(mode="after")
    def _validate_schedule_fields(self) -> "Schedule":
        if self.type == "manual":
            if self.interval_minutes is not None or self.cron_expression is not None:
                raise ValueError(
                    "schedule.type='manual' must not include interval_minutes or cron_expression"
                )
        elif self.type == "interval":
            if self.interval_minutes is None:
                raise ValueError(
                    "interval_minutes is required when schedule.type is 'interval'"
                )
            if self.cron_expression is not None:
                raise ValueError(
                    "schedule.type='interval' must not include cron_expression"
                )
        else:  # type == "cron"
            if self.cron_expression is None:
                raise ValueError(
                    "cron_expression is required when schedule.type is 'cron'"
                )
            if self.interval_minutes is not None:
                raise ValueError(
                    "schedule.type='cron' must not include interval_minutes"
                )
        return self


class Engine(StrictModel):
    """Per-run resource request.

    Minimums (`vcpu>=0.5`, `memory>=1024`) reserve the 0.25 vCPU / 512 MB
    sidecar baseline so the engine container always has at least the runtime
    floor (0.25 vCPU / 512 MB) after subtracting the destination container.
    """

    vcpu: float = Field(default=1.0, ge=0.5, description="vCPU allocation")
    memory: int = Field(default=8192, ge=1024, description="Memory allocation in MB")


class Logging(StrictModel):
    """Runtime logging and metrics defaults."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    metrics_enabled: bool = Field(
        default=True, description="Whether runtime metrics emission is enabled"
    )


class Batching(StrictModel):
    """Pipeline-wide record batching defaults."""

    batch_size: int = Field(default=100, ge=1, le=100_000, description="Records per batch")
    max_concurrent_batches: int = Field(
        default=3,
        ge=1,
        le=100,
        description=(
            "Per-binding cap for each (stream, destination) execution binding "
            "(not a pipeline-wide aggregate ceiling)"
        ),
    )


class ErrorHandling(RetryErrorHandlingBase):
    """Runtime error handling. Strategy controls behavior after retries are exhausted."""

    # Field bounds, defaulting, the cross-field rule, and the JSON-Schema
    # conditional rules are inherited from `RetryErrorHandlingBase`, shared with
    # the stream-level `StreamValidationErrorHandling` block so the two cannot
    # drift (#938). The fields below are re-declared only to attach public
    # descriptions (the stream block inherits them without descriptions); kept
    # out of the class docstring so the added prose stays out of the schema.

    strategy: Literal["fail", "dlq", "skip"] = Field(
        default="dlq", description="Action after retries are exhausted"
    )
    max_retries: int = Field(default=3, ge=0, le=5, description="Retry attempts before strategy")
    retry_delay_seconds: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Non-negative integer delay between retry attempts. Optional: when "
            "omitted the effective delay is 5 if max_retries > 0, else 0. Must "
            "be omitted or 0 when max_retries is 0."
        ),
    )


class Runtime(StrictModel):
    """Pipeline-wide execution defaults."""

    buffer_size: int = Field(default=5000, ge=100, description="Record buffer size")
    logging: Logging = Field(default_factory=Logging)
    batching: Batching = Field(default_factory=Batching)
    error_handling: ErrorHandling = Field(default_factory=ErrorHandling)


_VERSIONED_ID_SUFFIX_RE = re.compile(r"_v\d+$")


def _check_streams_unique_base(v: list[str] | None) -> list[str] | None:
    """Reject duplicate stream references.

    For versioned-ID-shaped refs (`<base>_v<digits>`), the dedup key is the
    base (so `<id>_v1` and `<id>_v2` are duplicates). For anything else
    (refs are runtime-resolved, no inherent shape), the dedup key is the
    full string. This avoids false positives on legitimate non-UUID refs
    that happen to contain `_v` followed by non-digits.
    """
    if v is None:
        return v
    seen: set[str] = set()
    duplicates: list[str] = []
    for s in v:
        m = _VERSIONED_ID_SUFFIX_RE.search(s)
        key = s[: m.start()] if m else s
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        raise ValueError(
            f"streams must not include duplicate references "
            f"(versioned IDs collapse to base; other refs compared as-is): "
            f"{sorted(set(duplicates))!r}"
        )
    return v


# Top-level activation rule for the published JSON Schema: an `active` pipeline
# must reference at least one stream. This is the single-document-checkable half
# of the §Cross-Field activation gate (the "≥1 runnable stream" half needs the
# referenced stream documents and stays at the stream-aware gates). Expressed as
# an `if`/`then` so external JSON-Schema validators enforce it identically to the
# `PipelineAuthored` model_validator below.
_ACTIVE_REQUIRES_STREAMS_RULE: dict[str, Any] = {
    "allOf": [
        {
            "if": {
                "properties": {"status": {"const": "active"}},
                "required": ["status"],
            },
            "then": {
                "required": ["streams"],
                "properties": {"streams": {"minItems": 1}},
            },
        },
    ],
}


class PipelineAuthored(BaseModel):
    """The authored pipeline fields.

    """

    schema_url: Literal[PIPELINE_SCHEMA_URL] | None = Field(
        default=None,
        alias="$schema",
        description="Pipeline schema URL (optional in API payloads).",
    )

    display_name: str | None = Field(
        default=None,
        min_length=DISPLAY_NAME_MIN,
        max_length=DISPLAY_NAME_MAX,
        pattern=NO_EDGE_WHITESPACE_PATTERN,
        description="User-facing pipeline label (1-120 chars, no leading/trailing whitespace)",
    )
    description: str | None = Field(
        default=None, max_length=DESCRIPTION_MAX, description="User-facing summary"
    )
    status: Literal["draft", "active", "inactive"] = Field(
        default="draft", description="Pipeline lifecycle status"
    )
    tags: list[TrimmedTag] | None = Field(
        default=None,
        max_length=TAGS_MAX,
        description="Grouping/search labels (max 50, each 1-64 chars, unique, trimmed)",
        json_schema_extra={"uniqueItems": True},
    )

    connections: PipelineConnections = Field(
        ..., description="Source and destination connection references"
    )
    streams: list[NonEmptyStr] = Field(
        default_factory=list,
        description=(
            "Ordered list of stream references. Typically versioned stream "
            "IDs (e.g. 'uuid_v1'); the schema accepts any non-empty string."
        ),
        json_schema_extra={"uniqueItems": True},
    )
    schedule: Schedule = Field(default_factory=Schedule)
    engine: Engine = Field(default_factory=Engine)
    runtime: Runtime = Field(default_factory=Runtime)

    @field_validator("display_name")
    @classmethod
    def _validate_display_name_field(cls, v: str | None) -> str | None:
        return validate_display_name(v)

    @field_validator("tags")
    @classmethod
    def _validate_tags_field(cls, v: list[str] | None) -> list[str] | None:
        return validate_tags(v)

    @field_validator("streams")
    @classmethod
    def _validate_streams_unique_base(cls, v: list[str]) -> list[str]:
        return _check_streams_unique_base(v)

    @model_validator(mode="after")
    def _check_active_requires_streams(self) -> "PipelineAuthored":
        """Enforce the single-document half of the §Cross-Field activation gate.

        `status: "active"` requires at least one stream reference. The companion
        "≥1 runnable stream" half needs the referenced stream documents and is
        enforced at the stream-aware gates (the pipeline API write path and the
        validator's bundle path); it cannot be checked from a lone pipeline
        document. `draft`/`inactive` allow an empty `streams` array.
        """
        if self.status == "active" and not self.streams:
            raise ValueError(
                "status='active' requires at least one stream reference"
            )
        return self


class PipelineInput(PipelineAuthored):
    """Strict API input variant — the source of truth for the `pipeline` published JSON Schema.

    This model declares exactly what an author may write, so anything Analitiq
    assigns is unrepresentable here and `extra="forbid"` rejects it like any
    other unknown key.

    `pipeline_id` is an optional authored identifier (RFC-4122 UUID) so
    externally-authored pipeline definitions can supply their own UUID; the
    service assigns one when the create payload omits it.

    `json_schema_extra` projects the `active ⇒ ≥1 stream reference` rule
    (enforced at runtime by `PipelineAuthored._check_active_requires_streams`)
    into the published JSON Schema so external validators reject the same
    documents the model does.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        json_schema_extra=_ACTIVE_REQUIRES_STREAMS_RULE,
    )

    pipeline_id: str | None = Field(
        default=None,
        pattern=UUID_PATTERN,
        description=(
            "Pipeline UUID (RFC-4122). Optional in authored definitions so "
            "external publishers can assign their own identifier; "
            "service-assigned on ingest when omitted."
        ),
    )

