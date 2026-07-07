---
name: connector-spec-storage
description: Stub for connector kinds the plugin recognizes but cannot yet author (file, s3, stdout — no engine execution; nosql, document — no non-SQL authoring path). Schema accepts these kinds but support is not yet shipped, so this skill is intentionally minimal. Loaded only by storage-connector-creator (also a stub) when the orchestrator dispatches one of these kinds.
disable-model-invocation: true
---

# connector-spec-storage (stub)

This skill is a placeholder. The published connector schema accepts
`kind ∈ {file, s3, stdout, nosql, document}`, but the plugin cannot yet
author them — `file`/`s3`/`stdout` are not executed by the engine, and
`nosql`/`document` share the `database-endpoint` schema but have no non-SQL
authoring path. Until support lands:

- The orchestrator routes these kinds to the `storage-connector-creator`
  agent.
- `storage-connector-creator` declines to author and returns a
  structured note explaining why.
- This skill exists so future expansion (auth flows, transports,
  encoding rules) has a place to live without restructuring the skill tree.

When support arrives, expand this skill with:

- `spec-file-transport.md` — local filesystem path templates and access
  modes.
- `spec-s3-transport.md` — S3 endpoint, region, prefix templates;
  `S3CredentialsBlock` shape; assume-role flows.
- `spec-stdout-transport.md` — debug sink configuration.
- A non-SQL creator path for `nosql` / `document` stores (e.g. MongoDB).
- `examples/` — one validated example per kind.
