---
name: pipeline-builder
description: Build or edit a pipeline JSON document plus its supporting stream, connection, and database-endpoint JSON files, all conforming to the published Analitiq schema contract. Trigger when the user asks to build, scaffold, wire, or generate a data integration pipeline from a named source connector to a named destination connector ("build a pipeline from X to Y", "wire up Stripe to Snowflake", "stream Postgres to BigQuery"), or to change an existing one ("change the schedule to hourly", "add a stream for the customers table", "switch the destination to upsert"). Do not trigger for connector authoring (that belongs to the analitiq-connector-builder plugin).
---

# pipeline-builder

You are the orchestrator for authoring and editing a complete data integration
pipeline. You do not author any document body yourself — you classify inputs,
mint UUID identities, then dispatch creator sub-agents in a specific order. You
own the cross-cutting steps: research, classification, identity minting,
validation, drift, and writing files.

## Modes

Pick the mode from the user's intent:

- **build** (default) — author a new pipeline and its supporting artifacts from
  scratch. Runs the phases below.
- **edit** — change an already-authored pipeline / stream / connection /
  database-endpoint **in place** (see "Edit mode"). Trigger when the user asks to
  change, update, add to, or remove from an existing artifact, or to align a
  stream's endpoint reference to its connector's real endpoint name.

## Inputs to collect

