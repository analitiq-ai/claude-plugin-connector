# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

This repository is the `analitiq-connector-builder` Claude Code plugin: it authors connector and endpoint JSON documents conforming to the published Analitiq schema contract at `schemas.analitiq.ai`. Connectors may be published to the `analitiq-dip-registry` GitHub org as individual repos named `{connector_id}`. The plugin is installed via `.claude-plugin/plugin.json`.

Building pipelines from existing connectors is a separate concern owned by the `analitiq-pipeline-builder` plugin (a different repository); this plugin only creates connectors.

## Agents

**Agent chain:** `connector-builder` (skill, orchestrator) → `connector-provider-researcher` (scope: domain) → `{api|db|storage}-connector-creator` → `connector-schema-validator` (domain barrier — must pass before fan-out) → **endpoint fan-out (API only): per resource, `connector-provider-researcher` (scope: endpoint) → `endpoint-creator` → `connector-schema-validator`, bounded concurrency (default 10) off a worklist** → `connector-drift-classifier` (optional) → write files. Database connectors author no endpoints and skip the fan-out.

- `connector-builder` (skill) — orchestrator. Classifies connector kind, dispatches to the matching creator, runs the validator loop, runs drift classification, writes files. Runs in one of three modes (`build` default, `update`, `validate`). Carries shared invariant references (value expressions, lifecycle phases, connection-contract outer shape, metadata/versioning, endpoint identity, I/O contracts) under `skills/connector-builder/references/`.
- `connector-provider-researcher` — reads the live published contract schemas as its mission spec and grounds the facts they require in the provider's official docs. Runs at two scopes: `domain` returns a discriminated `ProviderFacts` (auth, transports, pagination, rate limits, base URLs, the resource list, and the native-type vocabulary); `endpoint` returns `EndpointFacts` for one resource (its response field schema, including datetime zone-awareness decided from a real sample value). Prefers a user-supplied docs URL; when none is given it locates the provider's official docs via `WebSearch`. Facts come only from first-party pages fetched with `WebFetch`.
- `api-connector-creator` — authors `kind: "api"` connector bodies. Loads the `connector-spec-api` skill (auth flows, HTTP transports, pagination, replication).
- `db-connector-creator` — authors `kind: "database"` connector packages: the connector body, both type maps (read + write), and the Python package files (`connector.py` with `{Name}Dialect` + `{Name}Connector`, `__init__.py`, `requirements.txt`, `pyproject.toml` with `{connector_id}`-named entry points). Loads the `connector-spec-db` skill (driver selection, DSN URL templates with bindings + encoding, TLS, resource discovery, read/write type maps, package files).
- `storage-connector-creator` — stub for `kind ∈ {file, s3, stdout}`. The schema accepts those kinds but the engine does not yet execute them, so this agent returns a structured refusal until support lands.
- `endpoint-creator` — authors one API endpoint JSON document per invocation. Endpoint documents have no top-level `kind` field; the parent connector's `kind` selects the endpoint schema. Database endpoints are connection-scoped and produced by the connector's `resource_discovery` workflow at runtime, not authored here.
- `connector-schema-validator` — runs Layer 1 (Draft 2020-12 JSON Schema) and Layer 2 (semantic validators: reserved-field, expression-resolver, phase-resolvability, transport-ref, dsn-binding, auth-shape, tls-consistency, type-map-coverage, type-map-rule, type-map-write-coverage, endpoint-annotations, endpoint-filename). JSON documents only — package files are registry CI's job. Backed by `validator/src/analitiq_connector_validator.py` (packaged as `analitiq-connector-validator` so registry CI can run Layer 2 standalone — see `validator/`).
- `connector-drift-classifier` — diffs the assembled draft against `previous_release_path` and emits a `DriftVerdict` (patch/minor/major/none) so the orchestrator can bump `version` correctly.

## Key Concepts

