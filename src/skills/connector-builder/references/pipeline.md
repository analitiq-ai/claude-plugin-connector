# Orchestration pipeline

Phase-by-phase contract for the `connector-builder` orchestrator. Loaded
on demand by the orchestrator skill.

## Modes

The orchestrator runs in one of three modes (input `mode`, default
`build`). `build` and `update` share phases 1–5 and branch only at
phases 0, 6, and 7; `validate` runs phase 0 then validates the on-disk
documents (report-only, no fix loop), skipping research, authoring,
drift, and write.

- **`build`** (default) — author a fresh connector; phase 0 halts if the
  `{connector_id}/` directory already exists.
- **`update`** — re-author an existing connector from *current* docs and
  re-version it. Phases 1–5 run normally; the existing connector is read
  **only** as the drift baseline (phase 0), drift is required (phase 6),
  and phase 7 regenerates the tree in place. The prior files are the
  versioning baseline, never the working copy — they are not edited.
  Runs inside a VCS checkout so the regeneration is reviewable.
- **`validate`** — read-only. Skip phases 1–3 and 5–7; run phase 4
  (validation) over the on-disk documents (connector, type maps, and all
  endpoint files) and report the diagnostics. To fix findings, re-run in
  `update` mode.

## Phases

### 0. Pre-flight

Branch on `mode` before any other work.

**`build`** — check whether a directory named `{connector_id}/`
already exists in the current working directory.

- If it does NOT exist → proceed to phase 1.
- If it DOES exist → halt the run and surface a structured warning.

The warning must include:

- The full absolute path of the existing directory.
- The exact `rm -rf {path}` command the user can run to remove it.
  The orchestrator MUST NOT delete the directory itself — manual
  removal is required so the user has a chance to inspect or back up
  whatever's there.
- A note that re-running after removal produces a fresh connector
  authored from scratch (no migration of legacy connector shapes).

**`update`** — `connector_path` points at the existing connector. Read
its directory name and `connector.json` `connector_id` up front (the
target artifact, not spec material) and record it as the read-only drift
baseline (the default `previous_release_path`). Proceed to phase 1; do
not edit it in place — phase 7 regenerates the tree. If research/authoring
later yields a `connector_id` that differs from `connector_path`'s,
**halt** and surface the mismatch rather than writing a divergent tree (a
changed slug is a new connector, not an update). If `connector_path` does
NOT exist there is nothing to update: fall back to `build` semantics and
tell the user.

**`validate`** — read the on-disk documents under `connector_path`
(`definition/connector.json`, `definition/type-map-read.json`,
`definition/type-map-write.json` when present, and
`definition/endpoints/*.json`) and skip directly to phase 4; do no
research, authoring, or writing. If `connector_path` does NOT exist, halt
and tell the user there is nothing to validate.

**Why this exists.** The plugin authors connectors against the
published schema contract. Pre-existing connectors authored against
older shapes (e.g. with `placeholders` arrays or a `manifest.json`,
or with the plugin pre-rewriting on top of an existing tree) are not
migrated by this plugin. Stopping early avoids partial-state writes
and keeps the build path simple. A future migrator agent could relax
this check; for now, manual removal is the contract.

**Failure mode.** If the user reports they cannot remove the directory
(permissions, dirty tree under VCS, etc.), do not attempt workarounds.
Surface the OS-level error and let the user resolve it before
re-running.

### 1. Research (domain)

Invoke `connector-provider-researcher` at `scope: domain`, handing it the
**live contract schema URLs** as its mission spec (`connector` +
`type-map-read`, plus `type-map-write` for databases). The schema defines
*what to research*; the researcher walks it and grounds every fact in the
provider's docs. Pass `provider`, optional `kind_hint`, and the
official-docs URL when the user supplied one (when omitted, the researcher
locates the official docs via WebSearch and reports the URL it used).
Receive a `ProviderFacts` JSON object discriminated by `kind`, carrying
the connector skeleton, the **resource list** (`resources`) that seeds the
phase-5 fan-out, and the connector-wide `native_type_vocabulary` (so the
read map is authored complete before fan-out).

**Input:** `provider`, `kind_hint?`, `docs_url?`, `scope: domain`, contract
schema URLs.
**Output:** `ProviderFacts` (plus the docs URLs actually used).
**Failure mode:** if the researcher cannot access or locate official
docs, halt and ask the user for a URL or manually-supplied facts.

### 2. Classify

Run the closed-enum mappers inline (see `enum-mappers.md`):

- `KindMapper` → `kind` (one of `api`, `database`).
- `AuthTypeMapper` → `auth.type`.
- `TransportTypeMapper` → `transport_type` per transport.

