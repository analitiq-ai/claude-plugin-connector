"""Tests for validator/src/analitiq_connector_validator.py.

By default these tests run with `--semantic-only` so they don't depend on
network access to the live schema host. There is one explicit Layer-1
network test that fetches the real schema; it is marked so CI can skip
it offline.

Run all: `pytest tests/connector_validator/`
Run offline only: `pytest tests/connector_validator/ -m "not network"`
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "validator" / "src" / "analitiq_connector_validator.py"
FIXTURES = Path(__file__).parent / "fixtures"
VALID_API_CONNECTOR = FIXTURES / "valid_api_connector" / "connector.json"
EXAMPLES_GLOB = list(REPO_ROOT.glob("skills/connector-spec-*/examples/*/*.example.json"))
ENDPOINT_EXAMPLES_GLOB = list(REPO_ROOT.glob("skills/connector-spec-*/examples/*/endpoints/*.json"))
SCHEMA_URL = "https://schemas.analitiq.ai/connector/latest.json"
API_ENDPOINT_SCHEMA_URL = "https://schemas.analitiq.ai/api-endpoint/latest.json"
TYPE_MAP_READ_SCHEMA_URL = "https://schemas.analitiq.ai/type-map-read/latest.json"
TYPE_MAP_WRITE_SCHEMA_URL = "https://schemas.analitiq.ai/type-map-write/latest.json"

# Reference db packages ship a read map and a write map alongside the
# connector body. The network tests below exercise the Layer-1 schema-fetch
# path for both type-map directions against the live published schemas.
_DB_EXAMPLE_DIRS = sorted(
    d for d in (REPO_ROOT / "skills/connector-spec-db/examples").iterdir() if d.is_dir()
)
EXAMPLE_READ_MAPS = [d / "type-map-read.json" for d in _DB_EXAMPLE_DIRS if (d / "type-map-read.json").is_file()]
EXAMPLE_WRITE_MAPS = [d / "type-map-write.json" for d in _DB_EXAMPLE_DIRS if (d / "type-map-write.json").is_file()]


def run_validator(document_path: Path, *extra: str, schema_url: str = SCHEMA_URL) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--schema-url", schema_url, "--document", str(document_path), *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(proc.stdout)


def errors_of(result: dict, validator_id: str) -> list[dict]:
    return [f for f in result["findings"] if f["validator"] == validator_id and f["severity"] == "error"]


def warnings_of(result: dict, validator_id: str) -> list[dict]:
    return [f for f in result["findings"] if f["validator"] == validator_id and f["severity"] == "warning"]


# ---------------------------------------------------------------------------
# Layer 1 — JSON Schema (network)
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_layer1_valid_api_connector_passes_against_live_schema():
    """Single network test that exercises the schema fetch path.

    All other tests run with --semantic-only and are offline-safe.
    """
    result = run_validator(VALID_API_CONNECTOR)
    error_findings = [f for f in result["findings"] if f["severity"] == "error"]
    assert not error_findings, f"unexpected errors: {error_findings}"
    assert result["passed"] is True


@pytest.mark.network
@pytest.mark.parametrize("read_map", EXAMPLE_READ_MAPS, ids=lambda p: p.parent.name)
def test_layer1_example_read_map_passes_against_live_schema(read_map):
    """Reference read maps must validate against the published
    type-map-read schema — full Layer 1 + Layer 2, no --semantic-only."""
    result = run_validator(read_map, schema_url=TYPE_MAP_READ_SCHEMA_URL)
    errors = [f for f in result["findings"] if f["severity"] == "error"]
    assert not errors, f"{read_map.parent.name} read map: {errors}"


@pytest.mark.network
@pytest.mark.parametrize("write_map", EXAMPLE_WRITE_MAPS, ids=lambda p: p.parent.name)
def test_layer1_example_write_map_passes_against_live_schema(write_map):
    """Reference write maps must validate against the published
    type-map-write schema. This is the direction that previously had no
    published schema and ran --semantic-only; it now gets full Layer 1."""
    result = run_validator(write_map, schema_url=TYPE_MAP_WRITE_SCHEMA_URL)
    errors = [f for f in result["findings"] if f["severity"] == "error"]
    assert not errors, f"{write_map.parent.name} write map: {errors}"


@pytest.mark.network
def test_layer1_malformed_write_map_rejected_against_live_schema(tmp_path):
    """The published write schema must actually constrain shape, not merely be
    fetchable — a malformed write map is rejected at Layer 1. Without this, the
    positive write-map test above could stay green against a no-op/over-permissive
    or mis-referenced schema."""
    bad = tmp_path / "type-map-write.json"
    # `match` outside the enum and `native` (the render side) missing.
    bad.write_text(json.dumps([{"match": "glob", "canonical": "Boolean"}]))
    result = run_validator(bad, schema_url=TYPE_MAP_WRITE_SCHEMA_URL)
    # A real schema rejection points into the document (e.g. "/0", "/0/match");
    # a schema-fetch failure carries the same validator id but an empty path.
    # Require a document-anchored error so a misconfigured fetch can't pass as
    # enforcement.
    schema_errors = [
        f for f in result["findings"]
        if f["validator"] == "json-schema" and f["path"].startswith("/")
    ]
    assert schema_errors, f"expected a Layer-1 schema rejection into the document; got {result['findings']}"
    assert result["passed"] is False


def _write_endpoint_with_idempotency(tmp_path, *, idempotency, batching=None, body=None):
    mode = {
        "request": {"method": "POST", "path": "/contacts"},
        "input": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}},
        "idempotency": idempotency,
    }
    if batching is not None:
        mode["batching"] = batching
    if body is not None:
        mode["request"]["body"] = body
    doc = {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": "contacts",
        "operations": {"write": {"insert": mode}},
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "contacts.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.mark.network
def test_layer1_idempotency_constraints_enforced_by_live_schema(tmp_path):
    """The published api-endpoint schema must enforce the two structural
    idempotency constraints the plugin's prose restates (endpoint-creator.md,
    connector-spec-api/SKILL.md): mutual exclusion with `batching`, and
    `in: "body"` requiring a JSON-object `request.body`. The drift suite pins
    only the `in` enum; if the schema relaxed either constraint this would
    stay green vacuously while the prose claims enforcement."""
    def schema_errors(result):
        # Document-anchored errors only — a schema-fetch failure carries the
        # same validator id but an empty path (see the write-map test above).
        return [
            f for f in result["findings"]
            if f["validator"] == "json-schema" and f["path"].startswith("/")
        ]

    header_key = {"in": "header", "name": "Idempotency-Key"}
    body_key = {"in": "body", "name": "idempotency_key"}

    ok = run_validator(
        _write_endpoint_with_idempotency(tmp_path / "ok", idempotency=header_key),
        "--json-only", schema_url=API_ENDPOINT_SCHEMA_URL,
    )
    assert ok["passed"] is True, f"header idempotency alone must validate; got {ok['findings']}"

    both = run_validator(
        _write_endpoint_with_idempotency(
            tmp_path / "both", idempotency=header_key, batching={"max_records": 100}
        ),
        "--json-only", schema_url=API_ENDPOINT_SCHEMA_URL,
    )
    assert schema_errors(both), f"idempotency + batching must be rejected; got {both['findings']}"

    body_no_body = run_validator(
        _write_endpoint_with_idempotency(tmp_path / "nobody", idempotency=body_key),
        "--json-only", schema_url=API_ENDPOINT_SCHEMA_URL,
    )
    assert schema_errors(body_no_body), (
        f"body placement without a JSON-object request.body must be rejected; got {body_no_body['findings']}"
    )

    body_ok = run_validator(
        _write_endpoint_with_idempotency(
            tmp_path / "bodyok", idempotency=body_key, body={"name": {"ref": "stream.record.name"}}
        ),
        "--json-only", schema_url=API_ENDPOINT_SCHEMA_URL,
    )
    assert body_ok["passed"] is True, (
        f"body placement with a JSON-object request.body must validate; got {body_ok['findings']}"
    )


def test_db_example_maps_present():
    """Guard against the network parametrize collapsing to zero cases. The
    examples are a small set of diverse archetypes (sqlalchemy + adbc), not one
    per provider — per-provider maps are derived from research at authoring
    time (see spec-type-maps.md)."""
    assert len(EXAMPLE_READ_MAPS) >= 2, f"expected ≥ 2 example read maps, found {EXAMPLE_READ_MAPS}"
    assert len(EXAMPLE_WRITE_MAPS) >= 2, f"expected ≥ 2 example write maps, found {EXAMPLE_WRITE_MAPS}"


def test_schema_fetch_failure_is_diagnosed():
    bad_url = "http://127.0.0.1:1/nonexistent.json"
    result = run_validator(VALID_API_CONNECTOR, schema_url=bad_url)
    fetch_errors = [f for f in result["findings"] if f["validator"] == "json-schema" and "fetch" in f["message"].lower()]
    assert fetch_errors, f"expected a schema-fetch finding; got {result['findings']}"
    assert result["passed"] is False


# ---------------------------------------------------------------------------
# Reference examples — integration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("example", EXAMPLES_GLOB, ids=lambda p: p.name)
def test_reference_example_passes_semantic_validation(example):
    """Every shipped reference example must pass semantic validation.

    Layer 1 (JSON Schema) is exercised at build time by the dev workflow
    against the live schema; this test stays offline-safe.
    """
    result = run_validator(example, "--semantic-only")
    errors = [f for f in result["findings"] if f["severity"] == "error"]
    assert not errors, f"{example.name}: {errors}"


def test_examples_glob_is_non_empty():
    """Guard against the parametrize collapsing to zero cases silently. The
    reference set is a small group of diverse archetypes (api_key /
    oauth2_authorization_code / jwt; sqlalchemy / adbc), not one per provider."""
    assert len(EXAMPLES_GLOB) >= 5, f"expected ≥ 5 reference examples, found {len(EXAMPLES_GLOB)}"


@pytest.mark.network
@pytest.mark.parametrize(
    "endpoint", ENDPOINT_EXAMPLES_GLOB, ids=lambda p: f"{p.parent.parent.name}/{p.name}"
)
def test_endpoint_example_passes_against_live_schema(endpoint):
    """Every shipped endpoint example must validate against the live
    api-endpoint schema — full Layer 1 + Layer 2.

    Previously this ran `--json-only` (Layer 1 only) because the Layer 2
    expression-resolver mis-flagged the spec-mandated response-extraction
    namespace (`response.body`) as an unknown scope on a standalone endpoint.
    With the scope check now position-aware (`response.*` accepted under an
    operation's response/pagination subtree, rejected elsewhere), standalone
    endpoints get the full semantic pass and the shipped examples must come
    out clean.
    """
    result = run_validator(endpoint, schema_url=API_ENDPOINT_SCHEMA_URL)
    errors = [f for f in result["findings"] if f["severity"] == "error"]
    assert not errors, f"{endpoint.parent.parent.name}/{endpoint.name}: {errors}"


def test_endpoint_examples_glob_is_non_empty():
    """Guard against the endpoint parametrize collapsing to zero cases."""
    assert len(ENDPOINT_EXAMPLES_GLOB) >= 3, (
        f"expected ≥ 3 endpoint examples, found {len(ENDPOINT_EXAMPLES_GLOB)}"
    )


# ---------------------------------------------------------------------------
# Layer 2 — semantic validators (offline)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_reserved_field_caught(tmp_path, field):
    base = json.loads((VALID_API_CONNECTOR).read_text())
    base[field] = "should-not-be-here"
    doc_path = tmp_path / f"reserved_{field}.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "reserved-field")
    assert len(errs) == 1, f"expected exactly one reserved-field finding for '{field}', got {result['findings']}"
    assert errs[0]["path"] == f"/{field}"


def test_unknown_scope_caught():
    result = run_validator(FIXTURES / "invalid_unknown_scope.json", "--semantic-only")
    errs = errors_of(result, "expression-resolver")
    messages = " ".join(e["message"] for e in errs)
    assert "secret.api_key" in messages or "secret.api_key" in " ".join(str(e) for e in errs), \
        f"expected unknown-scope finding for 'secret.api_key' (typo); got: {messages}"
    assert "connection.bogus" in messages, f"expected unknown sub-scope 'connection.bogus' caught; got: {messages}"
    assert "session.token" in messages, f"expected template var 'session.token' caught; got: {messages}"
    assert "hmac_sign" in messages, f"expected unknown function 'hmac_sign' caught; got: {messages}"


def test_empty_template_variable_caught(tmp_path):
    """`${}` names no scope and resolves to nothing at runtime, so it must be
    flagged rather than slip through (the `[^}]*` regex fix in #48)."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    base["transports"]["api"]["headers"]["X-Empty"] = {"template": "Bearer ${}"}
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "expression-resolver")
    empty = [e for e in errs if "empty template variable" in e["message"]]
    assert len(empty) == 1, f"expected exactly one empty-template finding; got {result['findings']}"
    # The valid `${secrets.api_key}` template in the same doc must not be flagged.
    assert not any("secrets.api_key" in e["message"] for e in errs), \
        f"valid template var wrongly flagged; got {errs}"


def test_whitespace_template_variable_caught(tmp_path):
    """`${   }` is empty after stripping — reported as empty, not as an
    unknown scope (which would be a misleading message)."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    base["transports"]["api"]["headers"]["X-Blank"] = {"template": "Bearer ${   }"}
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "expression-resolver")
    assert any("empty template variable" in e["message"] for e in errs), \
        f"expected empty-template finding for '${{   }}'; got {result['findings']}"
    assert not any("unknown scope" in e["message"] for e in errs), \
        f"whitespace var should not be reported as unknown scope; got {errs}"


def test_unclosed_template_variable_caught(tmp_path):
    """A `${` with no closing `}` is not extracted, so it would survive as a
    literal at runtime — flag it. A legitimate template with literal JSON
    braces (`{ }` not preceded by `$`) must NOT trip this."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    base["transports"]["api"]["headers"]["X-Unclosed"] = {"template": "Bearer ${secrets.api_key"}
    base["transports"]["api"]["headers"]["X-Json"] = {"template": '{"key": "${secrets.api_key}"}'}
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "expression-resolver")
    unclosed = [e for e in errs if "unclosed template variable" in e["message"]]
    # Exactly one: the dangling `${`. The literal-brace JSON template is clean.
    assert len(unclosed) == 1, f"expected exactly one unclosed finding; got {result['findings']}"


def _endpoint_with_response_refs() -> dict:
    """A spec-compliant standalone api-endpoint document whose record selector,
    response metadata, and pagination predicates use the response-extraction
    namespace (`response.body.*` and `response.headers.*`)."""
    return {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": "widgets",
        "operations": {
            "read": {
                "request": {
                    "method": "GET",
                    "path": "/widgets",
                    # A legitimate request-side ref — positive control that
                    # request slots are still validated against request scopes.
                    "headers": {"Authorization": {"template": "Bearer ${secrets.api_key}"}},
                },
                "response": {
                    "records": {"ref": "response.body.data"},
                    # `response.headers.*` is a real, spec-documented response
                    # site (e.g. Link-header pagination) — exercise that head too.
                    "metadata": {"total": {"ref": "response.headers.x-total-count"}},
                    "schema": {"type": "object"},
                },
                "pagination": {
                    "type": "cursor",
                    "cursor": {
                        "param": "cursor",
                        "next_cursor": {"ref": "response.body.next_cursor"},
                    },
                    "stop_when": {"missing": {"ref": "response.body.next_cursor"}},
                },
            }
        },
    }


def _write_endpoint_with_response_refs() -> dict:
    """A spec-compliant standalone api-endpoint with a mode-keyed `write`
    operation whose response-extraction sits one level deeper than `read`
    (`/operations/write/<mode>/response/...`)."""
    return {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": "widgets",
        "operations": {
            "write": {
                "insert": {
                    "request": {
                        "method": "POST",
                        "path": "/widgets",
                        # Positive control: a request-side ref under the write
                        # mode block is still validated against request scopes.
                        "headers": {"Authorization": {"template": "Bearer ${secrets.api_key}"}},
                    },
                    "input": {"schema": {"type": "object"}},
                    "response": {
                        "generated_keys": {"ref": "response.body.id"},
                        "affected_records": {"ref": "response.body.count"},
                    },
                }
            }
        },
    }


def test_response_extraction_refs_not_flagged(tmp_path):
    """Issue #7: response-extraction refs (`response.body*`, `response.headers*`)
    at a read operation's response/pagination sites must NOT be flagged as
    unknown scope. A compliant standalone endpoint must emit zero
    expression-resolver findings."""
    doc_path = tmp_path / "widgets.json"
    doc_path.write_text(json.dumps(_endpoint_with_response_refs()))
    result = run_validator(doc_path, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    expr_findings = [f for f in result["findings"] if f["validator"] == "expression-resolver"]
    assert expr_findings == [], (
        f"spec-compliant response-extraction refs wrongly flagged; got {expr_findings}"
    )


def test_write_response_extraction_refs_not_flagged(tmp_path):
    """Issue #7 (write half): `operations.write` is a mode-keyed map, so write
    response refs live at `/operations/write/<mode>/response/...` — one level
    deeper than `read`. The carve-out must reach them (a destination connector's
    `generated_keys`/`affected_records` use `response.body.*`), while a request
    slot inside the same mode block stays rejected."""
    doc_path = tmp_path / "widgets.json"
    doc_path.write_text(json.dumps(_write_endpoint_with_response_refs()))
    result = run_validator(doc_path, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    expr_findings = [f for f in result["findings"] if f["validator"] == "expression-resolver"]
    assert expr_findings == [], (
        f"write-operation response-extraction refs wrongly flagged; got {expr_findings}"
    )


def test_write_response_scope_rejected_in_request_slot(tmp_path):
    """Issue #7 anti-shim (write half): `response.*` placed in a write request
    slot must still error — the deeper write nesting must not widen the carve-out
    to request-construction positions."""
    doc = _write_endpoint_with_response_refs()
    doc["operations"]["write"]["insert"]["request"]["headers"]["X-Bad"] = {"ref": "response.body"}
    doc_path = tmp_path / "widgets.json"
    doc_path.write_text(json.dumps(doc))
    result = run_validator(doc_path, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "expression-resolver")
    assert any("/request/" in e["path"] for e in errs), (
        f"expected response.* in a write request slot to be flagged; got {result['findings']}"
    )
    assert not any("/response/" in e["path"] for e in errs), (
        f"write response-extraction refs wrongly flagged; got {errs}"
    )


def test_response_template_var_position_aware(tmp_path):
    """Issue #7: the carve-out is plumbed through BOTH arms of check_expressions —
    `ref` and `template`. A `${response.body.*}` template variable at a response
    site (a pagination cursor built by interpolation) must be accepted; the same
    template variable in a request slot must still error."""
    doc = {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": "widgets",
        "operations": {
            "read": {
                "request": {
                    "method": "GET",
                    "path": "/widgets",
                    "headers": {"X-Bad": {"template": "cursor=${response.body.next_cursor}"}},
                },
                "response": {
                    "records": {"ref": "response.body.data"},
                    "schema": {"type": "object"},
                },
                "pagination": {
                    "type": "cursor",
                    "cursor": {
                        "param": "cursor",
                        "next_cursor": {"template": "${response.body.next_cursor}"},
                    },
                    "stop_when": {"missing": {"ref": "response.body.next_cursor"}},
                },
            }
        },
    }
    doc_path = tmp_path / "widgets.json"
    doc_path.write_text(json.dumps(doc))
    result = run_validator(doc_path, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "expression-resolver")
    # The request-slot template var is flagged...
    assert any("/request/" in e["path"] for e in errs), (
        f"expected response.* template var in a request slot to be flagged; got {result['findings']}"
    )
    # ...but the pagination template var at the response site is NOT.
    assert not any("/pagination/" in e["path"] for e in errs), (
        f"response-site template var wrongly flagged; got {errs}"
    )


def test_response_scope_rejected_in_request_slot(tmp_path):
    """Issue #7 anti-shim: the carve-out is position-aware. `response.*` is legal
    only at response-extraction sites — in a request slot it must still error, so
    the false positive isn't traded for a false negative (a request header that
    references the not-yet-existent response)."""
    doc = {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": "widgets",
        "operations": {
            "read": {
                "request": {
                    "method": "GET",
                    "path": "/widgets",
                    "headers": {"X-Bad": {"ref": "response.body"}},
                },
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {"type": "object"},
                },
            }
        },
    }
    doc_path = tmp_path / "widgets.json"
    doc_path.write_text(json.dumps(doc))
    result = run_validator(doc_path, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "expression-resolver")
    # The request-slot ref is flagged...
    request_errs = [e for e in errs if "/request/" in e["path"]]
    assert request_errs, f"expected response.* in a request slot to be flagged; got {result['findings']}"
    # ...but the identical ref at the response record selector is NOT.
    assert not any("/response/records" in e["path"] for e in errs), (
        f"response-extraction record selector wrongly flagged; got {errs}"
    )


def test_transport_ref_caught():
    result = run_validator(FIXTURES / "invalid_transport_ref.json", "--semantic-only")
    errs = errors_of(result, "transport-ref")
    paths = sorted(e["path"] for e in errs)
    # Both default_transport and the nested authorize.transport_ref should be flagged.
    assert "/default_transport" in paths, f"expected /default_transport finding; got {paths}"
    assert any("authorize" in p and p.endswith("transport_ref") for p in paths), \
        f"expected nested authorize.transport_ref finding; got {paths}"


def test_dsn_unbound_placeholder_caught():
    result = run_validator(FIXTURES / "invalid_dsn_unbound.json", "--semantic-only")
    errs = errors_of(result, "dsn-binding")
    unbound = sorted(
        ph
        for ph in ("password", "port", "database")
        if any(ph in e["message"] for e in errs)
    )
    # The fixture omits exactly these three placeholder bindings; assert all three.
    assert unbound == ["database", "password", "port"], \
        f"expected unbound={{'password','port','database'}}, got {unbound}; findings={errs}"


def test_dsn_empty_placeholder_caught(tmp_path):
    """An empty `{}` in a DSN url_template names no binding and resolves to
    nothing at runtime — flagged rather than silently ignored (the `[^}]*`
    fix applied to the DSN `{placeholder}` markers, same bug class as `${}`)."""
    base = {
        "$schema": "https://schemas.analitiq.ai/connector/latest.json",
        "kind": "database",
        "connector_id": "fixture-dsn-empty",
        "version": "1.0.0",
        "default_transport": "db",
        "transports": {
            "db": {
                "transport_type": "sqlalchemy",
                "driver": "postgresql+asyncpg",
                "dsn": {
                    "kind": "url_template",
                    "template": "postgresql://{host}:{}/{database}",
                    "bindings": {
                        "host": {"value": {"ref": "connection.parameters.host"}, "encoding": "host"},
                        "database": {"value": {"ref": "connection.parameters.database"}, "encoding": "url_path_segment"},
                    },
                },
            }
        },
        "auth": {"type": "db"},
        "connection_contract": {
            "inputs": {
                "host": {"source": "user", "phase": "pre_auth", "storage": "connection.parameters", "type": "string", "required": True},
                "database": {"source": "user", "phase": "pre_auth", "storage": "connection.parameters", "type": "string", "required": True},
            }
        },
    }
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "dsn-binding")
    assert any(
        "empty placeholder" in e["message"] and e["path"] == "/transports/db/dsn/template"
        for e in errs
    ), f"expected empty-placeholder dsn-binding finding; got {errs}"
    # The valid `{host}`/`{database}` markers are bound, so the only dsn-binding
    # error is the empty one — not a generic 'no matching binding'.
    assert not any("has no matching binding" in e["message"] for e in errs), \
        f"valid bindings wrongly flagged as unbound; got {errs}"


def test_dsn_unclosed_brace_caught(tmp_path):
    """A `{` with no closing `}` in a DSN url_template is an unbalanced brace
    that corrupts the connection string — flagged rather than silently ignored.
    Braces are reserved for `{placeholder}` markers in a DSN, so any stray one
    is malformed."""
    base = {
        "$schema": "https://schemas.analitiq.ai/connector/latest.json",
        "kind": "database",
        "connector_id": "fixture-dsn-unclosed",
        "version": "1.0.0",
        "default_transport": "db",
        "transports": {
            "db": {
                "transport_type": "sqlalchemy",
                "driver": "postgresql+asyncpg",
                "dsn": {
                    "kind": "url_template",
                    "template": "postgresql://{host}/{database",
                    "bindings": {
                        "host": {"value": {"ref": "connection.parameters.host"}, "encoding": "host"},
                        "database": {"value": {"ref": "connection.parameters.database"}, "encoding": "url_path_segment"},
                    },
                },
            }
        },
        "auth": {"type": "db"},
        "connection_contract": {
            "inputs": {
                "host": {"source": "user", "phase": "pre_auth", "storage": "connection.parameters", "type": "string", "required": True},
                "database": {"source": "user", "phase": "pre_auth", "storage": "connection.parameters", "type": "string", "required": True},
            }
        },
    }
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "dsn-binding")
    assert any(
        "unbalanced or unclosed brace" in e["message"] and e["path"] == "/transports/db/dsn/template"
        for e in errs
    ), f"expected unclosed-brace dsn-binding finding; got {errs}"


