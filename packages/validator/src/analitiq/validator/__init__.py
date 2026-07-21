"""analitiq.validator — the Analitiq artifact validator.

Validates Analitiq connector / endpoint / type-map / connection / stream /
pipeline JSON documents against the **contract models**
(`analitiq-contract-models`) plus the cross-file coverage and advisory checks a
single-document model cannot express, and validates an assembled **pipeline
bundle** for cross-document referential integrity. Output is a JSON report
(`{"passed": bool, "findings": [...]}`); the CLI exits non-zero on any
error-severity finding.

Importing this package pulls in the per-kind modules (`connectors`, `pipelines`,
`connections`, `streams`), each of which self-registers its detector→validator
pairs (and its validator ids) with the core dispatch registry — a new kind is a
new module registering the same way, without touching `_core`. The public surface
is re-exported here.
"""
from ._core import finding, main, validate_document, VALIDATOR_IDS
from . import connectors  # noqa: F401  — imported for its self-registration side effect
from . import pipelines  # noqa: F401  — imported for its self-registration side effect
from . import connections  # noqa: F401  — imported for its self-registration side effect
from . import streams  # noqa: F401  — imported for its self-registration side effect
from .connectors import (
    check_coverage,
    endpoint_filename_findings,
    is_api_endpoint_doc,
    is_connector_doc,
    is_database_endpoint_doc,
    is_stem_addressed_endpoint_path,
    _canonical_eq,
    _collect_native_arrow_pairs,
    _database_endpoint_locator_findings,
    _endpoint_locator_findings,
    _flatten_api_locator,
    _render_canonical,
)
from .pipelines import is_pipeline_bundle, is_pipeline_doc, validate_pipeline_bundle
from .connections import is_connection_doc
from .streams import is_stream_doc

__all__ = [
    "finding",
    "main",
    "validate_document",
    "VALIDATOR_IDS",
    "check_coverage",
    "endpoint_filename_findings",
    "is_stem_addressed_endpoint_path",
    "is_api_endpoint_doc",
    "is_connector_doc",
    "is_database_endpoint_doc",
    "is_connection_doc",
    "is_stream_doc",
    "is_pipeline_doc",
    "is_pipeline_bundle",
    "validate_pipeline_bundle",
]
