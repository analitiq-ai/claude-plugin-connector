"""Drift-check CI for schema-owned enums the plugin restates as decision logic.

A handful of enums can't simply be deleted from the plugin: they ARE the
mapping logic (`enum-mappers.md` maps researched provider facts onto schema
enum values; `ProviderFacts` classifies into them; `CLAUDE.md` documents the
closed sets). Per the drift policy
(`docs/design/contract-derived-research.md` §2), anything that must stay
duplicated is pinned to the live schema here. If the published schema's enum
changes, the matching test fails and names the divergence, so the prose +
mappers are updated in the same change instead of silently drifting.

The live-schema tests fetch the published schemas FRESH (`cache=False`) so a
warm disk cache can't mask real drift, and are marked `@pytest.mark.network`
like the rest of the suite's live-fetch tests (run `-m "not network"` to skip
them offline). The remaining tests exercise the validator's encoding-enum
derivation offline. Companion inventory: `docs/design/drift-audit.md`.
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import validate_connector as v  # noqa: E402

CONNECTOR_URL = v.CONNECTOR_SCHEMA_URL
API_ENDPOINT_URL = "https://schemas.analitiq.ai/api-endpoint/latest.json"

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
        return v.fetch_schema(CONNECTOR_URL, cache=False)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"live connector schema unreachable: {exc}")


@pytest.fixture(scope="module")
def api_endpoint_schema() -> dict:
    try:
        return v.fetch_schema(API_ENDPOINT_URL, cache=False)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.skip(f"live api-endpoint schema unreachable: {exc}")


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
    schema_set = v._enum_at(
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
    schema_set = v._enum_at(
        connector_schema, "$defs", "DsnBinding", "properties", "encoding"
    )
    assert schema_set == EXPECTED_DSN_ENCODINGS, _diff_msg(
        "DsnBinding.encoding",
        schema_set,
        EXPECTED_DSN_ENCODINGS,
        "update spec-dsn-bindings.md + CLAUDE.md.",
    )
    # The validator must derive (not fall back to) the same set it enforces.
    enum, derived_from_live = v.known_encodings()
    assert derived_from_live is True, "known_encodings() fell back instead of deriving from live schema"
    assert enum == frozenset(EXPECTED_DSN_ENCODINGS)


@pytest.mark.network
def test_pagination_styles_match_schema(api_endpoint_schema: dict) -> None:
    schema_set = _const_types(api_endpoint_schema, "Pagination")
    assert schema_set == EXPECTED_PAGINATION_STYLES, _diff_msg(
        "pagination style",
        schema_set,
        EXPECTED_PAGINATION_STYLES,
        "update io-contracts.md ProviderFacts and spec-pagination.md.",
    )


# ---------------------------------------------------------------------------
# Offline behaviour of the encoding-enum derivation + its wiring into the
# dsn-binding check. No network: these monkeypatch the fetch or use the cache.
# ---------------------------------------------------------------------------


def _dsn_doc(encoding: str) -> dict:
    """Minimal connector doc that reaches the dsn-binding encoding check."""
    return {
        "transports": {
            "main": {
                "dsn": {
                    "kind": "url_template",
                    "template": "postgresql://{user}@host/db",
                    "bindings": {
                        "user": {
                            "value": {"ref": "secrets.user"},
                            "encoding": encoding,
                        }
                    },
                }
            }
        }
    }


def _dsn_doc_multi(enc_a: str, enc_b: str) -> dict:
    """Two-binding doc, to prove the offline warning fires once, not per binding."""
    return {
        "transports": {
            "main": {
                "dsn": {
                    "kind": "url_template",
                    "template": "postgresql://{user}:{pw}@host/db",
                    "bindings": {
                        "user": {"value": {"ref": "secrets.user"}, "encoding": enc_a},
                        "pw": {"value": {"ref": "secrets.pw"}, "encoding": enc_b},
                    },
                }
            }
        }
    }


def test_fallback_set_equals_expected() -> None:
    # The offline fallback must not drift from the contract on its own.
    assert v._FALLBACK_ENCODINGS == EXPECTED_DSN_ENCODINGS


def test_enum_at_returns_none_on_broken_paths() -> None:
    schema = {"a": {"b": {"enum": ["x", "y"]}}, "c": {"enum": "notalist"}, "s": "str"}
    assert v._enum_at(schema, "a", "b") == {"x", "y"}
    assert v._enum_at(schema, "a", "missing") is None  # key absent (shallow)
    assert v._enum_at(schema, "a", "b", "deeper") is None  # key absent (deeper level)
    assert v._enum_at(schema, "s", "x") is None  # mid-traversal node is not a dict
    assert v._enum_at(schema, "s") is None  # final node is not a dict
    assert v._enum_at(schema, "c") is None  # enum present but not a list
    assert v._enum_at({}, "x") is None


def test_known_encodings_falls_back_when_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(v, "fetch_schema", boom)
    enum, derived_from_live = v.known_encodings()
    assert derived_from_live is False
    assert enum == frozenset(v._FALLBACK_ENCODINGS)


def test_known_encodings_falls_back_when_pointer_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Schema fetched fine but the enum moved → fall back AND flag it, don't crash.
    monkeypatch.setattr(v, "fetch_schema", lambda *_a, **_k: {"$defs": {}})
    enum, derived_from_live = v.known_encodings()
    assert derived_from_live is False
    assert enum == frozenset(v._FALLBACK_ENCODINGS)


def test_dsn_binding_rejects_unknown_encoding() -> None:
    # The reject path must actually be wired: a bogus encoding is an error,
    # a valid one is not.
    bad = v.check_dsn_bindings(_dsn_doc("url_query"))  # typo for url_query_value
    enc_errors = [
        f
        for f in bad
        if f["validator"] == "dsn-binding"
        and f["severity"] == "error"
        and f["path"].endswith("/encoding")
    ]
    assert enc_errors, "an out-of-enum encoding must be rejected"

    good = v.check_dsn_bindings(_dsn_doc("url_userinfo"))
    assert not [
        f for f in good if f["severity"] == "error" and f["path"].endswith("/encoding")
    ]


def test_dsn_binding_warns_but_accepts_valid_encoding_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Offline: a VALID encoding still passes (via fallback), but the run records
    # a warning that the enum wasn't derived from the live schema.
    def boom(*_a, **_k):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(v, "fetch_schema", boom)
    findings = v.check_dsn_bindings(_dsn_doc("url_userinfo"))
    assert not [f for f in findings if f["severity"] == "error"]
    warnings = [
        f
        for f in findings
        if f["validator"] == "dsn-binding"
        and f["severity"] == "warning"
        and "could not be derived from the live" in f["message"]
    ]
    assert len(warnings) == 1, "exactly one offline-fallback warning expected"


def test_offline_warning_emitted_once_across_multiple_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The `offline_encoding_warned` guard must dedupe: one warning for the doc,
    # not one per encoded binding.
    def boom(*_a, **_k):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(v, "fetch_schema", boom)
    findings = v.check_dsn_bindings(_dsn_doc_multi("url_userinfo", "url_query_value"))
    warnings = [
        f for f in findings if "could not be derived from the live" in f["message"]
    ]
    assert len(warnings) == 1, "warn once per document, not per binding"


def test_no_offline_warning_when_derived_from_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the enum IS derived from the (mocked) live schema, no fallback warning.
    live = {
        "$defs": {
            "DsnBinding": {
                "properties": {"encoding": {"enum": sorted(EXPECTED_DSN_ENCODINGS)}}
            }
        }
    }
    monkeypatch.setattr(v, "fetch_schema", lambda *_a, **_k: live)
    enum, derived_from_live = v.known_encodings()
    assert derived_from_live is True
    findings = v.check_dsn_bindings(_dsn_doc("url_userinfo"))
    assert not [
        f for f in findings if "could not be derived from the live" in f["message"]
    ]
