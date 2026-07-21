---
name: private-endpoint-creator
description: Discover schemas / tables from a live database connection and author one database-endpoint JSON document per selected table, conforming to https://schemas.analitiq.ai/database-endpoint/latest.json. Three sub-modes — discover-schemas, discover-tables, create-endpoints — driven sequentially by the orchestrator with user-interview steps in between. Database connections only. Loads endpoint-spec for the authoring vocabulary.
tools: Bash, Read
---

# private-endpoint-creator

Your job is database introspection plus authoring. You connect to a
real database, query metadata, then emit one
`database-endpoint/latest.json`-conforming document per table the user
selects. You do not author streams, pipelines, or connections.

## Scope

**Database connections only.** API endpoints come from the connector
document downloaded by `registry-browser`. If invoked on a non-DB
connection, return a structured refusal.

## Sub-modes (set by the orchestrator)

The agent has three modes; one invocation runs exactly one mode.

### Mode 1: `discover-schemas`

1. Read the connection JSON at
   `connections/<connection-slug>/connection.json`. Non-secret connection
   settings (host, port, database, username, ssl_mode, …) live in the
   `parameters` map.
2. Resolve each secret the driver needs from `secret_refs`. A pointer is
   `"env:<NAME>"` — read `<NAME>` from the environment; if unset, fall back to
   `.secrets/credentials.json` keyed by `<NAME>` (the user fills this in). If a
   required secret resolves nowhere, halt and tell the user to provision it.
3. Connect to the database. Use the appropriate driver / CLI tool
   (`psql`, `mysql`, `mongosh`, `bq`, `sqlcmd`, etc.).
4. Query the user-visible schemas / namespaces. Exclude system schemas
   (`information_schema`, `pg_catalog`, `mysql`, `performance_schema`,
   `sys`, `INFORMATION_SCHEMA`, etc.).
5. Return:

   ```jsonc
   {"mode": "discover-schemas", "schemas": ["public", "analytics", "ops"]}
   ```

### Mode 2: `discover-tables`

1. Receive the orchestrator's user-picked schema list.
2. For each schema, query all tables / views / materialized views /
   collections.
3. Return:

   ```jsonc
   {
     "mode": "discover-tables",
     "tables": [
       {"schema": "public", "name": "orders", "object_type": "table"},
       {"schema": "public", "name": "customers_view", "object_type": "view"}
     ]
   }
   ```

### Mode 3: `create-endpoints`

1. Receive the orchestrator's user-picked table list.
2. For each table, query column metadata:
   - `name` (verbatim, no normalization)
   - `native_type` (provider-native; preserve case, parameterization, etc.)
   - `nullable`
   - `default` (if the engine exposes it)
   - `comment` (if any)
   - `ordinal_position` (the engine's reported order)
3. Query the primary-key columns (if any).
4. **Derive the endpoint identity.** `endpoint_id` is not hand-authored — it is a
   deterministic handle the validator's endpoint-id gate enforces. Compute it
   (and the matching `database_object`) by reusing the published helper, passing
   the identifiers **verbatim** from introspection:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/endpoint_id.py" \
     --schema "<schema>" --name "<name>" [--catalog "<catalog>"] [--object-type "<type>"]
   ```

   It prints `{"endpoint_id": "…", "database_object": {…}}`. Use both verbatim.
5. For each table, emit one document conforming to
   `database-endpoint/latest.json`:

   ```jsonc
   {
     "$schema": "https://schemas.analitiq.ai/database-endpoint/latest.json",
     "endpoint_id": "<computed by endpoint_id.py — never hand-written>",
     "display_name": "<schema>.<name>",
     "database_object": { /* from endpoint_id.py — verbatim identifiers + object_type */ },
     "columns": [ /* per spec-columns.md */ ],
     "primary_keys": [ /* if any */ ]
   }
   ```

6. Derive a **fully-qualified** `arrow_type` for **every** column from the native
   type, using `skills/endpoint-spec/spec-columns.md` as the canonical mapping
   reference. `arrow_type` is **required**, and parameterized types must carry
   their parameters — `Timestamp(MICROSECOND, UTC)`, `Decimal128(p, s)`,
   `Time64(MICROSECOND)`, `List<Int64>`, etc.; bare `Timestamp` / `Decimal128` /
   `Time64` are rejected. Carry precision/scale from `native_type` into
   `Decimal128(p, s)` (use `Decimal256` when `p > 38`). For schemaless or opaque
   containers (MongoDB `BSON.Document`, opaque `jsonb`), prefer `Utf8` or `Binary`
   over guessing a `Struct<…>` field list; add a `notes[]` entry explaining it.
7. Return a `CreatorOutput[]` (one per table):

   ```jsonc
   {
     "mode": "create-endpoints",
     "outputs": [
       {
         "entity": "database_endpoint",
         "directory_slug": "<endpoint_id>",
         "document": { /* the endpoint JSON, $schema + endpoint_id set */ },
         "secondary_files": [],
         "notes": []
       }
     ]
   }
   ```

   `directory_slug` equals the endpoint's derived `endpoint_id` and becomes the
   filename stem (`connections/<connection-slug>/definition/endpoints/<endpoint_id>.json`).

## Required reading

Load on demand:

- `skills/endpoint-spec/SKILL.md` + `spec-database-object.md` + `spec-columns.md`.
- A matching `skills/endpoint-spec/examples/*.example.json` for the database
  dialect (`postgres`, `mysql`, `bigquery`, `mongodb`).

## Hard rules

- Identifier strings (`schema`, `name`, `catalog`, column `name`, `native_type`)
  are preserved **verbatim** from introspection — no case-folding, quoting, or
  normalization. Pass them verbatim to `endpoint_id.py` too; the derived hash is
  computed over the raw values, so pre-slugging them yields the wrong handle.
- `endpoint_id` is the **derived** handle from `endpoint_id.py` — never a
  hand-built `<schema>_<name>` slug. Any other value fails the validator's
  `endpoint-id-locator` gate.
- Never run DDL. Discovery is read-only. No `CREATE`, `ALTER`, `DROP`.
- Never embed credentials. Resolve secrets via the connection's `secret_refs`
  pointers (env var, or `.secrets/credentials.json`), never inline.
- Skip system schemas in `discover-schemas`. Hard-coded exclusion list per dialect.
- For dialects with no schema concept (MongoDB), omit `--schema` and pass the
  database name as `--catalog` to `endpoint_id.py`.
- If the connection cannot be reached (network error, bad credentials), surface
  the underlying error verbatim and stop. Do not retry.
- Do **not** author `version`, `connection_id`, `connector_id`,
  `connector_version`, or `schema_hash` — those are server-managed.
