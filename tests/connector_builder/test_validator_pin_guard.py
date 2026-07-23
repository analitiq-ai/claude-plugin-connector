"""Pin the wiring and verdict semantics of scripts/check_validator_pin_contract.py.

The guard's venv/PyPI probe runs OUTSIDE pytest (a CI job — see its module
docstring), so nothing in the ordinary suite would notice its file-reading
wiring rotting or its verdict logic regressing. These tests cover everything
offline-testable: the readers against the real working tree, the extraction's
all-or-error contract, every `main()` verdict branch with the probe
monkeypatched out (no venv, no network, no wheel install), the CI job's
wiring, and the one semantic invariant the verdicts rest on: the canon
extracted from prose must be accepted by the IN-REPO contract, so a guard
failure can only ever mean the PUBLISHED wheel lags the prose, never that the
prose itself is wrong.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from _pins import require_contract_models

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "check_validator_pin_contract.py"
_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


@pytest.fixture(scope="module")
def guard():
    spec = spec_from_file_location("check_validator_pin_contract", _SCRIPT)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the readers, against the real working tree ---------------------------


def test_reads_the_pin_from_its_single_source(guard):
    # `_analitiq.py` owns the pin; the guard references it by regex. If that
    # file is refactored so the regex misses, this fails here instead of the
    # guard erroring in CI.
    assert guard.read_pin().startswith("analitiq-validator==")
    assert guard.read_pin_version() != ""


def test_reads_the_shipped_version(guard):
    from _pins import PINNED_VERSION

    # Same value `_pins` tracks (packages/contract-models/pyproject.toml moves
    # in lockstep with the validator's — test_contract_models_pin.py enforces
    # that), read from the validator side by the guard's own regex.
    assert guard.read_shipped_version() == PINNED_VERSION


def test_extracts_the_canonical_drivers_from_prose(guard):
    drivers = guard.read_canonical_drivers()
    # Extraction is all-or-error by design; also require the driver this guard
    # exists for (the sync canonical path, issue #71) so a prose restructure
    # cannot silently drop it from coverage.
    assert "redshift+redshift_connector" in drivers


def test_extraction_is_all_or_error(guard, tmp_path, monkeypatch):
    # The docstring promises "a new canonical driver is guarded the moment it
    # is documented" — which is only true if a row the parser cannot read is
    # an ERROR, not a silent drop. An annotated first cell must raise, and a
    # well-formed added row must be picked up.
    table = (
        "## Driver examples\n\n"
        "| Driver | Template |\n|---|---|\n"
        "| `postgresql+asyncpg` | `postgresql+asyncpg://x` |\n"
        "| `databricks+dbsql` | `databricks+dbsql://x` |\n"
    )
    good = tmp_path / "good.md"
    good.write_text(table, encoding="utf-8")
    monkeypatch.setattr(guard, "CANON_SOURCE", good)
    assert guard.read_canonical_drivers() == [
        "postgresql+asyncpg",
        "databricks+dbsql",
    ]

    bad = tmp_path / "bad.md"
    bad.write_text(
        table + "| `mysql+aiomysql` (sync) | `mysql+aiomysql://x` |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "CANON_SOURCE", bad)
    with pytest.raises(guard.GuardError, match="unparseable row"):
        guard.read_canonical_drivers()


# --- main()'s verdict branches, probe monkeypatched out -------------------


def test_verdict_accepted_is_ok(guard, monkeypatch):
    monkeypatch.setenv("VALIDATOR_PIN_GUARD_STRICT", "")
    monkeypatch.setattr(guard, "probe_pinned_wheel", lambda pin, drivers: [])
    assert guard.main() == 0


def test_verdict_rejection_fails_in_steady_state(guard, monkeypatch):
    # pin == shipped and no env override: a rejection is a live defect.
    monkeypatch.setenv("VALIDATOR_PIN_GUARD_STRICT", "")
    monkeypatch.setattr(guard, "read_shipped_version", guard.read_pin_version)
    monkeypatch.setattr(
        guard, "probe_pinned_wheel", lambda pin, drivers: ["redshift+redshift_connector"]
    )
    assert guard.main() == 1


def test_verdict_rejection_warns_inside_a_release_window(guard, monkeypatch):
    # pin != shipped and the workflow set '' (ordinary PR): warn, exit 0. The
    # '' env case is load-bearing — the workflow's ternary emits '' on
    # non-strict refs, and only the literal '1' may mean strict.
    monkeypatch.setenv("VALIDATOR_PIN_GUARD_STRICT", "")
    monkeypatch.setattr(
        guard, "read_shipped_version", lambda: guard.read_pin_version() + ".post1"
    )
    monkeypatch.setattr(guard, "probe_pinned_wheel", lambda pin, drivers: ["x+y"])
    assert guard.main() == 0


def test_verdict_env_override_is_strict_even_in_a_window(guard, monkeypatch):
    monkeypatch.setenv("VALIDATOR_PIN_GUARD_STRICT", "1")
    monkeypatch.setattr(
        guard, "read_shipped_version", lambda: guard.read_pin_version() + ".post1"
    )
    monkeypatch.setattr(guard, "probe_pinned_wheel", lambda pin, drivers: ["x+y"])
    assert guard.main() == 1


def test_unrecognized_strict_value_is_a_guard_error(guard, monkeypatch):
    # A typo like 'true' must not silently downgrade strict to warn-only.
    monkeypatch.setenv("VALIDATOR_PIN_GUARD_STRICT", "true")
    monkeypatch.setattr(guard, "probe_pinned_wheel", lambda pin, drivers: [])
    assert guard.main() == 2


def test_guard_error_exits_2_never_a_verdict(guard, monkeypatch):
    # "A guard that cannot run must never read as green" — nor as a
    # contradiction. Both an infrastructure failure in the probe and an
    # unreadable source must exit 2.
    monkeypatch.setenv("VALIDATOR_PIN_GUARD_STRICT", "")

    def boom(pin, drivers):
        raise guard.GuardError("venv exploded")

    monkeypatch.setattr(guard, "probe_pinned_wheel", boom)
    assert guard.main() == 2

    monkeypatch.setattr(guard, "CANON_SOURCE", Path("/nonexistent/canon.md"))
    assert guard.main() == 2


# --- the CI wiring and the semantic invariant -----------------------------


def test_ci_job_runs_the_guard_with_the_strictness_key(guard):
    # The guard only closes issue #71's gap if CI actually runs it with the
    # documented strictness policy. Presence-first, same convention as the
    # CLAUDE.md pin assertions in test_contract_enforcement.py.
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/check_validator_pin_contract.py" in workflow
    assert "VALIDATOR_PIN_GUARD_STRICT" in workflow
    assert "startsWith(github.head_ref, 'release-please--')" in workflow
    assert "github.event_name == 'push'" in workflow


def test_canon_is_accepted_by_the_in_repo_contract(guard):
    # The guard's verdict semantics assume prose canon ⊆ contract: then a
    # rejection can only mean the published wheel is behind. Prove the
    # assumption against the in-repo source the rest of the suite grades.
    require_contract_models("analitiq.contracts.connector")
    from analitiq.contracts.connector import SqlAlchemyTransport

    for value in guard.read_canonical_drivers():
        SqlAlchemyTransport.model_validate(
            {"transport_type": "sqlalchemy", "driver": value}
        )
