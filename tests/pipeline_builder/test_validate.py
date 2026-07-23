"""Tests for the validator adapter (plugins/analitiq-pipeline-builder/scripts/validate.py).

The adapter holds no validation logic — it dispatches to the published
`analitiq-validator` / `analitiq-contract-models` packages. These tests therefore
require those packages installed (CI: `pip install -r requirements-dev.txt`); the
whole module skips cleanly when they are absent so a bare `pytest` never fails
confusingly. Canonical documents are defined inline and written to `tmp_path`, so
there are no committed fixtures to drift from the contract.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "plugins" / "analitiq-pipeline-builder"
sys.path.insert(0, str(ROOT / "scripts"))
import validate as V  # noqa: E402

pytest.importorskip("analitiq.validator",
                    reason="requires: pip install -r requirements-dev.txt")
from analitiq.contracts.endpoint_identity import (  # noqa: E402
    build_database_object, derive_db_endpoint_id,
)

SRC = "22222222-2222-4222-8222-222222222222"
DST = "33333333-3333-4333-8333-333333333333"
PID = "11111111-1111-4111-8111-111111111111"
SID = "44444444-4444-4444-8444-444444444444"
EID = derive_db_endpoint_id(None, "public", "orders")
DBOBJ = build_database_object(None, "public", "orders")
H = "https://schemas.analitiq.ai"

CONN_WISE = {
    "$schema": f"{H}/connection/latest.json", "connection_id": SRC, "connector_id": "wise",
    "display_name": "Wise", "parameters": {"environment": "live"},
    "secret_refs": {"api_token": "env:ANALITIQ_WISE_API_TOKEN"},
}
CONN_PG = {
    "$schema": f"{H}/connection/latest.json", "connection_id": DST, "connector_id": "postgresql",
    "display_name": "Prod Postgres",
    "parameters": {"host": "db.example.com", "port": 5432, "database": "analytics", "ssl_mode": "verify-full"},
    "secret_refs": {"password": "env:ANALITIQ_POSTGRESQL_PASSWORD"},
}
PIPELINE = {
    "$schema": f"{H}/pipeline/latest.json", "pipeline_id": PID, "display_name": "Wise to Postgres",
    "connections": {"source": SRC, "destinations": [DST]}, "streams": [SID],
    "schedule": {"type": "manual", "timezone": "UTC"}, "status": "draft",
}
STREAM = {
    "$schema": f"{H}/stream/latest.json", "stream_id": SID, "pipeline_id": PID, "display_name": "orders",
    "source": {
        "endpoint_ref": {"scope": "connector", "connection_id": SRC, "endpoint_id": "transfers"},
        "replication": {"method": "incremental", "cursor_field": "updated_at"},
    },
    "destinations": [{
        "endpoint_ref": {"scope": "connection", "connection_id": DST, "endpoint_id": EID, "database_object": DBOBJ},
        "write": {"mode": "upsert", "conflict_keys": ["id"]},
    }],
    "status": "draft",
}
DB_ENDPOINT = {
    "$schema": f"{H}/database-endpoint/latest.json", "endpoint_id": EID, "display_name": "public.orders",
    "database_object": DBOBJ,
    "columns": [
        {"name": "id", "native_type": "bigint", "arrow_type": "Int64", "nullable": False, "ordinal_position": 1},
        {"name": "updated_at", "native_type": "timestamptz", "arrow_type": "Timestamp(MICROSECOND, UTC)",
         "nullable": False, "ordinal_position": 2},
    ],
    "primary_keys": ["id"],
}


def _write(root: Path, rel: str, doc: dict | list) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2))
    return p


@pytest.mark.parametrize("entity,doc", [
    ("connection", CONN_PG), ("connection", CONN_WISE),
    ("pipeline", PIPELINE), ("stream", STREAM), ("database_endpoint", DB_ENDPOINT),
])
def test_valid_single_document(tmp_path, entity, doc):
    diag = V.diagnostics_for(entity, _write(tmp_path, f"{entity}.json", doc))
    assert diag["passed"], diag["findings"]


@pytest.mark.parametrize("entity,doc,validator_id", [
    # legacy connection carrying a `values` envelope — no longer part of the contract
    ("connection",
     {"$schema": f"{H}/connection/latest.json", "connector_id": "postgresql", "values": {"host": "x"}},
     "contract-model"),
    # legacy stream: flat endpoint_ref (missing database_object) + list-of-lists conflict_keys
    ("stream",
     {"$schema": f"{H}/stream/latest.json", "pipeline_id": PID,
      "source": {"endpoint_ref": {"scope": "connection", "connection_id": SRC, "endpoint_id": "orders"}},
      "destinations": [{"endpoint_ref": {"scope": "connection", "connection_id": DST, "endpoint_id": "orders"},
                        "write": {"mode": "upsert", "conflict_keys": [["id"]]}}]},
     "contract-model"),
    # database endpoint whose id is not the derived handle
    ("database_endpoint",
     {"$schema": f"{H}/database-endpoint/latest.json", "endpoint_id": "public_orders",
      "database_object": DBOBJ, "columns": [{"name": "id", "native_type": "bigint", "arrow_type": "Int64"}]},
     "endpoint-id-locator"),
])
def test_invalid_single_document(tmp_path, entity, doc, validator_id):
    diag = V.diagnostics_for(entity, _write(tmp_path, f"{entity}.json", doc))
    assert not diag["passed"]
    assert any(f["validator"] == validator_id for f in diag["findings"]), diag["findings"]


def test_active_pipeline_requires_stream_single_document(tmp_path):
    # the published pipeline contract enforces active => >=1 stream reference at the
    # single-document level; an active pipeline with empty streams is rejected without
    # needing the bundle
    doc = {**PIPELINE, "status": "active", "streams": []}
    diag = V.diagnostics_for("pipeline", _write(tmp_path, "pipeline.json", doc))
    assert not diag["passed"]
    assert any(f["validator"] == "contract-model" and "stream" in f["message"].lower()
               for f in diag["findings"]), diag["findings"]


def _build_bundle(root: Path) -> Path:
    _write(root, "connectors/wise/definition/connector.json", {"connector_id": "wise", "kind": "api"})
    _write(root, "connectors/postgresql/definition/connector.json", {"connector_id": "postgresql", "kind": "database"})
    _write(root, "connections/wise/connection.json", CONN_WISE)
    _write(root, "connections/postgresql/connection.json", CONN_PG)
    _write(root, f"connections/postgresql/definition/endpoints/{EID}.json", DB_ENDPOINT)
    _write(root, "pipelines/p/streams/orders.json", STREAM)
    return _write(root, "pipelines/p/pipeline.json", PIPELINE)


def test_valid_draft_bundle(tmp_path):
    doc = _build_bundle(tmp_path)
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert diag["passed"], diag["findings"]
    # a draft pipeline is not yet runnable by design; require_runnable=False suppresses
    # the runnability findings entirely — no /pipeline/status finding is emitted
    assert not any(f["path"] == "/pipeline/status" for f in diag["findings"]), diag["findings"]
    # a correctly-named endpoint yields no endpoint-filename finding (error or warning)
    assert not any(f["validator"] == "endpoint-filename" for f in diag["findings"]), diag["findings"]


def test_bundle_referential_error(tmp_path):
    doc = _build_bundle(tmp_path)
    stream_path = tmp_path / "pipelines/p/streams/orders.json"
    stream = json.loads(stream_path.read_text())
    stream["source"]["endpoint_ref"]["connection_id"] = "99999999-9999-4999-8999-999999999999"
    stream_path.write_text(json.dumps(stream))
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert not diag["passed"]
    assert any(f["validator"] == "bundle-connection-ref" for f in diag["findings"]), diag["findings"]


def test_bundle_endpoint_filename_mismatch(tmp_path):
    # the engine locates a connection-scoped endpoint by filename stem; a file named
    # something other than <endpoint_id>.json registers under the wrong id at runtime.
    # The id inside the file is still correct (so the referential checks pass), but the
    # bundle assembler flags the filename mismatch as an error.
    doc = _build_bundle(tmp_path)
    ep_dir = tmp_path / "connections/postgresql/definition/endpoints"
    (ep_dir / f"{EID}.json").rename(ep_dir / "orders.json")
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert not diag["passed"]
    assert any(f["validator"] == "endpoint-filename" and f["severity"] == "error"
               for f in diag["findings"]), diag["findings"]
    # the filename guard is the *sole* error — a rename must not also break referential
    # resolution, which would mask a guard regression
    assert {f["validator"] for f in diag["findings"] if f["severity"] == "error"} == {"endpoint-filename"}


def test_bundle_endpoint_missing_id_warns(tmp_path):
    # a missing/unusable endpoint_id yields an endpoint-filename *warning* (the shared
    # gate can't verify the name), not an error; the malformed state is still caught as
    # an error referentially, never silently passed
    doc = _build_bundle(tmp_path)
    ep_dir = tmp_path / "connections/postgresql/definition/endpoints"
    data = json.loads((ep_dir / f"{EID}.json").read_text())
    data.pop("endpoint_id")
    (ep_dir / f"{EID}.json").unlink()
    (ep_dir / "whatever.json").write_text(json.dumps(data))
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert any(f["validator"] == "endpoint-filename" and f["severity"] == "warning"
               for f in diag["findings"]), diag["findings"]
    assert not any(f["validator"] == "endpoint-filename" and f["severity"] == "error"
                   for f in diag["findings"]), diag["findings"]
    assert not diag["passed"]


def _add_wise_endpoint(root: Path, endpoint_id: str = "transfers") -> None:
    # Give the `wise` API connector a downloaded endpoint set on disk, so the plugin's
    # scope='connector' verification has something to resolve against. (_build_bundle
    # deliberately omits it — the connector's endpoint set is then 'unknown' and skipped.)
    _write(root, f"connectors/wise/definition/endpoints/{endpoint_id}.json",
           {"endpoint_id": endpoint_id})


def test_bundle_connector_endpoint_ref_ok(tmp_path):
    # a scope='connector' ref that names a real connector endpoint is clean — no warning
    doc = _build_bundle(tmp_path)
    _add_wise_endpoint(tmp_path, "transfers")  # matches STREAM's source endpoint_id
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert diag["passed"], diag["findings"]
    assert not any(f["validator"] == "connector-endpoint-ref" for f in diag["findings"]), diag["findings"]


def test_bundle_connector_endpoint_ref_missing_warns(tmp_path):
    # a scope='connector' ref to an endpoint the connector does not publish is a
    # WARNING (not an error — connectors are trusted, pinned at runtime), carrying a
    # closest-match alignment suggestion; the pipeline still passes
    doc = _build_bundle(tmp_path)
    _add_wise_endpoint(tmp_path, "transfers")
    stream_path = tmp_path / "pipelines/p/streams/orders.json"
    stream = json.loads(stream_path.read_text())
    stream["source"]["endpoint_ref"]["endpoint_id"] = "transferz"  # typo
    stream_path.write_text(json.dumps(stream))
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    warn = [f for f in diag["findings"] if f["validator"] == "connector-endpoint-ref"]
    assert len(warn) == 1, diag["findings"]
    assert warn[0]["severity"] == "warning"
    assert warn[0]["path"] == "/streams/0/source/endpoint_ref"
    assert "transfers" in warn[0]["message"]  # the suggested real endpoint name
    assert diag["passed"], "a warning must not fail validation"


def test_bundle_connector_endpoint_case_mismatch_suggests(tmp_path):
    # a case-only mismatch surfaces the correctly-cased connector endpoint as the
    # alignment target
    doc = _build_bundle(tmp_path)
    _add_wise_endpoint(tmp_path, "transfers")
    stream_path = tmp_path / "pipelines/p/streams/orders.json"
    stream = json.loads(stream_path.read_text())
    stream["source"]["endpoint_ref"]["endpoint_id"] = "Transfers"
    stream_path.write_text(json.dumps(stream))
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    warn = [f for f in diag["findings"] if f["validator"] == "connector-endpoint-ref"]
    assert len(warn) == 1 and "'transfers'" in warn[0]["message"], diag["findings"]


def test_bundle_connector_endpoint_no_close_match_still_warns(tmp_path):
    # a wrong ref against a KNOWN endpoint set must still warn even when no endpoint is
    # a close match — the suggestion is simply omitted. This pins the `suggestion=None`
    # branch so a "only append when there's a suggestion" refactor can't silently drop
    # warnings on the most-wrong refs.
    doc = _build_bundle(tmp_path)
    _add_wise_endpoint(tmp_path, "transfers")
    stream_path = tmp_path / "pipelines/p/streams/orders.json"
    stream = json.loads(stream_path.read_text())
    stream["source"]["endpoint_ref"]["endpoint_id"] = "zzz"  # no close match to 'transfers'
    stream_path.write_text(json.dumps(stream))
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    warn = [f for f in diag["findings"] if f["validator"] == "connector-endpoint-ref"]
    assert len(warn) == 1 and warn[0]["severity"] == "warning", diag["findings"]
    assert "Did you mean" not in warn[0]["message"], warn[0]["message"]
    assert diag["passed"]


def test_bundle_connector_endpoint_resolves_by_connector_id_not_dir_slug(tmp_path):
    # the connector's directory slug (wise-live) differs from the connector_id (wise)
    # the connection references; the endpoint set is keyed by connector_id too, so a
    # wrong ref still resolves the set and warns. If resolution regressed to dir-slug
    # only, the set would read as 'unknown' and the warning would vanish.
    doc = _build_bundle(tmp_path)
    (tmp_path / "connectors/wise").rename(tmp_path / "connectors/wise-live")
    _write(tmp_path, "connectors/wise-live/definition/endpoints/transfers.json",
           {"endpoint_id": "transfers"})  # connector.json still declares connector_id "wise"
    stream_path = tmp_path / "pipelines/p/streams/orders.json"
    stream = json.loads(stream_path.read_text())
    stream["source"]["endpoint_ref"]["endpoint_id"] = "nope"
    stream_path.write_text(json.dumps(stream))
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert any(f["validator"] == "connector-endpoint-ref" for f in diag["findings"]), diag["findings"]


def test_bundle_connector_endpoint_unknown_set_skips(tmp_path):
    # no downloaded endpoint set for the connector => 'unknown', not 'empty': the check
    # must skip rather than warn on a ref it cannot verify (false-positive guard)
    doc = _build_bundle(tmp_path)  # no connectors/wise/definition/endpoints/
    stream_path = tmp_path / "pipelines/p/streams/orders.json"
    stream = json.loads(stream_path.read_text())
    stream["source"]["endpoint_ref"]["endpoint_id"] = "does_not_exist"
    stream_path.write_text(json.dumps(stream))
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert not any(f["validator"] == "connector-endpoint-ref" for f in diag["findings"]), diag["findings"]


def test_unreadable_document(tmp_path):
    diag = V.diagnostics_for("pipeline", tmp_path / "does_not_exist.json")
    assert not diag["passed"]
    assert diag["findings"][0]["validator"] == "document"


def test_cli_main_valid(tmp_path, capsys):
    p = _write(tmp_path, "pipeline.json", PIPELINE)
    rc = V.main(["--entity", "pipeline", "--document", str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["passed"] is True  # stdout carries exactly one JSON object


def test_cli_main_invalid_exit_code(tmp_path, capsys):
    bad = {"$schema": f"{H}/connection/latest.json", "connector_id": "x", "values": {}}
    p = _write(tmp_path, "connection.json", bad)
    rc = V.main(["--entity", "connection", "--document", str(p)])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_cli_usage_error(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        V.main(["--document", "x.json"])  # missing required --entity
    assert excinfo.value.code == 2


def test_endpoint_id_helper(capsys):
    import endpoint_id  # sibling of validate.py on sys.path
    rc = endpoint_id.main(["--schema", "public", "--name", "orders"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["endpoint_id"] == EID
    assert out["database_object"]["name"] == "orders"


def test_active_pipeline_not_runnable_stays_error(tmp_path):
    doc = _build_bundle(tmp_path)  # the bundled stream is draft
    pipe = json.loads(doc.read_text())
    pipe["status"] = "active"
    doc.write_text(json.dumps(pipe))
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    # an active pipeline with no runnable stream is a real error — require_runnable is
    # True for an 'active' pipeline, so the runnability gate stays blocking
    assert not diag["passed"]
    assert any(f["validator"] == "bundle-pipeline" and f["severity"] == "error"
               for f in diag["findings"]), diag["findings"]


def test_active_pipeline_runnable_bundle_passes(tmp_path):
    # positive active path: an active pipeline whose referenced stream is itself
    # active is runnable, so require_runnable=True must accept it (no false reject)
    doc = _build_bundle(tmp_path)
    stream_path = tmp_path / "pipelines/p/streams/orders.json"
    stream = json.loads(stream_path.read_text())
    stream["status"] = "active"
    stream_path.write_text(json.dumps(stream))
    pipe = json.loads(doc.read_text())
    pipe["status"] = "active"
    doc.write_text(json.dumps(pipe))
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert diag["passed"], diag["findings"]


def test_bundle_malformed_sibling(tmp_path):
    doc = _build_bundle(tmp_path)
    (tmp_path / "pipelines/p/streams/orders.json").write_text("{ not valid json")
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert not diag["passed"]
    assert any(f["validator"] == "document" for f in diag["findings"]), diag["findings"]


def test_bundle_non_dict_sibling(tmp_path):
    doc = _build_bundle(tmp_path)
    # valid JSON but not an object → the "is not a JSON object" branch (connection path)
    (tmp_path / "connections/postgresql/connection.json").write_text("[]")
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert not diag["passed"]
    assert any(f["validator"] == "document" and "not a JSON object" in f["message"]
               for f in diag["findings"]), diag["findings"]


# ---------------------------------------------------------------------------
# Connection-scoped type maps (issue #49): the two type_map_* entities plus the
# bundle's file-level checks. Rule *content* findings come from the published
# validator; the adapter owns only the filename gates.
# ---------------------------------------------------------------------------

TYPE_MAP_READ = [
    {"match": "exact", "native": "CITEXT", "canonical": "Utf8"},
    # `Json` is the only container canonical a read rule can render (issue #81);
    # the dimension capture is intentionally discarded (no `(` in the render).
    {"match": "regex", "native": "^VECTOR\\((?<n>[0-9]+)\\)$", "canonical": "Json"},
]
# Deliberately direction-ASYMMETRIC: the regex rule's canonical is a matcher
# pattern, which is a contract-model error under read grading — so the "valid as
# type_map_write" assertions below pin that the adapter actually grades in the
# write direction (a regression to the read default would fail them). An
# exact-rule-only fixture validates clean under either direction and pins nothing.
TYPE_MAP_WRITE = [
    {"match": "exact", "canonical": "Json", "native": "JSONB"},
    {"match": "regex", "canonical": "^Decimal(128|256)\\((?<p>\\d+),\\s*(?<s>\\d+)\\)$",
     "native": "NUMERIC(${p}, ${s})"},
]


@pytest.mark.parametrize("entity,fname,doc", [
    ("type_map_read", "type-map-read.json", TYPE_MAP_READ),
    ("type_map_write", "type-map-write.json", TYPE_MAP_WRITE),
])
def test_valid_type_map_entity(tmp_path, entity, fname, doc):
    diag = V.diagnostics_for(entity, _write(tmp_path, fname, doc))
    assert diag["passed"], diag["findings"]


def test_type_map_entity_rejects_wrong_filename(tmp_path):
    # the engine loads the maps by exact filename; the gate must fire ALONE — a
    # misnamed file's content would otherwise be graded in the wrong direction
    diag = V.diagnostics_for("type_map_read", _write(tmp_path, "type-map.json", TYPE_MAP_READ))
    assert not diag["passed"]
    assert [f["validator"] for f in diag["findings"]] == ["connection-type-map"], diag["findings"]
    assert "type-map-read.json" in diag["findings"][0]["message"]


def test_type_map_entity_direction_mismatch_is_caught(tmp_path):
    # a write-shaped map under the read entity fails the filename gate, not the model
    diag = V.diagnostics_for("type_map_write", _write(tmp_path, "type-map-read.json", TYPE_MAP_WRITE))
    assert not diag["passed"]
    assert any(f["validator"] == "connection-type-map" for f in diag["findings"]), diag["findings"]


@pytest.mark.parametrize("doc", [
    [],                                                            # empty array — engine load-time error
    [{"match": "exact", "native": "citext", "canonical": "utf8"}],  # lowercase canonical fails the Arrow pattern
])
def test_invalid_type_map_content(tmp_path, doc):
    diag = V.diagnostics_for("type_map_read", _write(tmp_path, "type-map-read.json", doc))
    assert not diag["passed"]
    assert any(f["validator"] == "contract-model" for f in diag["findings"]), diag["findings"]


def test_bundle_with_valid_connection_type_maps(tmp_path):
    doc = _build_bundle(tmp_path)
    _write(tmp_path, "connections/postgresql/definition/type-map-read.json", TYPE_MAP_READ)
    _write(tmp_path, "connections/postgresql/definition/type-map-write.json", TYPE_MAP_WRITE)
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert diag["passed"], diag["findings"]


def test_bundle_rejects_dead_type_map_filename(tmp_path):
    # the engine never reads the pre-split name — a lingering file is silently inert
    # at runtime, so the bundle pass rejects it with a migration finding
    doc = _build_bundle(tmp_path)
    _write(tmp_path, "connections/postgresql/definition/type-map.json", TYPE_MAP_READ)
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert not diag["passed"]
    migration = [f for f in diag["findings"] if f["validator"] == "connection-type-map"]
    assert migration, diag["findings"]
    assert migration[0]["severity"] == "error"
    assert "type-map-read.json" in migration[0]["message"]  # the migration direction


def test_bundle_flags_invalid_connection_type_map(tmp_path):
    doc = _build_bundle(tmp_path)
    _write(tmp_path, "connections/postgresql/definition/type-map-read.json",
                [{"match": "exact", "native": "citext", "canonical": "utf8"}])
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert not diag["passed"]
    bad = [f for f in diag["findings"] if f["validator"] == "contract-model"]
    assert bad, diag["findings"]
    # findings are anchored to the owning file so a multi-connection bundle stays legible
    assert all(f["path"].startswith("connections/postgresql/definition/type-map-read.json")
               for f in bad), bad


def test_bundle_unreadable_connection_type_map(tmp_path):
    doc = _build_bundle(tmp_path)
    p = tmp_path / "connections/postgresql/definition/type-map-read.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[ not valid json")
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert not diag["passed"]
    assert any(f["validator"] == "connection-type-map" and "Cannot read" in f["message"]
               for f in diag["findings"]), diag["findings"]


def test_type_map_entity_rejects_non_array(tmp_path):
    # a dict under a load-bearing type-map filename must fail HERE: the published
    # dispatch detects by shape, so a stray connection document would otherwise be
    # graded as a connection and pass clean while the engine's loader chokes
    diag = V.diagnostics_for("type_map_read", _write(tmp_path, "type-map-read.json", CONN_PG))
    assert not diag["passed"]
    assert [f["validator"] for f in diag["findings"]] == ["connection-type-map"], diag["findings"]
    assert "JSON array" in diag["findings"][0]["message"]


def test_connection_write_map_filters_connector_vocabulary_warning(tmp_path):
    # the published type-map-write-coverage warning presumes a connector's
    # full-vocabulary write map; a gap-only connection map never satisfies it by
    # design, so the adapter filters it — for the entity run and the bundle alike
    diag = V.diagnostics_for("type_map_write", _write(tmp_path, "type-map-write.json", TYPE_MAP_WRITE))
    assert diag["passed"], diag["findings"]
    assert not any(f["validator"] == "type-map-write-coverage" for f in diag["findings"])

    root = tmp_path / "bundle"
    doc = _build_bundle(root)
    _write(root, "connections/postgresql/definition/type-map-write.json", TYPE_MAP_WRITE)
    diag = V.diagnostics_for("pipeline", doc, bundle_root=root)
    assert diag["passed"], diag["findings"]
    assert not any(f["validator"] == "type-map-write-coverage" for f in diag["findings"])


def test_bundle_flags_invalid_connection_write_type_map(tmp_path):
    # pins that the bundle loop reaches the WRITE entry too (a lowercase exact
    # canonical fails the Arrow pattern under write grading)
    doc = _build_bundle(tmp_path)
    _write(tmp_path, "connections/postgresql/definition/type-map-write.json",
           [{"match": "exact", "canonical": "utf8", "native": "TEXT"}])
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert not diag["passed"]
    bad = [f for f in diag["findings"] if f["validator"] == "contract-model"]
    assert bad and all(
        f["path"].startswith("connections/postgresql/definition/type-map-write.json")
        for f in bad), diag["findings"]


def test_bundle_flags_type_map_that_is_not_a_file(tmp_path):
    # a directory under a load-bearing name would validate clean and then fail at
    # the engine's loader — the bundle pass flags it instead
    doc = _build_bundle(tmp_path)
    (tmp_path / "connections/postgresql/definition/type-map-read.json").mkdir(parents=True)
    diag = V.diagnostics_for("pipeline", doc, bundle_root=tmp_path)
    assert not diag["passed"]
    assert any(f["validator"] == "connection-type-map" and "not a readable file" in f["message"]
               for f in diag["findings"]), diag["findings"]


def test_cli_main_type_map_entities(tmp_path, capsys):
    # the agents drive the CLI, and diagnostics_for-level routing keys off
    # _TYPE_MAP_FILENAMES — only this pins that ENTITIES exposes the new entities
    path = _write(tmp_path, "type-map-write.json", TYPE_MAP_WRITE)
    rc = V.main(["--entity", "type_map_write", "--document", str(path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["passed"], out
