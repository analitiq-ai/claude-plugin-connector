"""Tests for the pipeline-bundle referential validator.

These are pure: a bundle is a mapping of already-parsed documents, so the tests
build dicts and assert on findings — no models, no layer, no filesystem.

One valid bundle is mutated per rule so each test isolates a single referential
defect. The valid fixture deliberately exercises the id-matching contract:
`pipeline.streams` / `pipeline.connections` / `endpoint_ref.connection_id` carry
`{id}_v{n}` versioned refs while the stream and connection *documents* declare the
bare id — they must still resolve.
"""
import pytest

SOURCE_CONN = "11111111-1111-4111-8111-111111111111"
DEST_CONN = "22222222-2222-4222-8222-222222222222"
STREAM = "33333333-3333-4333-8333-333333333333"
PIPELINE = "44444444-4444-4444-8444-444444444444"
DB_ENDPOINT = "public__orders__a1b2c3d4"


def _valid_bundle() -> dict:
    return {
        "pipeline": {
            "pipeline_id": PIPELINE,
            "status": "active",
            "connections": {
                "source": f"{SOURCE_CONN}_v1",
                "destinations": [f"{DEST_CONN}_v1"],
            },
            "streams": [f"{STREAM}_v2"],
        },
        "streams": [
            {
                "stream_id": STREAM,  # bare, as stream documents declare it
                "pipeline_id": PIPELINE,  # each stream's immutable parent
                "status": "active",  # a runnable stream (active pipeline needs one)
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
                            "scope": "connection",
                            "connection_id": f"{DEST_CONN}_v1",
                            "endpoint_id": DB_ENDPOINT,
                        }
                    }
                ],
            }
        ],
        "connections": [
            {"connection_id": SOURCE_CONN, "connector_id": "stripe"},
            {"connection_id": DEST_CONN, "connector_id": "snowflake"},
        ],
        "connectors": ["stripe", "snowflake"],
        "endpoints": [
            {"scope": "connection", "connection_id": DEST_CONN, "endpoint_id": DB_ENDPOINT},
        ],
    }


def _validators(findings) -> set:
    return {f["validator"] for f in findings}


# --- happy path -------------------------------------------------------------

def test_valid_bundle_has_no_findings(validator):
    assert validator.validate_pipeline_bundle(_valid_bundle()) == []


def test_valid_bundle_detected_and_dispatched(validator):
    """The bundle is a recognized kind: `validate_document` routes it here and it
    passes (no error findings), exercising the registry seam end to end."""
    bundle = _valid_bundle()
    assert validator.is_pipeline_bundle(bundle) is True
    findings = validator.validate_document(bundle)
    assert [f for f in findings if f["severity"] == "error"] == []


def test_all_finding_ids_are_registered(validator):
    """Every id a bundle finding can carry is registered with the core."""
    for vid in (
        "bundle-pipeline",
        "bundle-stream-ref",
        "bundle-connection-ref",
        "bundle-connector-ref",
        "bundle-endpoint-ref",
    ):
        assert vid in validator.VALIDATOR_IDS


# --- pipeline runnable (bundle-pipeline) ------------------------------------

def test_missing_pipeline_id_flagged(validator):
    bundle = _valid_bundle()
    del bundle["pipeline"]["pipeline_id"]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-pipeline" and f["path"] == "/pipeline/pipeline_id"
        for f in findings
    )


@pytest.mark.parametrize("status", ["draft", "inactive", None])
def test_non_active_pipeline_flagged(validator, status):
    bundle = _valid_bundle()
    if status is None:
        del bundle["pipeline"]["status"]
    else:
        bundle["pipeline"]["status"] = status
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-pipeline" and f["path"] == "/pipeline/status"
        for f in findings
    )


def test_active_pipeline_with_no_stream_refs_flagged(validator):
    bundle = _valid_bundle()
    bundle["pipeline"]["streams"] = []
    bundle["streams"] = []
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-pipeline" and f["path"] == "/pipeline/streams"
        for f in findings
    )


