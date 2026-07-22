# Extension policy

The published Analitiq schemas are **closed** — every authored entity is
`additionalProperties: false` (see the per-entity field tables in each spec
skill, which state it as emitted). Unknown fields, including any `x-*` key, are
rejected at validation time.

The plugin does not author extension keys. If the user asks the plugin to attach
extra metadata to a document, decline and surface a clear message: the published
contract does not accept unknown fields, and the plugin will not route around the
validator by smuggling data into a known field.

Never infer undeclared behavior. If the contract does not declare a rule, the
plugin does not invent one — that applies to request, transport, auth,
pagination, replication, resource-discovery and lifecycle behavior alike. A gap
in the contract is a gap to raise, not a blank to fill in.

Cross-agent state (the orchestrator's payloads between phases) is **not** part of
the authored document — that lives in `references/io-contracts.md`. Use those
payloads for any inter-agent communication; never use the authored JSON as a
side-channel.