Storage kinds (`file`, `s3`, `stdout`) are accepted by the schema but not
yet supported by the engine. If the user explicitly asked for one,
dispatch to `storage-connector-creator` (which currently returns a
structured refusal); otherwise fail closed and ask.

### 3. Dispatch creator (domain body + type maps)

Based on `kind`:

- `kind = api` → invoke `api-connector-creator` with `ProviderFacts` plus
  classifications.
- `kind = database` → invoke `db-connector-creator` with the same.
- `kind ∈ {file, s3, stdout}` → invoke `storage-connector-creator` (stub).

Always pass `provider_facts`. The creator's **hard gate** refuses an
initial authoring dispatch without it — research cannot be skipped. The
creator authors the connector body and type map(s); it does **not** author
endpoints (that is the phase-5 fan-out).

Receive a `CreatorOutput` JSON object containing the assembled connector
body and type map(s). For `kind = database` it additionally carries the
`package_files` block (`connector.py`, `__init__.py`, `requirements.txt`,
`pyproject.toml` contents) — the connector is an installable Python
package and the creator owns all of its files.

### 4. Validate the domain (barrier)

Invoke `connector-schema-validator` over the connector body and type
map(s):

- Connector → `https://schemas.analitiq.ai/connector/latest.json`.
- Read map (`type-map-read.json`) →
  `https://schemas.analitiq.ai/type-map-read/latest.json`.
- Write map (`type-map-write.json`, database only) →
  `https://schemas.analitiq.ai/type-map-write/latest.json`.

Both maps run the full contract-model + semantic pass. The validator derives the
rule direction from the filename, so write the maps under their exact
filenames before standalone validation, or validate via the connector
document so the sibling walk picks them up.

The validator validates JSON documents only — the database package files
(`connector.py`, `__init__.py`, `requirements.txt`, `pyproject.toml`) are
enforced by registry CI (wheel build, entry-point checks), not by this
pipeline.

This is a **barrier**. In `build` / `update` mode the connector body and
type maps MUST validate clean before the phase-5 endpoint fan-out, because
every endpoint references the connector's transports/auth and resolves its
field types through `type-map-read`. For `kind = database` this completes
validation — database connectors ship no endpoint files (schema/table
combinations are connection-scoped and discovered at runtime via
`resource_discovery`), so phase 5 is skipped.

In `validate` mode, run the validator once over **every** on-disk document
— the connector, both type maps when present, and all
`definition/endpoints/*.json` — report the resulting `Diagnostics`, and
stop. There is no fix loop and no creator re-dispatch (phases 1–3 and 5
were skipped, so there is no `CreatorOutput` to revise). The fix loop
below applies to `build` and `update` only.

In `build` / `update` mode the orchestrator should attempt at most 5 fix
passes per artifact — re-dispatch the matching creator with the
validator's findings and the artifact it produced on the prior pass
(`CreatorOutput`), re-validate, repeat. The creator — not the
orchestrator — decides whether each finding is a real defect or a
validator false positive; it owns the spec. Pass `Diagnostics.findings`
verbatim; do not pre-filter, pre-diagnose, or read spec material to
interpret them. If `error`-severity findings persist after 5 passes, halt
and surface the diagnostics; do not write partial files. The validator
script is single-shot; iteration discipline lives in the orchestrator's
prose, not in the script. The cap is best-effort and not runtime-enforced;
runtime enforcement is tracked at
https://github.com/analitiq-ai/ai-plugins-official/issues/26.

### 5. Endpoint fan-out (api only)

With the domain authored and validated clean, author one endpoint per
resource — **concurrently, bounded, and each on its own research**.
Database connectors skip this phase entirely.

1. **Enumerate the worklist.** From `ProviderFacts.resources` (or the
   user-specified resource list), record every resource as a worklist item
   with a state: `pending → running → done · failed`. The worklist is how
   the orchestrator tracks the fan-out without dropping endpoints.
2. **Run branches, bounded.** Run at most **N branches concurrently**
   (default **10**); as one finishes, pull the next `pending`. Each branch
   is a full `researcher → endpoint-creator → validator` chain for one
   resource:
   - `connector-provider-researcher` at `scope: endpoint` researches that
     resource's response and returns `EndpointFacts` — the field-level
     schema (datetime zone-awareness from a real sample value, enum
     domains, nullability, formats). This is the per-resource research
     that grounds field types instead of guessing them.
   - `endpoint-creator` authors the endpoint document from `EndpointFacts`
     and the connector document (for transport/auth refs). The API connector
     body carries no connector-level pagination, so the orchestrator echoes
     the connector-wide pagination (`ProviderFacts.pagination` → style +
     params) into the branch's `EndpointFacts.pagination`. Its hard gate
     refuses if `EndpointFacts` is missing — it has no web access and may not
     guess field types.
   - `connector-schema-validator` validates the endpoint against
     `https://schemas.analitiq.ai/api-endpoint/latest.json`, with the same
     per-artifact 5-pass fix loop as phase 4 (re-dispatch
     `endpoint-creator` with `Diagnostics.findings` and the
     `EndpointCreatorOutput` it produced).
