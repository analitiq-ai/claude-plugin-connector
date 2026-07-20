"""The single place the drift suite states its pinned contract version.

The `1.0.0rc11` pin is necessarily repeated outside the test suite (the CI
workflow's install step, the README's install command, the validator agent's
self-install snippet) because those are different runtimes. Inside the suite it
is stated **once**, here, and imported — per `.claude/rules/no-drift-surfaces.md`
§4, "never increase" the number of hand-maintained copies.

`require_contract_models()` also carries the shared skip-vs-hard-fail contract:
an offline dev run skips, but CI sets `DRIFT_REQUIRE_CONTRACT_MODELS=1` so a
missing or broken package is a hard failure there, never a green all-skipped
gate.
"""

from __future__ import annotations

import os

import pytest

#: Keep in lockstep with `.github/workflows/tests.yml`, `README.md`, and
#: `src/agents/connector-schema-validator.md`.
PINNED_VERSION = "1.0.0rc11"

INSTALL_HINT = (
    'run `pip install --pre "analitiq-validator=={v}" '
    '"analitiq-contract-models=={v}"` to run the drift guards'.format(v=PINNED_VERSION)
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
    """Fail loudly when the installed packages are not the pinned version.

    Without this, a stale or newer local install silently validates the plugin's
    prose against a contract it does not ship against — the guards would pass
    while pinning the wrong thing.
    """
    from importlib.metadata import PackageNotFoundError, version

    mismatched = {}
    for dist in ("analitiq-validator", "analitiq-contract-models"):
        try:
            installed = version(dist)
        except PackageNotFoundError:
            installed = "not installed"
        if installed != PINNED_VERSION:
            mismatched[dist] = installed

    assert not mismatched, (
        f"drift guards expect {PINNED_VERSION}, found {mismatched}. The guards "
        "pin the plugin's prose to the contract it ships against, so running "
        f"them against another version proves nothing — {INSTALL_HINT}."
    )
