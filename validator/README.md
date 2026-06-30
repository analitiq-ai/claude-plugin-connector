# analitiq-connector-validator

Standalone, CI-consumable packaging of the Analitiq connector validator
(`analitiq_connector_validator`) — the same module the
`analitiq-connector-builder` plugin runs, exposed as an installable
distribution with a console entry point so it can run **outside** the Claude
Code plugin runtime (e.g. in the connector registry's required merge gate).

It runs two layers over connector / endpoint / type-map JSON documents:

- **Layer 1** — Draft 2020-12 JSON Schema validation against the published
  contract at `schemas.analitiq.ai` (requires network to fetch the schema).
- **Layer 2** — semantic validators (reserved-field, expression-resolver,
  phase-resolvability, transport-ref, dsn-binding, auth-shape,
  tls-consistency, **type-map-coverage**, type-map-rule,
  type-map-write-coverage, endpoint-annotations). **No network.**

The canonical source lives at `src/analitiq_connector_validator.py` and is the
single source of truth: the plugin runtime executes this same file by path, so
authoring, the plugin, and registry CI all run one implementation.

## Install

Pinned, from a tagged ref of this repo (no PyPI required):

```bash
pip install "analitiq-connector-validator @ git+https://github.com/analitiq-ai/claude-plugin-connector.git@validator-vX.Y.Z#subdirectory=validator"
```

The tag `validator-vX.Y.Z` is cut at release and its version must match
`version` in `pyproject.toml` (the first release is `validator-v0.1.0`). Until
that tag exists, pin to a commit SHA or branch instead.

## Use

```bash
# Layer 2 only (no network) — what registry CI gates on:
analitiq-validate-connector --document definition/connector.json --semantic-only

# Full Layer 1 + Layer 2 (fetches the published schema):
analitiq-validate-connector \
  --schema-url https://schemas.analitiq.ai/connector/latest.json \
  --document definition/connector.json
```

`--semantic-only` runs Layer 2 without fetching any schema, so `--schema-url`
is not required in that mode. Point `--document` at `definition/connector.json`
to trigger `type-map-coverage`: it is filesystem-anchored and discovers the
sibling `type-map-read.json` and `endpoints/*.json` from the connector's
directory.

Output is a JSON report (`{"passed": bool, "findings": [...]}`) on stdout; the
process exits non-zero when any finding has severity `error`, so CI can gate on
the exit code.
