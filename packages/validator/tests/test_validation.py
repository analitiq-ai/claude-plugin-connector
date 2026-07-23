"""End-to-end validation tests — the validator delegates single-document
validity to the contract models and adds the cross-file coverage checks.

The `invalid_write_from_input` case is the original sevdesk defect that started
this work: a write body mapping a bare field name (`{from_input: "category"}`)
instead of the record. The model rejects it, so the validator now catches it —
the gap the old validator missed.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from analitiq.contracts.endpoint_identity import derive_db_endpoint_id, slug

CORPUS = Path(__file__).resolve().parent / "corpus"
_REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_SRC_ROOT = _REPO_ROOT / "contract-models" / "src"
SRC_ROOT = _REPO_ROOT / "validator" / "src"
# Drive the CLI the way a consumer does: import the package and call main(). With
# `python -c "<code>" --document X`, argv is ["-c", "--document", "X"], so argparse
# parses the flags exactly as the `analitiq-validate` console script would. Only
# the two public source trees ride PYTHONPATH — the validator and the contract
# models — so this exercises precisely what an installed consumer gets.
_CLI_CODE = "from analitiq.validator import main; import sys; sys.exit(main())"
_CLI_PYTHONPATH = os.pathsep.join([str(SRC_ROOT), str(CONTRACTS_SRC_ROOT)])

# (corpus file, expected pass?) — single-document verdicts.
DOC_CASES = [
    ("valid_read.json", True),
    ("valid_write_insert.json", True),
    ("valid_connector_sync_driver.json", True),
    ("invalid_reserved_field.json", False),
    ("invalid_write_from_input.json", False),
    ("invalid_connector_bare_driver.json", False),
]


def _errors(findings):
    return [f for f in findings if f["severity"] == "error"]


@pytest.mark.parametrize("name,should_pass", DOC_CASES)
def test_single_document_verdict(name, should_pass, validator):
    # No doc_path: these exercise pure single-document validity, not the
    # filename↔endpoint_id cross-file check (the corpus filenames are labels).
    doc = json.loads((CORPUS / name).read_text())
    findings = validator.validate_document(doc)
    errors = _errors(findings)
    assert (not errors) == should_pass, (
        f"{name}: expected {'pass' if should_pass else 'fail'}, "
        f"got errors={[e['message'] for e in errors]}"
    )


def test_from_input_defect_is_caught(validator):
    """The sevdesk regression: a bare-field from_input write body must be rejected."""
    doc = json.loads((CORPUS / "invalid_write_from_input.json").read_text())
    findings = validator.validate_document(doc)
    assert any(
        "from_input" in f["message"] and f["severity"] == "error" for f in findings
    ), "the from_input contract rule was not enforced"


def test_bare_sqlalchemy_driver_is_a_contract_model_finding(validator):
    """The sync-driver boundary #70 opened, exercised end-to-end (issue #72):
    the valid corpus twin (`valid_connector_sync_driver.json`, driver
    `redshift+redshift_connector`) passes in DOC_CASES above; here the bare
    variant (no `dialect+` segment) must surface as a contract-model finding
    on the transport's driver field — the model rejection reaching a consumer
    of validate_document, not just the pydantic layer."""
    doc = json.loads((CORPUS / "invalid_connector_bare_driver.json").read_text())
    errors = _errors(validator.validate_document(doc))
    assert any(
        f["validator"] == "contract-model" and f["path"].endswith("/driver")
        for f in errors
    ), errors


def test_unrecognized_document_errors(validator):
    # A document we were asked to validate but cannot identify is a failure,
    # not a pass — otherwise a broken doc silently gets a green light.
    for doc in ({"totally": "unknown"}, {}, 42, "hello", None):
        findings = validator.validate_document(doc)
        assert _errors(findings), f"{doc!r} should error, got {findings}"


def test_kindless_connector_errors(validator):
    # A connector-shaped dict missing its `kind` discriminator must reach the
    # model and fail (not fall through to a silent pass).
    doc = {"connector_id": "x", "transports": {}, "connection_contract": {},
           "default_transport": "m"}
    assert _errors(validator.validate_document(doc))


def _write_tree(root: Path, connector: dict, read_map, endpoints: dict):
    (root / "endpoints").mkdir(parents=True)
    (root / "connector.json").write_text(json.dumps(connector))
    (root / "type-map-read.json").write_text(json.dumps(read_map))
    for name, ep in endpoints.items():
        (root / "endpoints" / name).write_text(json.dumps(ep))


API = "https://schemas.analitiq.ai/api-endpoint/latest.json"
JS = "https://json-schema.org/draft/2020-12/schema"


def _endpoint(native_type, arrow_type, endpoint_id="widgets", path="/widgets"):
    return {
        "$schema": API, "endpoint_id": endpoint_id,
        "operations": {"read": {
            "request": {"method": "GET", "path": path}, "params": {},
            "response": {
                "records": {"ref": "response.body"},
                "schema": {"$schema": JS, "type": "array", "items": {"type": "object",
                    "properties": {"a": {"type": "string",
                        "native_type": native_type, "arrow_type": arrow_type}}}},
            }}}}


def test_valid_embedded_schema_passes(validator):
    ep = _endpoint("STRING", "Utf8")
    assert not any(
        e["validator"] == "embedded-json-schema"
        for e in _errors(validator.validate_document(ep))
    )


def test_embedded_schema_must_be_valid_draft_2020_12(validator):
    """An embedded input/response schema that parses (arrow-valid) but is not a
    valid JSON Schema Draft 2020-12 document is caught by the validator. The
    contract model checks the arrow_type pairing, not meta-schema validity."""
    ep = _endpoint("STRING", "Utf8")
    # `minItems` must be a non-negative integer; a string is meta-invalid, and the
    # contract model doesn't inspect it, so only the embedded-json-schema check fires.
    ep["operations"]["read"]["response"]["schema"]["minItems"] = "notanumber"
    errors = _errors(validator.validate_document(ep))
    assert any(e["validator"] == "embedded-json-schema" for e in errors), errors


def test_embedded_schema_rejects_other_dialect(validator):
    """A meta-valid schema that DECLARES another draft (e.g. Draft-07) is not
    Draft 2020-12 and is rejected — `check_schema` alone would miss it."""
    ep = _endpoint("STRING", "Utf8")
    ep["operations"]["read"]["response"]["schema"]["$schema"] = (
        "http://json-schema.org/draft-07/schema#"
    )
    errors = _errors(validator.validate_document(ep))
    assert any(e["validator"] == "embedded-json-schema" for e in errors), errors


@pytest.fixture
def connector_base():
    # A real, model-valid api connector (hand-crafting the exact Connector
    # shape is error-prone; the corpus copy is the source of truth).
    return json.loads((CORPUS / "valid_connector.json").read_text())


def test_coverage_passes_when_map_covers_endpoints(tmp_path, connector_base, validator):
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "STRING", "canonical": "Utf8"}],
                {"widgets.json": _endpoint("STRING", "Utf8")})
    findings = validator.validate_document(connector_base, doc_path=tmp_path / "connector.json")
    assert not _errors(findings), [e["message"] for e in _errors(findings)]


def test_coverage_passes_with_lowercase_exact_matcher(tmp_path, connector_base, validator):
    # #1007: a lowercase `exact` native the runtime resolves fine must not be
    # reported as uncovered. The endpoint declares `varchar`; the runtime
    # normalizes both sides and matches, so coverage must too.
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "varchar", "canonical": "Utf8"}],
                {"widgets.json": _endpoint("varchar", "Utf8")})
    findings = validator.validate_document(connector_base, doc_path=tmp_path / "connector.json")
    assert not _errors(findings), [e["message"] for e in _errors(findings)]


def test_coverage_flags_uncovered_native(tmp_path, connector_base, validator):
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "STRING", "canonical": "Utf8"}],
                {"widgets.json": _endpoint("BIGINT", "Int64")})
    errors = _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))
    assert any("no matching rule" in e["message"] for e in errors)


def test_coverage_flags_arrow_mismatch(tmp_path, connector_base, validator):
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "STRING", "canonical": "Utf8"}],
                {"widgets.json": _endpoint("STRING", "Int64")})
    errors = _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))
    assert any("resolves to" in e["message"] and "Int64" in e["message"] for e in errors)


def test_coverage_flags_missing_read_map(tmp_path, connector_base, validator):
    (tmp_path / "connector.json").write_text(json.dumps(connector_base))
    errors = _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))
    assert any("type-map-read.json" in e["message"] for e in errors)


def _object_endpoint():
    # An `Object` arrow_type requires a sibling `properties` map (model rule).
    return {
        "$schema": API, "endpoint_id": "widgets",
        "operations": {"read": {
            "request": {"method": "GET", "path": "/widgets"}, "params": {},
            "response": {
                "records": {"ref": "response.body"},
                "schema": {"$schema": JS, "type": "array", "items": {"type": "object",
                    "properties": {"a": {"type": "object",
                        "native_type": "JSONB", "arrow_type": "Object",
                        "properties": {"inner": {"type": "string"}}}}}},
            }}}}


def test_coverage_json_narrowing_allowed(tmp_path, connector_base, validator):
    # A read map that renders `Json` satisfies an endpoint declaring `Object`.
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "JSONB", "canonical": "Json"}],
                {"widgets.json": _object_endpoint()})
    assert not _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))


def test_coverage_json_narrowing_is_narrow(tmp_path, connector_base, validator):
    # ...but `Json` does NOT satisfy a scalar like `Int64` (the allowance is narrow).
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "JSONB", "canonical": "Json"}],
                {"widgets.json": _endpoint("JSONB", "Int64")})
    assert _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))


def test_coverage_checks_field_named_like_a_keyword(tmp_path, connector_base, validator):
    # A response field literally named `default` must still be coverage-checked
    # (the schema-aware walk treats `properties` children as field names).
    ep = {"$schema": API, "endpoint_id": "widgets",
          "operations": {"read": {
              "request": {"method": "GET", "path": "/widgets"}, "params": {},
              "response": {"records": {"ref": "response.body"},
                  "schema": {"$schema": JS, "type": "array", "items": {"type": "object",
                      "properties": {"default": {"type": "string",
                          "native_type": "WEIRDTYPE", "arrow_type": "Utf8"}}}}}}}}
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "STRING", "canonical": "Utf8"}],  # no WEIRDTYPE rule
                {"widgets.json": ep})
    errors = _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))
    assert any("WEIRDTYPE" in e["message"] and "no matching rule" in e["message"] for e in errors)


def test_coverage_exact_match_normalizes_both_sides(validator):
    # Mirrors the runtime reader (#1007): an `exact` rule's `native` is
    # normalized the same way as the probe — trim, collapse internal whitespace
    # runs, uppercase — on BOTH sides. So a lowercase or extra-spaced matcher
    # covers the (normalized) endpoint native, exactly as the runtime resolves
    # it — the validator is no longer stricter than the runtime.
    assert validator._render_canonical("STRING", [{"match": "exact", "native": "string", "canonical": "Utf8"}]) == "Utf8"
    assert validator._render_canonical("STRING", [{"match": "exact", "native": "STRING", "canonical": "Utf8"}]) == "Utf8"
    # Whitespace: a two-space matcher covers a single-space native.
    assert validator._render_canonical("character varying", [{"match": "exact", "native": "CHARACTER  VARYING", "canonical": "Utf8"}]) == "Utf8"
    # A genuinely different native is still uncovered.
    assert validator._render_canonical("STRING", [{"match": "exact", "native": "BIGINT", "canonical": "Int64"}]) is None


def test_normalize_native_is_the_canonical(validator):
    # The validator imports the single source of truth — it does not
    # reimplement it — so coverage normalizes exactly as every reader does.
    from analitiq.contracts.type_map import normalize_native_type
    assert validator.connectors._normalize_native is normalize_native_type
    # strip → collapse internal whitespace runs → uppercase.
    assert normalize_native_type("  character  varying ") == "CHARACTER VARYING"
    assert normalize_native_type("varchar") == "VARCHAR"
    assert normalize_native_type("Timestamp\tWith Time  Zone") == "TIMESTAMP WITH TIME ZONE"


def test_canonical_eq_normalizes_separators_not_identifiers(validator):
    eq = validator._canonical_eq
    assert eq("Decimal128(38, 9)", "Decimal128(38,9)")          # param spacing insignificant
    assert eq("Timestamp(MICROSECOND, UTC)", "Timestamp(MICROSECOND,UTC)")
    # Whitespace INSIDE a token is significant — must NOT compare equal.
    assert not eq("Time stamp(SECOND)", "Timestamp(SECOND)")
    assert not eq("Timestamp(MICRO SECOND)", "Timestamp(MICROSECOND)")


def test_walk_collects_tuple_form_items(validator):
    # Draft-2019-09 tuple-form `items: [...]` must be traversed (mirrors the model).
    ep = {"operations": {"read": {"response": {"schema": {"type": "array",
        "items": [{"type": "object", "properties": {
            "x": {"native_type": "WEIRDTYPE", "arrow_type": "Utf8"}}}]}}}}}
    pairs = validator._collect_native_arrow_pairs(ep)
    assert ("WEIRDTYPE", "Utf8", "/operations/read/response/schema/items/0/properties/x") in pairs


def test_coverage_regex_rule_with_capture(tmp_path, connector_base, validator):
    # A regex read rule with a named capture + ${name} render must resolve.
    _write_tree(tmp_path, connector_base,
                [{"match": "regex", "native": r"NUMERIC\((?<p>\d+),\s*(?<s>\d+)\)",
                  "canonical": "Decimal128(${p}, ${s})"}],
                {"widgets.json": _endpoint("NUMERIC(38,9)", "Decimal128(38, 9)")})
    assert not _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))


def test_coverage_flags_duplicate_endpoint_id(tmp_path, connector_base, validator):
    # Issue #917 Gap 2(a): two endpoint files sharing an endpoint_id are flagged
    # as a duplicate (spec: endpoint_id unique within the connector release),
    # not only obliquely as a filename mismatch.
    ep = _endpoint("STRING", "Utf8", endpoint_id="dup", path="/dup")
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "STRING", "canonical": "Utf8"}],
                {"dup.json": ep, "other.json": ep})
    errors = _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))
    assert any(e["validator"] == "endpoint-id-unique" and "dup" in e["message"] for e in errors)


def test_coverage_distinct_endpoint_ids_pass(tmp_path, connector_base, validator):
    # Two endpoints with distinct ids (matching filenames + paths) raise no error.
    a = _endpoint("STRING", "Utf8", endpoint_id="alpha", path="/alpha")
    b = _endpoint("STRING", "Utf8", endpoint_id="beta", path="/beta")
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "STRING", "canonical": "Utf8"}],
                {"alpha.json": a, "beta.json": b})
    assert not _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))


# --- Gap 2(b): endpoint_id must be the derived path locator (io-contracts resources[].key) ---

def test_flatten_api_locator(validator):
    f = validator._flatten_api_locator
    assert f("/v1/blah/something/customer") == "v1__blah__something__customer"
    assert f("/v2/blah/something/customer") == "v2__blah__something__customer"
    assert f("/ping") == "ping"
    assert f("/v1/users/{id}/orders") == "v1__users__orders"   # path-param dropped
    assert f("/V1/Records/") == "v1__records"                  # lowercased, trailing slash
    assert f("/customers/v2/orders") == "customers__v2__orders"  # every segment IN ORDER (no hoist)
    assert f("/customers") == "customers"
    # A mixed segment ({id}-{slug}) is NOT a pure path-param -> not dropped, so it
    # does NOT collide with the pure-param sibling (Codex P3).
    assert f("/orders/{id}-{slug}") != f("/orders/{id}")
    assert "{" in f("/orders/{id}-{slug}")   # kept -> later flagged non-charset-safe
    assert f("/orders/{id}") == "orders"     # pure param dropped


def test_endpoint_id_must_match_locator(validator):
    # id equals the derived handle -> ok
    assert validator._endpoint_locator_findings(
        {"endpoint_id": "v1__records",
         "operations": {"read": {"request": {"path": "/v1/records"}}}}) == []
    # leaf-only id for a versioned path -> flagged with the expected handle
    errs = validator._endpoint_locator_findings(
        {"endpoint_id": "records",
         "operations": {"read": {"request": {"path": "/v1/records"}}}})
    assert errs and errs[0]["validator"] == "endpoint-id-locator"
    assert "v1__records" in errs[0]["message"]


def test_endpoint_locator_derives_from_read_canonical_path(validator):
    # The id is checked against the read (canonical resource) locator; a write mode
    # carrying a path-param or a sub-path (e.g. /bulk) does not force a split.
    for write_path in ("/v1/users/{id}", "/v1/users/bulk"):
        doc = {"endpoint_id": "v1__users", "operations": {
            "read": {"request": {"path": "/v1/users"}},
            "write": {"upsert": {"request": {"path": write_path}}}}}
        assert validator._endpoint_locator_findings(doc) == [], write_path
    # A write-only endpoint derives from its write path.
    write_only = {"endpoint_id": "v1__events",
                  "operations": {"write": {"insert": {"request": {"path": "/v1/events"}}}}}
    assert validator._endpoint_locator_findings(write_only) == []


def test_endpoint_locator_non_derivable_path_errors(validator):
    # A path with NO derivable id is a hard gate failure (not a warning that would
    # let a decoupled endpoint_id through), and the message is about the PATH — never
    # a self-contradictory "must equal <invalid-id>".
    # (a) non-charset-safe (Shopify-style `.json`); (b) all-path-param (empty handle).
    for path in ("/admin/api/2024-01/orders.json", "/{id}"):
        doc = {"endpoint_id": "orders",
               "operations": {"read": {"request": {"path": path}}}}
        findings = validator._endpoint_locator_findings(doc)
        assert findings and findings[0]["severity"] == "error", path
        assert findings[0]["validator"] == "endpoint-id-locator"
        assert "must equal" not in findings[0]["message"]      # no fabricated id
        assert "cannot derive" in findings[0]["message"]


def test_coverage_non_dict_endpoint_file_no_crash(tmp_path, connector_base, validator):
    # A JSON-array endpoint file is a recorded model error, NOT a generic
    # "validator bug" crash from the coverage walk calling .get() on a list (Codex P3).
    (tmp_path / "endpoints").mkdir(parents=True)
    (tmp_path / "connector.json").write_text(json.dumps(connector_base))
    (tmp_path / "type-map-read.json").write_text(
        json.dumps([{"match": "exact", "native": "STRING", "canonical": "Utf8"}]))
    (tmp_path / "endpoints" / "widgets.json").write_text("[]")  # array, not object
    errs = _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))
    assert errs
    assert not any("validator bug" in e["message"] for e in errs)


def test_coverage_flags_endpoint_id_locator_mismatch(tmp_path, connector_base, validator):
    # End-to-end: a model-valid endpoint whose id doesn't encode its versioned path
    # is gated (filename still matches the id; only the locator rule catches it).
    ep = _endpoint("STRING", "Utf8", endpoint_id="widgets", path="/v1/widgets")
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "STRING", "canonical": "Utf8"}],
                {"widgets.json": ep})
    errors = _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))
    assert any(e["validator"] == "endpoint-id-locator" for e in errors)


# --- Database endpoint id = slug+hash8 (shared analitiq.contracts.endpoint_identity SSOT) ---

DB = "https://schemas.analitiq.ai/database-endpoint/latest.json"


def _db_endpoint(endpoint_id, schema="public", name="orders", catalog=None):
    dbo = {"name": name}
    if schema is not None:
        dbo["schema"] = schema
    if catalog is not None:
        dbo["catalog"] = catalog
    return {"$schema": DB, "endpoint_id": endpoint_id, "database_object": dbo,
            "columns": [{"name": "id", "native_type": "BIGINT", "arrow_type": "Int64"}]}


def test_db_endpoint_id_golden_vectors():
    # KNOWN-ANSWER vectors (hardcoded, NOT recomputed) so any drift in the shared
    # derivation — payload/order/hash — breaks the test. These are the reference
    # the minting Lambda must reproduce. Source: analitiq.contracts.endpoint_identity.
    assert slug("Sales") == "sales"
    assert slug("Order Items") == "order_items"
    assert slug("a.b-c") == "a_b_c"          # any non-[a-z0-9] run -> single "_"
    assert slug("__weird__") == "weird"      # leading/trailing trimmed
    assert slug("***") == ""                 # all out-of-charset -> empty
    assert derive_db_endpoint_id(None, "public", "orders") == "public__orders__371c8422"
    assert derive_db_endpoint_id(None, "Sales", "Order Items") == "sales__order_items__0e62f7e9"
    # Same slug, different verbatim name -> different hash (no collision).
    assert derive_db_endpoint_id(None, "Sales", "order_items") == "sales__order_items__ce7aee55"
    # Catalog present: slug order is schema, table, catalog (catalog last, before hash).
    assert derive_db_endpoint_id("Analytics", "Sales", "orders") == "sales__orders__analytics__a045c614"
    # Schemaless object (no schema) -> table slug then hash.
    assert derive_db_endpoint_id(None, None, "orders") == "orders__e53bb11a"
    # All-symbol name -> bare hash8, still a valid endpoint_id.
    assert re.fullmatch(r"[0-9a-f]{8}", derive_db_endpoint_id(None, None, "***"))


def test_database_endpoint_locator_gate(validator):
    # The derived id passes; the legacy `{schema}__{name}` form (no hash) is gated.
    good_id = derive_db_endpoint_id(None, "public", "orders")
    assert not _errors(validator.validate_document(_db_endpoint(good_id)))
    legacy = _db_endpoint("public__orders")
    errs = _errors(validator.validate_document(legacy))
    assert any(e["validator"] == "endpoint-id-locator" and "public__orders" in e["message"]
               for e in errs)
    # Catalog + schemaless variants are gated the same way (derived id passes).
    assert not _errors(validator.validate_document(
        _db_endpoint(derive_db_endpoint_id("wh", "public", "orders"), schema="public", catalog="wh")))
    assert not _errors(validator.validate_document(
        _db_endpoint(derive_db_endpoint_id(None, None, "orders"), schema=None)))


# --- coverage matrix (check_coverage isolates file-behavior from model validity) ---

def _min_connector(kind: str):
    return {"kind": kind, "transports": {}}


def test_coverage_database_requires_write_map(tmp_path, validator):
    (tmp_path / "type-map-read.json").write_text('[{"match":"exact","native":"BIGINT","canonical":"Int64"}]')
    (tmp_path / "connector.json").write_text("{}")
    errors = _errors(validator.check_coverage(_min_connector("database"), tmp_path / "connector.json"))
    assert any("type-map-write.json" in e["message"] for e in errors)


def test_coverage_api_rejects_write_map(tmp_path, validator):
    (tmp_path / "type-map-read.json").write_text('[{"match":"exact","native":"STRING","canonical":"Utf8"}]')
    (tmp_path / "type-map-write.json").write_text('[{"match":"exact","canonical":"Utf8","native":"TEXT"}]')
    (tmp_path / "endpoints").mkdir()
    (tmp_path / "endpoints" / "w.json").write_text("{}")
    (tmp_path / "connector.json").write_text("{}")
    errors = _errors(validator.check_coverage(_min_connector("api"), tmp_path / "connector.json"))
    assert any("must not ship" in e["message"] for e in errors)


def test_coverage_flags_legacy_type_map(tmp_path, validator):
    (tmp_path / "type-map.json").write_text('[{"match":"exact","native":"X","canonical":"Utf8"}]')
    (tmp_path / "type-map-read.json").write_text('[{"match":"exact","native":"X","canonical":"Utf8"}]')
    (tmp_path / "endpoints").mkdir()
    (tmp_path / "connector.json").write_text("{}")
    errors = _errors(validator.check_coverage(_min_connector("api"), tmp_path / "connector.json"))
    assert any("pre-split name" in e["message"] for e in errors)


@pytest.mark.parametrize("kind", ["database", "nosql", "document"])
def test_coverage_database_family_requires_write_map(tmp_path, kind, validator):
    # nosql/document are database-family kinds — same read+write map requirement.
    (tmp_path / "type-map-read.json").write_text('[{"match":"exact","native":"BIGINT","canonical":"Int64"}]')
    (tmp_path / "connector.json").write_text("{}")
    errors = _errors(validator.check_coverage({"kind": kind, "transports": {}}, tmp_path / "connector.json"))
    assert any("type-map-write.json" in e["message"] for e in errors)


def test_database_endpoint_filename_not_checked_for_snapshot(validator, tmp_path):
    # The hash-addressed materialized snapshot lives at
    # `.../endpoints/{endpoint_id}/schemas/{schema_hash}.json` — its basename is a
    # content hash by design, so the filename↔endpoint_id gate must NOT fire there.
    eid = derive_db_endpoint_id(None, "public", "orders")
    db = _db_endpoint(eid)
    snap_dir = tmp_path / "endpoints" / eid / "schemas"
    snap_dir.mkdir(parents=True)
    p = snap_dir / "sha256-abc123.json"  # hash basename, not {endpoint_id}.json
    p.write_text(json.dumps(db))
    errors = _errors(validator.validate_document(db, doc_path=p))
    assert not any(e["validator"] == "endpoint-filename" for e in errors)


def test_database_endpoint_filename_checked_in_bundle_layout(validator, tmp_path):
    # The authored connection-scoped file the engine locates by stem lives at
    # `connections/{cid}/definition/endpoints/{endpoint_id}.json`. A correct id
    # inside but a mismatched filename stem passes model + locator gates yet fails
    # at runtime (the engine registers it under the wrong stem), so the gate fires.
    eid = derive_db_endpoint_id(None, "public", "orders")
    db = _db_endpoint(eid)
    ep_dir = tmp_path / "connections" / "conn-1" / "definition" / "endpoints"
    ep_dir.mkdir(parents=True)
    wrong = ep_dir / "orders.json"  # stem != endpoint_id
    wrong.write_text(json.dumps(db))
    errors = _errors(validator.validate_document(db, doc_path=wrong))
    assert any(e["validator"] == "endpoint-filename" for e in errors)
    # Correctly named -> no filename error.
    right = ep_dir / f"{eid}.json"
    right.write_text(json.dumps(db))
    assert not any(e["validator"] == "endpoint-filename"
                   for e in _errors(validator.validate_document(db, doc_path=right)))


def test_database_endpoint_filename_not_checked_when_unanchored(validator, tmp_path):
    # A staged single-doc path not yet at its final `definition/endpoints/` home
    # carries no stem contract, so the gate stays silent (per the issue's nuance).
    eid = derive_db_endpoint_id(None, "public", "orders")
    db = _db_endpoint(eid)
    p = tmp_path / "orders.json"  # bare staged file, wrong stem, no endpoints/ parent
    p.write_text(json.dumps(db))
    errors = _errors(validator.validate_document(db, doc_path=p))
    assert not any(e["validator"] == "endpoint-filename" for e in errors)


def test_endpoint_filename_findings_public_helper(validator):
    # Issue #971: the filename gate is exported so a bundle-assembling consumer —
    # which validates filename-less in-memory docs via validate_pipeline_bundle and
    # so cannot reach the gate there — calls ONE shared implementation instead of
    # reimplementing the ~4-line check, keeping the invariant define-once.
    eid = derive_db_endpoint_id(None, "public", "orders")
    db = _db_endpoint(eid)
    # Mismatched stem -> exactly one endpoint-filename error.
    mismatch = _errors(validator.endpoint_filename_findings(db, "orders.json"))
    assert [e["validator"] for e in mismatch] == ["endpoint-filename"]
    # Correct {endpoint_id}.json -> no findings.
    assert validator.endpoint_filename_findings(db, f"{eid}.json") == []
    # Missing/unusable endpoint_id -> a warning (can't verify), not an error.
    no_id = validator.endpoint_filename_findings({"database_object": {"name": "orders"}}, "orders.json")
    assert [(f["validator"], f["severity"]) for f in no_id] == [("endpoint-filename", "warning")]


def test_is_stem_addressed_endpoint_path_public_helper(validator):
    # Issue #971: consumers apply the gate on the SAME layout condition the
    # validator uses — true only for the authored `definition/endpoints/{id}.json`
    # the engine resolves by stem, false for the hash-addressed snapshot and any
    # bare/staged path.
    eid = derive_db_endpoint_id(None, "public", "orders")
    bundle = Path("connections/conn-1/definition/endpoints") / f"{eid}.json"
    snapshot = Path("connections/conn-1/endpoints") / eid / "schemas" / "sha256-abc.json"
    assert validator.is_stem_addressed_endpoint_path(bundle) is True
    assert validator.is_stem_addressed_endpoint_path(snapshot) is False
    assert validator.is_stem_addressed_endpoint_path(Path("orders.json")) is False


def test_type_map_direction_from_schema_url(validator, tmp_path):
    # A write map from a generic filename is validated as write when --schema-url
    # points at type-map-write (backward-compatible direction hint).
    write_rules = [{"match": "regex", "canonical": r"^Decimal128\((?<p>\d+),(?<s>\d+)\)",
                    "native": "NUMERIC(${p}, ${s})"}]
    p = tmp_path / "generic.json"
    as_read = _errors(validator.validate_document(write_rules, doc_path=p))
    as_write = _errors(validator.validate_document(
        write_rules, doc_path=p, schema_url="https://schemas.analitiq.ai/type-map-write/latest.json"))
    assert as_read and not as_write  # rejected as read, accepted as write


def test_coverage_flags_nested_endpoint_file(tmp_path, connector_base, validator):
    # A nested endpoints/**/x.json must be flagged (matches the registry gate,
    # which rejects non-flat endpoint paths) rather than silently ignored.
    _write_tree(tmp_path, connector_base,
                [{"match": "exact", "native": "STRING", "canonical": "Utf8"}],
                {"widgets.json": _endpoint("STRING", "Utf8")})
    nested = tmp_path / "endpoints" / "v1"
    nested.mkdir()
    (nested / "buried.json").write_text(json.dumps(_endpoint("STRING", "Utf8")))
    errors = _errors(validator.validate_document(connector_base, doc_path=tmp_path / "connector.json"))
    assert any("nested" in e["message"] for e in errors)


def test_coverage_flags_unparseable_read_map(tmp_path, validator):
    (tmp_path / "type-map-read.json").write_text("{ not json")
    (tmp_path / "connector.json").write_text("{}")
    errors = _errors(validator.check_coverage(_min_connector("database"), tmp_path / "connector.json"))
    assert any("could not be read or parsed" in e["message"] for e in errors)


# --- database-endpoint kind ---

def test_database_endpoint_valid_and_invalid(validator):
    db = {"$schema": "https://schemas.analitiq.ai/database-endpoint/latest.json",
          "endpoint_id": derive_db_endpoint_id(None, "public", "orders"),
          "database_object": {"schema": "public", "name": "orders", "object_type": "table"},
          "columns": [{"name": "id", "native_type": "uuid", "arrow_type": "Utf8"}]}
    assert not _errors(validator.validate_document(db))
    bad = json.loads(json.dumps(db))
    bad["columns"][0]["arrow_type"] = "NotAnArrowType"
    assert _errors(validator.validate_document(bad))


# --- advisory warnings ---

def _warnings(findings):
    return [f for f in findings if f["severity"] == "warning"]


def test_duplicate_type_map_rule_warns(validator):
    rules = [{"match": "exact", "native": "STRING", "canonical": "Utf8"},
             {"match": "exact", "native": "STRING", "canonical": "Utf8"}]
    warns = _warnings(validator.validate_document(rules))
    assert any("duplicate" in w["message"] for w in warns)


def test_duplicate_exact_read_rule_warns_across_case_and_whitespace(validator):
    # Two exact READ rules differing only by case/whitespace collapse to one
    # matcher at runtime (first wins), so the second is unreachable — the dedup
    # must normalize the same way the reader does and flag it, even when the
    # rules map to DIFFERENT canonicals (a real, if rare, authoring bug).
    rules = [{"match": "exact", "native": "character varying", "canonical": "Utf8"},
             {"match": "exact", "native": "CHARACTER  VARYING", "canonical": "LargeUtf8"}]
    warns = _warnings(validator.validate_document(rules))
    assert any("duplicate" in w["message"] for w in warns)


def test_write_vocabulary_gap_warns(validator, tmp_path):
    # A write map missing whole canonical families → advisory warning.
    p = tmp_path / "type-map-write.json"
    findings = validator.validate_document([{"match": "exact", "canonical": "Utf8", "native": "TEXT"}],
                                           doc_path=p)
    assert any(w["validator"] == "type-map-write-coverage" for w in _warnings(findings))


def test_write_vocabulary_probes_bare_container_markers(validator, tmp_path):
    # The engine probes the write map with a destination column's `arrow_type`
    # verbatim, and API-sourced documents carry the bare `Object`/`List` shape
    # markers — a map without rules for them hard-errors the stream at
    # configuration (issue #75). The coverage warning must name both.
    p = tmp_path / "type-map-write.json"
    findings = validator.validate_document([{"match": "exact", "canonical": "Utf8", "native": "TEXT"}],
                                           doc_path=p)
    gap = next(w for w in _warnings(findings) if w["validator"] == "type-map-write-coverage")
    assert "'Object'" in gap["message"] and "'List'" in gap["message"]

    covered = [{"match": "exact", "canonical": "Utf8", "native": "TEXT"},
               {"match": "exact", "canonical": "Object", "native": "JSONB"},
               {"match": "exact", "canonical": "List", "native": "JSONB"}]
    findings = validator.validate_document(covered, doc_path=p)
    gap = next(w for w in _warnings(findings) if w["validator"] == "type-map-write-coverage")
    assert "'Object'" not in gap["message"] and "'List'" not in gap["message"]


def test_write_vocabulary_fully_covered_map_warns_nothing(validator, tmp_path):
    # Every probe must be satisfiable by a realistic map, and a map covering
    # them all must clear the warning entirely — otherwise an unsatisfiable
    # probe (a typo, or a family no exact/regex rule can express) would warn
    # on every author's map forever, teaching authors to ignore the signal.
    # Mirrors the reference postgresql example: the Decimal/Time/Timestamp
    # families are covered by regex on purpose, pinning that a regex rule
    # fullmatching the bare probe satisfies it.
    full_map = [
        {"match": "exact", "canonical": c, "native": n}
        for c, n in [
            ("Boolean", "BOOLEAN"), ("Int8", "SMALLINT"), ("Int16", "SMALLINT"),
            ("Int32", "INTEGER"), ("Int64", "BIGINT"), ("UInt8", "SMALLINT"),
            ("UInt16", "INTEGER"), ("UInt32", "BIGINT"), ("UInt64", "BIGINT"),
            ("Float16", "REAL"), ("Float32", "REAL"), ("Float64", "DOUBLE PRECISION"),
            ("Utf8", "TEXT"), ("LargeUtf8", "TEXT"), ("Json", "JSONB"),
            ("Object", "JSONB"), ("List", "JSONB"), ("Binary", "BYTEA"),
            ("LargeBinary", "BYTEA"), ("Date32", "DATE"), ("Date64", "DATE"),
        ]
    ] + [
        {"match": "regex", "canonical": r"^Decimal(128|256)\((?<p>\d+),\s*(?<s>\d+)\)$",
         "native": "NUMERIC(${p}, ${s})"},
        {"match": "regex", "canonical": r"^Time(32|64)\([A-Z]+\)$", "native": "TIME"},
        {"match": "regex", "canonical": r"^Timestamp\([A-Z]+\)$", "native": "TIMESTAMP"},
    ]
    findings = validator.validate_document(full_map, doc_path=tmp_path / "type-map-write.json")
    coverage = [f for f in findings if f["validator"] == "type-map-write-coverage"]
    assert not coverage, coverage


# --- CLI / exit-code contract (the integration surface consumers depend on) ---

def _run_cli(tmp_path, doc, filename="doc.json"):
    p = tmp_path / filename
    p.write_text(json.dumps(doc))
    env = {**os.environ, "PYTHONPATH": _CLI_PYTHONPATH, "DOMAIN": "analitiq.ai"}
    return subprocess.run([sys.executable, "-c", _CLI_CODE, "--document", str(p)],
                          capture_output=True, text=True, env=env, check=False)


def test_cli_valid_doc_exit0(tmp_path):
    # Name the file after its endpoint_id so the filename↔id check is satisfied.
    doc = json.loads((CORPUS / "valid_read.json").read_text())
    r = _run_cli(tmp_path, doc, filename=f"{doc['endpoint_id']}.json")
    assert r.returncode == 0, r.stdout
    out = json.loads(r.stdout)
    assert out["passed"] is True and isinstance(out["findings"], list)


def test_cli_invalid_doc_exit1(tmp_path):
    r = _run_cli(tmp_path, json.loads((CORPUS / "invalid_write_from_input.json").read_text()))
    assert r.returncode == 1
    assert json.loads(r.stdout)["passed"] is False


def test_cli_unreadable_document_exit1(tmp_path):
    env = {**os.environ, "PYTHONPATH": _CLI_PYTHONPATH, "DOMAIN": "analitiq.ai"}
    # A directory path: read raises IsADirectoryError → must still emit JSON + exit 1.
    r = subprocess.run([sys.executable, "-c", _CLI_CODE, "--document", str(tmp_path)],
                       capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 1
    assert json.loads(r.stdout)["passed"] is False


def test_cli_missing_arg_exit2(tmp_path):
    env = {**os.environ, "PYTHONPATH": _CLI_PYTHONPATH, "DOMAIN": "analitiq.ai"}
    r = subprocess.run([sys.executable, "-c", _CLI_CODE], capture_output=True, text=True, env=env, check=False)
    assert r.returncode == 2
