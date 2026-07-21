# CLAUDE.md — analitiq-connector-builder

Contributor guidance for this plugin. Repo-wide concerns — layout, tests, the
contract pin, releases, credentials, drift policy — live in the root `CLAUDE.md`
and are not repeated here.

## What this plugin does

Authors connector and endpoint JSON documents conforming to the published
Analitiq contract at `schemas.analitiq.ai`. Connectors may be published to the
`analitiq-dip-registry` GitHub org as individual repos named `{connector_id}`.

## What it refuses

- **`connection` and `pipeline` documents** — runtime credentials for a connector
  instance, and the full integration definition. Owned by the sibling
  `analitiq-pipeline-builder` plugin.
- **Database endpoints** — connection-scoped, produced by the connector's
  `resource_discovery` workflow at runtime, never authored here.
- **Storage kinds** (`file`, `s3`, `stdout`) — accepted by the schema, but the
  engine does not execute them, so `storage-connector-creator` returns a
  structured refusal until support lands.

Agents must never author JSON that belongs to another agent's responsibility.

## Agent chain

```
connector-builder (skill, orchestrator)
  → connector-provider-researcher        scope: domain
  → {api|db|storage}-connector-creator
  → connector-schema-validator           ← domain barrier: must pass before fan-out
  → endpoint fan-out (API only), per resource, bounded concurrency (default 10):
        connector-provider-researcher (scope: endpoint)
          → endpoint-creator
          → connector-schema-validator
  → connector-drift-classifier           optional
  → write files
```

Database connectors author no endpoints and skip the fan-out entirely.

| Agent | Owns |
|---|---|
| `connector-builder` (skill) | Orchestration: classify kind, dispatch the creator, run the validator loop, run drift classification, write files. |
| `connector-provider-researcher` | Fact extraction from the provider's official docs, never authoring. Two scopes: `domain` → `ProviderFacts`, `endpoint` → `EndpointFacts` for one resource. Prefers a user-supplied docs URL; otherwise locates official docs via `WebSearch`. First-party pages only. |
| `api-connector-creator` | `kind: "api"` connector bodies. |
| `db-connector-creator` | `kind: "database"` packages: connector body, both type maps, and the Python package files. |
| `storage-connector-creator` | Stub — structured refusal (see above). |
| `endpoint-creator` | One API endpoint document per invocation. |
| `connector-schema-validator` | Structural + semantic validation of JSON documents only; package files are registry CI's job. |
| `connector-drift-classifier` | Diffs the draft against `previous_release_path`, emits a `DriftVerdict` so the orchestrator bumps `version` correctly. |

## Orchestrator modes

- **`build`** (default) — author a fresh connector; halts if a `{connector_id}/`
  directory already exists.
- **`update`** — re-author from *current* docs and re-version by diffing the
  fresh draft against the existing connector. The existing tree is a read-only
  versioning baseline, never edited in place; the tree is regenerated and the
  version bumps from the prior release. Run inside a VCS checkout so the
  regeneration is reviewable via `git diff`.
- **`validate`** — read-only pass over an on-disk connector. Reports diagnostics
  without researching, authoring, or writing. To fix findings, re-run `update`.

## Where the authoring rules live

Every rule about *how* to author lives in `skills/`, loaded by the agent that
needs it. **This file deliberately does not restate any of it** — a second copy
is a drift surface (root `CLAUDE.md` → drift policy).

| Topic | Skill |
|---|---|
| Orchestration, endpoint identity, I/O contracts, lifecycle phases, value expressions, connection contract, metadata + versioning | `skills/connector-builder/` and its `references/` |
| Auth flows, HTTP transports, pagination, replication | `skills/connector-spec-api/` |
| Driver selection, DSN bindings, TLS, resource discovery, read/write type maps, connector package files | `skills/connector-spec-db/` |
| Storage-kind stub | `skills/connector-spec-storage/` |

The published schema is the authority over all of it. Enum lists appearing in
skill prose are illustrative; the live schema and the pinned
`analitiq-contract-models` package are normative.
