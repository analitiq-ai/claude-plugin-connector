<!-- GENERATED — do not edit here. Canonical source: analitiq-ai/infrastructure (validator/). Synced on release. -->

# analitiq-connector-validator

Validates Analitiq connector / endpoint / type-map JSON documents against the
**contract models** — the same Pydantic models the published JSON Schemas are
generated from — so authoring, the connector-builder plugin, and the registry's
merge gate all enforce one contract with no drift.

Single-document validity (structure **and** every cross-field rule) is delegated
to `TypeAdapter(...).validate_python` from `analitiq-contract-models`. It runs
**offline** — no schema fetch, no network. On top of the models it adds only what
a single-document model cannot express:

- **cross-file coverage** — a connector ships the right sibling type-map files
  for its kind, and an API connector's read map covers every
  `(native_type, arrow_type)` its endpoint files declare;
- **filename ↔ id** — an endpoint file is named `{endpoint_id}.json`;
- **advisory warnings** the contract tolerates — duplicate type-map rules, dead
  uppercase-only read patterns, write-map vocabulary gaps.

## Source of truth

The canonical source is the **`analitiq-ai/infrastructure`** repo, under
`validator/` (with the contract models under `contract-models/` and the
`alq-models` layer). That repo owns the models, generates the JSON Schemas from
them, and builds + syncs this directory on release. The copy in
`analitiq-ai/claude-plugin-connector` is **generated — do not edit here**; edits
are overwritten by the next release sync. The plugin runtime still executes
`src/analitiq_connector_validator.py` by path, so the layout is preserved.

## Install

```bash
pip install "analitiq-connector-validator @ git+https://github.com/analitiq-ai/claude-plugin-connector.git@validator-vX.Y.Z#subdirectory=validator"
```

This pulls `analitiq-contract-models` (and pydantic) transitively.

## Use

```bash
analitiq-validate-connector --document definition/connector.json
```

Point `--document` at `definition/connector.json` to trigger cross-file coverage:
it discovers the sibling `type-map-read.json` / `type-map-write.json` and
`endpoints/*.json` from the connector's directory. `--schema-url`,
`--semantic-only`, `--json-only`, and `--no-cache` are accepted for backward
compatibility but are no-ops (validation is always model-driven and offline).

Output is a JSON report (`{"passed": bool, "findings": [...]}`) on stdout; the
process exits non-zero when any finding has severity `error`.
