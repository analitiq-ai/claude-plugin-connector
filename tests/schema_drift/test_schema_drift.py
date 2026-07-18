"""Drift-check CI for schema-owned enums the plugin restates as decision logic.

A handful of enums can't simply be deleted from the plugin: they ARE the
mapping logic (`enum-mappers.md` maps researched provider facts onto schema
enum values; `ProviderFacts` classifies into them; `CLAUDE.md` documents the
closed sets). Per the drift policy, anything that must stay duplicated is pinned
to the contract here. If the contract's enum changes, the matching test fails
and names the divergence, so the prose + mappers are updated in the same change
instead of silently drifting.

This guard reads the enums straight from the **pinned contract models**
(`analitiq-contract-models`) — the same models the `connector-schema-validator`
agent validates against, and the same ones the published JSON Schemas are
generated from. Each document's JSON Schema is generated locally (pydantic
`json_schema()`) and its enum sets compared. That makes the guard **offline,
CDN-free, and self-consistent with the validator**: it pins the plugin's prose
to the exact contract the plugin enforces at authoring time, not a
separately-hosted copy that can drift, 403, or 404.

The package is pinned — by CI and by the `connector-schema-validator` agent — to
`analitiq-validator==1.0.0rc10` / `analitiq-contract-models==1.0.0rc10`. When it
isn't installed the whole module is skipped (offline-dev convenience); CI
installs it, so CI runs the checks for real. Run `-rs` to print skip reasons.
"""

from __future__ import annotations

import pytest

# Read the SAME contract models the validator validates against. Skip the module
# if the package isn't installed rather than erroring — an offline dev run
# without it can't perform the check; CI installs the pin, so CI runs for real.
pytest.importorskip(
    "analitiq.contracts",
    reason=(
        "analitiq-contract-models not installed — run "
        "`pip install --pre analitiq-validator==1.0.0rc10` to run the drift guards"
    ),
)

from pydantic import TypeAdapter  # noqa: E402  (import gated by importorskip above)
from analitiq.contracts.connector import Connector  # noqa: E402
from analitiq.contracts.endpoints import ApiEndpointDoc  # noqa: E402

# --- plugin-side expected sets ---------------------------------------------
# These mirror the schema-owned enums restated across CLAUDE.md and
# enum-mappers.md. (io-contracts.md restates the pagination set verbatim and an
# auth `family` set that is intentionally a SUBSET — its API `auth_model.family`
# omits `db`, which never applies to an API.) When a test below fails, update
# BOTH the prose and the matching expected set here in the same change.

EXPECTED_AUTH_TYPES = {
    "api_key",
    "basic_auth",
    "oauth2_authorization_code",
    "oauth2_client_credentials",
    "jwt",
    "db",
    "credentials",
    "aws_iam",
    "none",
}
EXPECTED_ADBC_DRIVERS = {"postgresql", "snowflake", "bigquery"}
EXPECTED_DSN_ENCODINGS = {
    "raw",
    "host",
    "url_userinfo",
    "url_path_segment",
    "url_query_key",
    "url_query_value",
}
EXPECTED_PAGINATION_STYLES = {"offset", "page", "cursor", "link", "keyset"}
# WriteOperation.idempotency `in` targets (api-endpoint ≥ 9.1.0,
# infrastructure#890) — restated in io-contracts.md EndpointFacts,
# endpoint-creator.md, connector-provider-researcher.md, and
# connector-spec-api/SKILL.md.
EXPECTED_IDEMPOTENCY_TARGETS = {"header", "body"}
# Bare-marker arrow_type vocabulary enforced by the endpoint-annotations
# semantic check (Object→properties, List→items, Json→neither). Owned by the
# api-endpoint contract's `_ARROW_AUTHORED_SHAPE` (the same alternation the
# `arrow_type` pattern embeds); the contract model leaves the sibling-key
# contract open, so the validator enforces it — keep this set in lockstep with
# the prose.
EXPECTED_BARE_MARKER_ARROW_TYPES = {"Object", "List", "Json"}
# The kind + transport discriminators — the outputs of KindMapper /
# TransportTypeMapper in enum-mappers.md, restated in CLAUDE.md. Each lives as a
# `properties.<field>.const` across the per-variant `$defs`. The contract admits
# `nosql` / `document` alongside the storage stubs; the plugin recognizes them
# in its vocabulary but authors none (KindMapper still routes document DBs →
# `database`), the same posture it holds for `file` / `s3` / `stdout`.
EXPECTED_KINDS = {"api", "database", "nosql", "document", "file", "s3", "stdout"}
EXPECTED_TRANSPORT_TYPES = {"http", "sqlalchemy", "adbc", "s3", "file", "stdout"}


