# DSN URL templates + bindings

The full authoring contract for `transports.<name>.dsn` when
`dsn.kind == "url_template"`. Applies identically to `sqlalchemy` and
`adbc` transport types — the DSN shape is shared. The transport-specific
fields are:

| `transport_type` | Identity field | Extras |
|---|---|---|
| `sqlalchemy` | `driver` — a `dialect+driver`, sync or async (e.g. `"postgresql+asyncpg"`, `"mysql+aiomysql"`, `"redshift+redshift_connector"`; dispatch is engine-side — see `spec-driver-selection.md` §Constraints). Optional in the contract, since SQLAlchemy can derive it from the DSN's scheme — but **declare it anyway**: it is the one place a reader can see the sync/async choice was deliberate. | optional `tls` block (`ssl_mode` + `ssl_ca_certificate` refs; mode vocabulary is connector-defined) |
| `adbc` | `driver` — closed enum: `postgresql`, `snowflake`, `bigquery` | `db_kwargs` (object; values may be value expressions). **AdbcTransport requires at least one of `dsn` / `db_kwargs`.** TLS lives inside `db_kwargs` (e.g. `adbc.postgresql.sslmode`); no `tls` block. |

Transport choice follows the decision order in
`spec-driver-selection.md` (first-class ADBC → Flight SQL →
SQLAlchemy + native bulk path → SQLAlchemy batched INSERT; never the
JDBC bridge). For databases in the ADBC driver enum, prefer `adbc`
— it exchanges Arrow columns natively and avoids the SQLAlchemy
row-to-Arrow conversion. The chosen driver ships ONLY in the
connector's `requirements.txt` (the engine pins no database drivers).
ADBC drivers that accept all connection state via `db_kwargs` (e.g.
Snowflake) may omit `dsn` entirely.

## Shape

```json
{
  "dsn": {
    "kind": "url_template",
    "template": "postgresql+asyncpg://{username}:{password}@{host}:{port}/{database}",
    "bindings": {
      "username": { "value": { "ref": "connection.parameters.username" }, "encoding": "url_userinfo" },
      "password": { "value": { "ref": "secrets.password" }, "encoding": "url_userinfo" },
      "host":     { "value": { "ref": "connection.parameters.host" }, "encoding": "host" },
      "port":     { "value": { "ref": "connection.parameters.port" }, "encoding": "raw" },
      "database": { "value": { "ref": "connection.parameters.database" }, "encoding": "url_path_segment" }
    }
  }
}
```

## Rules

- `template` is a connector-authored string with `{placeholder}` markers.
  No direct `${...}` context references — those go inside binding `value`
  expressions.
- Every placeholder in the template must have a matching binding key, and
  every binding key must be referenced by the template (ADV-CTOR-011).
- Each binding declares:
  - `value` — a value expression (`ref` or `template` or `function`).
  - `encoding` — one of the closed enum values listed below.

## Encoding values (closed enum)

| Encoding | Use |
|---|---|
| `raw` | No encoding. Numeric or already-safe values (port, integers). |
| `host` | Hostname encoding rules (IPv6 brackets, IDN punycode). |
| `url_userinfo` | RFC 3986 userinfo encoding (passwords, usernames). |
| `url_path_segment` | RFC 3986 path-segment encoding (database names that may contain special chars). |
| `url_query_key` | RFC 3986 query-key encoding. |
| `url_query_value` | RFC 3986 query-value encoding (query parameter values such as warehouse, schema). |

## Authoring checklist

1. Pick the canonical DSN form for the driver (look at SQLAlchemy /
   driver documentation).
2. Write the template with one `{placeholder}` per logical field.
3. For each placeholder, declare the binding's `value` and `encoding`.
4. Use `secrets.password` for the password — never `connection.parameters.password`.
5. Never pre-encode any value. The runtime applies the declared encoding.

## Driver examples

| Driver | Template |
|---|---|
| `postgresql+asyncpg` | `postgresql+asyncpg://{username}:{password}@{host}:{port}/{database}` |
| `mysql+aiomysql` | `mysql+aiomysql://{username}:{password}@{host}:{port}/{database}` |
| `redshift+redshift_connector` | `redshift+redshift_connector://{username}:{password}@{host}:{port}/{database}` |

These are SQLAlchemy transports (DSN `url_template`) — the first two
async, the third a sync driver (Redshift). ADBC drivers
differ by driver: a driver may carry all connection state in `db_kwargs`
and omit the DSN entirely (Snowflake authenticates this way), while
`postgresql` keeps core coordinates in a `dsn` `url_template` and
reserves `db_kwargs` for driver-namespaced extras like TLS — see the
`postgresql-adbc` reference example for the DSN-plus-`db_kwargs` shape.
