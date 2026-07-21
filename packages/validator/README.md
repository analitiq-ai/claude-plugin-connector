# analitiq-validator

The Analitiq **artifact validator**. It validates Analitiq JSON documents against
the **contract models** — the same Pydantic models the published JSON Schemas are
generated from — plus the cross-file and cross-document checks a single-document
model cannot express, so authoring, the connector-builder plugin, and downstream
consumers all enforce one contract with no drift. It is structured so per-kind
validators slot in without touching each other.

Today it covers:

- **authored single documents** — connector, endpoint, and type-map files, plus
  `connection`, `stream`, and `pipeline` documents;
- **pipeline bundles** — the cross-document referential integrity of an assembled
  run (pipeline + streams + connections + connectors + endpoints).

Single-document validity (structure **and** every cross-field rule) is delegated
to `TypeAdapter(...).validate_python` from `analitiq-contract-models`
(`analitiq.contracts`). It runs **offline** — no schema fetch, no network. On top
of the models it adds only what a single-document model cannot express — the
connection / stream / pipeline kinds are pure model validation (the model IS the
whole contract), so `analitiq-validate --document x.json` validates every authored
kind and emits the same uniform `{passed, findings[]}`.

**Connector-package cross-file checks:**

- **cross-file coverage** — a connector ships the right sibling type-map files
  for its kind, and an API connector's read map covers every
  `(native_type, arrow_type)` its endpoint files declare;
- **filename ↔ id** — an endpoint file is named `{endpoint_id}.json`;
- **advisory warnings** the contract tolerates — duplicate type-map rules, dead
  uppercase-only read patterns, write-map vocabulary gaps.

**Pipeline-bundle referential integrity** (`validate_pipeline_bundle(bundle)`) —
a bundle is a mapping of already-parsed `{pipeline, streams, connections,
connectors, endpoints}` documents, checked for internal consistency independent of
any storage or on-disk layout:

- every `pipeline.streams[]` ref resolves to exactly one bundled stream document;
- every referenced connection is present in the bundle;
- every stream `endpoint_ref.connection_id` is one of the pipeline's connections;
- every connection's `connector_id` is present among the bundled connectors;
- every `scope='connection'` endpoint_ref resolves to a bundled endpoint document;
- the bundle names a pipeline (has a `pipeline_id`).

Referential integrity is separate from **runnability**. `require_runnable=True`
(the default) additionally gates the pipeline on `status='active'` with at least
one runnable stream — the check an executor needs. An authoring tool validating a
**draft** bundle passes `require_runnable=False` to get the referential checks
without the active-status gate.

It does not assume the documents were already contract-validated, so a missing
reference field (a connection naming no connector, a stream slot with no
`endpoint_ref`) is reported as an unresolved reference — while per-document *shape*
(field types, lengths, enums) remains each document model's own job. Stream and
connection refs match on their base form, so a `{id}_v{n}` versioned ref resolves
the document that declares the bare `{id}` (connector identities match whole).

## Install

```bash
pip install analitiq-validator
```

This pulls `analitiq-contract-models` (and pydantic) transitively.

## Use

```bash
analitiq-validate --document definition/connector.json
analitiq-validate --document connection.json   # connection / stream / pipeline work too
```

The kind is detected from the document's shape — no `--kind` flag. Point
`--document` at `definition/connector.json` to also trigger cross-file coverage:
it discovers the sibling `type-map-read.json` / `type-map-write.json` and
`endpoints/*.json` from the connector's directory. A `connection` / `stream` /
`pipeline` document is validated purely against its contract model. `--schema-url`,
`--semantic-only`, `--json-only`, and `--no-cache` are accepted but are no-ops
(validation is always model-driven and offline).

Output is a JSON report (`{"passed": bool, "findings": [...]}`) on stdout; the
process exits non-zero when any finding has severity `error`.

A pipeline bundle is assembled from many documents, so it is validated as a
library call rather than from a single file:

```python
from analitiq.validator import validate_pipeline_bundle

findings = validate_pipeline_bundle(
    {
        "pipeline": pipeline_doc,
        "streams": stream_docs,
        "connections": connection_docs,
        "connectors": connector_ids,          # the connector identities present
        "endpoints": connection_endpoint_docs,  # scope='connection', connection_id, endpoint_id
    },
    require_runnable=False,  # authoring a DRAFT bundle: referential checks, no active-status gate
)
if any(f["severity"] == "error" for f in findings):
    raise SystemExit(findings)
```

## Source of truth

The canonical source is the **`analitiq-ai/infrastructure`** repo, under
`validator/` (with the contract models under `contract-models/` and the model
layer). That repo owns the models, generates the JSON Schemas from them, and
**renders** the source package into the public `analitiq.validator` package on
release. The published package is generated — edit the source in the
infrastructure repo, not the installed copy.
