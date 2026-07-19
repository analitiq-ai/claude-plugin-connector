# Resource discovery (databases)

How database connectors declare the discovery strategy that the runtime
uses to enumerate a system's objects.

## The object hierarchy shapes discovery

Systems differ in how many namespace levels sit above a table, and discovery
has to walk the right number of them:

| Shape | Levels | Examples |
|---|---|---|
| Three-level | catalog → schema → table | Snowflake, BigQuery (project → dataset → table), Databricks |
| Two-level | schema → table | PostgreSQL, Redshift, Oracle |
| Schema-less | database → table | MySQL / MariaDB, where the "schema" *is* the database |

This shape is **not** a field you declare on the connector — the runtime derives
it from the transport's driver. What it changes is your authoring: a three-level
system must enumerate catalogs, and its endpoints carry a populated `catalog`;
a schema-less system must not invent a second level to look uniform.

Discovery therefore walks up to **four** actions, not three —
`list_catalogs` (three-level systems only), `list_schemas`, `list_resources`,
and `describe_resource`. A connector for Snowflake or BigQuery that enumerates
only schemas and tables silently hides everything outside the default catalog.

## Shape

```json
{
  "resource_discovery": {
    "strategy": "information_schema",
    "transport_ref": "database",
    "implementation": { "type": "builtin" },
    "options": {
      "exclude_schemas": ["information_schema", "pg_catalog"]
    },
    "produces": ["connection.endpoints", "connection.type_map"],
    "triggers": {
      "list_resources": "on_activation",
      "describe_resource": "on_resource_selected"
    }
  }
}
```

## Required fields

- `strategy` — registered strategy ID. Common values:
  - `information_schema` for ANSI-SQL databases that expose
    `information_schema`.
  - `snowflake_account_usage` for Snowflake.
  - Provider-specific IDs as appropriate. The strategy must already be
    registered with the engine, or a `connector_plugin` implementation
    must be declared.

## Optional fields

- `transport_ref` — which transport to use for discovery. Defaults to
  `default_transport`.
- `implementation` — `{ "type": "builtin" }` for engine-shipped
  strategies (the common case), or
  `{ "type": "connector_plugin", "entrypoint": "module.path:ClassName" }`
  to ship strategy code with the connector package.
- `options` — strategy-specific declarative options (e.g.
  `exclude_schemas`).
- `produces` — array of `connection.endpoints` and/or
  `connection.type_map`. Most database connectors produce both.
- `triggers` — when discovery actions run:
  - `list_resources`: `on_activation` | `on_connection_selected` | `on_resource_selected` | `on_demand` | `scheduled`.
  - `describe_resource`: same enum.

## Rules

- The discovery transport may be the same as the data transport, or a
  separate discovery-only transport with restricted credentials.
- Discovery output is connection-scoped, not connector-scoped.
- The connector-level `type-map-read.json` (see `spec-type-maps.md`)
  provides the seed mapping for native types encountered during
  discovery. Connection-scoped type maps are out of scope for this
  plugin; see `shared/type-maps.md` for runtime resolution rules.

## What discovery must record about each object

The generated endpoints are produced at runtime, not here, but the strategy you
declare determines whether they come out addressable:

- **Every namespace level above the table goes into `catalog` / `schema`,
  verbatim.** Exact case and special characters are preserved — the engine
  dialect-quotes them into the qualified identifier. Never fold a parent
  namespace into the object's `name`, and never try to recover it from the
  `endpoint_id`, which is a derived handle (see
  `connector-builder/references/endpoint-identity.md`).
- **A column whose native type cannot be determined is recorded as the literal
  `"unknown"`**, not omitted and not guessed. That surfaces as a visible
  type-map miss rather than a silently mistyped column.
- **`object_type` (table / view / …) is descriptive only.** Do not use it to
  gate readability or writability — capability comes from the connector class's
  protocol conformance, not from a discovered label.

## Common pitfalls

- Don't try to ship database endpoints directly from the connector. They
  are produced from discovery output at runtime.
- Don't embed credentials in `options`. Auth runs separately.
- Don't author a custom strategy in `implementation` unless one of the
  builtin IDs doesn't fit. Most connectors should use builtin
  strategies.
- Don't enumerate only schemas and tables on a three-level system — see the
  hierarchy table above.
