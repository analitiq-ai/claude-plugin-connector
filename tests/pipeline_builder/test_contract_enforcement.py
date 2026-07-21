"""Pin the contract behaviour the orchestrator depends on, through the adapter.

The validator pin is the plugin's contract with the outside world, and a bump
changes what is rejected AND where the rejection is reported. Both matter:

  * WHAT — rc10 closed several vocabularies that rc6 left open. If a later pin
    reopened one, the plugin would silently start authoring documents the engine
    cannot run, and no other test would notice.
  * WHERE — the orchestrator's fix-and-revalidate loop routes a finding back to a
    creator agent by its `path`. A finding that moves or coarsens breaks that
    routing while still "failing validation", so the paths are pinned too.

These assert through `validate.py` rather than against the models directly, so
they cover the adapter's normalization as well as the contract.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT = REPO_ROOT / "plugins" / "analitiq-pipeline-builder"
sys.path.insert(0, str(ROOT / "scripts"))
import validate as V  # noqa: E402

pytest.importorskip("analitiq.validator",
                    reason="requires: pip install -r requirements-dev.txt")

PID = "11111111-1111-4111-8111-111111111111"
SRC = "22222222-2222-4222-8222-222222222222"
DST = "33333333-3333-4333-8333-333333333333"
H = "https://schemas.analitiq.ai"


def _connection_ref(connection_id=SRC, name="orders"):
    return {"scope": "connection", "connection_id": connection_id,
            "database_object": {"name": name, "schema": "public"}}


def _connector_ref(connection_id=SRC, endpoint_id="transfers"):
    return {"scope": "connector", "connection_id": connection_id,
            "endpoint_id": endpoint_id}


def _stream(source_ref=None, destination=None, filters=None):
    source = {"endpoint_ref": source_ref or _connection_ref()}
    if filters is not None:
        source["filters"] = filters
    return {
        "$schema": f"{H}/stream/latest.json", "pipeline_id": PID, "source": source,
        "destinations": [destination or {"endpoint_ref": _connection_ref(DST),
                                         "write": {"mode": "insert"}}],
    }


def _diagnose(tmp_path, doc, entity="stream"):
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(doc))
    return V.diagnostics_for(entity, path)


def _paths(diagnostics):
    return {f["path"] for f in diagnostics["findings"] if f["severity"] == "error"}


def test_baseline_stream_is_valid(tmp_path):
    """Guard the guard: every rejection case below is this document plus one change."""
    assert _diagnose(tmp_path, _stream())["passed"]


# --- vocabularies rc10 closed that rc6 left open ---------------------------

@pytest.mark.parametrize("operator", ["matches", "regex_match", "LIKE", ""])
def test_filter_operator_is_a_closed_vocabulary(tmp_path, operator):
    """rc6 accepted any string here; an open operator reaches the engine as garbage."""
    doc = _stream(filters=[{"field": "x", "operator": operator, "value": "y"}])
    diagnostics = _diagnose(tmp_path, doc)
    assert not diagnostics["passed"], f"{operator!r} must not validate"
    assert "/source/filters/0/operator" in _paths(diagnostics), (
        "a closed-vocabulary violation must be reported at the operator itself, "
        "so the orchestrator can route the fix")


@pytest.mark.parametrize("operator", ["contains", "starts_with", "ends_with"])
def test_api_operators_rejected_on_a_database_source(tmp_path, operator):
    doc = _stream(filters=[{"field": "x", "operator": operator, "value": "y"}])
    assert not _diagnose(tmp_path, doc)["passed"]


@pytest.mark.parametrize("operator", ["like", "ilike", "is_null", "is_not_null"])
def test_database_operators_rejected_on_an_api_source(tmp_path, operator):
    probe = {"field": "x", "operator": operator}
    if operator not in ("is_null", "is_not_null"):
        probe["value"] = "y"
    doc = _stream(source_ref=_connector_ref(), filters=[probe])
    assert not _diagnose(tmp_path, doc)["passed"]


@pytest.mark.parametrize("mode", ["merge", "append", "overwrite", "replace"])
def test_database_write_mode_is_closed(tmp_path, mode):
    """A database destination takes insert/upsert only; rc6 accepted anything."""
    doc = _stream(destination={"endpoint_ref": _connection_ref(DST),
                               "write": {"mode": mode}})
    assert not _diagnose(tmp_path, doc)["passed"], f"{mode!r} must not validate"


def test_upsert_requires_conflict_keys(tmp_path):
    doc = _stream(destination={"endpoint_ref": _connection_ref(DST),
                               "write": {"mode": "upsert"}})
    assert not _diagnose(tmp_path, doc)["passed"]

    doc = _stream(destination={"endpoint_ref": _connection_ref(DST),
                               "write": {"mode": "upsert", "conflict_keys": ["id"]}})
    assert _diagnose(tmp_path, doc)["passed"]


def test_connection_scope_ref_requires_database_object(tmp_path):
    """The field infra prose said was optional is in fact required."""
    doc = _stream(source_ref={"scope": "connection", "connection_id": SRC,
                              "endpoint_id": "orders"})
    diagnostics = _diagnose(tmp_path, doc)
    assert not diagnostics["passed"]
    assert "/source/endpoint_ref/connection/database_object" in _paths(diagnostics)


# --- server-managed fields stay unauthorable -------------------------------

@pytest.mark.parametrize("entity,doc,field", [
    ("pipeline", {"$schema": f"{H}/pipeline/latest.json",
                  "connections": {"source": SRC, "destinations": [DST]}}, "version"),
    ("pipeline", {"$schema": f"{H}/pipeline/latest.json",
                  "connections": {"source": SRC, "destinations": [DST]}}, "org_id"),
    ("connection", {"$schema": f"{H}/connection/latest.json",
                    "connector_id": "postgresql"}, "version"),
    ("connection", {"$schema": f"{H}/connection/latest.json",
                    "connector_id": "postgresql"}, "created_at"),
])
def test_server_managed_fields_are_rejected(tmp_path, entity, doc, field):
    assert _diagnose(tmp_path, dict(doc), entity)["passed"], "baseline must be valid"
    diagnostics = _diagnose(tmp_path, {**doc, field: 1}, entity)
    assert not diagnostics["passed"], (
        f"{entity} must not accept the server-managed field {field!r}")
    # Reported per-key, not as an aggregated root-path message: the orchestrator
    # routes a fix by path, so `/version` is what makes the finding actionable.
    assert f"/{field}" in _paths(diagnostics)


def test_stream_has_no_error_status(tmp_path):
    """Infra prose claimed a fourth `error` member; authoring one must fail."""
    assert not _diagnose(tmp_path, {**_stream(), "status": "error"})["passed"]


def test_pinned_schema_url_is_rejected(tmp_path):
    """Only the `latest.json` form is authorable — there is no pinned X.Y.Z form."""
    doc = {**_stream(), "$schema": f"{H}/stream/1.0.0.json"}
    assert not _diagnose(tmp_path, doc)["passed"]


# --- cross-field rules carry their stable advisory id ----------------------

def test_advisory_findings_quote_their_rule_id(tmp_path):
    """Prose cites rules by id, so the id must survive into the message."""
    doc = {"$schema": f"{H}/pipeline/latest.json",
           "connections": {"source": SRC, "destinations": [DST, DST]}}
    diagnostics = _diagnose(tmp_path, doc, entity="pipeline")
    assert not diagnostics["passed"]
    assert any("ADV-PIPE-001" in f["message"] for f in diagnostics["findings"]), (
        "duplicate destinations must report ADV-PIPE-001; the docs cite that id")


def test_active_pipeline_requires_a_stream(tmp_path):
    doc = {"$schema": f"{H}/pipeline/latest.json", "status": "active",
           "connections": {"source": SRC, "destinations": [DST]}}
    diagnostics = _diagnose(tmp_path, doc, entity="pipeline")
    assert not diagnostics["passed"]
    # This rule reports its semantics rather than an [ADV-] id prefix -- unlike
    # ADV-PIPE-001 above. Pin the distinction so a pin bump that changes either
    # message style is a visible decision rather than a silent one.
    assert any("requires at least one stream" in f["message"]
               for f in diagnostics["findings"])


# --- the pin itself --------------------------------------------------------

def test_validator_pin_matches_the_package_this_repo_ships():
    """`VALIDATOR_PIN` and packages/validator's version are one fact, twice.

    The plugin self-installs the PUBLISHED validator at runtime (end users have
    no checkout), so the pin must be a real released version — it cannot simply
    read the source tree. But it must be the version *this repo publishes*, or
    agents would run against a contract the repo no longer considers current.

    Previously this compared against requirements-dev.txt. That file no longer
    carries the pin: the packages are source here, and installing the published
    wheel alongside would silently shadow that source.
    """
    from _analitiq import VALIDATOR_PIN

    pyproject = (REPO_ROOT / "packages" / "validator" / "pyproject.toml").read_text()
    shipped = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    assert shipped, "packages/validator/pyproject.toml has no top-level version"
    assert VALIDATOR_PIN == f"analitiq-validator=={shipped.group(1)}", (
        f"_analitiq.VALIDATOR_PIN is {VALIDATOR_PIN!r} but this repo ships "
        f"analitiq-validator {shipped.group(1)}; bump the pin when the package "
        "version moves, once that version is actually published to PyPI")

    # CLAUDE.md names the pin too, and sits outside the generator's plugin-root
    # scope, so nothing else would notice it going stale. Assert its presence
    # FIRST: a conditional check would silently disable itself the moment someone
    # reworded the line, which is exactly the drift it exists to catch.
    # Repo-root CLAUDE.md: both plugins consume the same validator, so the pin is
    # stated once for the monorepo rather than per plugin.
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text()
    assert "analitiq-validator==" in claude_md, (
        "CLAUDE.md no longer states the validator pin in the `analitiq-validator==X` "
        "form this test recognises; restore it or update this assertion.")
    assert VALIDATOR_PIN in claude_md, (
        f"CLAUDE.md documents a different validator pin than {VALIDATOR_PIN!r}")


def test_suite_exercises_in_repo_source_not_an_installed_wheel():
    """The suite must exercise this repo's source, not a published release.

    Same intent as the old `test_installed_validator_matches_the_pin`, inverted
    by the move: the contract is now SOURCE here, so "the installed version
    matches the pin" is the wrong invariant — nothing should be installed.

    This guards a failure mode that is silent and easy to reintroduce. The wheel
    ships a generated `analitiq/contracts/__init__.py`, making it a regular
    package; the in-repo tree deliberately has none, making it a namespace
    portion. A regular package always wins, so a stray
    `pip install analitiq-contract-models` would shadow the source no matter
    what sys.path says, and every test here would quietly grade the wrong code.
    """
    import analitiq.contracts as contracts
    import analitiq.validator as validator

    src = REPO_ROOT / "packages"
    for mod in (contracts, validator):
        resolved = Path(mod.__file__ or mod.__path__[0]).resolve()
        assert resolved.is_relative_to(src), (
            f"{mod.__name__} resolved to {resolved}, outside {src}. An installed "
            "analitiq-contract-models / analitiq-validator is shadowing the "
            "in-repo source — uninstall it (see requirements-dev.txt).")
