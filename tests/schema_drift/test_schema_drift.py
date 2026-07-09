"""Drift-check CI for schema-owned enums the plugin restates as decision logic.

A handful of enums can't simply be deleted from the plugin: they ARE the
mapping logic (`enum-mappers.md` maps researched provider facts onto schema
enum values; `ProviderFacts` classifies into them; `CLAUDE.md` documents the
closed sets). Per the drift policy, anything that must stay duplicated is pinned
to the live published schema here. If the published schema's enum changes, the
matching test fails and names the divergence, so the prose + mappers are updated
in the same change instead of silently drifting.

This guard is deliberately **validator-independent**: it fetches the live
published schema directly (stdlib `urllib`) and compares enum sets. The
connector validator is now an external package (`analitiq-validator`, tested
upstream); the plugin does not import it. Every test here is
`@pytest.mark.network` and fetches FRESH so a warm cache can't hide drift; run
`-m "not network"` to skip them offline.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

CONNECTOR_URL = "https://schemas.analitiq.ai/connector/latest.json"
API_ENDPOINT_URL = "https://schemas.analitiq.ai/api-endpoint/latest.json"
CANONICAL_TYPES_URL = "https://schemas.analitiq.ai/canonical-types.json"

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
# semantic check (Object→properties, List→items, Json→neither). Owned by
# canonical-types.json `$defs/authored_shape_type` (and accepted by the
# api-endpoint `arrow_type` pattern); the published schema does NOT enforce the
# siblings, so the validator must — keep this set in lockstep with the prose.
EXPECTED_BARE_MARKER_ARROW_TYPES = {"Object", "List", "Json"}


# --- helpers (self-contained; no validator dependency) ---------------------


def _fetch(url: str) -> dict:
    """GET + parse a live schema. Raises on network/parse failure (callers skip)."""
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed https host)
        return json.loads(resp.read().decode("utf-8"))


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


def _const_types(schema: dict, def_suffix: str) -> set[str]:
    """Collect the `type` const across `$defs/*<suffix>` definitions.

    Auth families and pagination styles are modelled as one discriminated
    `$def` per variant (e.g. `ApiKeyAuth`, `CursorPagination`), each pinning
    `properties.type.const` — not a single flat enum.
    """
    out: set[str] = set()
    for name, node in schema.get("$defs", {}).items():
        if name.endswith(def_suffix) and isinstance(node, dict):
            type_node = (node.get("properties") or {}).get("type") or {}
            if "const" in type_node:
                out.add(type_node["const"])
    return out


def _diff_msg(label: str, schema_set: set[str] | None, expected: set[str], fix: str) -> str:
    if schema_set is None:
        return (
            f"{label}: enum not found at the expected pointer — the schema was "
            f"restructured. {fix}"
        )
    return (
        f"{label} drift — {fix} "
        f"schema-only={sorted(schema_set - expected)} "
        f"plugin-only={sorted(expected - schema_set)}"
    )


# ---------------------------------------------------------------------------
# Live-schema drift checks (network) — fetch fresh so a warm cache can't hide
# drift; deselect offline with `-m "not network"`.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def connector_schema() -> dict:
    try:
        return _fetch(CONNECTOR_URL)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"live connector schema unreachable: {exc}")


@pytest.fixture(scope="module")
def api_endpoint_schema() -> dict:
    try:
        return _fetch(API_ENDPOINT_URL)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"live api-endpoint schema unreachable: {exc}")


@pytest.fixture(scope="module")
def canonical_types_schema() -> dict:
    try:
        return _fetch(CANONICAL_TYPES_URL)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"live canonical-types schema unreachable: {exc}")


@pytest.mark.network
def test_auth_types_match_schema(connector_schema: dict) -> None:
    schema_set = _const_types(connector_schema, "Auth")
    assert schema_set == EXPECTED_AUTH_TYPES, _diff_msg(
        "auth.type",
        schema_set,
        EXPECTED_AUTH_TYPES,
        "update CLAUDE.md '## Supported Auth Types' and AuthTypeMapper in "
        "skills/connector-builder/references/enum-mappers.md.",
    )


@pytest.mark.network
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


@pytest.mark.network
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


@pytest.mark.network
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


@pytest.mark.network
def test_pagination_styles_match_schema(api_endpoint_schema: dict) -> None:
    schema_set = _const_types(api_endpoint_schema, "Pagination")
    assert schema_set == EXPECTED_PAGINATION_STYLES, _diff_msg(
        "pagination style",
        schema_set,
        EXPECTED_PAGINATION_STYLES,
        "update io-contracts.md ProviderFacts and spec-pagination.md.",
    )


@pytest.mark.network
def test_bare_marker_arrow_types_match_schema(canonical_types_schema: dict) -> None:
    schema_set = _enum_at(canonical_types_schema, "$defs", "authored_shape_type")
    assert schema_set == EXPECTED_BARE_MARKER_ARROW_TYPES, _diff_msg(
        "authored_shape_type",
        schema_set,
        EXPECTED_BARE_MARKER_ARROW_TYPES,
        "update CLAUDE.md and the connector-schema-validator endpoint-annotations row.",
    )
