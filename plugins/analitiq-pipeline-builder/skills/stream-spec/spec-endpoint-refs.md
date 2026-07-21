# `endpoint_ref` shape

The `source` and every `destinations[]` entry carry an `endpoint_ref`. It is a
**discriminated union on `scope`** (`analitiq.contracts.stream.EndpointRef`) —
the two scopes have different shapes and different required fields, tabled below.

## Prefer the ref discovery handed you

When endpoint discovery (or a downloaded connector's endpoint set, or a
`private-endpoint-creator` result) already produced an `endpoint_ref` object,
submit it **as it stands**. Do not re-derive it, re-case it, drop fields you
judge redundant, or "tidy" the object shape. A rewritten ref is the single most
common way a stream stops resolving against the endpoint it was built for.

## `scope: "connector"` — public connector endpoint (API)

<!-- BEGIN GENERATED: fields-connector-endpoint-ref -->
`analitiq.contracts.stream.ConnectorEndpointRef` — closed (`additionalProperties: false`); required: `connection_id`, `endpoint_id`, `scope`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `connection_id` | **yes** | string | — | `pattern=\S`, `minLength=1` |
| `scope` | **yes** | const 'connector' | — | — |
| `endpoint_id` | **yes** | string | — | `minLength=1` |
<!-- END GENERATED: fields-connector-endpoint-ref -->

Refers to a public endpoint baked into the connector document (typically API
endpoints), pinned by the connection's `connector_version` at runtime.
`endpoint_id` matches a key under the connector's `definition/endpoints/*.json`.
A connector-scope ref carries no snapshot hash — the connector version is the
pin.

## `scope: "connection"` — private database endpoint

<!-- BEGIN GENERATED: fields-connection-endpoint-ref -->
`analitiq.contracts.stream.ConnectionEndpointRef` — closed (`additionalProperties: false`); required: `connection_id`, `database_object`, `scope`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `connection_id` | **yes** | string | — | `pattern=\S`, `minLength=1` |
| `scope` | **yes** | const 'connection' | — | — |
| `database_object` | **yes** | DatabaseObject | — | — |
| `endpoint_id` | no | string \| null | `None` | — |
<!-- END GENERATED: fields-connection-endpoint-ref -->

Refers to a private, connection-scoped database endpoint produced by
introspection. `database_object` (`analitiq.contracts.stream.DatabaseObject`)
carries the verbatim database-object identity, the same `{catalog?, schema?,
name}` recorded on the endpoint document — author it from the endpoint doc's
`database_object`, i.e. the `build_database_object(...)` output, so the two
always agree.

Omit `endpoint_id` and the contract derives it; supply it and the contract
verifies it against the derivation (`ADV-STRM-003`). Author it when the plugin
can compute it, so the cross-document bundle check can resolve the reference by
`(connection_id, endpoint_id)`.

<!-- BEGIN GENERATED: endpoint-id-derivation -->
A database `endpoint_id` is **derived**, not chosen: it is a deterministic handle over the endpoint's verbatim locator, computed by `analitiq.contracts.endpoint_identity.derive_db_endpoint_id(catalog, schema, name)`.

| `catalog` | `schema` | `name` | derived `endpoint_id` |
|---|---|---|---|
| — | `public` | `orders` | `public__orders__371c8422` |
| `cat` | `Public` | `Orders` | `public__orders__cat__a688ced5` |

Derivation must stay deterministic: a handle that changes for an unchanged resource mints a new endpoint and breaks every stream pinned to the old one. Never hand-write one — call the helper (`scripts/endpoint_id.py` wraps it).
<!-- END GENERATED: endpoint-id-derivation -->

That derived handle is an **Analitiq slug, not a database object name**. It is
opaque: no consumer may parse schema, table or catalog identity back out of it,
and the presence of recognizable-looking segments is an artifact of the
derivation, not an interface. When something needs the database's own identity —
displaying it, comparing it, driving DDL — read `database_object`, which is why
the ref carries it.

Database endpoints may be referenced by **either side**: a `scope: "connection"`
ref is equally valid on a stream's `source` and on a `destinations[]` entry.
Nothing about a private endpoint restricts it to reading.

`scope: "connection"` is valid only for **database** endpoints. Connection-scoped
API endpoints await an API-endpoint snapshot-hashing spec; `stream-creator`
refuses that combination.

## `connection_id`

The **`connection_id` UUID** of the connection the parent pipeline selected for
that side — `pipeline.connections.source` for the stream source, and one of
`pipeline.connections.destinations[]` for each destination.

## Uniqueness

Destination `endpoint_ref`s must be unique within a single stream — see
`ADV-STRM-001` in `SKILL.md` § Cross-field rules for the exact tuple.

## Cross-document consistency

Validated with `--bundle-root` (the `bundle-*` checks): every source
`endpoint_ref.connection_id` equals `pipeline.connections.source`; every
destination `endpoint_ref.connection_id` is one of
`pipeline.connections.destinations`; and every `scope: "connection"` ref resolves
to a bundled database-endpoint document by `(connection_id, endpoint_id)`.

## Connector-side endpoint verification (`connector-endpoint-ref`)

The published bundle validator resolves `scope: "connection"` refs but leaves
`scope: "connector"` refs unresolved — it receives connector *identity* only, not
connector endpoint *contents*. The plugin closes that gap locally: with
`--bundle-root`, each `scope: "connector"` ref is checked against the referenced
connection's connector endpoint set on disk
(`connectors/<slug>/definition/endpoints/*.json`, resolved
`endpoint_ref.connection_id` → `connection.connector_id` → connector dir).

- If the `endpoint_id` exists in that set → clean.
- If it does not → a **`connector-endpoint-ref` warning** (never an error;
  connectors are trusted registry artifacts pinned by `connector_version` at
  runtime), carrying a closest-match **alignment suggestion** ("Did you mean
  `transfers`?").
- If the connector's endpoint set is not on disk (connector not downloaded) → the
  ref is **skipped**, not warned; an unknown set is never treated as empty.

Because it is a warning, it does not fail validation. The orchestrator surfaces it
and, on the user's confirmation, **aligns** the stream's `endpoint_ref.endpoint_id`
to the connector's real endpoint name. The plugin never edits the connector — only
the stream ref moves. Endpoint refs live only in streams, so alignment is always a
stream edit.
