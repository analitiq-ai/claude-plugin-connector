# `engine` and `runtime` blocks

Both are optional and every field carries a contract default. Author them only
when the user has a specific reason to deviate.

## `engine`

<!-- BEGIN GENERATED: fields-engine -->
`analitiq.contracts.pipelines.config.Engine` — closed (`additionalProperties: false`); required: none

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `vcpu` | no | number | `1.0` | `min=0.5` |
| `memory` | no | integer | `8192` | `min=1024` |
<!-- END GENERATED: fields-engine -->

The minima are the contract's floor — below them the engine has no workable
baseline. There is **no public maximum** on either field: a deployment may
impose its own ceilings, and stricter minimums than these, so a document that
validates here can still be refused by the deployment it is submitted to. Treat
an unusually large request as a question for the user.

The pipeline schema sizes the run as a whole. It defines **no container
topology and no sidecar allocation**, so never author `vcpu` / `memory` as a
split between containers, and never explain the values to the user in those
terms — how the runtime divides them is not a contract fact.

## `runtime`

<!-- BEGIN GENERATED: fields-runtime -->
`analitiq.contracts.pipelines.config.Runtime` — closed (`additionalProperties: false`); required: none

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `buffer_size` | no | integer | `5000` | `min=100` |
| `logging` | no | Logging | — | — |
| `batching` | no | Batching | — | — |
| `error_handling` | no | ErrorHandling | — | — |
<!-- END GENERATED: fields-runtime -->

`buffer_size` has a floor but, like the `engine` fields, **no public maximum**.

### `batching`

<!-- BEGIN GENERATED: fields-batching -->
`analitiq.contracts.pipelines.config.Batching` — closed (`additionalProperties: false`); required: none

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `batch_size` | no | integer | `100` | `min=1`, `max=100000` |
| `max_concurrent_batches` | no | integer | `3` | `min=1`, `max=100` |
<!-- END GENERATED: fields-batching -->

### `logging`

<!-- BEGIN GENERATED: fields-logging -->
`analitiq.contracts.pipelines.config.Logging` — closed (`additionalProperties: false`); required: none

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `log_level` | no | 'DEBUG' \| 'INFO' \| 'WARNING' \| 'ERROR' \| 'CRITICAL' | `'INFO'` | — |
| `metrics_enabled` | no | boolean | `True` | — |
<!-- END GENERATED: fields-logging -->

### `error_handling`

<!-- BEGIN GENERATED: fields-error-handling -->
`analitiq.contracts.pipelines.config.ErrorHandling` — closed (`additionalProperties: false`); required: none

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `strategy` | no | 'fail' \| 'dlq' \| 'skip' | `'dlq'` | — |
| `max_retries` | no | integer | `3` | `min=0`, `max=5` |
| `retry_delay_seconds` | no | integer \| null | `None` | `min=0` |

Carries 1 declarative cross-field `if`/`then` rule(s) — see the advisory rules for their prose.
<!-- END GENERATED: fields-error-handling -->

`max_retries` and `retry_delay_seconds` are coupled — `ADV-RETRY-001` in the
table in `SKILL.md`. The coupling exists because a delay with no retries is
incoherent, and a retry loop with no wait is a hot loop against a system that
just failed.

Everything downstream of the strategy is **runtime-owned and absent from the
pipeline schema**: where dead-lettered records are stored, retry-attempt
metrics, and how a run's outcome is classified. Do not look for those fields
here, do not invent them, and do not promise the user a DLQ location the
document cannot express.

## Where batching is decided

Three documents have a say, and they are not alternatives:

- **pipeline `runtime.batching`** — the defaults for every stream in this pipeline.
- **stream `destinations[].execution`** — a per-binding override
  (see `stream-spec/spec-destinations.md`).
- **destination endpoint write `batching`** — the provider's declared capacity,
  owned by the connector and never authored here.

Resolution order is: pipeline defaults → stream overrides → endpoint and
runtime hard limits. An explicit stream pagination or destination `execution`
value overrides the pipeline default, and the hard limits then cap whatever
that override resolved to — an override can lower a value below the provider's
capacity but never raise it above.

So the pipeline-level values are defaults, and stream-level overrides win
within those caps. When a user asks for a specific write size on one
destination, change the stream, not the pipeline.

`max_concurrent_batches` is per `(stream, destination)` binding. There is **no
public pipeline-wide aggregate cap** on concurrent batches: aggregate
concurrency scales with the number of active bindings, so size the value with
that multiplication in mind rather than reading it as a pipeline ceiling.
