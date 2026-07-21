# `connections` block

<!-- BEGIN GENERATED: fields-pipeline-connections -->
`analitiq.contracts.pipelines.config.PipelineConnections` — closed (`additionalProperties: false`); required: `destinations`, `source`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `source` | **yes** | string | — | `pattern=\S`, `minLength=1` |
| `destinations` | **yes** | array of string | — | `minItems=1`, `uniqueItems=True`, `item pattern=\S`, `item minLength=1` |
<!-- END GENERATED: fields-pipeline-connections -->

Duplicate destinations are rejected — `ADV-PIPE-001` in the cross-field rule
table in `SKILL.md`.

## Connection reference format

A connection reference is the `connection_id` UUID of the corresponding
connection document. The contract constrains only non-emptiness here; emitting
the UUID rather than the directory slug is **plugin policy**, chosen because the
engine resolves references at runtime against authored `connection_id` values.

Directory layout stays human-readable
(`connections/<connection-slug>/connection.json`); the slug is only used
for file organization, not for cross-document identity. See
`../pipeline-builder/references/identity-and-versioning.md`.

## Rules

- `source` is a single reference, not an array.
- A destination reference may equal the source — that's a legitimate self-loop
  (e.g., copying data within a single database between schemas).
- The contract defines **no upper bound** on `destinations`. A deployment may
  impose one, so treat a very wide fan-out as a question for the user rather
  than as something the contract has blessed.
- Every reference must resolve to a connection owned by the same org. The
  plugin does not enforce ownership; the registry does at save time.

## What is NOT in this block

- Connection bodies. Those live in
  `connections/<connection-slug>/connection.json`.
- Connection credentials. Those live in
  `connections/<connection-slug>/.secrets/`.
- The connector reference. The pipeline references **connections**, not
  connectors. The connection points back at its connector via
  `connector_id`.
