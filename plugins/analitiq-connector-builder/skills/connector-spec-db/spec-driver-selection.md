# Driver selection

Every database connector package ships its own driver in
`requirements.txt` and picks its transport in `definition/connector.json`
via `transport_type` (`sqlalchemy` or `adbc`). This is the decision
guide for choosing the driver and bulk-write path when authoring a
connector for a new system.

## Decision order

Apply in order; stop at the first match.

1. **A first-class ADBC driver exists** → `transport_type: "adbc"`. The
   driver hands Arrow buffers directly to the system's native bulk
   protocol; no row-by-row path at all.
2. **The server exposes an Arrow Flight SQL endpoint** → ADBC via the
   generic Flight SQL driver.
3. **Neither, but the system has a native bulk-load protocol** →
   SQLAlchemy transport for connect/DDL, with the bulk path implemented
   in the connector's own class (the thick path) against the raw
   cursor.
4. **None of the above** → SQLAlchemy transport with batched INSERT.
   This is the fallback, not the default — pick it last.

## 1. First-class ADBC drivers

`cursor.adbc_ingest(...)` genuinely skips a row-by-row insert path for
exactly these:

| System | Package | Bulk path |
|---|---|---|
| PostgreSQL | `adbc-driver-postgresql` | libpq `COPY BINARY`. Production-ready. |
| Snowflake | `adbc-driver-snowflake` | Native Arrow ingestion via the internal Go-Snowflake driver. |
| BigQuery | `adbc-driver-bigquery` | Storage Write API (Arrow-native). |
| DuckDB | shipped with `duckdb` itself | Zero-copy in-process. |
| SQLite | `adbc-driver-sqlite` | Production-ready; mainly useful for testing, not volume. |

The schema's `AdbcTransport.driver` enum is the **sole validator** for
ADBC driver values (currently `postgresql`, `snowflake`, `bigquery`).
The engine derives the dbapi module from the `driver` value by the
upstream packaging convention `adbc_driver_{driver}.dbapi` — the
connector's `requirements.txt` must ship the matching
`adbc-driver-{driver}` wheel (plus `adbc-driver-manager`).

If the system's driver is not yet in the enum, that is a **contract gap to
raise, not a freeform workaround** — and it is not a one-line change. Adding a
driver means extending the published enum *and* provisioning the platform-side
support for it, so treat it as coordinated work with the contract and platform
owners rather than something a connector author can unblock alone. Until the
enum entry exists, select the next tier in the decision order.

**Redshift** takes the SQLAlchemy transport with the **sync**
`redshift+redshift_connector` driver — the canonical Redshift path.
`redshift_connector` is a sync DBAPI; the engine runs it on the sync
SQLAlchemy engine automatically (see "Constraints" below), so no ADBC
entry is needed. That dispatch is all the engine contributes: as with
every SQLAlchemy connector, system-specific interpretation — TLS,
upsert SQL — ships in the connector package's own dialect, never the
engine (see `spec-connector-package.md`). DSN template
`redshift+redshift_connector://{username}:{password}@{host}:{port}/{database}`.
The libpq-compatible PostgreSQL ADBC driver (`transport_type: "adbc"`,
driver `postgresql`) also reaches Redshift over the postgres wire, but
wire compatibility does not extend to the driver's option surface
(TLS parameters differ — research the actual driver, per
`spec-tls.md`), and the sync SQLAlchemy path is the canonical one.

## 2. Flight SQL

| Driver | Package | Covers |
|---|---|---|
| Flight SQL generic | `adbc-driver-flightsql` | Any server implementing the Arrow Flight SQL protocol — Dremio, Doris, InfluxDB 3.x, Databricks (in some configs), and a growing set of newer warehouses. |

Caveat: this only helps if the target server actually exposes a Flight
SQL endpoint. Ordinary MySQL/Postgres deployments do not.

## Do not use the JDBC bridge

| Driver | Package | What it does |
|---|---|---|
| JDBC bridge | `adbc-driver-jdbc` | Wraps any JDBC driver — gives an ADBC API surface over Oracle/MSSQL/MariaDB/MySQL/Redshift, but underneath it still binds row-by-row through JDBC. |

It buys the ADBC interface, not ADBC performance. A connector that
needs one of these systems takes the SQLAlchemy transport (or the
native bulk path below) instead.

## 3. Native bulk-load protocols (no ADBC)

Each of these is roughly 10x faster than parameterized INSERT, even
batched. The connect/DDL layer stays on the SQLAlchemy transport; the
bulk write runs against the raw driver cursor in the connector's own
class.

| System | Driver | Bulk path |
|---|---|---|
| MySQL / MariaDB | aiomysql (SQLAlchemy async) | `LOAD DATA LOCAL INFILE` via raw cursor — stream Arrow → CSV/TSV → server reads it directly. |
| PostgreSQL (when not on ADBC) | psycopg | `COPY FROM stdin BINARY`. |
| Oracle | python-oracledb (SQLAlchemy) | `cursor.executemany(sql, rows)` with tuned `arraysize` — the standard fast path; SQL*Loader is not practical from Python. |
| MSSQL / SQL Server | pyodbc (SQLAlchemy) | `fast_executemany=True` on the cursor — TDS batched parameter stream; single-line change. |
| ClickHouse | clickhouse-connect (skip SQLAlchemy) | `client.insert_arrow(table_name, arrow_table)` — first-class Arrow ingest, just not branded ADBC. |

## Constraints from the engine contract

- SQLAlchemy transports accept a **sync or async** DBAPI. The engine
  builds the sync vs async SQLAlchemy engine automatically from the
  dialect's own `is_async` capability — there is no driver allow-list.
  Async drivers (`postgresql+asyncpg`, `mysql+aiomysql`) run on the
  async engine; sync drivers (`redshift+redshift_connector`,
  `postgresql+psycopg2`) run on the sync engine, in a worker thread off
  the event loop. Prefer async where the system has
  a working async driver; use a sync driver when that is the system's
  viable path (Redshift's `redshift_connector` is the canonical sync
  case). The declared `driver` must be a real SQLAlchemy
  `dialect+driver` registration — e.g. `redshift_connector` registers
  under the `redshift` dialect, so `postgresql+redshift_connector` is
  invalid and fails at transport build.
- The driver lives ONLY in the connector's `requirements.txt`. The
  engine image ships no database drivers.
- Known pin: aiomysql's adapter still passes the deprecated positional
  argument to PyMySQL's `Connection.ping()`; pin `pymysql<1.2` until
  aiomysql ships a fix (the reference `mysql`/`mariadb` connectors do
  this).
- A connector may ship more than one driver when it declares (or is
  expected to grow) more than one transport — the reference `postgres`
  connector ships `asyncpg` for the SQLAlchemy transport plus
  `adbc-driver-postgresql`/`adbc-driver-manager` for the ADBC path.