# --- helpers ---------------------------------------------------------------


def _schema(model_or_type) -> dict:
    """Generate a contract document's JSON Schema from its pinned model.

    `ApiEndpointDoc` is a `BaseModel` subclass (has `model_json_schema`); the
    connector contract is a discriminated `Union` (not a class), so it goes
    through `TypeAdapter`. Same generation path that produces the published
    schema, so the `$defs` layout the extractors below walk is identical.
    """
    if isinstance(model_or_type, type):
        return model_or_type.model_json_schema()
    return TypeAdapter(model_or_type).json_schema()


def _enum_at(schema: dict, *path: str) -> set[str] | None:
    """Return the `enum` set at `$defs/.../<path>`, or None if the path/enum is absent.

    Tolerates a restructured schema: any missing key, a non-dict node
    mid-traversal, or an `enum` that isn't a list yields None (the caller turns
    that into an explicit "schema was restructured" failure).
    """
    node: object = schema
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    if not isinstance(node, dict):
        return None
    enum = node.get("enum")
    if not isinstance(enum, list):
        return None
    return set(enum)


def _const_types(schema: dict, def_suffix: str, const_field: str = "type") -> set[str]:
    """Collect the `<const_field>` const across `$defs/*<suffix>` definitions.

    Auth families, pagination styles, connector kinds, and transport types are
    each modelled as one discriminated `$def` per variant (e.g. `ApiKeyAuth`,
    `CursorPagination`, `DatabaseConnector`, `AdbcTransport`), pinning
    `properties.<const_field>.const` — not a single flat enum. The discriminator
    field name varies: `type` for auth/pagination, `kind` for connectors,
    `transport_type` for transports.
    """
    out: set[str] = set()
    for name, node in schema.get("$defs", {}).items():
        if name.endswith(def_suffix) and isinstance(node, dict):
            const_node = (node.get("properties") or {}).get(const_field) or {}
            if "const" in const_node:
                out.add(const_node["const"])
    return out


def _bare_marker_arrow_types() -> set[str] | None:
    """The authored-shape container markers, read from the pinned model.

    `analitiq.contracts.endpoints._ARROW_AUTHORED_SHAPE` is the single
    definition of the marker alternation (`"Object|List|Json"`) — the same
    constant the api-endpoint `arrow_type` pattern is built from and the
    endpoint-annotations validator keys off. Returns None if the symbol is gone
    (renamed upstream), which the caller surfaces as a restructure failure.
    """
    from analitiq.contracts import endpoints

    raw = getattr(endpoints, "_ARROW_AUTHORED_SHAPE", None)
    if not isinstance(raw, str) or not raw:
        return None
    return set(raw.split("|"))


def _diff_msg(label: str, schema_set: set[str] | None, expected: set[str], fix: str) -> str:
    if schema_set is None:
        return (
            f"{label}: enum not found at the expected pointer — the contract was "
            f"restructured. {fix}"
        )
    return (
        f"{label} drift — {fix} "
        f"schema-only={sorted(schema_set - expected)} "
        f"plugin-only={sorted(expected - schema_set)}"
    )


# ---------------------------------------------------------------------------
# Contract-model drift checks (offline) — the enum sets are read from the
# pinned `analitiq-contract-models` package, generated fresh each run.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def connector_schema() -> dict:
    return _schema(Connector)