def test_auth_shape_oauth_cc_forbidden_authorize_caught():
    result = run_validator(FIXTURES / "invalid_auth_shape_oauth_cc.json", "--semantic-only")
    errs = errors_of(result, "auth-shape")
    paths = [e["path"] for e in errs]
    assert "/auth/token_exchange" in paths, f"expected missing-token_exchange finding; got {paths}"
    assert "/auth/authorize" in paths, f"expected forbidden-authorize finding; got {paths}"


def test_tls_consistency_caught():
    result = run_validator(FIXTURES / "invalid_tls_consistency.json", "--semantic-only")
    errs = errors_of(result, "tls-consistency")
    assert errs, f"expected a tls-consistency finding; got {result['findings']}"
    assert any("ssl_ca_certificate" in e["message"] for e in errs)


def test_phase_resolvability_caught():
    result = run_validator(FIXTURES / "invalid_phase_resolvability.json", "--semantic-only")
    errs = errors_of(result, "phase-resolvability")
    assert errs, f"expected a phase-resolvability finding; got {result['findings']}"
    assert any("tenant_id" in e["message"] for e in errs)
    # Paths must NOT contain a spurious '/t/' segment — the walker must not
    # leak the iteration variable name as a JSON-pointer component.
    assert not any("/t/" in e["path"] for e in errs), f"finding path leaked '/t/' wrapper: {errs}"


def test_runtime_oauth_in_refresh_caught():
    result = run_validator(FIXTURES / "invalid_phase_runtime_oauth_in_refresh.json", "--semantic-only")
    errs = errors_of(result, "phase-resolvability")
    assert any("auth.refresh" in e["message"].lower() or "/auth/refresh" in e["path"] for e in errs), \
        f"expected runtime.oauth.* in auth.refresh to be caught; got {errs}"


