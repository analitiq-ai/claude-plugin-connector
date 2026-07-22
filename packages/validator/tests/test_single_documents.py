"""Single-document validation for the connection / stream / pipeline authored
kinds — the CLI now validates every authored kind, not just the connector family.

Each kind is validated wholly against its contract model, so these tests assert
two things per kind: it is DETECTED (routes to its model, not the generic
"unrecognized document" verdict), and its model verdict is surfaced — a valid
document passes and an invalid one fails. Detection is also checked to be mutually
exclusive: no authored kind claims another's shape.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_SRC_ROOT = _REPO_ROOT / "contract-models" / "src"
SRC_ROOT = _REPO_ROOT / "validator" / "src"

SOURCE_CONN = "11111111-1111-4111-8111-111111111111"
DEST_CONN = "22222222-2222-4222-8222-222222222222"
PIPELINE = "b4904c77-0a4a-4a8d-a768-4a8b5f2f2414"


def _errors(findings):
    return [f for f in findings if f["severity"] == "error"]


# --- valid document fixtures (minimal, model-valid) -------------------------

def _valid_connection() -> dict:
    return {"connector_id": "stripe"}


def _valid_stream() -> dict:
    return {
        "pipeline_id": PIPELINE,
        "source": {
            "endpoint_ref": {
                "scope": "connector",
                "connection_id": f"{SOURCE_CONN}_v1",
                "endpoint_id": "transfers",
            }
        },
        "destinations": [
            {
                "endpoint_ref": {
                    "scope": "connector",
                    "connection_id": f"{DEST_CONN}_v1",
                    "endpoint_id": "orders",
                },
                "write": {"mode": "insert"},
            }
        ],
    }


def _valid_pipeline() -> dict:
    return {
        "connections": {
            "source": f"{SOURCE_CONN}_v1",
            "destinations": [f"{DEST_CONN}_v1"],
        }
    }


# --- detection: each kind is recognized, and only its own kind ---------------

def test_detectors_are_mutually_exclusive(validator):
    """Each authored document is claimed by exactly one single-document detector,
    and never by the pipeline-bundle detector."""
    detectors = {
        "connection": validator.is_connection_doc,
        "stream": validator.is_stream_doc,
        "pipeline": validator.is_pipeline_doc,
    }
    docs = {
        "connection": _valid_connection(),
        "stream": _valid_stream(),
        "pipeline": _valid_pipeline(),
    }
    for owner, doc in docs.items():
        claimed = {kind for kind, is_kind in detectors.items() if is_kind(doc)}
        assert claimed == {owner}, f"{owner} doc claimed by {claimed}"
        assert not validator.is_pipeline_bundle(doc)


def test_connector_family_not_claimed_by_new_detectors(validator):
    """A connector / endpoint document must not be shadowed by the new detectors."""
    connector = {"kind": "api", "transports": {}}
    api_endpoint = {"endpoint_id": "x", "operations": {"read": {}}}
    for doc in (connector, api_endpoint):
        assert not validator.is_connection_doc(doc)
        assert not validator.is_stream_doc(doc)
        assert not validator.is_pipeline_doc(doc)


# --- connection --------------------------------------------------------------

def test_valid_connection_passes(validator):
    assert validator.validate_document(_valid_connection()) == []


def test_invalid_connection_is_detected_and_flagged(validator):
    # A secret-shaped key in `parameters` is a model rule violation — it must be
    # routed to the model (a contract-model error), not the unrecognized verdict.
    doc = {"connector_id": "stripe", "parameters": {"password": "hunter2"}}
    findings = validator.validate_document(doc)
    errors = _errors(findings)
    assert errors and all(e["validator"] == "contract-model" for e in errors)


def test_connection_missing_connector_id_is_unrecognized(validator):
    # `connector_id` IS the connection discriminator; without it the document is
    # not a connection (and matches no other kind) — the unrecognized verdict.
    findings = validator.validate_document({"display_name": "x"})
    assert any(f["validator"] == "document" for f in _errors(findings))


# --- stream ------------------------------------------------------------------

def test_valid_stream_passes(validator):
    assert validator.validate_document(_valid_stream()) == []


def test_invalid_stream_is_detected_and_flagged(validator):
    doc = _valid_stream()
    doc["status"] = "bogus"  # not a valid lifecycle status
    errors = _errors(validator.validate_document(doc))
    assert errors and all(e["validator"] == "contract-model" for e in errors)


def test_stream_extra_top_level_field_rejected(validator):
    # The authored-top-level guard (extra='forbid') is a model rule; proving it
    # fires confirms the real StreamInput model runs, not a lax stand-in.
    doc = _valid_stream()
    doc["org_id"] = "server-managed"
    assert _errors(validator.validate_document(doc))


# --- pipeline ----------------------------------------------------------------

def test_valid_pipeline_passes(validator):
    assert validator.validate_document(_valid_pipeline()) == []


def test_invalid_pipeline_is_detected_and_flagged(validator):
    doc = _valid_pipeline()
    doc["status"] = "bogus"
    errors = _errors(validator.validate_document(doc))
    assert errors and all(e["validator"] == "contract-model" for e in errors)


def test_single_pipeline_not_confused_with_bundle(validator):
    """A single pipeline document (no nested `pipeline`) routes to the model and
    passes; it must not be validated as a bundle."""
    doc = _valid_pipeline()
    assert validator.is_pipeline_doc(doc) and not validator.is_pipeline_bundle(doc)
    assert validator.validate_document(doc) == []


def test_active_pipeline_without_streams_flagged(validator):
    """#963: an `active` pipeline with no stream references violates the
    single-document half of the activation gate — a contract-model error, not a
    passing document. (The API previously rejected such a document that this
    single-doc path accepted.)"""
    doc = {**_valid_pipeline(), "status": "active"}
    errors = _errors(validator.validate_document(doc))
    assert errors and all(e["validator"] == "contract-model" for e in errors)
    assert any("at least one stream reference" in e["message"] for e in errors)


def test_active_pipeline_with_stream_passes(validator):
    """The active-status gate is not blanket rejection: an `active` pipeline that
    references a stream still passes the single-document model."""
    doc = {**_valid_pipeline(), "status": "active", "streams": [f"{PIPELINE}_v1"]}
    assert validator.validate_document(doc) == []


# --- CLI end to end (the one-stop-validator ask) -----------------------------

def _run_cli(tmp_path, doc, filename="doc.json"):
    """Drive `analitiq-validate --document` on a single authored document.

    Only the two public source trees are on the path — the validator and the
    contract models it depends on. Nothing private, matching an installed
    consumer exactly.
    """
    p = tmp_path / filename
    p.write_text(json.dumps(doc))
    code = "from analitiq.validator import main; import sys; sys.exit(main())"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(SRC_ROOT), str(CONTRACTS_SRC_ROOT)]),
        "DOMAIN": "analitiq.ai",
    }
    return subprocess.run(
        [sys.executable, "-c", code, "--document", str(p)],
        capture_output=True, text=True, env=env, check=False,
    )


def test_cli_validates_connection_exit0(tmp_path):
    r = _run_cli(tmp_path, _valid_connection())
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["passed"] is True


def test_cli_invalid_stream_exit1(tmp_path):
    bad = _valid_stream()
    bad["status"] = "bogus"
    r = _run_cli(tmp_path, bad)
    assert r.returncode == 1
    assert json.loads(r.stdout)["passed"] is False