def test_active_pipeline_with_no_runnable_stream_flagged(validator):
    """An active pipeline needs a referenced stream that is itself active."""
    bundle = _valid_bundle()
    bundle["streams"][0]["status"] = "draft"
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-pipeline" and f["path"] == "/streams"
        for f in findings
    )


# --- stream refs (bundle-stream-ref) ----------------------------------------

def test_pipeline_stream_ref_without_document_flagged(validator):
    bundle = _valid_bundle()
    bundle["pipeline"]["streams"] = ["99999999-9999-4999-8999-999999999999_v1"]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(f["validator"] == "bundle-stream-ref" for f in findings)


def test_duplicate_stream_refs_collapsing_to_same_base_flagged(validator):
    bundle = _valid_bundle()
    bundle["pipeline"]["streams"] = [f"{STREAM}_v1", f"{STREAM}_v2"]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-stream-ref" and "resolve" in f["message"]
        for f in findings
    )


def test_duplicate_stream_documents_flagged(validator):
    bundle = _valid_bundle()
    bundle["streams"].append(dict(bundle["streams"][0]))
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-stream-ref" and "two bundled stream documents" in f["message"]
        for f in findings
    )


def test_stream_document_without_id_flagged(validator):
    bundle = _valid_bundle()
    del bundle["streams"][0]["stream_id"]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-stream-ref" and f["path"] == "/streams/0/stream_id"
        for f in findings
    )


def test_stream_belonging_to_another_pipeline_flagged(validator):
    """A bundled stream whose pipeline_id names a different pipeline is a
    mis-assembly, even though its stream_id resolves."""
    bundle = _valid_bundle()
    bundle["streams"][0]["pipeline_id"] = "77777777-7777-4777-8777-777777777777"
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-stream-ref" and f["path"] == "/streams/0/pipeline_id"
        for f in findings
    )


def test_stream_without_pipeline_id_not_flagged_here(validator):
    """A stream that omits pipeline_id is a per-document shape defect (the stream
    model's job), not a parent-mismatch — this check must not fire on it."""
    bundle = _valid_bundle()
    del bundle["streams"][0]["pipeline_id"]
    findings = validator.validate_pipeline_bundle(bundle)
    assert not any(f["path"] == "/streams/0/pipeline_id" for f in findings)


# --- connection presence + scoping (bundle-connection-ref) ------------------

def test_referenced_connection_absent_from_bundle_flagged(validator):
    bundle = _valid_bundle()
    bundle["connections"] = [c for c in bundle["connections"] if c["connection_id"] != DEST_CONN]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-connection-ref" and f["path"] == "/pipeline/connections"
        for f in findings
    )


def test_duplicate_connection_documents_flagged(validator):
    """Two connection documents collapsing to the same base are ambiguous."""
    bundle = _valid_bundle()
    bundle["connections"].append({"connection_id": f"{SOURCE_CONN}_v2", "connector_id": "stripe"})
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-connection-ref" and f["path"] == "/connections/2/connection_id"
        for f in findings
    )


def test_pipeline_pinning_two_versions_of_one_connection_flagged(validator):
    """source `X_v1` + destination `X_v2` collapse to one connection at load."""
    bundle = _valid_bundle()
    bundle["pipeline"]["connections"]["destinations"] = [
        f"{DEST_CONN}_v1", f"{SOURCE_CONN}_v2"
    ]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-connection-ref" and "two versions" in f["message"]
        for f in findings
    )


def test_endpoint_ref_connection_not_in_pipeline_flagged(validator):
    bundle = _valid_bundle()
    bundle["streams"][0]["source"]["endpoint_ref"]["connection_id"] = (
        "55555555-5555-4555-8555-555555555555_v1"
    )
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-connection-ref"
        and f["path"].endswith("/source/endpoint_ref/connection_id")
        for f in findings
    )


