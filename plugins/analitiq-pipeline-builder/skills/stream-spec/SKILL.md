---
name: stream-spec
description: Stream authoring vocabulary — endpoint refs, source filters/replication/pagination, destinations write modes, mapping assignments, validation rules. Loaded by stream-creator only. Not invoked directly by users.
disable-model-invocation: true
---

# stream-spec

This skill is loaded by `stream-creator` when authoring a stream document. The
`$schema` value is a `const` in the contract — copy it from the row below, never
retype it, and never invent a version-pinned variant of it:

<!-- BEGIN GENERATED: schema-urls -->
| Entity | Authored file | `$schema` value |
|---|---|---|
| Pipeline | `pipelines/<slug>/pipeline.json` | `https://schemas.analitiq.ai/pipeline/latest.json` |
| Stream | `pipelines/<slug>/streams/<stream-slug>.json` | `https://schemas.analitiq.ai/stream/latest.json` |
| Connection | `connections/<slug>/connection.json` | `https://schemas.analitiq.ai/connection/latest.json` |
| Database endpoint | `connections/<slug>/definition/endpoints/<endpoint_id>.json` | `https://schemas.analitiq.ai/database-endpoint/latest.json` |
<!-- END GENERATED: schema-urls -->

## Required reading (load on demand)

- `spec-endpoint-refs.md` — scope=connector vs scope=connection rules.
- `spec-source.md` — selected_columns, filters, replication, database_pagination, primary_keys.
- `spec-destinations.md` — write modes, conflict_keys, execution overrides.
- `spec-mapping.md` — assignments shape; what the registry computes.
- `spec-validation-rules.md` — assignment-level validation.
- `spec-filter-operators.md` — DB vs API operator vocabularies.
- At least one of `examples/*.example.json` for the source/destination kind you're authoring.

## What this skill covers

- The stream's top-level shape (below).
- The mapping expression vocabulary: `{op: "get", path}` (the default) plus
  `pipe`/`fn` conversion chains, and `{arrow_type, value}` constants.
  `arrow_type` is a fully-qualified Apache Arrow canonical type string (see
  `spec-mapping.md`).
- The closed source-filter operator vocabularies per endpoint kind.

## Top-level shape

<!-- BEGIN GENERATED: fields-stream -->
`analitiq.contracts.stream.StreamInput` — closed (`additionalProperties: false`); required: `destinations`, `pipeline_id`, `source`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `$schema` | no | const 'https://schemas.analitiq.ai/stream/latest.json' \| null | `None` | — |
| `display_name` | no | string \| null | `None` | `pattern=^\S(?:[\s\S]*\S)?$`, `minLength=1`, `maxLength=120` |
| `description` | no | string \| null | `None` | `maxLength=2000` |
| `pipeline_id` | **yes** | string | — | `pattern=\S`, `minLength=1` |
| `source` | **yes** | StreamSource | — | — |
| `destinations` | **yes** | array of StreamDestination | — | `minItems=1` |
| `mapping` | no | StreamMapping \| null | `None` | — |
| `status` | no | 'draft' \| 'active' \| 'inactive' | `'draft'` | — |
| `tags` | no | array of string \| null | `None` | `maxItems=50`, `item pattern=^\S(?:[\s\S]*\S)?$`, `item minLength=1` |
| `stream_id` | no | string \| null | `None` | `pattern=^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$` |
<!-- END GENERATED: fields-stream -->

The model is closed, so a field the table does not list is not merely ignored —
it is rejected. That includes every server-managed field (`version`, `org_id`,
timestamps): the published model is the **authored** shape, not the persisted
one, so those fields are not authorable at all.

## Closed vocabularies

Each value below is picked from a closed member list; anything outside it is a
validation error, not a pass-through value.

