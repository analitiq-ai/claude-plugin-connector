# New-table endpoints (pending physical creation)

A destination endpoint may target a table that does not exist yet. There is
no flag for this: the authored document is an ordinary
`database-endpoint/latest.json` document, and the engine creates the missing
table on the first pipeline run (`CREATE TABLE IF NOT EXISTS`) from the
document's `columns` — rendering each column's DDL type from its `arrow_type`
through the write type maps (connection first, then connector). `native_type`
is not consulted for DDL. This file governs deriving such a document without
introspection; the mode contract lives in `private-endpoint-creator`
(`author-new-table`).

## Identity

- `schema` / `name` are the user's spelling, **verbatim** — for a new table
  the user's spelling *is* the canonical identifier; it determines what the
  engine creates. Pass it to `endpoint_id.py` unchanged, `--object-type table`.
- The target namespace must be one discovery returned (`schema`, or the
  database-as-`catalog` for schemaless dialects). Whether the engine creates
  a missing schema is dialect-owned (a connector pre-DDL hook; not every
  dialect declares one), so a discovered namespace is the only target that
  succeeds everywhere.

## Column derivation

Columns mirror the source that will feed the table:

- **Database source** (a `database-endpoint` document): one column per source
  column — `name`, `arrow_type`, `nullable` carried verbatim, `properties` /
  `items` container specs carried whole. Drop `default`, `comment`, and
  `ordinal_position`: they are facts about the *source* dialect and object,
  not about the table being created.
- **API source** (the connector's api-endpoint document): one column per
  field of the record object the read operation's `response.records` ref
  selects. Field enumeration is owned by the published package — resolve the
  record shape with `analitiq.contracts.endpoints.resolve_read_record_schema`
  / `find_record_field_properties` (an addressed array node's `items` is the
  record shape), never by eyeballing `response.schema`. Per field: `name`
  from the key, `arrow_type` from its annotation, `nullable` when the schema
  states it; nested declared shapes become `ColumnFieldSpec` container specs
  (each nested node needs its own `arrow_type`). A field with no `arrow_type`
  annotation is contract-legal — derive its canonical from the field's
  JSON-Schema shape with `spec-columns.md` judgment.
- Never author `_synced_at` or `_record_hash`, and drop them from the mirror
  when the source carries them (an engine-created source table does) — the
  engine appends its own synthetic columns at creation.

## `native_type` for a table that does not exist

The honest value is the one the engine will create: resolve each distinct
`arrow_type` through the write maps (invocation and precedence per
`spec-type-map-gaps.md`) and freeze the rendered native.

For an uncovered canonical:

- If the connector's package files show a `render_column_type` dialect
  override covering the family, the engine renders it in code — no rule can
  say what. Author `native_type: "unknown"` with a `type_maps.notes` entry.
- Otherwise the engine cannot render the DDL at all. The user must supply the
  native (the orchestrator interviews → `write_render_choices`); author it as
  the column's `native_type` **and** as a connection-scoped write rule per
  `spec-type-map-gaps.md` — the user's choice takes the place of the
  "discovered native" those rules normally render, and without the rule the
  first run hard-errors.

## `primary_keys`

User-confirmed (the orchestrator interviews; the source's `primary_keys` are
the suggested default). Must reference derived columns (`ADV-DBEP-003`). Omit
when the user declines keys — but an `upsert` stream then has no destination
key columns to name as `conflict_keys`.
