# analitiq-contract-models

The Analitiq **authored contract models** (Pydantic) — the write/input shapes for
connectors, connections, endpoints, type-maps, streams, and pipelines. These are
the single source of truth the published JSON Schemas (`schemas.analitiq.ai`) are
generated from, packaged so the validator and the pipeline engine can
validate documents against the contract **directly** (`model_validate`) instead
of re-implementing its cross-field rules.

## Install & use

```bash
pip install analitiq-contract-models
```

```python
from analitiq.contracts.endpoints import ApiEndpointDoc
from pydantic import ValidationError

try:
    ApiEndpointDoc.model_validate(doc)   # structural + cross-field, offline
except ValidationError as exc:
    ...  # every contract rule, enforced from the one source
```

Importing needs no environment: the package defaults `DOMAIN` to the public
contract host (`analitiq.ai`) before the models load.

## What it ships

Analitiq's **public contract surface** — nothing internal.

The **authored** contract: the documents you write and Analitiq validates.

- `analitiq.contracts.connector` — `Connector`
- `analitiq.contracts.connection` — `ConnectionInput`
- `analitiq.contracts.endpoints` — `ApiEndpointDoc`, `DatabaseEndpointDoc`, `DatabaseObject`
- `analitiq.contracts.type_map` — `TypeMapReadDoc`, `TypeMapWriteDoc`
- `analitiq.contracts.stream` — `StreamInput` (+ the endpoint refs)
- `analitiq.contracts.pipelines.config` — `PipelineInput`
- `analitiq.contracts.credentials_file` — `CredentialsFile`
- `analitiq.contracts.endpoint_identity` — `derive_db_endpoint_id`
- `analitiq.contracts.shared` — the shared building blocks those embed

The **API** contract: the request/response shapes of the public REST API.

- `analitiq.contracts.pipelines.data_sync` — Data Sync API: `PipelineRunRequest`,
  the run/terminate/status responses, and the public run vocabulary
  (`PublicRunStatus`, `PublicErrorCode`)

Both are shape and rules only. Nothing here knows how Analitiq runs a pipeline,
stores a run, or names a state internally — those live server-side and depend on
this package, never the reverse. Internal persistence/catalog variants
(`*Document`/`*Config`/`*Patch`/`*Read`, resolved-connector/catalog shapes,
auth-state) are **not** included.

`analitiq` is a [PEP 420 namespace](https://peps.python.org/pep-0420/) shared with
`analitiq-validator`.

## Source of truth

These modules **are** the canonical Analitiq contract models — not a copy of
them, and not rendered from one. The published JSON Schemas are generated from
the same files, so the schemas and this package cannot drift: there is only one
definition to drift from.

## Versioning

`analitiq-contract-models` is a strict validator (`extra=forbid`, closed enums), so
version bumps are coordinated: a consumer on an older minor rejects a newly-added
field a producer emits. See the publishing/versioning notes in the source repo for
the major/minor/patch semantics and the consumer rollout rule.