def test_destination_endpoint_ref_connection_not_in_pipeline_flagged(validator):
    """Rule applies to the destination side too, not just the source."""
    bundle = _valid_bundle()
    bundle["streams"][0]["destinations"][0]["endpoint_ref"]["connection_id"] = (
        "55555555-5555-4555-8555-555555555555_v1"
    )
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-connection-ref"
        and f["path"].endswith("/destinations/0/endpoint_ref/connection_id")
        for f in findings
    )


def test_stream_source_pointing_at_a_destination_connection_flagged(validator):
    """Role-specific: source must equal connections.source, not merely be some
    pipeline connection — pointing it at a destination connection is wrong."""
    bundle = _valid_bundle()
    bundle["streams"][0]["source"]["endpoint_ref"]["connection_id"] = f"{DEST_CONN}_v1"
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-connection-ref"
        and f["path"].endswith("/source/endpoint_ref/connection_id")
        for f in findings
    )


def test_stream_destination_pointing_at_the_source_connection_flagged(validator):
    """Role-specific: a destination must be one of connections.destinations, so
    pointing it at the pipeline source (not a listed destination) is wrong."""
    bundle = _valid_bundle()
    bundle["streams"][0]["destinations"][0]["endpoint_ref"]["connection_id"] = f"{SOURCE_CONN}_v1"
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-connection-ref"
        and f["path"].endswith("/destinations/0/endpoint_ref/connection_id")
        for f in findings
    )


# --- connector refs (bundle-connector-ref) ----------------------------------

def test_connection_connector_not_bundled_flagged(validator):
    bundle = _valid_bundle()
    bundle["connectors"] = ["stripe"]  # drop snowflake
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-connector-ref" and "snowflake" in f["message"]
        for f in findings
    )


def test_connectors_accepts_mapping_keyed_by_id(validator):
    """The assembler may hold connectors as a map keyed by id."""
    bundle = _valid_bundle()
    bundle["connectors"] = {"stripe": {"detail": "x"}, "snowflake": {"detail": "y"}}
    assert validator.validate_pipeline_bundle(bundle) == []


def test_connector_id_matched_verbatim_not_version_stripped(validator):
    """A connector identity is matched whole — unlike stream/connection refs it is
    not base-normalized, so a differing trailing `_vN` is a genuine mismatch."""
    bundle = _valid_bundle()
    bundle["connections"][0]["connector_id"] = "custom_v2"
    bundle["connectors"] = ["custom_v3", "snowflake"]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(f["validator"] == "bundle-connector-ref" for f in findings)

    bundle["connectors"] = ["custom_v2", "snowflake"]
    assert validator.validate_pipeline_bundle(bundle) == []


def test_connectors_accepts_list_of_meta_dicts(validator):
    """Connectors may be held as meta dicts carrying connector_id / slug / id."""
    bundle = _valid_bundle()
    bundle["connectors"] = [{"connector_id": "stripe"}, {"slug": "snowflake"}]
    assert validator.validate_pipeline_bundle(bundle) == []


def test_connector_meta_dict_without_recognized_id_key_not_matched(validator):
    bundle = _valid_bundle()
    bundle["connectors"] = [{"name": "stripe"}, {"name": "snowflake"}]  # no id/slug/connector_id
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(f["validator"] == "bundle-connector-ref" for f in findings)


# --- connection-scoped endpoints (bundle-endpoint-ref) ----------------------

def test_connection_scoped_endpoint_missing_document_flagged(validator):
    bundle = _valid_bundle()
    bundle["endpoints"] = []
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-endpoint-ref"
        and f["path"].endswith("/destinations/0/endpoint_ref")
        for f in findings
    )


