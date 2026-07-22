# CLAUDE.md — analitiq-pipeline-builder

Contributor guidance for this plugin. Repo-wide concerns — layout, tests, the
contract pin, releases, credentials, drift policy — live in the root `CLAUDE.md`
and are not repeated here.

## What this plugin does

A **local authoring tool for pipelines and streams** that move data between
Analitiq DIP connectors. It authors **pipeline**, **stream**, **connection**, and
**database-endpoint** documents against the published contract, downloads
pre-defined connectors from the DIP registry, and wires them together.

Connectors are the building blocks it wires — one repo per connector under the
[`analitiq-dip-registry`](https://github.com/orgs/analitiq-dip-registry/repositories)
org, downloaded read-only by `registry-browser`.

## What it refuses

- **Connector documents** — owned by the sibling `analitiq-connector-builder`
  plugin. This plugin consumes connectors; it never authors them.
- **API endpoints** — they come from the connector document.
- **Any registration API call.** Authoring is entirely local.
- **Writing a secret value into a document.** Secrets are authored as
  `secret_refs` pointers and templated into a gitignored
  `.secrets/credentials.json` the user fills in.
- **Deleting or overwriting user files** — especially a connection's
  `.secrets/`. The orchestrator halts and asks instead.

Agents must never author JSON that belongs to another agent's responsibility.

## Agent chain

```
pipeline-builder (skill, orchestrator)
  → pipeline-provider-researcher
  → registry-browser
  → connection-creator              one per side
  → private-endpoint-creator        database connections only
  → pipeline-creator
  → stream-creator                  one per endpoint, in parallel
  → pipeline-schema-validator       loop
  → pipeline-drift-classifier
```

| Agent | Owns |
|---|---|
| `pipeline-builder` (skill) | Orchestration: collect intent, dispatch creators, run the validator loop, write files only when every artifact passes. In **edit** mode it changes an existing artifact in place (surgical, non-destructive) instead of building from scratch. |
| `pipeline-provider-researcher` | Collects `PipelineFacts` from the user. No WebSearch. |
| `registry-browser` | Downloads source + destination connectors, read-only; reuses connectors already on disk. |
| `connection-creator` | A `connection.json` per side plus its `.secrets/credentials.json` template. |
| `private-endpoint-creator` | Database connections only: introspects the live database and authors `database-endpoint` documents per selected table — or derives one, without connecting, for a new destination table the engine will create on first run — plus connection-scoped type-map gap files (`connections/<slug>/definition/type-map-{read,write}.json`) when the connector's base maps don't cover a discovered native. |
| `pipeline-creator` | The `pipeline.json` shell referencing connections by UUID. |
| `stream-creator` | One `stream.json` per selected endpoint. |
| `pipeline-schema-validator` | Runs `scripts/validate.py` and returns a `Diagnostics` object. |
| `pipeline-drift-classifier` | Structural diff against a previous release — informational; does not drive a version bump. |

## Generated prose — the CI gate

This plugin does not restate contract facts in prose; it **generates** them from
the pinned package into marked regions:

```bash
python3 plugins/analitiq-pipeline-builder/scripts/gen_contract_docs.py          # rewrite in place
python3 plugins/analitiq-pipeline-builder/scripts/gen_contract_docs.py --check  # CI gate
```

Rules when editing this plugin's prose:

- **Never hand-edit between a `BEGIN GENERATED` / `END GENERATED` marker pair** —
  the generator overwrites it and CI fails.
- Everything outside the markers is hand-written **judgment**: when to apply a
  rule, what to ask the user, what the plugin refuses to do.
- A block id with no renderer is an error, not a no-op — `UnknownBlock` fails loud.
- Bumping the contract pin means re-running the generator;
  `test_generated_blocks_in_sync` is the gate.
- This file is excluded from the generator (`NOT_GENERATED` in the script)
  because it *documents* the markers rather than carrying a real block.

## Where the authoring rules live

Every rule about *how* to author lives in `skills/`, loaded by the agent that
needs it. **This file deliberately does not restate any of it.**

| Topic | Skill |
|---|---|
| Orchestration, artifact reuse, per-build vs shared state | `skills/pipeline-builder/` and its `references/` |
| Pipeline shape, identity model, schedule, engine runtime, file output | `skills/pipeline-spec/` |
| Stream source/destination, endpoint refs, mapping, filter operators, validation rules | `skills/stream-spec/` |
| Connection envelope, `secret_refs` schemes, credentials template | `skills/connection-spec/` |
| Database endpoint authoring, connection-scoped type-map gaps | `skills/endpoint-spec/` |