3. **Isolate failure.** A branch that still fails after the fix cap is
   marked `failed` in the worklist and surfaced — it does **not** block its
   siblings. The orchestrator reports partial results rather than silently
   dropping the endpoint.
4. **Join.** When the worklist is drained (no `pending` / `running`),
   proceed to phase 6.

**Type vocabulary stays connector-level.** If a resource exposes a native
not covered by `type-map-read`, that is a **domain-level** type-map
addition — re-author and re-validate the domain (phases 3–4), never patch
the map per endpoint. This keeps canonical types consistent across
endpoints.

(Database endpoints validate against
`https://schemas.analitiq.ai/database-endpoint/latest.json` when a future
mode authors them; this plugin does not.)

### 6. Drift

The classifier reads `previous_version` from `previous_release_path` and
returns the computed `next_version`; set the connector's top-level
`version` to that `next_version` directly (do not recompute the semver
yourself). This `version` is the connector's own release semver, owned by
`connector-drift-classifier` — unrelated to the plugin package version,
which this repo bumps via PR labels. The classifier needs a `current_path`
to diff, so stage the freshly-authored draft to a temporary path first.

- **`update`** — required: invoke `connector-drift-classifier` with
  `previous_release_path` = the existing connector and `current_path` =
  the staged draft, and apply the returned `next_version` (never reset to
  `1.0.0`).
- **`build`** — if `previous_release_path` was supplied, invoke
  `connector-drift-classifier` the same way (staged draft as
  `current_path`) and apply `next_version`; otherwise this is a first
  release; set `version` to `1.0.0`.

### 7. Write

In `update` mode the regenerated files replace the existing connector
tree — the prior files were read as the drift baseline in phase 6 and
are never edited in place. Report that the tree was regenerated and
recommend the user review `git diff` before committing. Otherwise write
the connector document, type map(s), package files (database only), and
any endpoint files to disk at predictable paths. The connector root IS
the Python package for database connectors:

```
{connector_id}/
├── definition/
│   ├── connector.json
│   ├── type-map-read.json          # required for both api and db; native → Arrow
│   ├── type-map-write.json         # database only; Arrow → native DDL render rules
│   └── endpoints/
│       └── {endpoint_id}.json      # api connectors only — one file per endpoint; filename = document.endpoint_id
├── __init__.py                     # database only — re-exports the connector class
├── connector.py                    # database only — {Name}Dialect(SqlDialect) + {Name}Connector(GenericSQLConnector)
├── requirements.txt                # database only — THIS connector's driver(s) only
├── pyproject.toml                  # database only — analitiq-connector-{connector_id}; entry points named {connector_id}
└── README.md
```

**Reproducibility (update mode).** An update fully regenerates the tree
from `ProviderFacts` + creator logic; connector content is treated as
reproducible, so non-reproducible hand edits to a connector are not
preserved across an update. This is a deliberate limitation — running
updates inside a VCS checkout keeps any regeneration reviewable and
revertible. A preserve/merge step for genuinely bespoke connector code
is out of scope for now.

Never write a `type-map.json` — that pre-split filename is dead to the
engine and the validator rejects it with a migration finding.

Each endpoint file's basename **must** equal its `endpoint_id` (write the
endpoint to `endpoints/{endpoint_id}.json`, never a renamed or aliased
file). The engine looks an endpoint up by `endpoint_id` and reads
`endpoints/{endpoint_id}.json`, so a divergent filename is unreachable at
runtime. The validator's `endpoint-filename` check enforces this — both on
an endpoint validated directly and on every sibling endpoint during
connector validation.

## Failure modes

- Research timeout: ask user for offline-supplied facts or a different docs URL.
- Classification ambiguity: fail closed; ask the user to confirm.
- Validator stuck: surface findings; do not write incomplete files.
- Drift classifier rolls back to `none`: treat as first release.
- `update` / `validate` mode but `connector_path` is absent: there is
  nothing to update or validate — for `update`, fall back to `build`
  semantics and tell the user; for `validate`, halt and tell the user.
