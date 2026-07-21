# `database_object` block

<!-- BEGIN GENERATED: fields-database-object -->
`analitiq.contracts.endpoints.DatabaseObject` — closed (`additionalProperties: false`); required: `name`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `catalog` | no | string \| null | `None` | `minLength=1` |
| `schema` | no | string \| null | `None` | `minLength=1` |
| `name` | **yes** | string | — | `minLength=1` |
| `object_type` | no | string \| null | `None` | — |
<!-- END GENERATED: fields-database-object -->

Which provider concept lands in `catalog` and `schema` is dialect-specific — see
the tables below.

## Identifier preservation

**All identifier strings are stored verbatim from introspection** — no
case-folding, quoting, or normalization. PostgreSQL is case-sensitive
when quoted, BigQuery names are case-sensitive in the catalog API,
and MongoDB collection names are case-sensitive throughout. Whatever
the source database reports, that's what goes here.

The hashing layer (`schema_hash`, server-managed) relies on this
verbatim preservation. Normalizing breaks the hash and triggers false
drift.

## Derived `endpoint_id`

`database_object` and the endpoint's `endpoint_id` are two views of one identity.

<!-- BEGIN GENERATED: endpoint-id-derivation -->
A database `endpoint_id` is **derived**, not chosen: it is a deterministic handle over the endpoint's verbatim locator, computed by `analitiq.contracts.endpoint_identity.derive_db_endpoint_id(catalog, schema, name)`.

| `catalog` | `schema` | `name` | derived `endpoint_id` |
|---|---|---|---|
| — | `public` | `orders` | `public__orders__371c8422` |
| `cat` | `Public` | `Orders` | `public__orders__cat__a688ced5` |

Derivation must stay deterministic: a handle that changes for an unchanged resource mints a new endpoint and breaks every stream pinned to the old one. Never hand-write one — call the helper (`scripts/endpoint_id.py` wraps it).
<!-- END GENERATED: endpoint-id-derivation -->

The hash is taken over the **verbatim** identifiers, so compute both together
with `scripts/endpoint_id.py` (see `private-endpoint-creator`) and they always
agree with what the validator recomputes.

`endpoint_id` is an Analitiq **slug**, not a database object name. Nothing may
parse database identity back out of it: the segments are slugified (lossy) and
the trailing hash is not reversible, so a consumer that splits the handle to
recover a schema or table name will be wrong the moment an identifier contains a
character slugging folds away. Anything needing the catalog / schema / name
reads `database_object` — that is what the block is for.

The trailing `<hash8>` is **not** `schema_hash`, and the two must never be
substituted for one another. `<hash8>` identifies the *object*: it is taken over
`catalog.schema.name` and does not move while the object keeps its name, whatever
happens to its columns. `schema_hash` (server-managed) versions a *captured
snapshot* of that object's shape and changes whenever a column does. One answers
"which object is this", the other "which capture of it".

## `name`

The provider-native object identifier. No quoting or escaping — the value is the
raw identifier as it appears in the catalog.

## `catalog`

The outermost containment level, when the dialect has one:

| Dialect | what goes in `catalog` |
|---|---|
| BigQuery | project ID (`analytics-prod`) |
| Snowflake | database name (`PROD_DB`) |
| SQL Server | database name (`master`) |
| Trino / Presto | catalog name (`hive`) |
| MongoDB | database name (`analytics`) |
| PostgreSQL | database name (often omitted; the connection already identifies it) |

## `schema`

The intermediate namespace:

| Dialect | what goes in `schema` |
|---|---|
| PostgreSQL | schema (`public`) |
| Snowflake / SQL Server | schema (`DBO`) |
| BigQuery | dataset (`warehouse`) |
| MongoDB | (omitted; collections live directly in the database) |

## `object_type`

Descriptive — **not** a closed enum. Common values:
`table`, `view`, `materialized_view`, `collection`, `external_table`,
`stream`. The catalog stores this verbatim; downstream tools may inspect
it for behavioral hints (e.g., "don't write to a view").

## Uniqueness

The tuple `(catalog, schema, name)` must be unique within the owner (the
connection that owns this endpoint). Column-name and primary-key issues are
caught by the contract model at the document level; cross-file uniqueness of
`database_object` tuples is enforced server-side at catalog-merge time.
