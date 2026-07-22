"""Connection-document validation — the `connection` authored artifact kind.

A connection document is validated wholly against its contract model
(`ConnectionInput`, the same model the published `connection` JSON Schema is
generated from): `TypeAdapter(...).validate_python` enforces its structure *and*
every cross-field rule (the storage-map / `secret_refs` scheme rules, the
authored-top-level guard) offline, no schema fetch, no drift. There is no
cross-file or referential check a connection document needs in isolation — its
place in an assembled run is checked by the pipeline-bundle kind — so the model
IS the whole validity story, and this kind registers via `register_model_kind`.

At import this module registers its detector -> validator pair with the core
dispatch registry, so `_core` never hard-codes a connection branch — a new kind
is a new module.
"""
from __future__ import annotations

from typing import Any

from ._core import contract_model_domain, register_model_kind

# Import the contract model under the shared DOMAIN guard (the model binds the
# `$schema` host at import; see `contract_model_domain`).
with contract_model_domain():
    from pydantic import TypeAdapter
    from analitiq.contracts.connection import ConnectionInput

_CONNECTION_ADAPTER = TypeAdapter(ConnectionInput)


def is_connection_doc(doc: Any) -> bool:
    """A connection configures a connector: it carries `connector_id` and none of
    the connector/endpoint discriminators (`kind` / `operations`). Structurally
    distinct from every other authored kind — the connector-family detectors run
    first and claim their own shapes, so a `connector_id`-bearing document that is
    not one of them is a connection."""
    return (
        isinstance(doc, dict)
        and "connector_id" in doc
        and "kind" not in doc
        and "operations" not in doc
    )


register_model_kind(is_connection_doc, _CONNECTION_ADAPTER)
