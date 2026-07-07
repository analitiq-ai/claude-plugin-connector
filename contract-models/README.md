# analitiq-contract-models

The Analitiq connector-contract **Pydantic models** — the single source of truth
the published JSON Schemas (`schemas.analitiq.ai`) are generated from, packaged
so the connector validator (and any other Python tool) can validate documents
against the contract **directly** (`model_validate`) instead of re-implementing
its cross-field rules.

Ships the boto3-free `k2m.models` subset the validator needs:

- `k2m.models.connector` — `ConnectorConfig`
- `k2m.models.endpoints` — `ApiEndpointDoc`, `DatabaseEndpointDoc`
- `k2m.models.shared` — the shared building blocks those two embed

The connection / stream / pipeline models are intentionally **not** included:
they depend on `k2m.dynamodb` (boto3), and connector authoring doesn't need them.

## Source of truth

Canonical source is the **`analitiq-ai/infrastructure`** repo's `alq-models`
layer. This package is **built from that layer verbatim** on release — there is
no second, re-authored copy. The published JSON Schemas and this package are two
renderings of the same models, so they cannot drift.

## Use

```python
from k2m.models.endpoints import ApiEndpointDoc
from pydantic import ValidationError

try:
    ApiEndpointDoc.model_validate(doc)   # structural + cross-field, offline
except ValidationError as exc:
    ...  # every contract rule, enforced from the one source
```

Importing needs no environment: the package defaults `DOMAIN` to the public
contract host (`analitiq.ai`).