def test_oauth_runtime_on_non_oauth_connector_caught():
    result = run_validator(FIXTURES / "invalid_phase_oauth_runtime_on_apikey.json", "--semantic-only")
    errs = errors_of(result, "phase-resolvability")
    assert any("oauth2_authorization_code" in e["message"] for e in errs), \
        f"expected oauth-only-on-oauth-connector finding; got {errs}"


def test_unknown_runtime_key_caught():
    result = run_validator(FIXTURES / "invalid_phase_unknown_runtime.json", "--semantic-only")
    errs = errors_of(result, "phase-resolvability")
    assert any("bogus_key" in e["message"] or "closed set" in e["message"] for e in errs), \
        f"expected unknown runtime key finding; got {errs}"


def test_undeclared_connection_input_caught():
    result = run_validator(FIXTURES / "invalid_phase_undeclared_input.json", "--semantic-only")
    errs = errors_of(result, "phase-resolvability")
    assert any("connection.parameters.region" in e["message"] for e in errs), \
        f"expected undeclared input finding; got {errs}"


def test_post_auth_input_referenced_in_auth_caught():
    """connection.parameters.tenant_id is phase=post_auth; auth.authorize is phase=auth.

    The validator must flag the cross-phase reference because the input
    isn't yet collected when authorize fires.
    """
    result = run_validator(FIXTURES / "invalid_phase_auth_input_in_authorize.json", "--semantic-only")
    errs = errors_of(result, "phase-resolvability")
    assert any("tenant_id" in e["message"] and "auth" in e["message"] for e in errs), \
        f"expected cross-phase finding for tenant_id in auth.authorize; got {errs}"


def test_type_map_missing_sibling_caught(tmp_path):
    """A connector with no sibling type-map-read.json triggers a coverage error."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert any("type-map-read.json" in e["message"] and "missing" in e["message"] for e in errs), \
        f"expected missing-sibling finding; got {errs}"


def test_type_map_empty_array_caught(tmp_path):
    """A sibling type-map-read.json that is an empty array triggers a coverage error."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-read.json").write_text("[]")
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert any("non-empty" in e["message"] for e in errs), \
        f"expected non-empty finding; got {errs}"


def test_api_endpoint_coverage_passes_when_all_natives_covered():
    """API connector with sibling type-map-read.json covering every
    (native_type, arrow_type) pair; the (unannotated) read param is ignored."""
    result = run_validator(
        FIXTURES / "api_endpoints_covered" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    assert not errs, f"expected no coverage errors when fully covered; got {errs}"


def test_oauth_code_in_authorize_caught():
    """runtime.oauth.code is only available in auth.token_exchange, not authorize."""
    result = run_validator(FIXTURES / "invalid_phase_oauth_code_in_authorize.json", "--semantic-only")
    errs = errors_of(result, "phase-resolvability")
    assert any("runtime.oauth.code" in e["message"] and "token_exchange" in e["message"] for e in errs), \
        f"expected oauth.code-in-authorize finding; got {errs}"


def test_stream_scope_in_auth_phase_caught():
    """stream.* is only available in the active phase; auth.authorize is at auth phase."""
    result = run_validator(FIXTURES / "invalid_phase_stream_in_authorize.json", "--semantic-only")
    errs = errors_of(result, "phase-resolvability")
    assert any("stream" in e["message"].lower() and "active" in e["message"] for e in errs), \
        f"expected stream-only-in-active finding; got {errs}"


def test_auth_scope_in_pre_post_auth_phase_caught():
    """auth.* is only available from post_auth onward; auth.authorize runs at auth phase."""
    result = run_validator(FIXTURES / "invalid_phase_auth_in_authorize.json", "--semantic-only")
    errs = errors_of(result, "phase-resolvability")
    assert any("auth.*" in e["message"] and "post_auth" in e["message"] for e in errs), \
        f"expected auth-scope-not-before-post_auth finding; got {errs}"


def test_pagination_outside_operation_caught():
    """runtime.pagination.* is operation-local; connector-level transport refs to it must error."""
    result = run_validator(FIXTURES / "invalid_phase_pagination_outside_op.json", "--semantic-only")
    errs = errors_of(result, "phase-resolvability")
    assert any("operation-local" in e["message"] for e in errs), \
        f"expected operation-local pagination finding; got {errs}"


def test_malformed_post_auth_outputs_warned():
    """A post_auth_output whose `value_path` is missing/empty (the schema requires
    a non-empty response-extraction path) should produce exactly one warning. A
    bare response field like `"id"` is *valid* and must NOT warn (see
    test_user_selection_response_value_path_not_flagged).

    The fixture also references the output's DERIVED path
    (`connection.discovered.bad_path`) from a transport: even though `value_path`
    is empty, the produced reference path is derived from storage + key and must
    still be indexed, so the ref resolves with no phase-resolvability *error*.
    The empty `value_path` is surfaced as a standalone warning, not a misdirected
    "not produced" error on the ref."""
    result = run_validator(FIXTURES / "invalid_post_auth_outputs_malformed.json", "--semantic-only")
    warns = warnings_of(result, "phase-resolvability")
    value_path_warns = [w for w in warns if "value_path" in w["message"]]
    assert len(value_path_warns) == 1, f"expected exactly one value_path warning; got {warns}"
    # The derived-path ref must resolve — the malformed-but-indexed entry keeps
    # the produced path declared, so no "not produced" error is misdirected at it.
    assert errors_of(result, "phase-resolvability") == [], (
        f"derived produced path should resolve despite empty value_path; got "
        f"{errors_of(result, 'phase-resolvability')}"
    )


def test_user_selection_response_value_path_not_flagged(tmp_path):
    """Issue #8: a compliant `user_selection` output whose `value_path` is a bare
    response field (e.g. `"id"`) must produce no phase-resolvability finding, and
    a ref to its *derived* path (storage + '.' + key) must resolve.

    Per the connector schema, `value_path` / `label_path` / `options_path` are
    response-extraction paths (the field read out of the options/discovery
    response), not the materialized `connection.*` reference path. The durable
    reference path is derived as `storage` + '.' + the output key.
    """
    doc = {
        "$schema": SCHEMA_URL,
        "kind": "api",
        "connector_id": "fixture-user-selection",
        "version": "1.0.0",
        "default_transport": "api",
        "transports": {
            "api": {
                "transport_type": "http",
                # References the DERIVED produced path, not value_path.
                "base_url": {
                    "template": "https://api.example.com/${connection.selections.region_id}/v1"
                },
            }
        },
        "auth": {"type": "api_key"},
        "connection_contract": {
            "inputs": {
                "api_key": {
                    "source": "user",
                    "phase": "auth",
                    "storage": "secrets",
                    "type": "string",
                    "required": True,
                    "secret": True,
                }
            },
            "post_auth_outputs": {
                "region_id": {
                    "mode": "user_selection",
                    "storage": "connection.selections",
                    "type": "string",
                    # value_path / label_path / options_path are response-extraction
                    # paths (fields read out of the options_request response), NOT
                    # the materialized connection.* reference path.
                    "value_path": "id",
                    "label_path": "name",
                    "options_path": "items",
                    "options_request": {
                        "transport_ref": "api",
                        "method": "GET",
                        "path": "/regions",
                    },
                }
            },
            "required_for_activation": ["connection.selections.region_id"],
        },
    }
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(doc))
    result = run_validator(doc_path, "--semantic-only")
    phase_findings = [f for f in result["findings"] if f["validator"] == "phase-resolvability"]
    assert phase_findings == [], (
        f"bare response-field value_path must not be flagged, and the derived "
        f"reference path must resolve; got {phase_findings}"
    )


