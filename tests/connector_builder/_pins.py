"""The single place the suite states which contract it exercises.

Introduced on main (#45/#46) to stop the pin being restated across the drift
modules. The monorepo move changes *what* it asserts, not the principle:

  * Before, the contract arrived as a published wheel, so "am I running the
    right contract?" meant comparing `importlib.metadata` against a pinned
    version string.
  * Now this repo IS the contract's source (`packages/*/src`), put on `sys.path`
    by the repo-root `conftest.py`. There is no metadata to read, and an
    installed wheel is precisely the thing that must NOT be present — it ships a
    generated `analitiq/contracts/__init__.py`, making it a regular package that
    beats the in-repo namespace portion regardless of `sys.path`.

So `PINNED_VERSION` becomes documentation of what this repo ships, and the real
assertion is about *provenance*: every contract module must resolve inside
`packages/`.

`require_contract_models()` keeps its original skip-vs-hard-fail contract: an
offline dev run skips, but CI sets `DRIFT_REQUIRE_CONTRACT_MODELS=1` so a
missing or broken package is a hard failure there, never a green all-skipped
gate.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES_ROOT = REPO_ROOT / "packages"

#: What this repo ships. The source of truth is
#: `packages/contract-models/pyproject.toml`; this is the human-facing copy,
#: pinned to it by `assert_pinned_version_matches_pyproject()` below so it
#: cannot rot silently.
PINNED_VERSION = "1.0.0rc16"

INSTALL_HINT = (
    "run `pip install -r requirements-dev.txt` from the repo root and run pytest "
    "from a complete checkout — do NOT pip install analitiq-validator or "
    "analitiq-contract-models, which would shadow the in-repo source"
)


def require_contract_models(*modules: str) -> None:
    """Import each module, or skip the calling test module (hard-fail in CI).

    Call at module scope before importing anything from the contract packages.
    """
    import importlib

    for module in modules:
        try:
            importlib.import_module(module)
        except ImportError:
            if os.environ.get("DRIFT_REQUIRE_CONTRACT_MODELS") == "1":
                raise
            pytest.skip(
                f"{module} not installed — {INSTALL_HINT}",
                allow_module_level=True,
            )


def assert_pinned_versions() -> None:
    """Fail loudly when the contract is not this repo's source.

    Replaces the metadata-version comparison this function used to do. Reading
    `importlib.metadata` now proves the opposite of what we want: metadata
    exists only when a wheel is installed, and an installed wheel silently
    shadows the source the suite is supposed to be grading.
    """
    import analitiq.contracts as contracts
    import analitiq.validator as validator

    stray = {}
    for module in (contracts, validator):
        origins = ([Path(module.__file__).resolve()] if module.__file__
                   else [Path(p).resolve() for p in module.__path__])
        outside = [o for o in origins if not o.is_relative_to(PACKAGES_ROOT)]
        if outside:
            stray[module.__name__] = [str(o) for o in outside]

    assert not stray, (
        f"contract modules resolved outside {PACKAGES_ROOT}: {stray}. An "
        "installed analitiq-contract-models / analitiq-validator is shadowing "
        f"the in-repo source, so these guards would prove nothing — {INSTALL_HINT}."
    )


def assert_pinned_version_matches_pyproject() -> None:
    """`PINNED_VERSION` restates a value pyproject.toml owns — pin the copy.

    Per `.claude/rules/no-drift-surfaces.md` an unavoidable restatement of a
    contract value must be pinned by a test, or it is a defect rather than
    documentation.
    """
    import re

    pyproject = (PACKAGES_ROOT / "contract-models" / "pyproject.toml").read_text()
    project = pyproject.split("[project]", 1)[-1].split("\n[", 1)[0]
    match = re.search(r'^version\s*=\s*"([^"]+)"', project, re.M)
    assert match, "packages/contract-models/pyproject.toml has no [project] version"
    assert PINNED_VERSION == match.group(1), (
        f"_pins.PINNED_VERSION is {PINNED_VERSION!r} but "
        f"packages/contract-models ships {match.group(1)!r}"
    )
