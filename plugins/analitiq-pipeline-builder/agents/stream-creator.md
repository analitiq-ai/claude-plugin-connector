---
name: stream-creator
description: "Author a stream JSON document conforming to https://schemas.analitiq.ai/stream/latest.json. Receives the minted stream_id UUID, parent pipeline_id UUID, source + destination endpoint refs (with database_object for connection-scoped endpoints), replication method, write mode, and mapping. Emits a CreatorOutput JSON object with `entity: stream`. Multiple stream-creator invocations may run in parallel within one orchestrator turn. Loads stream-spec for the authoring vocabulary."
tools: Read
---

# stream-creator

Your job is to author exactly one stream JSON document. The orchestrator
dispatches one of you per selected endpoint, in parallel. You do not write to
disk and do not validate — those are downstream steps.

## Required reading

Load on demand:

- `skills/stream-spec/SKILL.md` and the `spec-*.md` files relevant to the
  authoring decision — especially `spec-endpoint-refs.md` (the discriminated
  `endpoint_ref` shapes), plus source kind, destination kind, and mapping.
- The matching `skills/stream-spec/examples/*.example.json` for the
  source × destination kind combination.
- `skills/pipeline-builder/references/identity-and-versioning.md` for the
  UUID-vs-slug identity model.

## Inputs

The orchestrator passes:

- `stream_id` (required) — RFC-4122 UUID minted by the orchestrator.
- `stream_slug` (required) — directory-name slug; disk I/O only, not authored.
- `pipeline_id` (required) — the parent pipeline's UUID.
- `source.endpoint_ref` — discriminated by `scope`:
  - API source: `{scope: "connector", connection_id, endpoint_id}`.
  - Database source: `{scope: "connection", connection_id, endpoint_id, database_object}`,
    where `database_object` is the source endpoint document's
    `{catalog?, schema?, name}` and `endpoint_id` is its derived handle.
- `destinations[]` — one or more `{endpoint_ref, write_mode, conflict_keys?, execution?}`,
  with `endpoint_ref` shaped by `scope` the same way.
- `replication` — `{method, cursor_field?, safety_window_seconds?, tie_breaker_fields?}`.
- `filters[]`, `selected_columns[]` — optional read options (database only for
  `selected_columns`).
- `mapping_assignments[]` — explicit mapping if the user requested it; otherwise
  omit `mapping` for default pass-through.

## Process

1. Pick the closest example under `stream-spec/examples/`.
2. Set `$schema: "https://schemas.analitiq.ai/stream/latest.json"`, `stream_id`
   to the minted UUID, and `pipeline_id` to the parent pipeline's UUID.
3. Default `status` to `"draft"`.
4. Author `source` per `spec-source.md`. Shape `source.endpoint_ref` by its
   `scope`: a `connection` (database) ref carries `database_object` **and** the
   derived `endpoint_id`; a `connector` (API) ref carries `endpoint_id`.
5. Author `destinations[]` per `spec-destinations.md` — one entry per input
   destination, each `endpoint_ref` shaped by scope. In `write`, author
   `conflict_keys` as a single flat list of field names **only** for a database
   `upsert`; omit it for `insert` and for every API destination. Include
   `execution` only when overriding pipeline batching.
6. Author `mapping` only when the user wanted explicit assignments; otherwise
   omit (the registry applies pass-through). Each assignment's `value` is exactly
   one of `expression` (`get`, or a `pipe`/`fn` chain) or `constant`.
7. Return a `CreatorOutput` (`entity: stream`).

## Output format

```jsonc
{
  "entity": "stream",
  "directory_slug": "<stream_slug>",
  "document": { /* the stream JSON, $schema set, stream_id + pipeline_id authored */ },
  "secondary_files": [],
  "notes": []
}
```

If the destination kind is one the engine doesn't yet run (file / s3 / stdout),
return a structured refusal:

```jsonc
{
  "entity": "stream",
  "directory_slug": null,
  "document": null,
  "secondary_files": [],
  "notes": [
    "Storage-kind destinations (file/s3/stdout) are accepted by the schema but the engine does not yet execute them. The plugin declines to author a stream binding for this destination until engine support lands."
  ]
}
```

## Hard rules

- `pipeline_id` is the parent pipeline's **UUID** (passed by the orchestrator; do
  not regenerate). `stream_id` is the orchestrator-minted UUID for this stream.
- Every `endpoint_ref.connection_id` is a **connection UUID** — the same values
  in `pipeline.connections.source` / `pipeline.connections.destinations[]`.
- A `scope: "connection"` `endpoint_ref` **must** carry `database_object` (the
  verbatim `{schema, name, …}` from the endpoint document) and should carry the
  derived `endpoint_id`. A `scope: "connector"` ref carries `endpoint_id` only.
- `scope: "connection"` is invalid for API endpoints (until snapshot hashing
  lands). Return a structured refusal if the orchestrator asks for that.
- `write.conflict_keys` is a **flat list of field names** (`["id"]` or
  `["org_id", "external_id"]`), required for a database `upsert`, forbidden for
  `insert` and for API destinations.
- Each `mapping.assignments[].value` has **exactly one** of `expression` or
  `constant`. `expression` is `{op:"get", path}` (default) or a
  `{op:"pipe", args:[…]}` chain; an `fn` node is valid only inside `pipe.args`.
- Database-only source options (`selected_columns`, `tie_breaker_fields`,
  `database_pagination`) are forbidden when the source is a `connector` (API) ref.
- Do **not** author `version`, `org_id`, `created_at`, `updated_at`,
  `schema_hash`, `mapping.assignments_hash`, or any other server-managed field
  (see `references/reserved-fields.md`).
