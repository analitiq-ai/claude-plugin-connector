# `source` block

<!-- BEGIN GENERATED: fields-stream-source -->
`analitiq.contracts.stream.StreamSource` — closed (`additionalProperties: false`); required: `endpoint_ref`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `endpoint_ref` | **yes** | ConnectorEndpointRef \| ConnectionEndpointRef (by `scope`) | — | — |
| `selected_columns` | no | array of string \| null | `None` | — |
| `filters` | no | array of Filter \| null | `None` | — |
| `replication` | no | FullRefreshReplication \| IncrementalReplication (by `method`) \| null | `None` | — |
| `database_pagination` | no | OffsetDatabasePagination \| KeysetDatabasePagination (by `type`) \| null | `None` | — |
| `primary_keys` | no | array of string \| null | `None` | — |
<!-- END GENERATED: fields-stream-source -->

`replication` and `database_pagination` are discriminated unions —
`analitiq.contracts.stream.{FullRefreshReplication, IncrementalReplication}` and
`analitiq.contracts.stream.{OffsetDatabasePagination, KeysetDatabasePagination}`
respectively. The sketch below illustrates a filled-in source; it is not a
statement of what the contract requires.

```jsonc
{
  "source": {
    "endpoint_ref": { /* see spec-endpoint-refs.md */ },
    "selected_columns": ["id", "amount", "updated_at"],
    "filters": [
      {"field": "status", "operator": "eq", "value": "paid"}
    ],
    "replication": {
      "method": "incremental",
      "cursor_field": "updated_at",
      "safety_window_seconds": 300,
      "tie_breaker_fields": ["id"]
    },
    "database_pagination": {
      "type": "offset",
      "page_size": 1000
    },
    "primary_keys": ["id"]
  }
}
```

## Field references are verbatim

`selected_columns`, `filters[].field`, `replication.cursor_field`,
`replication.tie_breaker_fields`, `database_pagination.order_by_field` and
`primary_keys` all reference **source-endpoint field names as discovered**.
Preserve the spelling and casing the endpoint document records — never
case-fold, trim, quote, unquote or otherwise normalize a field reference on the
way into a stream. `Order_ID` and `order_id` are different fields; the contract
compares them literally and so does the engine.

## `selected_columns` (database only)

A field projection. Omit for "all columns from the endpoint schema."
Every entry must reference an existing column in the source endpoint's
`columns[]`. The local validator does **not** resolve column names
against endpoint files — this check happens server-side at save time;
typos surface as a registry rejection rather than a local error.

## `filters`

Stream-owned read predicates; the operator vocabulary is closed and depends on
the source's endpoint scope — see `spec-filter-operators.md`. For an API source
(`scope: connector`) the endpoint narrows it further per parameter through
`operations.read.params.<name>.operators`.

A filter may reference a database column that is **not** in
`selected_columns`: the projection controls what is carried to the destination,
the filter controls which rows are read. Filtering on `updated_at` while
projecting only `id` and `amount` is legitimate and common.

## `replication`

`replication` is the stream's **policy** declaration, and that is all it is.
Ownership across the system:

| Concern | Owner |
|---|---|
| Replication policy for this stream | the stream (`source.replication`) |
| Which methods a source actually supports | the source endpoint / runtime |
| How a cursor maps onto a provider request | the API endpoint's `operations.read.replication.cursor_mappings` |
| The current cursor **value** | runtime state — never the stream document |
| Late-arrival safety window | stream-authored, runtime-applied |
| Tie-breaking when cursor values collide | contract-specific (`tie_breaker_fields`, database sources only) |

Omitting `replication` is allowed **only when the source endpoint supports full
refresh**. Nothing local can check that — the plugin has no endpoint-capability
view at authoring time — so when the source's full-refresh support is not
established, author an explicit `replication` policy rather than relying on the
omission default. A source that cannot full-refresh and carries no policy is
rejected server-side.

`cursor_field` is the **source record field** used as the watermark. It is not a
provider request parameter and not a page-ordering key. Two consequences the
local validator cannot enforce:

- For a database source, `cursor_field` must name a column that exists in the
  source endpoint's schema.
- For an API source, it must match an
  `operations.read.replication.cursor_mappings[].cursor_field` on the endpoint
  exactly — the mapping is what turns the watermark into a request.

Both resolve server-side at save time. Read the field name back to the user
rather than guessing it.

`safety_window_seconds` is a stream-authored overlap that the **runtime** applies
when it resumes from the stored cursor. Authoring it does not store a cursor and
does not move one; the stream never carries a cursor value.

## `database_pagination` (database only)

Pagination governs how a read is **paged**; replication governs where a read
**resumes**. They are independent even when both name the same field: declaring
`order_by_field: "updated_at"` alongside `cursor_field: "updated_at"` is
legitimate, and the two declarations still mean different things — one orders
pages, the other watermarks progress. Never author one expecting it to imply the
other.

`order_by_field` is required for keyset paging (it defines the seek order) and
optional for offset paging. Whichever form is used, it must reference an
existing source column — resolved server-side, not locally.

When `database_pagination` is omitted for a database source, the runtime pages
with offset pagination sized from `pipeline.runtime.batching.batch_size`.

## `primary_keys`

A **fallback** identity hint, for endpoints that carry no primary-key metadata of
their own. When the source endpoint declares primary keys, omit it — declaring
conflicting keys is an error.

For API endpoints this is the only source identity hint there is: an API endpoint
document has no primary-key metadata to inherit, so if the stream needs record
identity (an upsert destination, for example), the stream must supply it here.