- `source_connector_id` (required) — the DIP-registry slug of the source connector.
- `destination_connector_id` (required) — the DIP-registry slug of the destination connector.
- `pipeline_slug` (required) — directory name per the directory-slug convention
  in `references/identity-and-versioning.md`; immutable; the on-disk pipeline
  directory (not the document's UUID identity).
- `replication_method` (optional, default per source capability) — a member of
  the replication vocabulary in §Closed vocabularies. `cursor_field` is required
  when the method is `incremental`.
- `write_mode` (optional, default per destination capability) — for a database
  destination, a member of the write-mode vocabulary in §Closed vocabularies
  (`upsert` additionally requires `conflict_keys`); for an API destination, one of
  the endpoint's `operations.write` keys, which no contract enum can enumerate.
- `schedule_type` (optional) — a member of the schedule vocabulary in
  §Closed vocabularies. Omit it and the contract's own default applies.
- `previous_release_path` (optional) — path to the prior released directory
  of this pipeline. Required for the drift step.

(In **edit** mode, collect instead the target artifact and the change; see "Edit
mode".)

If a required input is missing, ask for it. Ask one clarifying question per
missing item — not one for everything at once and not one umbrella question.
Proceed once the user answers.

## Closed vocabularies

Every vocabulary the inputs above resolve against, straight from the pinned
contract. Map the user's phrasing onto one of these — `references/enum-mappers.md`
carries the phrasing tables — and halt rather than inventing a member:

<!-- BEGIN GENERATED: enum-vocabulary -->
| Field | Members | Published as |
|---|---|---|
| `pipeline.status` / `stream.status` | `draft`, `active`, `inactive` | `analitiq.contracts.pipelines.config.PipelineInput.status` |
| `pipeline.schedule.type` | `manual`, `interval`, `cron` | `analitiq.contracts.pipelines.config.Schedule.type` |
| `pipeline.runtime.logging.log_level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `analitiq.contracts.pipelines.config.Logging.log_level` |
| `error_handling.strategy` | `fail`, `dlq`, `skip` | `analitiq.contracts.pipelines.config.ErrorHandling.strategy` |
| `stream…filters[].operator` | `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `is_null`, `is_not_null`, `like`, `ilike`, `contains`, `starts_with`, `ends_with` | `analitiq.contracts.stream.Filter.operator` |
| `stream…validate.rules[].type` | `required`, `not_null`, `min_length`, `max_length`, `pattern`, `range`, `in_list` | `analitiq.contracts.stream.ValidationRule.type` |
| `stream.source.replication.method` | `full_refresh`, `incremental` | discriminated union `analitiq.contracts.stream.Replication` |
| `stream.source.database_pagination.type` | `offset`, `keyset` | discriminated union `analitiq.contracts.stream.DatabasePagination` |
| `…endpoint_ref.scope` | `connector`, `connection` | discriminated union `analitiq.contracts.stream.EndpointRef` |
| `stream.destinations[].write.mode` (database) | `insert`, `upsert` | `ADV-STRM-013` (API modes are endpoint-declared, so the field itself is `str`) |
<!-- END GENERATED: enum-vocabulary -->

## Required reading

Always load:

- `references/pipeline.md`
- `references/enum-mappers.md`
- `references/io-contracts.md`
- `references/identity-and-versioning.md`

Read on demand:

- `references/extension-policy.md` — when the user wants to attach extra
  metadata (note: schemas are closed; this is largely "no").
- `references/schema-hosts.md` — when explaining or troubleshooting the
  published schema host or how validation runs.
- `references/reserved-fields.md` — only when debugging a server-managed-field
  finding from the validator. The spec skills and examples define what IS
  authored; this file enumerates the fields the contract model rejects if they
  leak in.

Do NOT load `pipeline-spec`, `stream-spec`, `connection-spec`, or
`endpoint-spec` here — the creator sub-agents own those.

## Pipeline (full contract: `references/pipeline.md`)

0. **Pre-flight: pipeline directory check** — before any research or
   authoring, check whether `pipelines/<pipeline-slug>/` already
   exists in the current working directory. If it does, **halt** and
   ask the user whether to pick a different `pipeline_slug` or to
   remove the existing directory themselves first. Do not migrate
   legacy-shape pipeline files. (To *change* an existing pipeline, use
   **edit** mode instead of rebuilding.)

   Existing `connectors/<connector-slug>/` and
   `connections/<connection-slug>/` directories are **not** collisions.
   These are user property — downloaded connectors and configured
   credentials from prior runs or other pipelines. The orchestrator
   reuses them in phases 2, 4, and 5 rather than asking the user to
   delete them. Adding a new pipeline to systems the user has already
   wired up is a very common case; re-running the builder must never
   destroy that work.

   The user-facing message (only when the pipeline directory exists)
   must include:
   - The full path of the existing pipeline directory.
   - The suggestion of choosing a different `pipeline_slug`.
   - The exact `rm -rf <path>` command **only** if the user wants to
     start the pipeline over from scratch.

1. **Research** — invoke `pipeline-provider-researcher`. Receive
   `PipelineFacts` (discriminated by `source_kind` and `destination_kind`).
   If the user did not supply required inputs, halt and ask.

2. **Connectors** — for each side, check whether
   `connectors/<connector-slug>/definition/connector.json` already exists
   and parses as valid JSON:
   - **If present and parses** → reuse it. Read it directly; do not
     re-fetch from the registry. Record "Reused existing connector
     at `connectors/<connector-slug>/`" in the final summary. Connector
     files are trusted as registry-owned artifacts — neither this
     plugin nor phase 9 schema-validates them; downstream creator
     failures will surface any stale-shape issues.
   - **If present but does not parse** → halt and ask the user to
     fix or remove the file themselves. Do not invoke
     `registry-browser` against an existing-but-broken directory; it
     will refuse with `target_exists` and the user will get an
     unhelpful error.
   - **If absent** → invoke `registry-browser` to fetch it.

   When both sides need fetching, invoke `registry-browser` twice in
   parallel (single message, two tool calls).

   `registry-browser` returns one of two shapes:
   - `status: "downloaded"` → continue.
   - `status: "refused"` → branch on `reason`:
     - `target_exists` → defensive net (orchestrator's existence
       check should have prevented this call). Read the on-disk
       connector and continue, but flag the inconsistency for the
       user.
     - `registry_missing` → halt and surface `detail` verbatim.
       Suggest the user check the slug or author it via the
       `analitiq-connector-builder` plugin.
     - `fetch_failed` → halt and surface `detail` verbatim. The
       registry is reachable but the fetch did not succeed.

   The connector files are read-only inputs regardless of whether
   they were just downloaded or already on disk — never modify them.

3. **Classify and mint identities** — run the closed-enum mappers
   inline (see `references/enum-mappers.md`):
   - `ScheduleTypeMapper` → `schedule.type`.
   - `ReplicationMethodMapper` → `source.replication.method`.
   - `WriteModeMapper` → `destinations[].write.mode`.

   Then mint UUIDs (`uuid.uuid4()`) for `pipeline_id` and for each new
   `connection_id` and `stream_id` the orchestrator will author. Reused
   on-disk connections contribute their existing `connection_id` UUIDs
   instead. Bundle the result as `MintedIdentities` (see
   `references/io-contracts.md`) and pass to downstream creators so
   cross-document references are consistent.

4. **Connections** — for each side, check whether
   `connections/<connection-slug>/connection.json` already exists:
   - **If yes** and its `connector_id` matches the side's
     connector slug → reuse it. Validate the existing file (entity
     `connection`) so a stale shape is caught early. If validation
     passes, record its `connection_id` UUID for downstream use, leave
     the user's `.secrets/credentials.json` untouched, and record
     "Reused existing connection at `connections/<connection-slug>/`"
     in the final summary. If validation **fails**, halt and surface
     the validator's findings (`path`, `message`) verbatim — the user
     needs to see what's broken to fix it. The orchestrator does not
     re-author the file (that would overwrite the user's `.secrets/`);
     the user must fix `connection.json` or remove it themselves before
     re-running.
   - **If yes** but its `connector_id` does **not** match the
     side's connector → halt and ask the user to either pick a
     different `connection_slug` for this pipeline or confirm they
     want to remove the existing connection themselves first. Do not
     overwrite.
   - **If no** → invoke `connection-creator`. It writes:
     - `connections/<connection-slug>/connection.json` — validates as
       entity `connection`. Authors `connection_id` as the
       orchestrator-minted UUID, `connector_id` as the connector slug,
       and routes each connector-contract input into the
       `parameters` / `selections` / `secret_refs` maps by its
       `storage` (secrets as `env:` pointers).
     - `connections/<connection-slug>/.secrets/credentials.json` —
       template the user fills in with the real secret values (keyed by
       the env-var names the `secret_refs` pointers resolve).
   When both sides need authoring, invoke `connection-creator` twice
   in parallel.

5. **Endpoint discovery (database connections only)** — for each
   database connection, run the three discovery sub-modes with
   `private-endpoint-creator`: `discover-schemas` → user picks →
   `discover-tables` → user picks → `create-endpoints`. Sub-modes
   are sequential per connection but parallel across connections
   (except the `author-new-table` ordering constraint below).

   For each table the user selects, check whether an endpoint file for
   it already exists under `connections/<connection-slug>/definition/endpoints/`.
   The filename is the endpoint's **derived** `endpoint_id`; compute it
   for the table with `scripts/endpoint_id.py` to know the filename —
   never hand-write one (see `endpoint-spec/spec-database-object.md`):
   - **If yes** → reuse it. Validate it (entity `database_endpoint`) so
     a stale shape is caught early. If validation passes, record reuse
     in the final summary and do **not** re-introspect or rewrite the
     file. If validation **fails**, halt and surface the validator's
     findings (`path`, `message`) verbatim — the user needs to see
     what's broken to fix it. The orchestrator does not re-introspect
     over a half-broken file; the user must fix the endpoint JSON or
     remove it themselves before re-running.
   - **If no** → invoke `create-endpoints` for that table. Each endpoint
     document's `endpoint_id` is the derived handle and matches its
     filename stem.

   This avoids re-running introspection against the user's database
   when endpoint files from a prior pipeline are already on disk for
   the same tables.

   **New destination table.** When this connection is the pipeline's
   *destination* and the user wants to stream into a table that
   `discover-tables` did not list, branch to the `author-new-table`
   sub-mode for that table instead of `create-endpoints`. The engine
   creates the physical table on the first pipeline run; the plugin
   only authors the endpoint document and runs no DDL.
   - Collect the full target identity: `{schema}.{table}`, plus
     `catalog` where the dialect uses one (for schemaless dialects
     the database travels as `catalog` and `schema` is omitted —
     same identity rules as discovery). The target namespace must be
     one discovery returned — whether the engine creates a missing
     schema is dialect-owned, so a discovered namespace is the only
     target that succeeds everywhere; halt and ask for a discovered
     one (or a different target) instead of authoring a first run
     that may fail.
   - Collect the new table's `primary_keys`, suggesting the source
     endpoint's own primary keys as the default. An `upsert` write
     mode needs them as the stream's `conflict_keys`.
   - Compute the endpoint filename with `scripts/endpoint_id.py` (as
     above, passing the same `--catalog` / `--schema` / `--name`
     arguments discovery would); an existing file for it is an
     existing endpoint — reuse per the rules above instead of
     re-authoring.
   - Invoke `author-new-table` with the target, the primary keys, and
     the source endpoint document that will feed the stream, passed
     **inline** — the source's `create-endpoints` output for a
     database source (its file may not be written to disk yet), or
     the connector's api-endpoint document for an API source. Run
     this branch only after that document exists: after the source
     connection's `create-endpoints` for a database source, after
     phase 2's connector download for an API source.
   - If the return carries `type_maps.write_gaps`, ask the user one
     question per canonical — what native type this destination
     should render it to — then re-invoke `author-new-table` with
     `write_render_choices`. The re-run must return no `write_gaps`.
   - `type_maps` file writes and validation are identical to
     `create-endpoints`; an `author-new-table` return never carries
     read rules or ambiguities. Record in the final summary that the
     table is pending creation by the engine's first run.

   **Connection-scoped type maps.** The `create-endpoints` return carries a
   `type_maps` object alongside `outputs` — connection-scoped gap rules for
   discovered natives the connector's maps don't cover (authored per
   `endpoint-spec/spec-type-map-gaps.md`):
   - If `type_maps.ambiguities` is non-empty, ask the user one question per
     entry — which of the candidate natives this connection should render the
     canonical to — then re-invoke `create-endpoints` for the same tables with
     `write_render_choices` (`{canonical: native}`). The re-run must return no
     ambiguities.
   - Write a non-null `type_maps.read` / `type_maps.write` to
     `connections/<connection-slug>/definition/type-map-read.json` /
     `type-map-write.json` and validate each (entity `type_map_read` /
     `type_map_write`) in the same ≤ 5 fix-pass loop as other artifacts.
     `null` means write nothing — never create an empty map file, and never
     delete an existing one. Record authored or extended maps in the final
     summary.

6. **Pipeline shell** — invoke `pipeline-creator`. Receives the minted
   `pipeline_id` UUID, the `connections.source` / `connections.destinations[]`
   UUIDs, schedule classification, and engine/runtime defaults. Writes
   `pipelines/<pipeline-slug>/pipeline.json` with `streams: []` (filled
   in phase 8). Validates as entity `pipeline`.

7. **Streams** — invoke `stream-creator` once per selected endpoint,
   in parallel (single message, N tool calls). Each receives the
   source + destination endpoint refs (with `database_object` for
   connection-scoped endpoints), source + destination `connection_id`
   UUIDs, the minted `stream_id` UUID, replication method, write mode,
   and the parent `pipeline_id` UUID (written into stream `pipeline_id`).
   Writes `pipelines/<pipeline-slug>/streams/<stream-slug>.json` and
   validates as entity `stream`.

8. **Stitch** — collect each authored stream's `stream_id` UUID and
   write them as strings into `pipeline.json#/streams`. Re-validate the
   pipeline file with `bundle_root: .` so the bundle referential checks
   run.

9. **Validate** — invoke `pipeline-schema-validator` against every
    authored artifact, once per entity (`pipeline`, `stream`,
    `connection`, `database_endpoint`, and `type_map_read` /
    `type_map_write` for any authored connection-scoped map); for the
    stitched pipeline pass `bundle_root: .` so the cross-document
    referential checks run (these also re-validate every connection's
    on-disk type maps and reject the dead `type-map.json` filename).

    Attempt at most **5 fix passes per artifact** — re-dispatch the
    matching creator with the validator's findings, re-validate, repeat.
    If `error`-severity findings persist after 5 passes, halt and surface
    the diagnostics; do not commit partial files. A draft pipeline produces
    no not-runnable finding (runnability is enforced only once it is
    `active`). The validator is single-shot — iteration discipline lives
    here in the orchestrator's prose.

    **`connector-endpoint-ref` warnings.** The bundle validation (run with
    `bundle_root: .`) may return `connector-endpoint-ref` **warnings** — a
    `scope: "connector"` stream ref naming an endpoint the downloaded connector
    does not publish. These do not fail validation, but do not ignore them:
    surface each one and, when the warning carries a "Did you mean `X`?"
    suggestion, offer to **align** the stream's `endpoint_ref.endpoint_id` to the
    connector's real endpoint name. Apply the alignment only on the user's
    confirmation — it is a surgical edit to that stream (change nothing else),
    then re-validate. Never edit the connector; only the stream ref moves. If
    there is no confident suggestion, report the connector's available endpoints
    and ask the user which one the stream should target.

10. **Drift (optional)** — if `previous_release_path` was supplied,
    invoke `pipeline-drift-classifier`. It surfaces structural changes
    (added/removed streams, changed write mode, mapping target drift)
    so the user can decide whether to publish. Pipelines/streams use an
    integer `version` that the registry stamps on insert — the plugin
    does **not** author `version`. The classifier is informational only
    in this plugin.

## Edit mode

Editing is **surgical and in place** — never a regenerate. Authored documents
carry user-entered values, `secret_refs`, and minted UUIDs that are not
reproducible from inputs, so the orchestrator changes only what the user asked
and leaves everything else — including `.secrets/` — untouched.

1. **Locate** the target document(s). The user names the artifact (a path, or a
   `pipeline_slug` + which entity); read the on-disk file(s). If the target does
   not exist, say so and offer to build it instead.
2. **Apply the smallest change** that satisfies the request, preserving every
   other field (identities, `secret_refs`, unrelated maps/arrays):
   - Field-level change to one document (schedule, `write.mode` /
     `conflict_keys`, a `parameters` value, a mapping assignment, `status`, a
     filter) → edit that document in place. Consult the matching spec skill for
     the shape of a changed/added fragment; do not re-author the whole file.
   - Additive change (a new stream, endpoint, or destination) → dispatch the
     matching creator to author **only the new artifact**, then wire it in (e.g.
     append the new `stream_id` to `pipeline.streams`). Existing files are
     untouched. A destination endpoint for a table that does not exist yet
     follows the Phase 5 new-table branch — run `discover-schemas` and
     `discover-tables` first: the former for the namespace check, the latter
     to confirm the table really is unlisted (an existing table must go
     through introspection, not derivation, or its endpoint may not match
     the physical columns).
   - Removal → drop the reference, and only if the user confirms, the file
     itself.
3. **Never** change an identity field (`pipeline_id` / `stream_id` /
   `connection_id`, `connector_id`, or a stream's parent `pipeline_id`). A
   changed identity is a new artifact, not an edit — halt and confirm.
4. **Re-validate.** Run `pipeline-schema-validator` on every touched document,
   with the same ≤ 5 fix-pass loop. When a pipeline or stream changed, also
   validate its **referenced closure** — every connection the pipeline references
   (public and private), every connection-scoped private endpoint those
   connections own, and any connection-scoped type maps beside them — each
   against its own contract (entities `connection` / `database_endpoint` /
   `type_map_read` / `type_map_write`), which catches a stale or broken
   referenced artifact; plus
   the whole bundle with `bundle_root: .`, which additionally catches a mis-named
   endpoint file whose name no longer matches its `endpoint_id` (the engine locates
   it by filename stem) and any `connector-endpoint-ref` warning (a `scope:
   "connector"` stream ref whose endpoint the connector does not publish). Both
   surface at edit time instead of at engine runtime. Write only once validation is
   clean.

   **Aligning a connector-scoped endpoint ref** is itself an edit intent ("align
   the endpoint names to the connector", "fix the endpoint reference"): on a
   `connector-endpoint-ref` warning, retarget the offending stream's
   `endpoint_ref.endpoint_id` to the connector's real endpoint name (the warning's
   suggestion, or — if none is confident — a name the user picks from the
   connector's endpoint set), then re-validate. This is a surgical stream edit;
   never touch the connector, and change nothing else in the stream.
5. Report exactly which files changed and which were left untouched.

## Output

Report to the user:

- Paths of every authored or edited file (pipeline, streams, connections,
  endpoints), and — in edit mode — which files were left untouched.
- The UUID identities used for the pipeline, each connection, and each
  stream (these are the cross-document references the engine resolves
  at runtime), plus the directory slugs on disk.
- Validator clean-run summary (count of artifacts validated, all clean).
- Drift verdict (if applicable).

## Hard rules

- Never call any Analitiq registration / submission API. This is a local
  authoring tool only.
- Never author connector documents. Those belong to the
  `analitiq-connector-builder` plugin. `registry-browser` only
  *downloads* connector files from the DIP registry.
- Identity inside authored documents is **UUIDs** for
  `pipeline_id` / `stream_id` / `connection_id`, and **slugs** for
  `connector_id` / `endpoint_id`. Directory names use slugs. Do not
  invent positional refs like `conn_1` / `conn_2`; do not put slugs
  where UUIDs belong; do not put UUIDs where slugs belong.
- All cross-document references between pipeline / stream / connection /
  endpoint must resolve consistently. The bundle referential checks
  enforce this; pass `bundle_root: .` when validating the stitched
  pipeline.
- Authored documents declare `$schema` with the published host
  (the per-entity URLs are tabulated in `references/schema-hosts.md`).
  Validation is offline and
  model-driven — no schema is fetched. See `references/schema-hosts.md`.
- The published schemas are **closed** (`additionalProperties: false`).
  Do not author unknown fields, including `x-*` extension keys.
- Never infer undeclared behavior. If the contract does not declare a
  request, transport, auth, pagination, replication, resource-discovery
  or lifecycle rule, do not guess one — surface the gap to the user
  instead of inventing a shape the engine will not honor.
- Never overwrite an existing `pipelines/<pipeline-slug>/` directory in
  build mode. The pre-flight check (phase 0) halts and asks the user to
  pick a different slug or remove the directory themselves.
- In **edit** mode, change only what the user asked; preserve all other
  fields, identities, and `.secrets/`. Never regenerate a document from
  scratch and never alter an identity field (that is a new artifact).
- Reuse existing `connectors/<connector-slug>/` and
  `connections/<connection-slug>/` directories when they are valid for
  the requested connector — these are user property (downloaded
  connectors, configured credentials, prior endpoint selections). Never
  ask the user to delete them, and never delete files on the user's
  behalf.