def test_api_endpoint_coverage_walks_combiners_and_array_items():
    """oneOf/anyOf/allOf and tuple-style items[] must be recursed into."""
    result = run_validator(
        FIXTURES / "api_endpoints_combiners" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    messages = " ".join(e["message"] for e in errs)
    # The endpoint declares ipv6 (oneOf branch), email + uri (items as list).
    # The sibling type-map only covers string + integer, so the three rare natives must be flagged.
    assert "'ipv6'" in messages, f"expected ipv6 from oneOf to be flagged; got {messages}"
    assert "'email'" in messages, f"expected email from items[0] to be flagged; got {messages}"
    assert "'uri'" in messages, f"expected uri from items[1] to be flagged; got {messages}"


def test_api_endpoint_coverage_flags_uncovered_natives():
    """API connector with sibling type-map.json missing rules for endpoint natives."""
    result = run_validator(
        FIXTURES / "api_endpoints_uncovered" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    messages = " ".join(e["message"] for e in errs)
    # The sibling type-map covers string + integer but the endpoint references uuid, boolean, date-time.
    assert "'uuid'" in messages, f"expected uncovered 'uuid' to be flagged; got {errs}"
    assert "'boolean'" in messages, f"expected uncovered 'boolean' to be flagged; got {errs}"
    assert "'date-time'" in messages, f"expected uncovered 'date-time' to be flagged; got {errs}"


def test_semantic_only_runs_without_schema_url():
    """Registry-CI shape: `--document … --semantic-only` with no `--schema-url`.

    Layer 2 needs no schema fetch, so `--schema-url` is optional under
    `--semantic-only`. This is the exact invocation the connector registry's
    merge gate runs; it must surface `type-map-coverage` errors and exit
    non-zero so CI can gate on the exit code — with no network and no
    `${CLAUDE_PLUGIN_ROOT}`.
    """
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--document",
            str(FIXTURES / "api_endpoints_uncovered" / "connector.json"),
            "--semantic-only",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, f"expected non-zero exit on coverage failure; stderr={proc.stderr}"
    result = json.loads(proc.stdout)
    assert result["passed"] is False
    errs = errors_of(result, "type-map-coverage")
    assert errs, f"expected a type-map-coverage error; got {result['findings']}"


def test_schema_url_required_when_layer1_runs():
    """Without `--semantic-only`, Layer 1 runs and `--schema-url` is mandatory.

    Omitting it is an argparse usage error (exit 2), not a silent skip — the
    validator must never green-light a document by quietly dropping Layer 1.
    """
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--document",
            str(VALID_API_CONNECTOR),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, f"expected argparse usage error; got rc={proc.returncode}"
    assert "--schema-url is required" in proc.stderr


def test_semantic_only_passes_clean_connector_without_schema_url():
    """The merge gate's PASS path: a clean connector under `--semantic-only`
    with no `--schema-url` exits 0 / `passed: True`.

    The two checks above only assert non-zero exits, so a regression where
    omitting `--schema-url` crashed or wrongly re-required the URL on a
    *passing* document would slip past them. This pins the exact green
    invocation the registry gate relies on.
    """
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--document",
            str(VALID_API_CONNECTOR),
            "--semantic-only",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"expected clean pass; stderr={proc.stderr}"
    assert json.loads(proc.stdout)["passed"] is True


def test_db_connector_missing_sibling_type_map_caught(tmp_path):
    """The missing-sibling check must fire for kind=database too, not just api."""
    base = json.loads((FIXTURES / "valid_db_connector" / "connector.json").read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert any("type-map-read.json" in e["message"] and "missing" in e["message"] for e in errs), \
        f"expected missing-sibling finding for DB connector; got {errs}"


def test_db_connector_with_sibling_type_maps_passes():
    """Happy path for DB connectors — non-empty sibling read + write maps, no endpoints/ required."""
    result = run_validator(
        FIXTURES / "valid_db_connector" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    assert not errs, f"expected DB connector with sibling type maps to pass; got {errs}"
    # The fixture write map covers the full canonical vocabulary, so the
    # rule-8 probe must stay quiet too.
    warns = warnings_of(result, "type-map-write-coverage")
    assert not warns, f"expected no vocabulary-gap warnings; got {warns}"


def test_db_connector_missing_write_map_caught(tmp_path):
    """kind=database requires a sibling type-map-write.json; absence is an error."""
    base = json.loads((FIXTURES / "valid_db_connector" / "connector.json").read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-read.json").write_text(
        (FIXTURES / "valid_db_connector" / "type-map-read.json").read_text()
    )
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert any("type-map-write.json" in e["message"] and "missing" in e["message"] for e in errs), \
        f"expected missing-write-map finding for DB connector; got {errs}"


def test_legacy_type_map_filename_caught(tmp_path):
    """A pre-split sibling `type-map.json` must surface a rename pointer even
    when the new read map is also present — the stale file would otherwise
    linger unnoticed."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    src = FIXTURES / "valid_api_connector"
    (tmp_path / "type-map-read.json").write_text((src / "type-map-read.json").read_text())
    (tmp_path / "type-map.json").write_text((src / "type-map-read.json").read_text())
    (tmp_path / "endpoints").mkdir()
    (tmp_path / "endpoints" / "ping.json").write_text((src / "endpoints" / "ping.json").read_text())
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert any("legacy" in e["message"] and "type-map-read.json" in e["message"] for e in errs), \
        f"expected legacy-filename finding; got {errs}"


def test_api_connector_with_write_map_caught(tmp_path):
    """API connectors must not ship a write map — the write direction is a
    database-package concept."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    src = FIXTURES / "valid_api_connector"
    (tmp_path / "type-map-read.json").write_text((src / "type-map-read.json").read_text())
    (tmp_path / "type-map-write.json").write_text(json.dumps([
        {"match": "exact", "canonical": "Utf8", "native": "TEXT"}
    ]))
    (tmp_path / "endpoints").mkdir()
    (tmp_path / "endpoints" / "ping.json").write_text((src / "endpoints" / "ping.json").read_text())
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert any("must not ship" in e["message"] and "type-map-write.json" in e["message"] for e in errs), \
        f"expected api-with-write-map finding; got {errs}"


def test_api_connector_missing_endpoints_dir_caught():
    """An API connector with no sibling endpoints/ dir is now a hard error."""
    result = run_validator(
        FIXTURES / "api_connector_no_endpoints" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    assert any("endpoints/" in e["message"] and "missing" in e["message"] for e in errs), \
        f"expected missing-endpoints finding; got {errs}"


def test_api_connector_asymmetric_native_arrow_pair_caught():
    """A schema-tree field declaring only one of native_type / arrow_type is a
    contract violation; a param declaring either at all is one too (Param
    forbids both). Exercises ALL FOUR walker sites (read.response.schema,
    read.params, write.<mode>.input.schema, write.<mode>.params) plus the
    'both keys present, non-string values' variant."""
    result = run_validator(
        FIXTURES / "api_connector_asymmetric_pair" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    # Asymmetric (exactly-one-of-pair) — schema-tree sites.
    assert any("exactly one of" in e["message"]
               and "/operations/read/response/schema/properties/id" in e["message"]
               for e in errs), \
        f"expected asymmetric finding from read.response.schema; got {errs}"
    assert any("exactly one of" in e["message"]
               and "/operations/write/insert/input/schema/properties/name" in e["message"]
               for e in errs), \
        f"expected asymmetric finding from write.insert.input.schema; got {errs}"
    # Param sites — annotations are forbidden wholesale, even half-declared.
    assert any("forbids both" in e["message"] and "/operations/read/params/q" in e["message"]
               for e in errs), \
        f"expected param-annotations finding from read.params; got {errs}"
    assert any("forbids both" in e["message"] and "/operations/write/insert/params/tenant" in e["message"]
               for e in errs), \
        f"expected param-annotations finding from write.insert.params; got {errs}"
    # Both-keys param: rejected wholesale, never fed to coverage. Its native
    # ('weird') has no read-map rule, so a revert of the coverage-source
    # removal would surface as a spurious coverage finding at a /params/
    # pointer.
    assert any("forbids both" in e["message"] and "/operations/read/params/legacy" in e["message"]
               for e in errs), \
        f"expected param-annotations finding for both-keys param; got {errs}"
    assert not any("/params/" in e["message"]
                   and ("no matching rule" in e["message"] or "resolves to" in e["message"])
                   for e in errs), \
        f"params must never feed type-map coverage; got {errs}"
    # Non-string-both variant.
    assert any("non-string value(s)" in e["message"]
               and "/operations/read/response/schema/properties/raw" in e["message"]
               for e in errs), \
        f"expected non-string-both finding for /raw field; got {errs}"


def test_unknown_kind_skipped_silently(tmp_path):
    """Storage kinds (file/s3/stdout) are accepted by the schema but type-map is
    not yet defined for them; coverage should no-op without crashing."""
    base = json.loads((VALID_API_CONNECTOR).read_text())
    base["kind"] = "file"
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    cov = [f for f in result["findings"] if f["validator"] == "type-map-coverage"]
    assert not cov, f"unsupported kind should produce no type-map-coverage findings; got {cov}"


def test_storage_kind_still_validates_sibling_type_map_rules(tmp_path):
    """Storage kinds (file/s3/stdout) have no per-kind coverage contract, but
    a sibling broken type-map-read.json must still surface — authors who ship
    a type map ahead of engine support shouldn't get a silent pass."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    base["kind"] = "file"
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-read.json").write_text(json.dumps([
        {"match": "regex", "native": "^BAD[REGEX", "canonical": "Utf8"}
    ]))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-rule")
    assert any("not a valid regex" in e["message"] for e in errs), \
        f"storage-kind sibling type-map must still be rule-checked; got {errs}"


def test_adbc_example_passes_semantic_validation():
    """End-to-end semantic check against the ADBC postgres example —
    distinct shape from the sqlalchemy postgres example, so it deserves its
    own pass. Pins that future validator changes don't accidentally flag
    ADBC's lack of `tls` block or its db_kwargs-with-value-expressions."""
    result = run_validator(
        REPO_ROOT / "skills" / "connector-spec-db" / "examples" /
        "postgresql-adbc" / "postgresql-adbc.example.json",
        "--semantic-only",
    )
    errs = [f for f in result["findings"] if f["severity"] == "error"]
    assert not errs, f"ADBC example should pass semantic validation; got {errs}"


def test_connector_with_endpoints_and_broken_type_map_renders_skipped_rule():
    """End-to-end: broken regex in sibling type-map is reported by
    check_type_map_rules, AND _render_canonical's defensive 'except re.error:
    continue' is exercised when the endpoint walker calls it — proving the
    cross-validator wiring + the render-time fallback both stay correct."""
    result = run_validator(
        FIXTURES / "connector_with_broken_type_map" / "connector.json",
        "--semantic-only",
    )
    # Rule-level error from the cross-validator dispatch.
    rule_errs = errors_of(result, "type-map-rule")
    assert any("not a valid regex" in e["message"] for e in rule_errs), \
        f"expected broken-regex rule error; got {rule_errs}"
    # Coverage walker tried to resolve `varchar(255)` against the broken rule;
    # `_render_canonical` swallowed the re.error and returned None, surfacing
    # an unresolved-native error from check_type_map_coverage.
    cov_errs = errors_of(result, "type-map-coverage")
    assert any("no matching rule" in e["message"] and "varchar(255)" in e["message"]
               for e in cov_errs), \
        f"expected unresolved-native coverage error proving _render_canonical was reached; got {cov_errs}"


def test_arrow_narrowing_only_accepted_from_json_rule():
    """Object/List narrowings are valid ONLY when the rule resolves to Json."""
    result = run_validator(
        FIXTURES / "api_connector_arrow_narrowing_invalid" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    msgs = " ".join(e["message"] for e in errs)
    # Rule resolves uuid → Utf8 (not Json); endpoint declares Object → must be a mismatch error.
    assert "'Utf8'" in msgs and "'Object'" in msgs, \
        f"expected non-Json → Object narrowing to be flagged as mismatch; got {errs}"


def test_api_endpoint_arrow_mismatch_caught():
    """Endpoint arrow_type that disagrees with the sibling type-map's rendered canonical is an error."""
    result = run_validator(
        FIXTURES / "api_endpoints_arrow_mismatch" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    messages = " ".join(e["message"] for e in errs)
    # id: native=uuid, arrow=Int64 vs type-map resolves uuid → Utf8 → mismatch
    assert "'Utf8'" in messages and "'Int64'" in messages, \
        f"expected uuid/Utf8 vs Int64 mismatch finding; got {errs}"
    # metadata: native=json, arrow=Object; type-map resolves json → Json; narrowing OK.
    # That site must NOT appear as an error.
    assert "'Object'" not in messages, \
        f"narrowing Json → Object should not be flagged; got {errs}"


# ---------------------------------------------------------------------------
# Bare-marker arrow_type sibling-key rules (Object/List/Json)
# ---------------------------------------------------------------------------
#
# The published `JsonSchemaPropertyNode` accepts `Object` / `List` / `Json`
# as arrow_type values but is `additionalProperties: true`, so the JSON
# Schema layer does NOT enforce the sibling-key contract. `endpoint-annotations`
# is the only layer that catches an `Object` with no `properties`, a `List`
# with no `items`, or a `Json` carrying an inner declaration.


def _write_marker_endpoint(tmp_path: Path, schema: dict) -> Path:
    """Write a minimal api-endpoint document whose read.response.schema is `schema`.

    Validated directly (schema_url=api-endpoint) under --semantic-only so the
    endpoint-annotations walker runs without a network fetch.
    """
    endpoint = {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": "items",
        "operations": {
            "read": {
                "request": {"method": "GET", "path": "/items"},
                "response": {"records": {"ref": "response.body"}, "schema": schema},
            }
        },
    }
    path = tmp_path / "items.json"
    path.write_text(json.dumps(endpoint))
    return path


def test_marker_object_requires_properties(tmp_path):
    """`arrow_type: "Object"` with no `properties` is a contract violation."""
    ep = _write_marker_endpoint(tmp_path, {
        "type": "object",
        "native_type": "json",
        "arrow_type": "Object",
    })
    result = run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "endpoint-annotations")
    assert any('"Object"' in e["message"] and "properties" in e["message"] for e in errs), \
        f"expected Object-without-properties error; got {errs}"


def test_marker_object_empty_properties_rejected(tmp_path):
    """An empty `properties` map does not satisfy the `Object` marker."""
    ep = _write_marker_endpoint(tmp_path, {
        "type": "object",
        "native_type": "json",
        "arrow_type": "Object",
        "properties": {},
    })
    result = run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "endpoint-annotations")
    assert any('"Object"' in e["message"] and "properties" in e["message"] for e in errs), \
        f"expected empty-properties Object error; got {errs}"


def test_marker_object_forbids_items(tmp_path):
    """`Object` must not carry an `items` sibling (that belongs to `List`)."""
    ep = _write_marker_endpoint(tmp_path, {
        "type": "object",
        "native_type": "json",
        "arrow_type": "Object",
        "properties": {"id": {"native_type": "int", "arrow_type": "Int64"}},
        "items": {"native_type": "text", "arrow_type": "Utf8"},
    })
    result = run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "endpoint-annotations")
    assert any('"Object"' in e["message"] and "items" in e["message"] for e in errs), \
        f"expected Object-with-items error; got {errs}"


def test_marker_list_requires_items(tmp_path):
    """`arrow_type: "List"` with no `items` is a contract violation."""
    ep = _write_marker_endpoint(tmp_path, {
        "type": "array",
        "native_type": "array",
        "arrow_type": "List",
    })
    result = run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "endpoint-annotations")
    assert any('"List"' in e["message"] and "items" in e["message"] for e in errs), \
        f"expected List-without-items error; got {errs}"


def test_marker_list_forbids_properties(tmp_path):
    """`List` must not carry a `properties` sibling (that belongs to `Object`)."""
    ep = _write_marker_endpoint(tmp_path, {
        "type": "array",
        "native_type": "array",
        "arrow_type": "List",
        "items": {"native_type": "text", "arrow_type": "Utf8"},
        "properties": {"id": {"native_type": "int", "arrow_type": "Int64"}},
    })
    result = run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "endpoint-annotations")
    assert any('"List"' in e["message"] and "properties" in e["message"] for e in errs), \
        f"expected List-with-properties error; got {errs}"


def test_marker_json_forbids_inner_declaration(tmp_path):
    """`Json` is opaque: it must carry neither `properties` nor `items`."""
    ep_props = _write_marker_endpoint(tmp_path, {
        "type": "object",
        "native_type": "jsonb",
        "arrow_type": "Json",
        "properties": {"id": {"native_type": "int", "arrow_type": "Int64"}},
    })
    errs = errors_of(
        run_validator(ep_props, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL),
        "endpoint-annotations",
    )
    assert any('"Json"' in e["message"] and "properties" in e["message"] for e in errs), \
        f"expected Json-with-properties error; got {errs}"

    ep_items = _write_marker_endpoint(tmp_path, {
        "type": "array",
        "native_type": "jsonb",
        "arrow_type": "Json",
        "items": {"native_type": "text", "arrow_type": "Utf8"},
    })
    errs = errors_of(
        run_validator(ep_items, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL),
        "endpoint-annotations",
    )
    assert any('"Json"' in e["message"] and "items" in e["message"] for e in errs), \
        f"expected Json-with-items error; got {errs}"


def test_marker_valid_shapes_pass(tmp_path):
    """Valid Object+properties, List+items, and bare Json produce no marker errors."""
    schema = {
        "type": "object",
        "properties": {
            "checkAccount": {
                "type": "object",
                "native_type": "json",
                "arrow_type": "Object",
                "properties": {
                    "id": {"native_type": "int", "arrow_type": "Int64"},
                    "objectName": {"native_type": "text", "arrow_type": "Utf8"},
                },
            },
            "tags": {
                "type": "array",
                "native_type": "array",
                "arrow_type": "List",
                "items": {"native_type": "text", "arrow_type": "Utf8"},
            },
            "metadata": {
                "native_type": "jsonb",
                "arrow_type": "Json",
            },
        },
    }
    ep = _write_marker_endpoint(tmp_path, schema)
    result = run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "endpoint-annotations")
    assert not errs, f"valid marker shapes should produce no endpoint-annotations errors; got {errs}"


def test_marker_violation_recurses_into_nested_properties(tmp_path):
    """The sibling-key rule is recursive: an Object nested inside `properties`
    that itself lacks `properties` is flagged at its recursive pointer."""
    ep = _write_marker_endpoint(tmp_path, {
        "type": "object",
        "native_type": "json",
        "arrow_type": "Object",
        "properties": {
            "inner": {"native_type": "json", "arrow_type": "Object"},
        },
    })
    result = run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "endpoint-annotations")
    assert any(
        "/operations/read/response/schema/properties/inner" in e["message"]
        and '"Object"' in e["message"]
        for e in errs
    ), f"expected nested Object violation at recursive pointer; got {errs}"


def test_connector_endpoint_marker_violation_surfaced_via_coverage(tmp_path):
    """During connector-level validation the marker rule is enforced on each
    sibling endpoint and surfaced under type-map-coverage (parity with the
    asymmetric-pair walker)."""
    connector = json.loads(VALID_API_CONNECTOR.read_text())
    (tmp_path / "connector.json").write_text(json.dumps(connector))
    # json → Json so the Object narrowing itself is accepted by coverage; the
    # only finding must be the missing-properties marker error.
    (tmp_path / "type-map-read.json").write_text(json.dumps([
        {"match": "exact", "native": "json", "canonical": "Json"},
    ]))
    (tmp_path / "endpoints").mkdir()
    (tmp_path / "endpoints" / "items.json").write_text(json.dumps({
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": "items",
        "operations": {
            "read": {
                "request": {"method": "GET", "path": "/items"},
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {
                        "type": "object",
                        "properties": {
                            "metadata": {
                                "type": "object",
                                "native_type": "json",
                                "arrow_type": "Object",
                            }
                        },
                    },
                },
            }
        },
    }))
    result = run_validator(tmp_path / "connector.json", "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert any(
        "items.json" in e["message"]
        and '"Object"' in e["message"]
        and "properties" in e["message"]
        for e in errs
    ), f"expected connector-level marker error from sibling endpoint; got {errs}"


