---
name: pipeline-spec
description: Pipeline authoring vocabulary — connection refs, schedule, engine, runtime, streams, status. Loaded by pipeline-creator only. Not invoked directly by users.
disable-model-invocation: true
---

# pipeline-spec

This skill is loaded by `pipeline-creator` when authoring a pipeline document.

## Required reading (load on demand)

- `spec-connections.md` — UUID refs for source + destinations.
- `spec-schedule.md` — manual / interval / cron with IANA timezone.
- `spec-engine-runtime.md` — vcpu/memory floor, batching, logging, error_handling.
- `spec-streams-and-status.md` — stream pinning rules and lifecycle gating.
- At least one of `examples/*.example.json` for the schedule style you're authoring.

## `$schema`

<!-- BEGIN GENERATED: schema-urls -->
| Entity | Authored file | `$schema` value |
|---|---|---|
| Pipeline | `pipelines/<slug>/pipeline.json` | `https://schemas.analitiq.ai/pipeline/latest.json` |
| Stream | `pipelines/<slug>/streams/<stream-slug>.json` | `https://schemas.analitiq.ai/stream/latest.json` |
| Connection | `connections/<slug>/connection.json` | `https://schemas.analitiq.ai/connection/latest.json` |
| Database endpoint | `connections/<slug>/definition/endpoints/<endpoint_id>.json` | `https://schemas.analitiq.ai/database-endpoint/latest.json` |
<!-- END GENERATED: schema-urls -->

## What this skill covers

A pipeline document owns exactly six areas — identity and metadata, the
connection set, the stream set, the schedule, engine resources, and runtime
defaults. Everything else a pipeline needs is **referenced**, never inlined.

<!-- BEGIN GENERATED: fields-pipeline -->
`analitiq.contracts.pipelines.config.PipelineInput` — closed (`additionalProperties: false`); required: `connections`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `$schema` | no | const 'https://schemas.analitiq.ai/pipeline/latest.json' \| null | `None` | — |
| `display_name` | no | string \| null | `None` | `pattern=^\S(?:[\s\S]*\S)?$`, `minLength=1`, `maxLength=120` |
| `description` | no | string \| null | `None` | `maxLength=2000` |
| `status` | no | 'draft' \| 'active' \| 'inactive' | `'draft'` | — |
| `tags` | no | array of string \| null | `None` | `maxItems=50`, `item pattern=^\S(?:[\s\S]*\S)?$`, `item minLength=1` |
| `connections` | **yes** | PipelineConnections | — | — |
| `streams` | no | array of string | — | `uniqueItems=True`, `item pattern=\S`, `item minLength=1` |
| `schedule` | no | Schedule | — | — |
| `engine` | no | Engine | — | — |
| `runtime` | no | Runtime | — | — |
| `pipeline_id` | no | string \| null | `None` | `pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$` |

Carries 1 declarative cross-field `if`/`then` rule(s) — see the advisory rules for their prose.
<!-- END GENERATED: fields-pipeline -->

Every field above with a default may be omitted; omitting it and authoring the
default are equivalent. Author a value only when the user asked for one.

## What this skill does NOT cover

- Stream bodies — see `stream-spec`.
- Connection bodies — see `connection-spec`.
- Database endpoint bodies — see `endpoint-spec`.
- Connector bodies — that's the `analitiq-connector-builder` plugin.

A pipeline document is also **not an import bundle**. The contract defines no
packaging that ships a pipeline together with stream or connection fixtures, so
never nest another entity's body inside `pipeline.json` to make it
self-contained — author each document as its own file and reference it by id.

## Cross-field rules the contract enforces

These are the relational constraints no single field can express. The validator
emits each one's stable id in the finding message, so a failure like
`[ADV-PIPE-002] …` points straight at the rule below.

<!-- BEGIN GENERATED: advisory-pipeline -->
| Rule | Constraint |
|---|---|
| `ADV-PIPE-001` | connections.destinations must not contain duplicate connection IDs. |
| `ADV-PIPE-002` | schedule.type gates its fields: manual forbids interval/cron, interval requires interval_minutes, cron requires cron_expression. |
| `ADV-PIPE-003` | streams must be unique by version-stripped base id. |
| `ADV-PIPE-004` | An active pipeline must reference at least one stream. |
| `ADV-RETRY-001` | retry_delay_seconds must be omitted or 0 when max_retries is 0. |
<!-- END GENERATED: advisory-pipeline -->

## Output rules

Every authored document must:

1. Declare `$schema` with the pipeline URL above. The contract makes it
   optional; the plugin always writes it so the file is self-describing.
2. Include a non-empty `connections` object — see `spec-connections.md`.
   Author `pipeline_id` as a UUID the plugin generates (plugin convention; the
   contract permits omission and the service assigns one on ingest). The
   directory name (`pipelines/<slug>/`) stays human-readable and is independent
   of that UUID.
3. Use **connection UUIDs** in `connections.source` and
   `connections.destinations[]`, and **stream UUIDs** in `streams[]` — set to
   the `connection_id` / `stream_id` of the corresponding
   `connections/<slug>/connection.json` and `streams/<slug>.json` files. That
   pairing is plugin convention: the contract constrains only non-emptiness,
   and the bundle referential checks verify the wiring with `--bundle-root`.
4. Pass validation (the `pipeline-schema-validator`, entity `pipeline`) with
   zero error findings.