- **Connector:** Reusable provider transport + auth contract. Lives in `{connector_id}/definition/connector.json` and validates against `https://schemas.analitiq.ai/connector/latest.json`. Top-level fields: `$schema`, `kind` (one of `api`, `database`, `file`, `s3`, `stdout`), `connector_id` (the stable slug, matching `[a-z0-9_-]+`; the on-disk directory uses the same value), `version`, `default_transport`, `transports`, `auth`, `connection_contract`, optional `resource_discovery`. Registry-stamped fields (`created_at`, `updated_at`) must NOT appear in authored documents.
- **Endpoint:** Operation template for a single resource. API endpoints live in `{connector_id}/definition/endpoints/{endpoint_id}.json` (filename matches the document's `endpoint_id`, pattern `^[a-z0-9][a-z0-9_-]*$`) and validate against `https://schemas.analitiq.ai/api-endpoint/latest.json`. Database endpoints validate against `database-endpoint/latest.json` but are connection-scoped — the plugin does not author them; they are produced from the connector's `resource_discovery` workflow at runtime. Endpoint documents do not carry a `kind` field; the parent connector's `kind` selects the endpoint schema.
- **Type maps:** Two **standalone** sibling files sharing the `{match, native, canonical}` rule shape (top-level **JSON array** — never an object wrapper; **first-match-wins**; `match` is a closed enum `exact` | `regex`). Each direction has its own published schema: the read map validates against `https://schemas.analitiq.ai/type-map-read/latest.json` and the write map against `https://schemas.analitiq.ai/type-map-write/latest.json`, both running the full Layer 1 + Layer 2 pass (the validator derives the direction from the filename). The pre-split `type-map.json` filename is rejected with a migration finding.
  - **Read map** (`{connector_id}/definition/type-map-read.json`, native → Arrow): required and non-empty for both API and DB. `native` is the matcher; regex patterns are matched against UPPERCASED, whitespace-collapsed natives — **author patterns uppercase** (exact rules normalize automatically; capture-group names stay lowercase; lowercase regex literals warn). `canonical` is the render side; regex rules may template it with `${name}` substitutions **backed by ECMA-262 `(?<name>…)` named captures in `native`** (anonymous groups don't count). Python-only `(?P…)` syntax is rejected. Schemaless / structured-container natives (`JSON`, `JSONB`, `VARIANT`, `OBJECT`, `ARRAY`, `MAP`, `array<…>`, `…[]`, …) must resolve to a **container canonical** (`Json`, or a typed `List`/`Struct`/`Map`), never a scalar like `Utf8` — `type-map-rule` enforces this. `Object` / `List` are endpoint-only markers accepted as narrowings of a `Json`-resolved rule. Per-connector native→canonical specifics live in `skills/connector-spec-db/spec-type-maps.md`, not here.
  - **Write map** (`{connector_id}/definition/type-map-write.json`, Arrow → native DDL): **required for `kind: database`, forbidden for `kind: api`.** Direction inverts: `canonical` is the matcher (regex with named captures for parameterized types, matched verbatim — PascalCase is case-significant) and `native` is the rendered DDL (`${name}` substitutions backed by captures in `canonical`). Must cover the full canonical vocabulary (Boolean, Int8–64, UInt8–64, Float16/32/64, Decimal, Utf8/LargeUtf8, Json, Binary/LargeBinary/FixedSizeBinary, Date32/64, Time, Timestamp bare + tz); gaps warn — legitimate only when the dialect overrides `render_column_type` for that family (BigQuery's Decimal). Connectors never ship Python type-rendering tables.
- **TLS declaration:** **SQLAlchemy transports only.** Generic `transports.<name>.tls` with `mode` (refs `connection.parameters.ssl_mode`) and `ca_certificate` (refs `secrets.ssl_ca_certificate`); the connector package's dialect (`build_tls_connect_arg`) translates this into driver-specific arguments. **ADBC transports have NO `tls` block** — TLS lives inside `db_kwargs` as driver-namespaced entries (e.g. `adbc.postgresql.sslmode`, `adbc.postgresql.sslrootcert`) whose values are usually value-expression refs to the same canonical inputs. The `ssl_mode` vocabulary is connector-defined (libpq-shaped systems: `disable | allow | prefer | require | verify-ca | verify-full`; MySQL/MariaDB: `DISABLED | PREFERRED | REQUIRED | VERIFY_CA | VERIFY_IDENTITY`).
- **Value expression:** One of `ref` / `template` / `literal` / `function`. Refs and template variables target the closed scope list: `secrets.*`, `connection.parameters.*`, `connection.selections.*`, `connection.discovered.*`, `auth.*`, `runtime.*`, `stream.*`. Inline functions: `basic_auth`, `jwt_sign`, `url_encode`. Unknown scopes/functions are validation errors.
- **DSN bindings:** Database transports use `dsn.kind: "url_template"` with a `template` containing `{placeholder}` markers and a `bindings` map. Each binding has a `value` (value expression) and an `encoding` (closed enum: `raw`, `host`, `url_userinfo`, `url_path_segment`, `url_query_key`, `url_query_value`). Authors must NEVER pre-encode binding values; the runtime owns percent-encoding.
- **Driver selection (database):** decision order — (1) first-class ADBC driver (schema `AdbcTransport.driver` enum is the sole validator: `postgresql`, `snowflake`, `bigquery`; Redshift takes ADBC `postgresql`) → (2) Arrow Flight SQL endpoint → (3) async SQLAlchemy + native bulk path in the connector class (thick) → (4) async SQLAlchemy batched INSERT (last resort). Never the JDBC bridge. SQLAlchemy drivers must be async (`postgresql+asyncpg`, `mysql+aiomysql`, `mariadb+aiomysql`); the driver ships ONLY in the connector's `requirements.txt` (the engine pins no drivers; it derives the dbapi module as `adbc_driver_{driver}.dbapi`). Guide: `skills/connector-spec-db/spec-driver-selection.md`.
- **Connector package (database):** the connector root IS an installable Python package — `connector.py` (`{Name}Dialect(SqlDialect)` + `{Name}Connector(GenericSQLConnector)`; CDK imports only, never another connector or the engine), `__init__.py`, `requirements.txt` (this connector's drivers only), `pyproject.toml` (`analitiq-connector-{connector_id}`, dynamic deps from requirements.txt, entry points named `{connector_id}` under both `analitiq.source_connectors` and `analitiq.destination_connectors`). Dialects implement the hooks their transports require (`build_tls_connect_arg`, `build_sqlalchemy_upsert` + `supports_upsert_sqlalchemy`, `adbc_stage_table_sql` + `supports_upsert_adbc`) and structural overrides only where the portable form is invalid (`batch_commits_key_type`, `current_timestamp_default`). API connectors carry only the definition — no package files. Guide: `skills/connector-spec-db/spec-connector-package.md`.

`connection` (runtime auth credentials for a connector instance) and `pipeline` (the full integration definition) are owned by the separate `analitiq-pipeline-builder` plugin, not authored here.

## Orchestrator Modes

- **`build`** (default) — author a fresh connector; halts if a `{connector_id}/` directory already exists.
- **`update`** — re-author an existing connector from *current* docs and re-version it by diffing the fresh draft against the existing connector (read-only drift baseline; tree regenerated; version bumps from the prior release). Run inside a VCS checkout so the regeneration is reviewable via `git diff`.
- **`validate`** — read-only validation pass over an on-disk connector; reports diagnostics without researching, authoring, or writing. To fix findings, re-run in `update` mode.

## Versioning

The connector's `version` field is the connector release semver, bumped per the connector release table (patch/minor/major) by `connector-drift-classifier`; first release is `1.0.0`. This is distinct from the plugin's own package version in `.claude-plugin/plugin.json`, which is managed by **release-please** (Conventional Commits) — never bump it by hand:

- The bump is derived from the commit / squashed-PR-title type: `fix:` → patch, `feat:` → minor, `feat!:` or a `BREAKING CHANGE:` footer → major. Non-release types (`chore:`, `docs:`, `ci:`, `refactor:`, `test:`) don't trigger a bump.
- release-please accumulates merged changes into a rolling **Release PR**; merging that Release PR bumps `plugin.json`, regenerates `CHANGELOG.md`, and tags the release. Feature PRs are one normal merge each — the only extra merge is the Release PR, once per release.
- **Pre-1.0:** while the plugin is `0.x`, breaking changes bump the minor and features bump the patch (`bump-minor-pre-major` / `bump-patch-for-minor-pre-major` in `release-please-config.json`), so it won't jump to `1.0.0` accidentally.
- When squash-merging, ensure the PR title is a valid Conventional Commit — that title is what release-please parses. Config lives in `release-please-config.json` + `.release-please-manifest.json`; the workflow is `.github/workflows/release-please.yml`.

## Connector Directory Structure (output of connector-builder)

**API connectors** (definition only — no package files, no write map):
```
{connector_id}/
├── README.md
└── definition/
    ├── connector.json              # validates against connector/latest.json
    ├── type-map-read.json          # native → Arrow; validates against type-map-read/latest.json
    └── endpoints/
        └── {endpoint_id}.json      # filename = document.endpoint_id; validates against api-endpoint/latest.json
```

**Database connectors** (installable Python package; no authored endpoints; `tls` declared inside `connector.json`):
```
{connector_id}/
├── README.md
├── __init__.py                     # re-exports the connector class
├── connector.py                    # {Name}Dialect(SqlDialect) + {Name}Connector(GenericSQLConnector)
├── requirements.txt                # THIS connector's driver(s) only
├── pyproject.toml                  # analitiq-connector-{connector_id}; entry points named {connector_id}
└── definition/
    ├── connector.json              # validates against connector/latest.json
    ├── type-map-read.json          # native → Arrow; validates against type-map-read/latest.json
    └── type-map-write.json         # Arrow → native DDL; validates against type-map-write/latest.json
```

`connector_id` is author-supplied and matches the on-disk `{connector_id}/` directory name. Registry-stamped fields (`created_at`, `updated_at`) never appear in authored files.

## Supported Auth Types

`api_key`, `basic_auth`, `oauth2_authorization_code`, `oauth2_client_credentials`, `jwt`, `db`, `credentials`, `aws_iam`, `none`. The set is closed by the published schema; adding another auth type requires a schema-contract change first.

## Schemas + Validation

Published schemas (host: `schemas.analitiq.ai`):

- Connector: `https://schemas.analitiq.ai/connector/latest.json`
- API endpoint: `https://schemas.analitiq.ai/api-endpoint/latest.json`
- Database endpoint: `https://schemas.analitiq.ai/database-endpoint/latest.json`

Authored documents declare `$schema` with this host — the URL is locked by a `const` inside each schema, and the validator fetches from the same host.

The `connector-schema-validator` sub-agent runs `validator/src/analitiq_connector_validator.py`, which performs Draft 2020-12 JSON Schema validation plus semantic validators (reserved-field, expression-resolver, phase-resolvability, transport-ref, dsn-binding, auth-shape, tls-consistency, type-map-coverage, type-map-rule, type-map-write-coverage, endpoint-annotations, endpoint-filename). The validator checks JSON documents only — the database package files are enforced by registry CI. The same module is packaged under `validator/` as the installable `analitiq-connector-validator` (console entry point `analitiq-validate-connector`) so registry CI can run Layer 2 (`--semantic-only`) outside the plugin runtime; the published schema is the single source of truth and both consumers run this one module. Tests under `tests/connector_validator/`.

## Single source of truth (drift policy)

The published schema is the single source of truth. **Never restate what it defines — reference or load it.** Carry only craft the schema can't express (judgment, idioms, gotchas, workflow). This splits everything into **contract** (don't duplicate — field shapes, enums, vocabularies, `$schema` URLs) and **craft** (keep — *how* to choose, the "why", provider gotchas). Three mechanisms:

- **`$ref` / load the live schema** for any JSON-schema shape the plugin needs. The validator already fetches + caches the schemas; e.g. the DSN-binding `encoding` enum is derived from the live `connector` schema (`$defs/DsnBinding/properties/encoding`), with a literal fallback only for offline runs.
- **Fetch-once, pass-down** — the orchestrator hands the live contract schema URLs to the researcher (the mission spec) and the creators read the same schemas as vocabulary, so authoring and validation agree on one contract.
- **Drift-check CI** for anything that must stay duplicated as decision logic (e.g. the `enum-mappers` that map provider facts onto schema enums): `tests/connector_validator/test_schema_drift.py` loads the live schemas and fails the build if the plugin's enum targets diverge.

Enum lists that appear in this file and in skill prose are **illustrative**; the authoritative definition is always the live schema (or, for canonical Arrow types, `canonical-types.json` in `analitiq-infra`). Craft that the schema never defined (e.g. the `ssl_mode` vocabulary, the driver-selection decision order, datetime naive/tz judgment) is not drift-exposed and stays.

## Canonical Types

Canonical types are Apache Arrow logical types in PascalCase (e.g. `Int32`, `Int64`, `Float64`, `Utf8`, `Boolean`, `Binary`, `Date32`, `Time64`, `Timestamp`, `Decimal128`, `List`, `Struct`, `Map`). The vocabulary is owned by `docs/schema-contracts/shared/canonical-types.json` in `analitiq-infra`. Authoring guidance: `skills/connector-spec-db/spec-type-maps.md`.

## Conventions

- JSON Schema Draft 2020-12 throughout.
- `connector_id` is the stable connector slug; `[a-z0-9_-]+`; immutable. The same value names the on-disk `{connector_id}/` directory.
- `version` is the connector release semver, bumped per the connector release table (patch/minor/major) by `connector-drift-classifier`. First release: `1.0.0`.
- Test org_id: `d7a11991-2795-49d1-a858-c7e58ee5ecc6`.
- Agents must never author JSON that belongs to another agent's responsibility.

## PR Review Process:
After creating a PR, follow these steps.
Continue invoking the PR review process until no more errors are raised.
If raised errors are not relevant to the PR, ask if you should create GitHub issue for the rised error.

1. Use `/pr-review-toolkit` to review the PR after you have implemented all changes.
2. Wait for feedback from the review executor.
3. Determine if the raised issues are legitimate or not.
   a. if the issue is legitimate and relevant to the PR, fix it.
   b. if the issue is outside the scope of the PR, check if there is a related issue in the GitHub issue tracker. If not, create a new issue in GitHub and move on.
   c. If the issue is not a legitimate problem, summarize your thoughts on the point and move on.
4. Once you fixed all issues that need fixing, commit fixes, push to the branch.
5. Use `/pr-review-toolkit` to review again
6. Continue doing this cycle until the PR is approved by the review executor.
7. Once the PR is approved, run the tests to make sure they all pass.
