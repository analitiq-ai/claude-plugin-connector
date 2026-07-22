# `destinations` block

`stream.destinations[]` is a non-empty array of:

<!-- BEGIN GENERATED: fields-stream-destination -->
`analitiq.contracts.stream.StreamDestination` — closed (`additionalProperties: false`); required: `endpoint_ref`, `write`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `endpoint_ref` | **yes** | ConnectorEndpointRef \| ConnectionEndpointRef (by `scope`) | — | — |
| `write` | **yes** | Write | — | — |
| `execution` | no | Execution \| null | `None` | — |

Carries 4 declarative cross-field `if`/`then` rule(s) — see the advisory rules for their prose.
<!-- END GENERATED: fields-stream-destination -->

The sketch below illustrates a filled-in destination.

```jsonc
{
  "destinations": [
    {
      "endpoint_ref": { /* see spec-endpoint-refs.md */ },
      "write": {
        "mode": "upsert",
        "conflict_keys": ["id"]
      },
      "execution": {
        "batch_size": 1000,
        "max_concurrent_batches": 3
      }
    }
  ]
}
```

## Uniqueness and repeated connections

Destinations must be distinct by their endpoint ref — `ADV-STRM-001` (see
`SKILL.md` § Cross-field rules) states the tuple and the contract model enforces
it. The emitted JSON Schema carries no `uniqueItems` keyword for `destinations`,
so a schema-only reading looks permissive; it is not. Duplicates fail validation.

Because uniqueness is over the whole ref and not over `connection_id`, the **same
destination connection may legitimately appear in several destination entries**
as long as the endpoint differs — fanning one stream into two tables of the same
warehouse is a normal shape, not a duplicate.

## `write`

<!-- BEGIN GENERATED: fields-stream-write -->
`analitiq.contracts.stream.Write` — closed (`additionalProperties: false`); required: `mode`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `mode` | **yes** | string | — | `minLength=1` |
| `conflict_keys` | no | array of string \| null | `None` | `minItems=1`, `item minLength=1` |
<!-- END GENERATED: fields-stream-write -->

### `write.mode`

| Destination kind | allowed values |
|---|---|
| database (scope=connection) | the closed database write-mode set — see `ADV-STRM-013` |
| API (scope=connector) | one of the endpoint's `operations.write` keys (e.g. `create`, `update`, `upsert`) — taken verbatim |

`ADV-STRM-013` is what makes `write.mode` scope-sensitive: the field's type is an
open string because an API mode is endpoint-declared, but a database destination
is narrowed to the closed set. The orchestrator's `WriteModeMapper` (see
`../pipeline-builder/references/enum-mappers.md`) classifies the user's intent to
one of these.

### `write.conflict_keys`

`ADV-STRM-011` governs when this field is required and when it is forbidden; the
`StreamDestination` model can enforce it because the destination's ref tells it
the scope. It is a **single composite key set** of destination field names — not
a list of alternative key sets:

```jsonc
["id"]                       // or ["org_id", "external_id"] for a composite key
```

Every key field must exist in the destination endpoint's schema; that is resolved
server-side at save time, not by the local validator.

## `execution` (per-destination override)

<!-- BEGIN GENERATED: fields-stream-execution -->
`analitiq.contracts.stream.Execution` — closed (`additionalProperties: false`); required: none

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `batch_size` | no | integer \| null | `None` | `min=1`, `max=100000` |
| `max_concurrent_batches` | no | integer \| null | `None` | `min=1`, `max=100` |
<!-- END GENERATED: fields-stream-execution -->

`execution` is one of **three** places batching is decided, and each has a
different owner:

| Layer | Field | Owner | Meaning |
|---|---|---|---|
| Pipeline default | `pipeline.runtime.batching` | the pipeline | the baseline for every binding |
| Stream override | destination `execution` | this stream | overrides the default for *this* `(stream, destination)` binding only |
| Provider capacity | destination endpoint `operations.write.batching` | the endpoint | how much the provider will accept in one request |

Resolution runs in that order — pipeline defaults, then the stream override, then
endpoint and runtime hard limits capping whatever the override produced. The
endpoint's `batching` is not a default and not an override: it is a ceiling
describing the provider, and no stream may raise it.

Use `execution` sparingly — pipeline defaults exist for a reason. Typical use: a
low-throughput destination next to a high-throughput one in the same
`destinations[]`.

What the two knobs mean per destination kind:

- **API destination** — the endpoint's write `batching.max_records` caps how many
  records may ride in one provider request.
- **Database destination** — `execution.batch_size` is the write chunk size once
  defaults have resolved.

If a write operation declares no `batching` at all, the runtime treats it as
single-record writes. That is a real throughput cliff on an API destination:
absent batching does not mean "unbounded", it means one record per request.