@pytest.fixture(scope="module")
def api_endpoint_schema() -> dict:
    return _schema(ApiEndpointDoc)


def test_auth_types_match_schema(connector_schema: dict) -> None:
    schema_set = _const_types(connector_schema, "Auth")
    assert schema_set == EXPECTED_AUTH_TYPES, _diff_msg(
        "auth.type",
        schema_set,
        EXPECTED_AUTH_TYPES,
        "update CLAUDE.md '## Supported Auth Types' and AuthTypeMapper in "
        "src/skills/connector-builder/references/enum-mappers.md.",
    )


def test_adbc_drivers_match_schema(connector_schema: dict) -> None:
    schema_set = _enum_at(
        connector_schema, "$defs", "AdbcTransport", "properties", "driver"
    )
    assert schema_set == EXPECTED_ADBC_DRIVERS, _diff_msg(
        "AdbcTransport.driver",
        schema_set,
        EXPECTED_ADBC_DRIVERS,
        "update the driver-selection guidance (enum-mappers.md, "
        "spec-driver-selection.md).",
    )


def test_dsn_encodings_match_schema(connector_schema: dict) -> None:
    schema_set = _enum_at(
        connector_schema, "$defs", "DsnBinding", "properties", "encoding"
    )
    assert schema_set == EXPECTED_DSN_ENCODINGS, _diff_msg(
        "DsnBinding.encoding",
        schema_set,
        EXPECTED_DSN_ENCODINGS,
        "update spec-dsn-bindings.md + CLAUDE.md.",
    )


def test_idempotency_targets_match_schema(api_endpoint_schema: dict) -> None:
    schema_set = _enum_at(
        api_endpoint_schema, "$defs", "Idempotency", "properties", "in"
    )
    assert schema_set == EXPECTED_IDEMPOTENCY_TARGETS, _diff_msg(
        "idempotency.in",
        schema_set,
        EXPECTED_IDEMPOTENCY_TARGETS,
        "update io-contracts.md EndpointFacts.idempotency, endpoint-creator.md, "
        "connector-provider-researcher.md, and connector-spec-api/SKILL.md.",
    )


def test_pagination_styles_match_schema(api_endpoint_schema: dict) -> None:
    schema_set = _const_types(api_endpoint_schema, "Pagination")
    assert schema_set == EXPECTED_PAGINATION_STYLES, _diff_msg(
        "pagination style",
        schema_set,
        EXPECTED_PAGINATION_STYLES,
        "update io-contracts.md ProviderFacts and spec-pagination.md.",
    )


def test_bare_marker_arrow_types_match_schema() -> None:
    schema_set = _bare_marker_arrow_types()
    assert schema_set == EXPECTED_BARE_MARKER_ARROW_TYPES, _diff_msg(
        "authored_shape_type",
        schema_set,
        EXPECTED_BARE_MARKER_ARROW_TYPES,
        "update CLAUDE.md and the connector-schema-validator endpoint-annotations row.",
    )


def test_kinds_match_schema(connector_schema: dict) -> None:
    schema_set = _const_types(connector_schema, "Connector", const_field="kind")
    assert schema_set == EXPECTED_KINDS, _diff_msg(
        "connector.kind",
        schema_set,
        EXPECTED_KINDS,
        "update CLAUDE.md ('kind (one of ...)') and KindMapper in "
        "src/skills/connector-builder/references/enum-mappers.md.",
    )


def test_transport_types_match_schema(connector_schema: dict) -> None:
    schema_set = _const_types(connector_schema, "Transport", const_field="transport_type")
    assert schema_set == EXPECTED_TRANSPORT_TYPES, _diff_msg(
        "transport.transport_type",
        schema_set,
        EXPECTED_TRANSPORT_TYPES,
        "update TransportTypeMapper in "
        "src/skills/connector-builder/references/enum-mappers.md.",
    )