def test_duplicate_connection_scoped_endpoint_documents_flagged(validator):
    """Two endpoint documents for the same (connection, endpoint_id) are ambiguous."""
    bundle = _valid_bundle()
    bundle["endpoints"].append(
        {"scope": "connection", "connection_id": f"{DEST_CONN}_v2", "endpoint_id": DB_ENDPOINT}
    )
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-endpoint-ref" and f["path"] == "/endpoints/1"
        for f in findings
    )


def test_connection_scoped_endpoint_wrong_endpoint_id_flagged(validator):
    """Presence is keyed on the full (connection_id, endpoint_id) tuple: an endpoint
    doc for the right connection but a different endpoint_id does not satisfy it."""
    bundle = _valid_bundle()
    bundle["endpoints"] = [
        {"scope": "connection", "connection_id": DEST_CONN, "endpoint_id": "a_different_endpoint"}
    ]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-endpoint-ref"
        and f["path"].endswith("/destinations/0/endpoint_ref")
        for f in findings
    )


def test_connector_scoped_endpoint_entry_does_not_satisfy_connection_ref(validator):
    """An endpoint entry explicitly marked scope='connector' must not satisfy a
    connection-scoped ref, even with a matching (connection_id, endpoint_id)."""
    bundle = _valid_bundle()
    bundle["endpoints"] = [
        {"scope": "connector", "connection_id": DEST_CONN, "endpoint_id": DB_ENDPOINT}
    ]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-endpoint-ref"
        and f["path"].endswith("/destinations/0/endpoint_ref")
        for f in findings
    )


def test_endpoint_entry_without_scope_still_satisfies_connection_ref(validator):
    """A minimal endpoint entry (no scope) defaults to connection-scoped, so it
    still resolves a connection-scoped ref."""
    bundle = _valid_bundle()
    bundle["endpoints"] = [{"connection_id": DEST_CONN, "endpoint_id": DB_ENDPOINT}]
    assert validator.validate_pipeline_bundle(bundle) == []


def test_connector_scoped_endpoint_needs_no_bundled_document(validator):
    """A `scope='connector'` ref is resolved from the connector, not the bundle's
    connection-scoped endpoint documents — so it needs none present."""
    bundle = _valid_bundle()
    # The source is connector-scoped with endpoint_id 'transfers' and there is no
    # endpoint document for it; a valid bundle must not flag that.
    findings = validator.validate_pipeline_bundle(bundle)
    assert not any(
        f["validator"] == "bundle-endpoint-ref" and "transfers" in f["message"]
        for f in findings
    )


# --- referencing-side field presence ----------------------------------------
# A missing reference FIELD is an unresolved reference (a referential failure),
# not a per-document shape nit to skip — these guard against that silent-pass class.

def test_connection_without_connector_id_flagged(validator):
    bundle = _valid_bundle()
    del bundle["connections"][0]["connector_id"]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-connector-ref" and f["path"] == "/connections/0/connector_id"
        for f in findings
    )


def test_endpoint_ref_without_connection_id_flagged(validator):
    bundle = _valid_bundle()
    del bundle["streams"][0]["source"]["endpoint_ref"]["connection_id"]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-connection-ref"
        and f["path"].endswith("/source/endpoint_ref/connection_id")
        for f in findings
    )


def test_connection_scoped_endpoint_ref_without_endpoint_id_flagged(validator):
    bundle = _valid_bundle()
    del bundle["streams"][0]["destinations"][0]["endpoint_ref"]["endpoint_id"]
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-endpoint-ref"
        and f["path"].endswith("/destinations/0/endpoint_ref")
        for f in findings
    )


def test_stream_source_without_endpoint_ref_flagged(validator):
    bundle = _valid_bundle()
    bundle["streams"][0]["source"] = {}
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-endpoint-ref" and f["path"] == "/streams/0/source/endpoint_ref"
        for f in findings
    )


def test_stream_destination_without_endpoint_ref_flagged(validator):
    bundle = _valid_bundle()
    bundle["streams"][0]["destinations"][0] = {}
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-endpoint-ref"
        and f["path"] == "/streams/0/destinations/0/endpoint_ref"
        for f in findings
    )