def test_marker_list_items_must_be_a_subschema(tmp_path):
    """`List` + `items` that isn't a sub-schema (boolean/null/scalar) does not
    declare an element shape and is flagged the same as a missing `items`."""
    for bad in (False, None, 5):
        ep = _write_marker_endpoint(tmp_path, {
            "type": "array",
            "native_type": "array",
            "arrow_type": "List",
            "items": bad,
        })
        errs = errors_of(
            run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL),
            "endpoint-annotations",
        )
        assert any('"List"' in e["message"] and "items" in e["message"] for e in errs), \
            f"expected List+items={bad!r} to be flagged as no element spec; got {errs}"


def test_annotations_on_params_flagged(tmp_path):
    """The published `Param` def forbids native_type / arrow_type outright (a
    param is a request input, typed only by its `type` field), so ANY annotated
    param is flagged wholesale — marker, scalar, or half-declared alike — on
    both read and write ops; an unannotated param is clean."""
    endpoint = {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": "items",
        "operations": {
            "read": {
                "request": {"method": "GET", "path": "/items"},
                "response": {"records": {"ref": "response.body"}, "schema": {"type": "object"}},
                "params": {
                    "shape": {"native_type": "obj", "arrow_type": "Object"},
                    "q": {"native_type": "text", "arrow_type": "Utf8"},
                    "half": {"arrow_type": "Utf8"},
                    "clean": {"in": "query", "type": "string"},
                },
            },
            "write": {
                "insert": {
                    "request": {"method": "POST", "path": "/items"},
                    "input": {"schema": {"type": "object"}},
                    "params": {"wshape": {"native_type": "obj", "arrow_type": "Object"}},
                }
            },
        },
    }
    path = tmp_path / "items.json"
    path.write_text(json.dumps(endpoint))
    errs = errors_of(
        run_validator(path, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL),
        "endpoint-annotations",
    )
    # Exactly one finding per annotated param — the wholesale rejection, and
    # nothing else. A second finding at the same pointer means the marker
    # param branch grew back (the coverage-branch revert is pinned by
    # test_api_connector_asymmetric_native_arrow_pair_caught).
    for pointer in (
        "/operations/read/params/shape",
        "/operations/read/params/q",
        "/operations/read/params/half",
        "/operations/write/insert/params/wshape",
    ):
        matches = [e for e in errs if pointer in e["message"]]
        assert len(matches) == 1 and "forbids both" in matches[0]["message"], \
            f"expected exactly one param-annotations finding at {pointer}; got {matches or errs}"
    assert not any("/params/clean" in e["message"] for e in errs), \
        f"unannotated param must not be flagged; got {errs}"


def test_marker_violation_in_write_input_schema(tmp_path):
    """The write-mode `input.schema` branch is enforced, with a write-scoped pointer."""
    endpoint = {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": "items",
        "operations": {
            "write": {
                "insert": {
                    "request": {"method": "POST", "path": "/items"},
                    "input": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "tags": {"type": "array", "native_type": "array", "arrow_type": "List"},
                            },
                        }
                    },
                }
            }
        },
    }
    path = tmp_path / "items.json"
    path.write_text(json.dumps(endpoint))
    errs = errors_of(
        run_validator(path, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL),
        "endpoint-annotations",
    )
    assert any(
        "/operations/write/insert/input/schema/properties/tags" in e["message"]
        and '"List"' in e["message"] and "items" in e["message"]
        for e in errs
    ), f"expected write-input List-without-items error; got {errs}"


def test_marker_parameterized_forms_not_flagged(tmp_path):
    """Self-describing parameterized containers (`List<…>`, `Struct<…>`) and
    scalars carry no sibling contract — they must NOT trip the marker check
    even with no `properties`/`items`. Guards against loosening the exact-set
    membership test to a prefix match."""
    schema = {
        "type": "object",
        "properties": {
            "ids": {"native_type": "array", "arrow_type": "List<Int64>"},
            "rec": {"native_type": "struct", "arrow_type": "Struct<id:Int64>"},
            "n": {"native_type": "int", "arrow_type": "Int64"},
        },
    }
    ep = _write_marker_endpoint(tmp_path, schema)
    errs = errors_of(
        run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL),
        "endpoint-annotations",
    )
    assert not errs, f"parameterized/scalar arrow types must not trip the marker check; got {errs}"


def test_marker_recurses_through_items(tmp_path):
    """The recursion covers the `items` branch too: a List-of-Objects whose
    element Object lacks `properties` is flagged at the `/items` pointer."""
    ep = _write_marker_endpoint(tmp_path, {
        "type": "array",
        "native_type": "array",
        "arrow_type": "List",
        "items": {"type": "object", "native_type": "json", "arrow_type": "Object"},
    })
    errs = errors_of(
        run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL),
        "endpoint-annotations",
    )
    assert any(
        "/operations/read/response/schema/items" in e["message"] and '"Object"' in e["message"]
        for e in errs
    ), f"expected nested Object-in-items violation at /items pointer; got {errs}"


def test_marker_recurses_through_combiners(tmp_path):
    """Recursion also reaches combiner keywords (`anyOf`/`allOf`/`oneOf`): a
    marker violation inside an `anyOf` branch is flagged at the branch pointer,
    proving the marker walker descends the full `_recurse_jsonschema` keyword
    set, not just `properties`/`items`."""
    ep = _write_marker_endpoint(tmp_path, {
        "type": "object",
        "properties": {
            "either": {
                "anyOf": [
                    {"type": "string", "native_type": "text", "arrow_type": "Utf8"},
                    {"type": "object", "native_type": "json", "arrow_type": "Object"},
                ]
            }
        },
    })
    errs = errors_of(
        run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL),
        "endpoint-annotations",
    )
    assert any(
        "/operations/read/response/schema/properties/either/anyOf/1" in e["message"]
        and '"Object"' in e["message"]
        for e in errs
    ), f"expected Object violation inside anyOf branch to be flagged; got {errs}"


def test_marker_message_keys_cover_every_emitted_kind():
    """Drift guard: every `kind` `_check_marker_siblings` can emit should have a
    `_MARKER_SIBLING_MESSAGES` entry. A missing entry no longer crashes — the
    emission sites route through `_marker_sibling_message()`, which falls back to
    a generic message — but it silently drops the kind-specific guidance, so
    keep the mapping complete."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("analitiq_connector_validator", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    emitted: set[str] = set()
    probes = [
        ({}, "Object"),
        ({"properties": {"a": {}}, "items": {}}, "Object"),
        ({}, "List"),
        ({"items": {}, "properties": {"a": {}}}, "List"),
        ({"properties": {"a": {}}}, "Json"),
        ({"items": {}}, "Json"),
    ]
    for node, arrow in probes:
        out: list = []
        mod._check_marker_siblings(node, arrow, "/", out)
        emitted |= {kind for _, kind in out}
    assert emitted == set(mod._MARKER_SIBLING_MESSAGES), \
        f"emitted kinds and message keys diverged: {emitted ^ set(mod._MARKER_SIBLING_MESSAGES)}"


def test_api_endpoint_arrow_template_substitution_renders():
    """A regex rule with ${name} substitution renders before comparison."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        connector = json.loads(VALID_API_CONNECTOR.read_text())
        (td / "connector.json").write_text(json.dumps(connector))
        # Pattern is authored UPPERCASE per the contract; the endpoint's
        # lowercase native must still resolve via the engine-mirroring
        # normalization (uppercase + whitespace-collapse) before matching.
        (td / "type-map-read.json").write_text(json.dumps([
            {
                "match": "regex",
                "native": "^NUMERIC\\((?<precision>[0-9]+),(?<scale>[0-9]+)\\)$",
                "canonical": "Decimal128(${precision}, ${scale})",
            }
        ]))
        (td / "endpoints").mkdir()
        (td / "endpoints" / "items.json").write_text(json.dumps({
            "$schema": "https://schemas.analitiq.ai/api-endpoint/latest.json",
            "endpoint_id": "items",
            "operations": {
                "read": {
                    "request": {"method": "GET", "path": "/items"},
                    "response": {
                        "records": {"ref": "response.body"},
                        "schema": {
                            "type": "object",
                            "properties": {
                                "amount": {
                                    "type": "string",
                                    "native_type": "numeric(10,2)",
                                    "arrow_type": "Decimal128(10, 2)",
                                }
                            },
                        },
                    },
                }
            },
        }))
        result = run_validator(td / "connector.json", "--semantic-only")
        errs = errors_of(result, "type-map-coverage")
        assert not errs, f"expected templated canonical to render and match; got {errs}"


