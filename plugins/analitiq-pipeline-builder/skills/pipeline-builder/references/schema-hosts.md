# Schema host

All Analitiq schemas are served from a single host:
`https://schemas.analitiq.ai/`. Each authored document declares `$schema`
against it, and the value is locked to a `const` inside the published schema.

<!-- BEGIN GENERATED: schema-urls -->
| Entity | Authored file | `$schema` value |
|---|---|---|
| Pipeline | `pipelines/<slug>/pipeline.json` | `https://schemas.analitiq.ai/pipeline/latest.json` |
| Stream | `pipelines/<slug>/streams/<stream-slug>.json` | `https://schemas.analitiq.ai/stream/latest.json` |
| Connection | `connections/<slug>/connection.json` | `https://schemas.analitiq.ai/connection/latest.json` |
| Database endpoint | `connections/<slug>/definition/endpoints/<endpoint_id>.json` | `https://schemas.analitiq.ai/database-endpoint/latest.json` |
<!-- END GENERATED: schema-urls -->

`$schema` is **optional** on every authored entity — omitting it is valid. The
plugin sets it anyway so each file stays self-describing on disk.

There is no authorable pinned form. Only the `latest.json` URL above validates;
a version-pinned `…/<X.Y.Z>.json` variant is rejected outright. Which contract
revision a document conforms to is identified by that `$schema` URL — there is no
separate per-document schema-version field.

## How validation works

The plugin does **not** fetch or cache schemas. `analitiq-validator` validates
each document against the bundled Pydantic contract models — the same source of
truth the published JSON Schemas are rendered from. The
`pipeline-schema-validator` agent runs the plugin's adapter (`scripts/validate.py`),
which self-installs the pinned validator into a managed virtualenv on first use
and is offline thereafter:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/validate.py" \
  --entity pipeline \
  --document path/to/pipeline.json \
  [--bundle-root .]
```

Because validation is offline, a document's declared `$schema` URL is a label,
not a fetch target — keep it in sync with the entity so the file stays
self-describing.

Validating the authored payload locally is exactly what the published package is
for: the `*Input` models the JSON Schemas render from ship in the offline pip
package, so the plugin never has to submit a document to discover it is invalid.