<!-- BEGIN GENERATED: enum-vocabulary -->
| Field | Members | Published as |
|---|---|---|
| `pipeline.status` / `stream.status` | `draft`, `active`, `inactive` | `analitiq.contracts.pipelines.config.PipelineInput.status` |
| `pipeline.schedule.type` | `manual`, `interval`, `cron` | `analitiq.contracts.pipelines.config.Schedule.type` |
| `pipeline.runtime.logging.log_level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `analitiq.contracts.pipelines.config.Logging.log_level` |
| `error_handling.strategy` | `fail`, `dlq`, `skip` | `analitiq.contracts.pipelines.config.ErrorHandling.strategy` |
| `stream…filters[].operator` | `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `is_null`, `is_not_null`, `like`, `ilike`, `contains`, `starts_with`, `ends_with` | `analitiq.contracts.stream.Filter.operator` |
| `stream…validate.rules[].type` | `required`, `not_null`, `min_length`, `max_length`, `pattern`, `range`, `in_list` | `analitiq.contracts.stream.ValidationRule.type` |
| `stream.source.replication.method` | `full_refresh`, `incremental` | discriminated union `analitiq.contracts.stream.Replication` |
| `stream.source.database_pagination.type` | `offset`, `keyset` | discriminated union `analitiq.contracts.stream.DatabasePagination` |
| `…endpoint_ref.scope` | `connector`, `connection` | discriminated union `analitiq.contracts.stream.EndpointRef` |
| `stream.destinations[].write.mode` (database) | `insert`, `upsert` | `ADV-STRM-013` (API modes are endpoint-declared, so the field itself is `str`) |
<!-- END GENERATED: enum-vocabulary -->

`status` is the only execution gate on a stream — there is no parallel boolean
flag, and no member beyond those listed exists (in particular there is no
`error` status to author).

## What this skill does NOT cover

- The full registry-side type vocabulary expansion. Authored mapping
  declares one assignment per destination field; the registry computes
  `source_to_generic` / `generic_to_destination` / hashes.
- Endpoint bodies. The stream **references** endpoints by ref; it does
  not embed them.

## Cross-field rules the contract enforces

These are the relational constraints no single field can express. The validator
emits each one's stable id in the finding message, so a failure like
`[ADV-STRM-011] …` points straight at the rule below.

<!-- BEGIN GENERATED: advisory-stream -->
| Rule | Constraint |
|---|---|
| `ADV-STRM-001` | destinations must be unique by (endpoint_ref.scope, endpoint_ref.connection_id, endpoint_ref.endpoint_id). |
| `ADV-STRM-002` | mapping.assignments[].target.path must be unique within the mapping. |
| `ADV-STRM-003` | A supplied endpoint_id must equal derive_db_endpoint_id(database_object). |
| `ADV-STRM-004` | A unary filter operator (is_null/is_not_null) must omit value; every other operator requires it. |
| `ADV-STRM-005` | A pipe expression must start with a get step and be followed only by fn steps. |
| `ADV-STRM-006` | An arrow field's arrow_type must match its container shape: Object declares properties, List declares items, scalars neither. |
| `ADV-STRM-007` | constant.value's JSON kind must match arrow_type, and the Object/List/scalar container shape rule applies. |
| `ADV-STRM-008` | An assignment value must declare exactly one of expression or constant. |
| `ADV-STRM-009` | A validation rule requires value for value-taking types and omits it for required/not_null. |
| `ADV-STRM-010` | An assignment target's arrow_type must match its container shape: Object declares properties, List declares items, scalars neither. |
| `ADV-STRM-011` | conflict_keys is required for a connection-scope upsert destination and forbidden for a connector-scope or non-upsert destination. |
| `ADV-STRM-012` | A filter operator must belong to the source scope's vocabulary: the database operator set for a connection source, the API operator set for a connector source. |
| `ADV-STRM-013` | A database (connection-scope) destination's write.mode must be one of {insert, upsert}; an API (connector-scope) destination's mode is an endpoint-declared operations.write key. |
<!-- END GENERATED: advisory-stream -->

## Output rules

Every authored document must:

1. Declare `$schema` with the stream URL from the table at the top of this file.
2. Carry every required field from the top-level shape table. Author `stream_id`
   as an RFC-4122 UUID the plugin generates (plugin convention; the contract
   permits omission and the service assigns one on ingest). `pipeline_id`
   carries the parent pipeline's UUID.
3. Use **connection UUIDs** in every `endpoint_ref.connection_id` — they
   must match the `connection_id` of the corresponding connection
   document.
4. Shape each `endpoint_ref` by its `scope` (see `spec-endpoint-refs.md`): a
   `connector` ref carries `endpoint_id` (the connector endpoint key); a
   `connection` ref carries the endpoint's `database_object`, plus the derived
   `endpoint_id` handle when the plugin can compute it.
5. Pass validation (the `pipeline-schema-validator`, entity `stream`) with zero
   error findings.
