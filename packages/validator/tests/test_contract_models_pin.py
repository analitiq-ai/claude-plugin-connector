"""The validator and contract-models ship as one release unit (#951 point 4).

`analitiq-validator` renders from the same authored model layer as
`analitiq-contract-models` and validates through it, so a version skew between
the two is a real defect. The two are kept in sync on BOTH fronts:
  - the validator's own `version` equals contract-models' `version`, and
  - the validator pins contract-models with an EXACT `==` at that same version.
This test is the CI equality gate — it fails the build if either diverges.

Fields are parsed with a small regex rather than `tomllib`: `tomllib` is stdlib
only on Python 3.11+, but both packages declare `requires-python = ">=3.10"`, so
the test suite must import on 3.10 too. The two fields we read have a fixed,
simple shape.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PYPROJECT = REPO_ROOT / "validator" / "pyproject.toml"
CONTRACT_MODELS_PYPROJECT = REPO_ROOT / "contract-models" / "pyproject.toml"


def _project_version(pyproject: Path) -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
    assert m, f"no [project].version found in {pyproject}"
    return m.group(1)


def _validator_contract_models_specifier() -> str:
    # Matches only the dependency form `"analitiq-contract-models<spec>"` (the
    # name preceded by a double-quote); the backticked mention in the package
    # description cannot match.
    text = VALIDATOR_PYPROJECT.read_text()
    matches = re.findall(r'"analitiq-contract-models([^"]*)"', text)
    assert len(matches) == 1, (
        f"expected exactly one analitiq-contract-models dependency, got {matches!r}"
    )
    return matches[0].strip()


def test_validator_pins_contract_models_exactly():
    spec = _validator_contract_models_specifier()
    m = re.fullmatch(r"==(?P<v>[^\s,;]+)", spec)
    assert m, (
        "validator must pin analitiq-contract-models with an exact '==' so the "
        f"two packages ship in sync (#951); got specifier {spec!r}"
    )
    assert m.group("v") == _project_version(CONTRACT_MODELS_PYPROJECT), (
        f"validator pins analitiq-contract-models=={m.group('v')} but "
        f"contract-models/pyproject.toml is {_project_version(CONTRACT_MODELS_PYPROJECT)!r} — "
        "bump both together so the pin stays in sync."
    )


def test_validator_version_matches_contract_models_version():
    validator_v = _project_version(VALIDATOR_PYPROJECT)
    contract_models_v = _project_version(CONTRACT_MODELS_PYPROJECT)
    assert validator_v == contract_models_v, (
        f"analitiq-validator is {validator_v!r} but analitiq-contract-models is "
        f"{contract_models_v!r} — the two ship as one release unit and their own "
        "versions must stay equal (#951 point 4); bump both together."
    )
