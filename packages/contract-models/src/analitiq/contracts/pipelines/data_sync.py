"""Public Data Sync API contract (`rest.<domain>/v1`).

The request/response shapes an external API-key consumer sees:

    POST /pipelines/{pipeline_id}/run                   -> PipelineRunAcceptedResponse (202)
    POST /pipelines/{pipeline_id}/terminate             -> PipelineTerminateResponse (200)
    GET  /pipelines/{pipeline_id}/runs/{invocation_id}  -> PipelineRunStatusResponse (200)

This module is SHAPE AND RULES ONLY. It knows nothing about how Analitiq runs a
pipeline, stores a run, or names a state internally — and it must not, because
that is precisely what an external consumer must not be pinned to. The
projection from internal run records to these shapes lives server-side and
imports this module, never the reverse.

`success`/`message` are envelope-level and re-declared here rather than derived:
the public contract is self-contained. Absent optional fields are omitted from
the wire, never null.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from analitiq.contracts.shared.common import NonEmptyStr, StrictModel
from analitiq.contracts.shared.types import ISO_TS_PATTERN, CoerceInt, to_iso8601


class PublicRunStatus(str, Enum):
    """Coarse, stable run status for external API-key consumers.

    A deliberate reduction of the internal run lifecycle so the public contract
    stays stable and infra-agnostic: the execution states we run on can change
    without reshaping what an external consumer sees. The last four members are
    terminal."""

    QUEUED = "queued"
    """Accepted; waiting to run (Batch submitted/pending/runnable/starting)."""
    RUNNING = "running"
    """Executing."""
    SUCCEEDED = "succeeded"
    """Completed; all records replicated."""
    PARTIAL = "partial"
    """Completed; some records failed (see record counts)."""
    FAILED = "failed"
    """Did not complete."""
    CANCELLED = "cancelled"
    """Terminated by the user or superseded by a newer run."""

# NB (developer note, deliberately a comment not a docstring so it stays out of
# the published schema description): this enum is NOT asserted against the
# engine's `ErrorCode` at import. The engine's set lives in another repo and is
# an append-only published contract, so a code this contract predates degrades to
# `INTERNAL` where a stored code is read, rather than failing the build. Do not
# import the engine enum to "stay in sync" — that would create a hard cross-repo
# dependency for no gain over the safe soft-degrade.
class PublicErrorCode(str, Enum):
    """Customer-safe failure category. Mirrors the engine's `ErrorCode`
    (analitiq-engine #258). `INTERNAL` is the catch-all for failures with no
    engine-reported category (e.g. a failure before the engine started)."""

    SOURCE_AUTH_FAILED = "SOURCE_AUTH_FAILED"
    SOURCE_UNREACHABLE = "SOURCE_UNREACHABLE"
    DESTINATION_WRITE_FAILED = "DESTINATION_WRITE_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    CONFIG_INVALID = "CONFIG_INVALID"
    INTERNAL = "INTERNAL"


# Fixed, customer-safe message per code. Owned HERE (not read from the stored
# run-log) so the public surface can never echo a raw exception string or the
# engine's internal-only `error_detail`.
PUBLIC_ERROR_MESSAGES: dict[PublicErrorCode, str] = {
    PublicErrorCode.SOURCE_AUTH_FAILED: (
        "Authentication to the data source failed. "
        "Check the source connection credentials."
    ),
    PublicErrorCode.SOURCE_UNREACHABLE: (
        "The data source could not be reached. "
        "Check the source connection settings and network access."
    ),
    PublicErrorCode.DESTINATION_WRITE_FAILED: (
        "Writing to the destination failed. "
        "Check the destination connection and its permissions."
    ),
    PublicErrorCode.RATE_LIMITED: (
        "The run was throttled by a rate limit on the source or destination. "
        "Try again later."
    ),
    PublicErrorCode.CONFIG_INVALID: (
        "The pipeline configuration is invalid. "
        "Review the pipeline, its connections, and streams."
    ),
    PublicErrorCode.INTERNAL: (
        "The run failed due to an internal error. "
        "If this persists, contact Analitiq support."
    ),
}

# Fail loud at import if a code is added without a message.
if set(PUBLIC_ERROR_MESSAGES) != set(PublicErrorCode):
    raise AssertionError("PUBLIC_ERROR_MESSAGES must cover every PublicErrorCode member")


class PublicRunError(StrictModel):
    """Customer-safe failure category for a run. Present only on `failed`
    (always) and `partial` (when the engine reported a category)."""

    code: PublicErrorCode = Field(
        description="Stable failure category. Mirrors the engine's error codes.",
    )
    message: NonEmptyStr = Field(
        description="Fixed, customer-safe description of the failure category.",
    )

class PipelineRunStatusData(StrictModel):
    """`data` payload of GET /pipelines/{pipeline_id}/runs/{invocation_id}.

    A small, stable projection of one `pipeline_run_logs` row. Optional fields
    are dropped from the wire when absent (`exclude_none`) — a consumer treats a
    missing field as "not recorded yet for this run" (an in-flight run has no
    `finished_at` or counts), never as a defect. No internal infrastructure
    detail is exposed.
    """

    # Mirrors `_error_matches_status` in the published JSON Schema: a failed run
    # must carry an `error`, and `error` may only ride a failed or partial run.
    # `extra="forbid"` is re-declared for readability (pydantic merges it from
    # StrictModel either way), matching the `Filter`/`RetryErrorHandlingBase`
    # style of stating strictness alongside a co-located `json_schema_extra`.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"status": {"const": "failed"}},
                        "required": ["status"],
                    },
                    "then": {"required": ["error"]},
                },
                {
                    "if": {"required": ["error"]},
                    "then": {"properties": {"status": {"enum": ["failed", "partial"]}}},
                },
            ]
        },
    )

    invocation_id: NonEmptyStr = Field(
        description="Unique identifier of this run (the id returned by the 202 accept).",
    )
    pipeline_id: NonEmptyStr = Field(
        description="Pipeline the run belongs to.",
    )
    status: PublicRunStatus = Field(
        description="Coarse run status. Terminal: succeeded, partial, failed, cancelled.",
    )
    submitted_at: str = Field(
        pattern=ISO_TS_PATTERN,
        description="ISO-8601 timestamp the run was accepted. Always present.",
    )
    started_at: str | None = Field(
        default=None,
        pattern=ISO_TS_PATTERN,
        description="ISO-8601 timestamp the run began executing.",
    )
    finished_at: str | None = Field(
        default=None,
        pattern=ISO_TS_PATTERN,
        description="ISO-8601 timestamp the run reached a terminal state.",
    )
    records_processed: CoerceInt | None = Field(
        default=None,
        description="Records successfully replicated.",
    )
    records_failed: CoerceInt | None = Field(
        default=None,
        description="Records that failed to replicate.",
    )
    records_total: CoerceInt | None = Field(
        default=None,
        description="Total records read from the source.",
    )
    error: PublicRunError | None = Field(
        default=None,
        description="Customer-safe failure category; present only for failed/partial runs.",
    )

    @field_validator("submitted_at", "started_at", "finished_at", mode="before")
    @classmethod
    def _iso8601(cls, value):
        return to_iso8601(value)

    @model_validator(mode="after")
    def _error_matches_status(self) -> "PipelineRunStatusData":
        """An `error` only rides a failed or partial run, and a failed run always
        carries one. Enforced on the model so the illegal combination is
        unrepresentable for ANY producer, not merely avoided by the current
        one."""
        terminal_failure = self.status in (PublicRunStatus.FAILED, PublicRunStatus.PARTIAL)
        if self.error is not None and not terminal_failure:
            raise ValueError("error may only be set on a failed or partial run")
        if self.status is PublicRunStatus.FAILED and self.error is None:
            raise ValueError("a failed run must carry an error category")
        return self


class PipelineRunAcceptedData(StrictModel):
    """`data` payload of an accepted run (202).

    The public counterpart of `run_accepted.PipelineRunAcceptedData`, which is
    the frontend's contract. Same shape today; owned separately so either
    audience can evolve without breaking the other.
    """

    invocation_id: NonEmptyStr = Field(
        description=(
            "Identifier for this run. Poll the run-status endpoint with it to "
            "follow the run to completion."
        ),
    )
    pipeline_id: NonEmptyStr = Field(
        description="Pipeline the run was accepted for.",
    )


class PipelineTerminateData(StrictModel):
    """`data` payload of a terminate request.

    Present only when the terminate resolved the pipeline it acted on, so
    `pipeline_id` is always set; `job_id` only when a running job was actually
    stopped. "Nothing to terminate" outcomes (no run recorded, run already
    terminal) carry no `data` at all and are distinguished by `message`.

    The public counterpart of `run_action.PipelineTerminateData` (the
    frontend's contract). Note the prose difference that motivates the split:
    that one describes `job_id` in terms of the infrastructure the UI team
    operates; this one describes it in terms an external consumer can act on.
    """

    pipeline_id: NonEmptyStr = Field(
        description="Pipeline the terminate acted on.",
    )
    job_id: NonEmptyStr | None = Field(
        default=None,
        description=(
            "Identifier of the execution that was stopped. Present only when a "
            "running execution was actually terminated."
        ),
    )

class PipelineRunRequest(StrictModel):
    """Request body of `POST /pipelines/{pipeline_id}/run`.

    `pipeline_id` comes from the path and the org from the API key, so
    `terminate_existing_sync` is the only client-supplied field. An empty body
    is valid — the default applies. Unknown keys are rejected (`StrictModel`).
    """

    terminate_existing_sync: bool = Field(
        default=False,
        description=(
            "Terminate an already-running job for this pipeline before "
            "submitting the new run. When false (default) and a run is already "
            "in flight, the request is rejected with 409 Conflict."
        ),
    )


class PipelineRunAcceptedResponse(StrictModel):
    """Success body of `POST /pipelines/{pipeline_id}/run` — the run was accepted (202).

    The run is dispatched asynchronously in a background self-invocation, so
    the response carries only the tracking identifiers (`invocation_id`,
    `pipeline_id`) — never a `job_id` (it does not exist yet). Poll run history by
    `invocation_id` for the final outcome.
    """

    success: Literal[True] = Field(
        description=(
            "Always true on this 2xx body. Error responses use the canonical "
            "`{success: false, message, ...}` error envelope."
        ),
    )
    message: str | None = Field(
        default=None,
        description="Human-readable outcome.",
    )
    data: PipelineRunAcceptedData = Field(
        description=(
            "The accepted run — tracking identifiers only; always present on a "
            "successful accept. Poll run history by `invocation_id`."
        ),
    )


class PipelineTerminateResponse(StrictModel):
    """Success body of `POST /pipelines/{pipeline_id}/terminate`.

    Terminate is idempotent: when a running job is stopped, `data` carries the
    resolved `pipeline_id` (and `job_id`); "nothing to terminate" outcomes (no
    run recorded, run already terminal) omit `data` entirely and are
    distinguished by `message`.
    """

    success: Literal[True] = Field(
        description=(
            "Always true on this 2xx body. Error responses use the canonical "
            "`{success: false, message, ...}` error envelope."
        ),
    )
    message: str | None = Field(
        default=None,
        description="Human-readable outcome.",
    )
    data: PipelineTerminateData | None = Field(
        default=None,
        description=(
            "The resolved terminate — present when a pipeline was acted on; "
            "omitted on idempotent no-op outcomes."
        ),
    )


class PipelineRunStatusResponse(StrictModel):
    """Success body of `GET /pipelines/{pipeline_id}/runs/{invocation_id}` (200).

    The canonical `{success, message?, data}` envelope where `data` is the
    public run-status projection (`PipelineRunStatusData`) — always present on a
    200. Lets an API-key client poll a run's outcome by `invocation_id`.
    """

    success: Literal[True] = Field(
        description=(
            "Always true on this 2xx body. Error responses use the canonical "
            "`{success: false, message, ...}` error envelope."
        ),
    )
    message: str | None = Field(
        default=None,
        description="Human-readable outcome.",
    )
    data: PipelineRunStatusData = Field(
        description="The run's public status — always present on a 200.",
    )

    def body(self) -> dict:
        """Wire payload: absent optionals are omitted, never null."""
        return self.model_dump(mode="json", exclude_none=True)
