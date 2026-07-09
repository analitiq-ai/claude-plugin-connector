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
# The kind + transport discriminators — the outputs of KindMapper /
# TransportTypeMapper in enum-mappers.md, restated in CLAUDE.md. Each lives as a
# `properties.<field>.const` across the per-variant `$defs`.
EXPECTED_KINDS = {"api", "database", "file", "s3", "stdout"}
EXPECTED_TRANSPORT_TYPES = {"http", "sqlalchemy", "adbc", "s3", "file", "stdout"}


# --- helpers (self-contained; no validator dependency) ---------------------


def _fetch(url: str) -> dict:
    """GET + JSON-parse a live schema.

    Raises `HTTPError` on a 4xx/5xx status, `URLError`/`OSError` on a
    connectivity failure, and `JSONDecodeError` on a non-JSON body — the caller
    (`_fetch_or_skip`) decides skip-vs-fail per exception type.
    """
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed https host)
        return json.loads(resp.read().decode("utf-8"))


def _fetch_or_skip(url: str) -> dict:
    """Fetch a live schema for a drift check, mapping failures to skip vs fail.

    A definitive negative from the host — an HTTP 4xx/5xx (the schema URL was
    moved or removed, itself a form of contract drift) or a 200 with a non-JSON
    body (proxy/captive-portal page) — **fails** the guard. Only a genuine
    connectivity loss (DNS/refused/timeout) **skips**, so a real outage shows as
    skipped rather than a false green.
    """
    try:
        return _fetch(url)
    except urllib.error.HTTPError as exc:  # host answered — 404/500 is drift, not an outage
        pytest.fail(f"schema host returned HTTP {exc.code} for {url}: {exc}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:  # connectivity loss
        pytest.skip(f"live schema unreachable: {exc}")
    except json.JSONDecodeError as exc:  # 200 with a non-JSON body
        pytest.fail(f"schema host returned non-JSON for {url}: {exc}")


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
    return _fetch_or_skip(CONNECTOR_URL)


@pytest.fixture(scope="module")
def api_endpoint_schema() -> dict:
    return _fetch_or_skip(API_ENDPOINT_URL)


@pytest.fixture(scope="module")
def canonical_types_schema() -> dict:
    return _fetch_or_skip(CANONICAL_TYPES_URL)


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


@pytest.mark.network
def test_kinds_match_schema(connector_schema: dict) -> None:
    schema_set = _const_types(connector_schema, "Connector", const_field="kind")
    assert schema_set == EXPECTED_KINDS, _diff_msg(
        "connector.kind",
        schema_set,
        EXPECTED_KINDS,
        "update CLAUDE.md ('kind (one of ...)') and KindMapper in "
        "skills/connector-builder/references/enum-mappers.md.",
    )


@pytest.mark.network
def test_transport_types_match_schema(connector_schema: dict) -> None:
    schema_set = _const_types(connector_schema, "Transport", const_field="transport_type")
    assert schema_set == EXPECTED_TRANSPORT_TYPES, _diff_msg(
        "transport.transport_type",
        schema_set,
        EXPECTED_TRANSPORT_TYPES,
        "update TransportTypeMapper in "
        "skills/connector-builder/references/enum-mappers.md.",
    )