def test_pipeline_streams_not_a_list_flagged(validator):
    bundle = _valid_bundle()
    bundle["pipeline"]["streams"] = f"{STREAM}_v2"  # a bare string, not a list
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-stream-ref" and f["path"] == "/pipeline/streams"
        for f in findings
    )


# --- structural guards ------------------------------------------------------

def test_non_mapping_bundle_flagged(validator):
    findings = validator.validate_pipeline_bundle(["not", "a", "bundle"])
    assert findings and findings[0]["validator"] == "bundle-pipeline"


def test_bundle_without_pipeline_flagged(validator):
    findings = validator.validate_pipeline_bundle({"streams": [], "connections": []})
    assert any(f["path"] == "/pipeline" for f in findings)


def test_multiple_defects_all_reported(validator):
    """Findings accumulate — one bad bundle surfaces every violated rule at once."""
    bundle = _valid_bundle()
    bundle["pipeline"]["status"] = "draft"
    bundle["connectors"] = []
    bundle["endpoints"] = []
    findings = validator.validate_pipeline_bundle(bundle)
    assert {"bundle-pipeline", "bundle-connector-ref", "bundle-endpoint-ref"} <= _validators(findings)


# --- referential integrity vs runnability (require_runnable) -----------------
# An authoring tool produces DRAFT bundles: it wants the referential checks
# WITHOUT the active-status runnability gate.

def test_draft_bundle_fails_runnable_by_default(validator):
    """The default (require_runnable=True) still flags a draft pipeline — the
    executor's contract is unchanged."""
    bundle = _valid_bundle()
    bundle["pipeline"]["status"] = "draft"
    findings = validator.validate_pipeline_bundle(bundle)
    assert any(
        f["validator"] == "bundle-pipeline" and f["path"] == "/pipeline/status"
        for f in findings
    )


def test_draft_bundle_passes_without_runnable_gate(validator):
    """A referentially-sound DRAFT bundle passes when runnability is not required —
    the authoring use case (no hand-downgrading of the status finding)."""
    bundle = _valid_bundle()
    bundle["pipeline"]["status"] = "draft"
    bundle["streams"][0]["status"] = "draft"
    assert validator.validate_pipeline_bundle(bundle, require_runnable=False) == []


def test_require_runnable_false_skips_active_gate(validator):
    """The active-gate (an active pipeline needs a runnable stream) is a runnability
    check too, so it must not fire when runnability is not required."""
    bundle = _valid_bundle()
    bundle["streams"][0]["status"] = "draft"  # no runnable stream
    assert validator.validate_pipeline_bundle(bundle, require_runnable=False) == []
    # ...but with the default it is flagged.
    assert any(
        f["path"] == "/streams" for f in validator.validate_pipeline_bundle(bundle)
    )


def test_referential_checks_still_run_without_runnable_gate(validator):
    """Dropping the runnability gate must NOT drop referential integrity: a broken
    connector reference is still flagged on a draft bundle."""
    bundle = _valid_bundle()
    bundle["pipeline"]["status"] = "draft"
    bundle["connectors"] = ["stripe"]  # drop snowflake
    findings = validator.validate_pipeline_bundle(bundle, require_runnable=False)
    assert any(f["validator"] == "bundle-connector-ref" for f in findings)


def test_missing_pipeline_id_flagged_even_without_runnable(validator):
    """`pipeline_id` presence is REFERENTIAL (stream parent refs resolve against
    it), not runnability — it is checked regardless of require_runnable."""
    bundle = _valid_bundle()
    bundle["pipeline"]["status"] = "draft"
    del bundle["pipeline"]["pipeline_id"]
    findings = validator.validate_pipeline_bundle(bundle, require_runnable=False)
    assert any(
        f["validator"] == "bundle-pipeline" and f["path"] == "/pipeline/pipeline_id"
        for f in findings
    )