def test_api_endpoint_write_coverage_passes_when_input_covered():
    """API connector with write-side input.schema natives fully covered by the
    sibling type-map-read.json; the (unannotated) write param is ignored."""
    result = run_validator(
        FIXTURES / "api_endpoints_write_covered" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    assert not errs, f"expected no coverage errors when write fully covered; got {errs}"


def test_api_endpoint_write_coverage_flags_uncovered_input():
    """Write-side natives in operations.write.<mode>.input.schema must be
    walked; params are never a coverage source (Param carries no annotations)."""
    result = run_validator(
        FIXTURES / "api_endpoints_write_uncovered" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    messages = " ".join(e["message"] for e in errs)
    assert "'uuid'" in messages, f"expected uncovered write input 'uuid' to be flagged; got {errs}"
    assert "'date-time'" in messages, f"expected uncovered write input 'date-time' to be flagged; got {errs}"
    # JSON pointers must locate the natives under the mode-keyed write path,
    # not at the bare operations/write level — guards against the walker
    # dropping the <mode> layer.
    assert "/operations/write/insert/input/schema" in messages, \
        f"expected /operations/write/insert/input/schema in pointers; got {messages}"
    assert "/params/" not in messages, \
        f"params must produce no coverage findings; got {messages}"


def test_api_endpoint_write_coverage_walks_all_modes():
    """Both insert and upsert modes must be walked — guards the per-mode loop."""
    result = run_validator(
        FIXTURES / "api_endpoints_write_multimode" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-coverage")
    messages = " ".join(e["message"] for e in errs)
    # insert.input.schema declares uuid; upsert.input.schema declares date-time.
    # A regression that hardcoded only one mode would fail one of these.
    assert "'uuid'" in messages, f"expected insert-mode 'uuid' to be flagged; got {errs}"
    assert "'date-time'" in messages, f"expected upsert-mode 'date-time' to be flagged; got {errs}"
    assert "/operations/write/insert/input/schema" in messages, \
        f"expected insert pointer; got {messages}"
    assert "/operations/write/upsert/input/schema" in messages, \
        f"expected upsert pointer; got {messages}"


# ---------------------------------------------------------------------------
# endpoint-filename — the file's basename must equal `{endpoint_id}.json`
# ---------------------------------------------------------------------------


def _write_named_endpoint(tmp_path: Path, *, filename: str, endpoint_id: str) -> Path:
    """Write a minimal valid api-endpoint document under `filename`."""
    endpoint = {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": endpoint_id,
        "operations": {
            "read": {
                "request": {"method": "GET", "path": "/items"},
                "response": {"records": {"ref": "response.body"}, "schema": {"type": "object"}},
            }
        },
    }
    path = tmp_path / filename
    path.write_text(json.dumps(endpoint))
    return path


def test_endpoint_filename_mismatch_caught(tmp_path):
    """A standalone endpoint whose basename ≠ `{endpoint_id}.json` is an error."""
    ep = _write_named_endpoint(tmp_path, filename="people.json", endpoint_id="users")
    result = run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "endpoint-filename")
    assert any(
        "people.json" in e["message"] and "users.json" in e["message"] and e["path"] == "/endpoint_id"
        for e in errs
    ), f"expected filename↔endpoint_id mismatch error; got {errs}"
    assert result["passed"] is False


def test_endpoint_filename_match_passes(tmp_path):
    """A standalone endpoint named `{endpoint_id}.json` produces no finding."""
    ep = _write_named_endpoint(tmp_path, filename="users.json", endpoint_id="users")
    result = run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    assert not [f for f in result["findings"] if f["validator"] == "endpoint-filename"], \
        f"matching filename should produce no endpoint-filename finding; got {result['findings']}"


def test_endpoint_filename_surfaced_via_connector_coverage(tmp_path):
    """During connector-level validation the basename rule is enforced on each
    sibling endpoint (parity with the asymmetric-pair / marker walkers)."""
    connector = json.loads(VALID_API_CONNECTOR.read_text())
    (tmp_path / "connector.json").write_text(json.dumps(connector))
    (tmp_path / "type-map-read.json").write_text(
        (VALID_API_CONNECTOR.parent / "type-map-read.json").read_text()
    )
    (tmp_path / "endpoints").mkdir()
    # endpoint_id is "ping" but the file is named misnamed.json.
    _write_named_endpoint(tmp_path / "endpoints", filename="misnamed.json", endpoint_id="ping")
    result = run_validator(tmp_path / "connector.json", "--semantic-only")
    errs = errors_of(result, "endpoint-filename")
    assert any(
        "misnamed.json" in e["message"] and "ping.json" in e["message"] for e in errs
    ), f"expected connector-level filename error from sibling endpoint; got {errs}"
    # An error-severity endpoint-filename finding must flip the connector verdict.
    assert result["passed"] is False


def test_endpoint_filename_extension_mismatch_caught(tmp_path):
    """The expected name is `{endpoint_id}.json` — a matching stem with a
    different extension still diverges (guards against a `stem == endpoint_id`
    simplification that would let `users.txt` through)."""
    ep = _write_named_endpoint(tmp_path, filename="users.txt", endpoint_id="users")
    result = run_validator(ep, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    errs = errors_of(result, "endpoint-filename")
    assert any("users.txt" in e["message"] and "users.json" in e["message"] for e in errs), \
        f"expected extension-mismatch error; got {errs}"


def test_endpoint_filename_database_endpoint_not_flagged(tmp_path):
    """A database-endpoint document (`endpoint_id` + `columns[]`, no
    `operations`, no `kind`) is out of plugin scope and must NOT route to the
    api-endpoint filename check — the dispatcher gate (`is_endpoint_doc`
    requires `operations`) is the sole protection, so pin it."""
    db_endpoint = {
        "$schema": "https://schemas.analitiq.ai/database-endpoint/latest.json",
        "endpoint_id": "accounts",
        "columns": [{"name": "id", "native_type": "bigint", "arrow_type": "Int64"}],
    }
    path = tmp_path / "misnamed_db.json"
    path.write_text(json.dumps(db_endpoint))
    result = run_validator(path, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    assert not [f for f in result["findings"] if f["validator"] == "endpoint-filename"], \
        f"DB-endpoint doc must not produce endpoint-filename findings; got {result['findings']}"


@pytest.mark.parametrize("endpoint_id", [None, 123], ids=["absent", "non-string"])
def test_endpoint_filename_unusable_id_warns_standalone(tmp_path, endpoint_id):
    """An absent / non-string `endpoint_id` can't be compared to the basename.
    Rather than silently pass (no Layer 1 backstop under `--semantic-only`),
    the check warns."""
    doc = {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "operations": {
            "read": {
                "request": {"method": "GET", "path": "/u"},
                "response": {"records": {"ref": "response.body"}, "schema": {"type": "object"}},
            }
        },
    }
    if endpoint_id is not None:
        doc["endpoint_id"] = endpoint_id
    path = tmp_path / "whatever.json"
    path.write_text(json.dumps(doc))
    result = run_validator(path, "--semantic-only", schema_url=API_ENDPOINT_SCHEMA_URL)
    warns = warnings_of(result, "endpoint-filename")
    assert any("endpoint_id is absent or non-string" in w["message"] for w in warns), \
        f"expected an unusable-endpoint_id warning; got {result['findings']}"
    assert not errors_of(result, "endpoint-filename"), \
        "an unusable endpoint_id must warn, not error (Layer 1 owns the hard error)"


def test_endpoint_filename_unusable_id_warns_via_connector_coverage(tmp_path):
    """The sibling-endpoint path has NO Layer 1 backstop (endpoints are never
    schema-validated during a connector run), so an absent `endpoint_id` on a
    sibling must warn rather than pass silently."""
    connector = json.loads(VALID_API_CONNECTOR.read_text())
    (tmp_path / "connector.json").write_text(json.dumps(connector))
    (tmp_path / "type-map-read.json").write_text(
        (VALID_API_CONNECTOR.parent / "type-map-read.json").read_text()
    )
    (tmp_path / "endpoints").mkdir()
    (tmp_path / "endpoints" / "orphan.json").write_text(json.dumps({
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "operations": {
            "read": {
                "request": {"method": "GET", "path": "/x"},
                "response": {"records": {"ref": "response.body"}, "schema": {"type": "object"}},
            }
        },
    }))
    result = run_validator(tmp_path / "connector.json", "--semantic-only")
    warns = warnings_of(result, "endpoint-filename")
    assert any("orphan.json" in w["message"] and "absent or non-string" in w["message"] for w in warns), \
        f"expected sibling unusable-endpoint_id warning; got {result['findings']}"


def test_connector_non_dict_sibling_endpoint_is_clean_error(tmp_path):
    """A parsed-but-non-dict `endpoints/*.json` yields a clean per-file error and
    does NOT crash or abort the walk: a misnamed sibling sorting after it is
    still flagged (pins `continue`, not `break`), while a correctly-named one
    produces no finding — all with no 'validator crashed' finding."""
    connector = json.loads(VALID_API_CONNECTOR.read_text())
    (tmp_path / "connector.json").write_text(json.dumps(connector))
    (tmp_path / "type-map-read.json").write_text(
        (VALID_API_CONNECTOR.parent / "type-map-read.json").read_text()
    )
    (tmp_path / "endpoints").mkdir()
    # Walked in sorted order: bad.json < ping.json < zebra.json.
    (tmp_path / "endpoints" / "bad.json").write_text(json.dumps(["not", "an", "object"]))
    (tmp_path / "endpoints" / "ping.json").write_text(
        (VALID_API_CONNECTOR.parent / "endpoints" / "ping.json").read_text()
    )
    # A misnamed-but-valid sibling AFTER bad.json: its endpoint-filename error
    # can only surface if the loop continued past the non-dict sibling.
    _write_named_endpoint(tmp_path / "endpoints", filename="zebra.json", endpoint_id="users")
    result = run_validator(tmp_path / "connector.json", "--semantic-only")
    cov_errs = errors_of(result, "type-map-coverage")
    assert any("bad.json" in e["message"] and "not a JSON object" in e["message"] for e in cov_errs), \
        f"expected a clean non-dict-sibling error; got {cov_errs}"
    assert not any("crashed" in f["message"] for f in result["findings"]), \
        f"non-dict sibling must not surface as a validator crash; got {result['findings']}"
    # Loop continued past bad.json: the later misnamed sibling is still flagged.
    ef_errs = errors_of(result, "endpoint-filename")
    assert any("zebra.json" in e["message"] for e in ef_errs), \
        f"loop must continue past the non-dict sibling and flag later siblings; got {ef_errs}"
    # The correctly-named sibling produces no finding of any kind.
    assert not any("ping.json" in f["message"] for f in result["findings"]), \
        f"correctly-named sibling should produce no finding; got {result['findings']}"


def test_endpoint_filename_no_path_warns():
    """Invoked without a filesystem anchor, the check warns instead of a silent pass."""
    sys.path.insert(0, str(REPO_ROOT / "validator" / "src"))
    import analitiq_connector_validator as v

    doc = {
        "$schema": API_ENDPOINT_SCHEMA_URL,
        "endpoint_id": "users",
        "operations": {"read": {"request": {"method": "GET", "path": "/u"},
                                "response": {"records": {"ref": "response.body"}, "schema": {"type": "object"}}}},
    }
    findings = v.check_endpoint_filename(doc, None)
    assert findings and findings[0]["severity"] == "warning" and findings[0]["validator"] == "endpoint-filename", \
        f"expected a no-path warning; got {findings}"


# ---------------------------------------------------------------------------
# type-map.json self-validation
# ---------------------------------------------------------------------------


def test_type_map_exact_rule_with_template_caught():
    result = run_validator(
        FIXTURES / "invalid_type_map_exact_with_template.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    errs = errors_of(result, "type-map-rule")
    assert any("exact" in e["message"] and "${" in e["message"] for e in errs), \
        f"expected exact-with-template finding; got {errs}"


def test_type_map_regex_missing_capture_caught():
    result = run_validator(
        FIXTURES / "invalid_type_map_regex_missing_capture.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    errs = errors_of(result, "type-map-rule")
    assert any("precision" in e["message"] and "capture" in e["message"] for e in errs), \
        f"expected missing-capture finding; got {errs}"


def test_type_map_empty_placeholder_caught(tmp_path):
    """An empty `${}` on a render value renders to nothing — flagged as a
    type-map-rule error rather than surviving into the output verbatim (the
    `_PLACEHOLDER_RE` `[^}]*` fix in #48)."""
    read_path = tmp_path / "type-map-read.json"
    read_path.write_text(json.dumps([
        {"match": "exact", "native": "BOOLEAN", "canonical": "Boolean${}"}
    ]))
    result = run_validator(read_path, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    errs = errors_of(result, "type-map-rule")
    assert any(
        e["path"] == "/0/canonical" and "empty" in e["message"] and "${}" in e["message"]
        for e in errs
    ), f"expected empty-placeholder finding on /0/canonical; got {errs}"


def test_type_map_regex_empty_placeholder_caught(tmp_path):
    """The load-bearing path: a `regex` write rule whose render value mixes a
    valid `${p}` with an empty `${}`. The empty-placeholder gate sits BEFORE
    the named-capture check, so the empty `${}` short-circuits with a precise
    error (not a misleading 'no matching capture group'), and the valid sibling
    capture neither masks it nor is itself wrongly flagged. Also exercises the
    write direction (render key = `native`), which the exact-rule test above
    does not."""
    write_path = tmp_path / "type-map-write.json"
    write_path.write_text(json.dumps([
        {
            "match": "regex",
            "canonical": "^Decimal128\\((?<p>\\d+),\\s*(?<s>\\d+)\\)$",
            "native": "NUMERIC(${p}, ${})",
        }
    ]))
    result = run_validator(write_path, "--semantic-only", schema_url=TYPE_MAP_WRITE_SCHEMA_URL)
    errs = errors_of(result, "type-map-rule")
    empty = [
        e for e in errs
        if e["path"] == "/0/native" and "empty" in e["message"] and "${}" in e["message"]
    ]
    assert len(empty) == 1, f"expected one empty-placeholder finding on /0/native; got {errs}"
    # The empty placeholder short-circuits the rule, so the valid `${p}` capture
    # is not reported as unbacked — no misleading 'capture' error.
    assert not any("capture" in e["message"] for e in errs), \
        f"valid capture wrongly flagged; got {errs}"


def test_type_map_unclosed_placeholder_caught(tmp_path):
    """A `${` with no closing `}` on a render value renders as a literal —
    flagged as a type-map-rule error (the #48 unclosed-brace fold-in)."""
    write_path = tmp_path / "type-map-write.json"
    write_path.write_text(json.dumps([
        {"match": "regex", "canonical": "^Decimal128\\((?<p>\\d+)\\)$", "native": "NUMERIC(${p)"}
    ]))
    result = run_validator(write_path, "--semantic-only", schema_url=TYPE_MAP_WRITE_SCHEMA_URL)
    errs = errors_of(result, "type-map-rule")
    assert any(e["path"] == "/0/native" and "unclosed" in e["message"] for e in errs), \
        f"expected unclosed-placeholder finding on /0/native; got {errs}"


def test_type_map_schemaless_native_scalar_canonical_caught(tmp_path):
    """A schemaless/structured-container native (JSONB) mapped to a scalar
    canonical (Utf8) is a type-map-rule error — the canonical must describe the
    shape, not collapse it to an opaque string."""
    read_path = tmp_path / "type-map-read.json"
    read_path.write_text(json.dumps([
        {"match": "exact", "native": "JSONB", "canonical": "Utf8"}
    ]))
    result = run_validator(read_path, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    errs = errors_of(result, "type-map-rule")
    assert any(
        e["path"] == "/0/canonical" and "schemaless" in e["message"] and "JSONB" in e["message"]
        for e in errs
    ), f"expected schemaless-container finding on /0/canonical; got {errs}"


def test_type_map_schemaless_native_container_canonical_ok(tmp_path):
    """The same natives mapped to a container canonical (Json) are clean —
    including a parameterized `array<...>` and a SQL `[]` array suffix."""
    read_path = tmp_path / "type-map-read.json"
    read_path.write_text(json.dumps([
        {"match": "exact", "native": "JSONB", "canonical": "Json"},
        {"match": "exact", "native": "VARIANT", "canonical": "Json"},
        {"match": "exact", "native": "object", "canonical": "Json"},
        {"match": "regex", "native": "^ARRAY<(?<t>[A-Z]+)>$", "canonical": "List"},
        {"match": "regex", "native": ".*\\[\\]$", "canonical": "Json"},
    ]))
    result = run_validator(read_path, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    assert not [
        e for e in errors_of(result, "type-map-rule")
        if "schemaless" in e["message"]
    ], "container canonicals must not be flagged"


def test_type_map_array_suffix_scalar_caught(tmp_path):
    """A SQL array-suffix native (`integer[]`, regex `.*[]$`) resolving to a
    scalar is caught; a non-empty bracketed type like `FOO[3]` is not a false
    positive."""
    read_path = tmp_path / "type-map-read.json"
    read_path.write_text(json.dumps([
        {"match": "regex", "native": ".*\\[\\]$", "canonical": "Utf8"},
        {"match": "regex", "native": "^FOO\\[[0-9]+\\]$", "canonical": "Utf8"},
    ]))
    result = run_validator(read_path, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    flagged = [e["path"] for e in errors_of(result, "type-map-rule") if "schemaless" in e["message"]]
    assert flagged == ["/0/canonical"], f"only the []-array rule should flag; got {flagged}"


def test_type_map_schemaless_rule_is_read_direction_only(tmp_path):
    """The schemaless check is read-only. A write map (canonical is the
    matcher, native the render) is never subject to it — `Json` rendering a
    scalar-looking native must stay clean."""
    write_path = tmp_path / "type-map-write.json"
    write_path.write_text(json.dumps([
        {"match": "exact", "canonical": "Json", "native": "JSONB"},
        {"match": "exact", "canonical": "Utf8", "native": "TEXT"},
    ]))
    result = run_validator(write_path, "--semantic-only", schema_url=TYPE_MAP_WRITE_SCHEMA_URL)
    assert not [
        e for e in errors_of(result, "type-map-rule") if "schemaless" in e["message"]
    ], "write-direction rules must not trigger the schemaless check"


def test_type_map_duplicate_rule_warned():
    result = run_validator(
        FIXTURES / "invalid_type_map_duplicate.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    warns = warnings_of(result, "type-map-rule")
    assert any("duplicate" in w["message"] and "BIGINT" in w["message"] for w in warns), \
        f"expected duplicate-rule warning; got {warns}"


def test_to_python_regex_passthroughs():
    """Direct unit test of the ECMA→Python translator's pass-through contract."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("vc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # ECMA named groups rewritten:
    assert mod._to_python_regex(r"^(?<n>\d+)$") == r"^(?P<n>\d+)$"
    # Anonymous groups untouched:
    assert mod._to_python_regex(r"^(\d+)$") == r"^(\d+)$"
    # Non-capturing untouched:
    assert mod._to_python_regex(r"^(?:foo|bar)$") == r"^(?:foo|bar)$"
    # Mixed: ECMA rewritten, anonymous left alone:
    assert mod._to_python_regex(r"^(?<a>\d+)-(\d+)$") == r"^(?P<a>\d+)-(\d+)$"


def test_malformed_sibling_type_map_caught(tmp_path):
    """A sibling type-map-read.json that's syntactically broken JSON must raise a hard error."""
    base = json.loads((VALID_API_CONNECTOR).read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-read.json").write_text("{")
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert any("could not be read or parsed" in e["message"] for e in errs), \
        f"expected JSON-decode error for malformed sibling; got {errs}"


def test_lambda_handles_empty_capture(tmp_path):
    """An empty-capture match must render as empty string (not leak the literal ${name})."""
    base = json.loads((VALID_API_CONNECTOR).read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-read.json").write_text(json.dumps([
        {
            "match": "regex",
            "native": "^OPTIONAL_(?<size>[0-9]*)$",
            "canonical": "FixedSizeBinary(${size})",
        }
    ]))
    (tmp_path / "endpoints").mkdir()
    (tmp_path / "endpoints" / "items.json").write_text(json.dumps({
        "$schema": "https://schemas.analitiq.ai/api-endpoint/latest.json",
        "endpoint_id": "items",
        "operations": {
            "read": {
                "request": {"method": "GET", "path": "/items"},
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "string", "native_type": "optional_", "arrow_type": "FixedSizeBinary()"}
                        }
                    }
                }
            }
        }
    }))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    # The rendered canonical should be `FixedSizeBinary()` (empty capture → ""),
    # which matches the endpoint's arrow_type. The literal `${size}` must not leak.
    assert not any("${size}" in e["message"] for e in errs), \
        f"empty capture leaked ${{size}} literal into rendered canonical; got {errs}"


def test_adbc_example_shape_invariants():
    """Pin ADBC contract distinguishing properties so a regression that re-adds
    `tls` or uses a non-enum `driver` is caught — Layer 1 already rejects these,
    but the example is canonical and worth defending here too."""
    ex = json.loads((REPO_ROOT / "skills" / "connector-spec-db" / "examples" /
                     "postgresql-adbc" / "postgresql-adbc.example.json").read_text())
    transport = ex["transports"]["database"]
    assert transport["transport_type"] == "adbc"
    assert transport["driver"] in ("postgresql", "snowflake", "bigquery"), \
        f"driver must be in the closed enum; got {transport['driver']!r}"
    assert "tls" not in transport, "ADBC transport must not declare a tls block"
    assert "db_kwargs" in transport or "dsn" in transport, \
        "AdbcTransport requires at least one of dsn / db_kwargs"


def test_valid_type_map_passes_semantic():
    result = run_validator(
        FIXTURES / "valid_type_map.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    errs = errors_of(result, "type-map-rule")
    assert not errs, f"expected no errors on valid type-map; got {errs}"


def test_type_map_python_named_group_caught():
    """ECMA-262 is the contract; (?P<name>...) Python declaration syntax must be rejected."""
    result = run_validator(
        FIXTURES / "invalid_type_map_python_syntax.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    errs = errors_of(result, "type-map-rule")
    assert any("Python-only" in e["message"] for e in errs), \
        f"expected Python-syntax finding; got {errs}"


def test_type_map_broken_regex_caught_without_template():
    """A regex rule with a malformed native must be flagged even when canonical has no ${...}."""
    result = run_validator(
        FIXTURES / "invalid_type_map_broken_regex.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    errs = errors_of(result, "type-map-rule")
    assert any("not a valid regex" in e["message"] and e["path"] == "/0/native" for e in errs), \
        f"expected broken-regex finding on rule 0; got {errs}"


def test_type_map_python_recursive_call_caught():
    """Python-only `(?P>name)` recursive calls are also rejected."""
    result = run_validator(
        FIXTURES / "invalid_type_map_python_recursive.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    errs = errors_of(result, "type-map-rule")
    assert any("Python-only" in e["message"] for e in errs), \
        f"expected Python-syntax finding for recursive call; got {errs}"


def test_type_map_unknown_match_value_caught():
    """`match` is a closed enum {exact, regex}; typos must be rejected, not silently skipped."""
    result = run_validator(
        FIXTURES / "invalid_type_map_unknown_match.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    errs = errors_of(result, "type-map-rule")
    assert any("'exact' | 'regex'" in e["message"] and "prefix" in e["message"] for e in errs), \
        f"expected unknown-match finding; got {errs}"


def test_type_map_legacy_wrapped_shape_warned():
    """The legacy wrapped `{native_to_arrow: {rules: [...]}}` shape is no
    longer the on-disk shape. Authors who haven't migrated must see a hint."""
    result = run_validator(
        FIXTURES / "invalid_type_map_legacy_wrapped.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    warns = warnings_of(result, "type-map-rule")
    assert any("pre-migration type-map shape" in w["message"] for w in warns), \
        f"expected legacy-shape warning; got {warns}"


def test_type_map_legacy_method_list_caught():
    """Top-level list with legacy `method` key (most common transcription of
    the old shape) must surface the rename pointer, not just opaque per-rule
    errors."""
    result = run_validator(
        FIXTURES / "invalid_type_map_legacy_method_list.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    warns = warnings_of(result, "type-map-rule")
    assert any("pre-migration type-map shape" in w["message"]
               and "method" in w["message"]
               for w in warns), \
        f"expected legacy-method-list warning; got {warns}"


def test_type_map_legacy_rules_keyed_caught():
    """`{rules: [...]}` object wrapper (variant 2 of the legacy shape) also
    surfaces the migration hint."""
    result = run_validator(
        FIXTURES / "invalid_type_map_legacy_rules_keyed.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    warns = warnings_of(result, "type-map-rule")
    assert any("pre-migration type-map shape" in w["message"] for w in warns), \
        f"expected legacy-rules-keyed warning; got {warns}"


def test_storage_kind_malformed_sibling_type_map_surfaced(tmp_path):
    """Storage-kind branch must NOT silently swallow OSError/JSONDecodeError.
    Mirror the api/db branch's error surfacing."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    base["kind"] = "file"
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-read.json").write_text("{")  # malformed JSON
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert any("could not be read or parsed" in e["message"] for e in errs), \
        f"storage-kind malformed sibling JSON must be surfaced; got {errs}"


def test_storage_kind_legacy_wrapped_sibling_type_map_surfaced(tmp_path):
    """Storage-kind branch must reject non-list sibling type-map shape with
    the same 'must be a non-empty array' error as api/db. Mirrors the api/db
    branch's list-shape guard — without it the storage branch would silently
    no-op on a legacy-wrapped (`{native_to_arrow: {rules: [...]}}`) sibling."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    base["kind"] = "stdout"
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-read.json").write_text(json.dumps({
        "native_to_arrow": {"rules": [
            {"method": "exact", "native": "X", "canonical": "Utf8"}
        ]}
    }))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert any("non-empty array" in e["message"] for e in errs), \
        f"storage-kind non-list sibling must be flagged; got {errs}"


def test_type_map_non_dict_entry_warned(tmp_path):
    """Mixed-content list `[{valid}, "garbage"]`: dict entries are validated;
    non-dict entries surface a warning instead of silent drop."""
    tm = tmp_path / "type-map-read.json"
    tm.write_text(json.dumps([
        {"match": "exact", "native": "BIGINT", "canonical": "Int64"},
        "stray-string",
    ]))
    result = run_validator(tm, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    warns = warnings_of(result, "type-map-rule")
    assert any("not an object" in w["message"] and w["path"] == "/1" for w in warns), \
        f"expected non-dict-entry warning; got {warns}"


def test_type_map_python_backreference_caught():
    """Python-only `(?P=name)` backreferences must also be rejected."""
    result = run_validator(
        FIXTURES / "invalid_type_map_python_backref.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    errs = errors_of(result, "type-map-rule")
    assert any("Python-only" in e["message"] for e in errs), \
        f"expected Python-syntax finding for backref; got {errs}"


def test_unhashable_rule_value_does_not_crash(tmp_path):
    """A `match`/`native` that isn't a primitive must not crash the validator,
    AND must surface a finding so the un-checkable rule isn't a silent skip.
    The non-string-native check supersedes the unhashable-key path for most
    cases (caught earlier with a clearer message)."""
    tm = tmp_path / "type-map-read.json"
    tm.write_text(json.dumps([
        {"match": "exact", "native": ["X"], "canonical": "Utf8"},
        {"match": "exact", "native": "BIGINT", "canonical": "Int64"}
    ]))
    result = run_validator(tm, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    assert "findings" in result, f"expected structured output, got {result}"
    warns = warnings_of(result, "type-map-rule")
    assert any("native must be a string" in w["message"] for w in warns), \
        f"expected non-string-native warning so the rule isn't a silent skip; got {warns}"


def test_regex_rule_with_nonstring_canonical_still_compile_validated():
    """A broken regex must surface even when `canonical` is non-string —
    the canonical-string gate must not short-circuit regex compilation."""
    result = run_validator(
        FIXTURES / "invalid_type_map_regex_with_nonstring_canonical.json",
        "--semantic-only",
        schema_url=TYPE_MAP_READ_SCHEMA_URL,
    )
    errs = errors_of(result, "type-map-rule")
    assert any("not a valid regex" in e["message"] and e["path"] == "/0/native" for e in errs), \
        f"expected broken-regex finding on rule 0 (canonical=null); got {errs}"
    assert any("not a valid regex" in e["message"] and e["path"] == "/1/native" for e in errs), \
        f"expected broken-regex finding on rule 1 (canonical=list); got {errs}"


def test_empty_type_map_warns_under_semantic_only(tmp_path):
    """An empty type map must surface a warning under `--semantic-only`
    (Layer 1 owns the minItems error, but bypassing it would otherwise be a
    silent pass)."""
    tm = tmp_path / "type-map-read.json"
    tm.write_text("[]")
    result = run_validator(tm, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    warns = warnings_of(result, "type-map-rule")
    assert any("empty array" in w["message"] for w in warns), \
        f"expected empty-array warning; got {warns}"


def test_connector_validation_surfaces_sibling_rule_errors():
    """check_type_map_coverage must run rule checks on the sibling so a broken regex
    in type-map-read.json is caught when validating the connector, not only when
    invoked against the type map directly."""
    result = run_validator(
        FIXTURES / "connector_with_broken_type_map" / "connector.json",
        "--semantic-only",
    )
    errs = errors_of(result, "type-map-rule")
    assert any("not a valid regex" in e["message"] for e in errs), \
        f"expected broken sibling regex to surface via connector path; got {errs}"


# ---------------------------------------------------------------------------
# Write-direction maps (type-map-write.json) + uppercase pattern rule
# ---------------------------------------------------------------------------


WRITE_MAP_RULES = [
    {"match": "exact", "canonical": "Utf8", "native": "TEXT"},
    {
        "match": "regex",
        "canonical": "^Decimal(128|256)\\((?<p>\\d+),\\s*(?<s>\\d+)\\)$",
        "native": "NUMERIC(${p}, ${s})",
    },
]


def test_write_map_rules_pass_under_write_filename(tmp_path):
    """Canonical-as-matcher / native-as-render rules (the write orientation)
    must validate cleanly when the file is named type-map-write.json."""
    write_path = tmp_path / "type-map-write.json"
    write_path.write_text(json.dumps(WRITE_MAP_RULES))
    result = run_validator(write_path, "--semantic-only", schema_url=TYPE_MAP_WRITE_SCHEMA_URL)
    errs = errors_of(result, "type-map-rule")
    assert not errs, f"expected write-direction rules to validate under the write filename; got {errs}"


def test_direction_detected_by_filename(tmp_path):
    """Direction is a filename contract: the exact-rule `${}` gate applies to
    the RENDER side, which is `native` for write maps and `canonical` for
    read maps. The same two rules must flag opposite entries under the two
    filenames."""
    rules = [
        # ${} in native → render-side violation under WRITE only.
        {"match": "exact", "canonical": "Utf8", "native": "VARCHAR(${n})"},
        # ${} in canonical → render-side violation under READ only.
        {"match": "exact", "native": "TEXT", "canonical": "Utf8(${x})"},
    ]
    write_path = tmp_path / "type-map-write.json"
    write_path.write_text(json.dumps(rules))
    result = run_validator(write_path, "--semantic-only", schema_url=TYPE_MAP_WRITE_SCHEMA_URL)
    paths = [e["path"] for e in errors_of(result, "type-map-rule")]
    assert paths == ["/0/native"], f"write direction must flag /0/native only; got {paths}"

    read_path = tmp_path / "type-map-read.json"
    read_path.write_text(json.dumps(rules))
    result = run_validator(read_path, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    paths = [e["path"] for e in errors_of(result, "type-map-rule")]
    assert paths == ["/1/canonical"], f"read direction must flag /1/canonical only; got {paths}"


def test_write_map_unbacked_placeholder_caught(tmp_path):
    """`${name}` in the write map's `native` must be backed by a `(?<name>…)`
    capture in `canonical` — the inverted form of the read-map rule."""
    write_path = tmp_path / "type-map-write.json"
    write_path.write_text(json.dumps([
        {
            "match": "regex",
            "canonical": "^Decimal128\\((?<p>\\d+),\\s*(?<s>\\d+)\\)$",
            "native": "NUMERIC(${p}, ${q})",
        }
    ]))
    result = run_validator(write_path, "--semantic-only", schema_url=TYPE_MAP_WRITE_SCHEMA_URL)
    errs = errors_of(result, "type-map-rule")
    assert any(
        e["path"] == "/0/native" and "${q}" in e["message"] and "canonical" in e["message"]
        for e in errs
    ), f"expected unbacked ${{q}} finding on /0/native; got {errs}"


def test_write_map_vocabulary_gap_warned(tmp_path):
    """A write map that misses canonical families gets a grouped rule-8
    warning (not an error — render_column_type overrides are legitimate)."""
    write_path = tmp_path / "type-map-write.json"
    write_path.write_text(json.dumps(WRITE_MAP_RULES))
    result = run_validator(write_path, "--semantic-only", schema_url=TYPE_MAP_WRITE_SCHEMA_URL)
    warns = warnings_of(result, "type-map-write-coverage")
    assert len(warns) == 1, f"expected one grouped vocabulary warning; got {warns}"
    msg = warns[0]["message"]
    listed = msg.split("families: ", 1)[1].split(". The write", 1)[0].split(", ")
    for family in ("Boolean", "Int64", "Json", "Timestamp (tz)"):
        assert family in listed, f"expected missing family {family!r} listed; got {listed}"
    assert "Utf8" not in listed, \
        f"Utf8 is covered and must not be listed as missing; got {listed}"
    # Note: ", ".split also bisects "Decimal128(p, s)" — check the fragment.
    assert "Decimal128(p" not in listed, \
        f"Decimal is covered by the regex rule and must not be listed; got {listed}"


def test_write_map_full_vocabulary_passes(tmp_path):
    """The postgres-reference-shaped write map fixture resolves every probe."""
    result = run_validator(
        FIXTURES / "valid_db_connector" / "type-map-write.json",
        "--semantic-only",
        schema_url=TYPE_MAP_WRITE_SCHEMA_URL,
    )
    errs = errors_of(result, "type-map-rule")
    assert not errs, f"expected reference write map to pass rule checks; got {errs}"
    warns = warnings_of(result, "type-map-write-coverage")
    assert not warns, f"expected full vocabulary coverage; got {warns}"


def test_write_map_vocabulary_gap_surfaces_from_connector_path(tmp_path):
    """The rule-8 probe must also run when the connector is validated with
    its siblings, not only against the standalone write map."""
    base = json.loads((FIXTURES / "valid_db_connector" / "connector.json").read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-read.json").write_text(
        (FIXTURES / "valid_db_connector" / "type-map-read.json").read_text()
    )
    (tmp_path / "type-map-write.json").write_text(json.dumps(WRITE_MAP_RULES))
    result = run_validator(doc_path, "--semantic-only")
    warns = warnings_of(result, "type-map-write-coverage")
    assert warns and "Boolean" in warns[0]["message"], \
        f"expected vocabulary-gap warning via connector path; got {warns}"


def test_read_regex_lowercase_pattern_warned(tmp_path):
    """Read-map regex patterns are matched against UPPERCASED natives;
    lowercase literals are dead rules and must warn. Capture group names
    and escapes (\\d) stay exempt."""
    tm = tmp_path / "type-map-read.json"
    tm.write_text(json.dumps([
        {"match": "regex", "native": "^varchar\\((?<len>\\d+)\\)$", "canonical": "Utf8"},
        {"match": "regex", "native": "^NUMERIC\\((?<p>\\d+)\\)$", "canonical": "Decimal128(${p}, 0)"},
    ]))
    result = run_validator(tm, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    warns = warnings_of(result, "type-map-rule")
    lowercase_warns = [w for w in warns if "UPPERCASED" in w["message"]]
    assert len(lowercase_warns) == 1 and lowercase_warns[0]["path"] == "/0/native", \
        f"expected exactly one uppercase warning on /0/native; got {warns}"


def test_read_exact_lowercase_not_warned(tmp_path):
    """Exact rules are normalized automatically by the engine — lowercase
    exact natives must not trigger the uppercase warning."""
    tm = tmp_path / "type-map-read.json"
    tm.write_text(json.dumps([
        {"match": "exact", "native": "jsonb", "canonical": "Json"},
    ]))
    result = run_validator(tm, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    warns = [w for w in warnings_of(result, "type-map-rule") if "UPPERCASED" in w["message"]]
    assert not warns, f"exact rules must be exempt from the uppercase warning; got {warns}"


def test_unrecognized_filename_direction_default_warned(tmp_path):
    """A type map validated under a filename that is neither
    type-map-read.json nor type-map-write.json silently got READ semantics
    before; now the defaulted direction must surface as a warning so a
    misplaced write map's vanished write-direction checks aren't silent."""
    odd_path = tmp_path / "some-map.json"
    odd_path.write_text(json.dumps(WRITE_MAP_RULES))
    result = run_validator(odd_path, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    warns = warnings_of(result, "type-map-rule")
    assert any("direction defaulted to 'read'" in w["message"] for w in warns), \
        f"expected direction-default warning for unrecognized filename; got {warns}"
    # Under the recognized filenames the warning must NOT fire.
    for name in ("type-map-read.json", "type-map-write.json"):
        good_path = tmp_path / name
        good_path.write_text(json.dumps(WRITE_MAP_RULES))
        result = run_validator(good_path, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
        warns = warnings_of(result, "type-map-rule")
        assert not any("direction defaulted" in w["message"] for w in warns), \
            f"direction-default warning must not fire for {name}; got {warns}"


def test_escaped_lowercase_letter_not_silent(tmp_path):
    """An unknown lowercase-letter escape (`\\q`) cannot slip past the
    uppercase check unseen: Python's `re` rejects unknown ASCII-letter
    escapes outright, so the rule surfaces as a compile ERROR before the
    warning stage. Escaped punctuation (`\\(`) and known class escapes
    (`\\d`, `\\s`) stay exempt from the uppercase warning."""
    tm = tmp_path / "type-map-read.json"
    tm.write_text(json.dumps([
        {"match": "regex", "native": "^NUMERIC\\(\\q\\)$", "canonical": "Utf8"},
        {"match": "regex", "native": "^VARCHAR\\(\\d+\\)\\s*$", "canonical": "Utf8"},
    ]))
    result = run_validator(tm, "--semantic-only", schema_url=TYPE_MAP_READ_SCHEMA_URL)
    errs = errors_of(result, "type-map-rule")
    assert any("not a valid regex" in e["message"] and e["path"] == "/0/native" for e in errs), \
        f"expected \\q rule to fail the compile gate; got {errs}"
    warns = [w for w in warnings_of(result, "type-map-rule") if "UPPERCASED" in w["message"]]
    assert not warns, \
        f"the all-uppercase rule with \\d/\\s escapes must not warn; got {warns}"


def test_tls_consistency_uppercase_verify_modes_caught(tmp_path):
    """MySQL-style VERIFY_CA / VERIFY_IDENTITY enum values must trigger the
    ssl_ca_certificate requirement (the check normalizes case and _/-)."""
    base = json.loads((FIXTURES / "valid_db_connector" / "connector.json").read_text())
    inputs = base["connection_contract"]["inputs"]
    inputs["ssl_mode"] = {
        "source": "user",
        "phase": "pre_auth",
        "storage": "connection.parameters",
        "type": "string",
        "required": False,
        "default": "PREFERRED",
        "enum": ["DISABLED", "PREFERRED", "REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"],
    }
    inputs.pop("ssl_ca_certificate", None)
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "tls-consistency")
    assert any("ssl_ca_certificate" in e["message"] for e in errs), \
        f"expected VERIFY_CA/VERIFY_IDENTITY to require ssl_ca_certificate; got {errs}"
    # Positive counterpart: declaring the CA input clears the finding.
    inputs["ssl_ca_certificate"] = {
        "source": "user",
        "phase": "pre_auth",
        "storage": "secrets",
        "type": "string",
        "required": False,
        "secret": True,
    }
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "tls-consistency")
    assert not errs, f"expected no tls-consistency error once ssl_ca_certificate declared; got {errs}"


def test_storage_kind_write_map_sibling_rule_checked(tmp_path):
    """The storage-kind branch must rule-check a present type-map-write.json
    with WRITE direction — a broken matcher regex (in `canonical` for the
    write direction) must surface."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    base["kind"] = "file"
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-write.json").write_text(json.dumps([
        {"match": "regex", "canonical": "^Decimal128([0-9", "native": "NUMERIC"}
    ]))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-rule")
    assert any("not a valid regex" in e["message"] and e["path"] == "/0/canonical" for e in errs), \
        f"storage-kind write-map sibling must be rule-checked in write direction; got {errs}"


def test_read_coverage_normalizes_native_case(tmp_path):
    """Endpoint natives are matched after UPPERCASE + whitespace-collapse
    normalization, so an uppercase exact rule covers a lowercase endpoint
    native (mirrors the engine)."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-read.json").write_text(json.dumps([
        {"match": "exact", "native": "STRING", "canonical": "Utf8"},
        {"match": "exact", "native": "BOOLEAN", "canonical": "Boolean"},
    ]))
    src = FIXTURES / "valid_api_connector"
    (tmp_path / "endpoints").mkdir()
    (tmp_path / "endpoints" / "ping.json").write_text((src / "endpoints" / "ping.json").read_text())
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert not errs, f"expected lowercase endpoint natives to resolve via uppercase rules; got {errs}"


def test_read_coverage_collapses_native_whitespace(tmp_path):
    """The other half of native normalization: runs of whitespace collapse
    to a single space, so 'double  precision' (two spaces) matches an
    exact rule authored 'DOUBLE PRECISION'."""
    base = json.loads(VALID_API_CONNECTOR.read_text())
    doc_path = tmp_path / "connector.json"
    doc_path.write_text(json.dumps(base))
    (tmp_path / "type-map-read.json").write_text(json.dumps([
        {"match": "exact", "native": "DOUBLE PRECISION", "canonical": "Float64"},
    ]))
    (tmp_path / "endpoints").mkdir()
    (tmp_path / "endpoints" / "items.json").write_text(json.dumps({
        "$schema": "https://schemas.analitiq.ai/api-endpoint/latest.json",
        "endpoint_id": "items",
        "operations": {
            "read": {
                "request": {"method": "GET", "path": "/items"},
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {
                        "type": "object",
                        "properties": {
                            "amount": {
                                "type": "number",
                                "native_type": "double  precision",
                                "arrow_type": "Float64",
                            }
                        },
                    },
                },
            }
        },
    }))
    result = run_validator(doc_path, "--semantic-only")
    errs = errors_of(result, "type-map-coverage")
    assert not errs, \
        f"expected multi-space native to collapse and match single-space rule; got {errs}"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_malformed_json_diagnosed(tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text('{"kind":')
    result = run_validator(bad, "--semantic-only")
    errs = [f for f in result["findings"] if f["validator"] == "json-schema"]
    assert errs, f"expected a json-schema finding for malformed JSON; got {result['findings']}"
    assert result["passed"] is False


def test_missing_document_path_diagnosed(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    result = run_validator(missing, "--semantic-only")
    errs = [f for f in result["findings"] if f["validator"] == "json-schema"]
    assert errs, f"expected a json-schema finding for missing path; got {result['findings']}"
    assert result["passed"] is False


def test_semantic_and_json_only_are_mutually_exclusive(tmp_path):
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--schema-url", SCHEMA_URL,
            "--document", str(VALID_API_CONNECTOR),
            "--semantic-only", "--json-only",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "mutually exclusive" in proc.stderr


def test_multiple_validators_all_fire(tmp_path):
    """A doc that triggers reserved-field AND auth-shape should report both."""
    base = json.loads((FIXTURES / "invalid_auth_shape_oauth_cc.json").read_text())
    base["created_at"] = "should-not-be-here"
    doc_path = tmp_path / "multi.json"
    doc_path.write_text(json.dumps(base))
    result = run_validator(doc_path, "--semantic-only")
    ids = {f["validator"] for f in result["findings"] if f["severity"] == "error"}
    assert {"reserved-field", "auth-shape"}.issubset(ids), f"expected both validator ids; got {ids}"
