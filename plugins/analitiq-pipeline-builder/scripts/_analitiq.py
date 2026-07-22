"""Shared dependency bootstrap for the plugin's Python helpers.

Both `validate.py` and `endpoint_id.py` consume the published
`analitiq-validator` (which pulls `analitiq-contract-models`). This module
guarantees that package is importable: if the current interpreter lacks the
pinned version it installs it into a managed virtualenv and re-execs the calling
script under it. A venv sidesteps PEP-668 externally-managed interpreters; pip
output is routed to stderr so a caller's stdout stays clean.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Single source of the validator pin — the PUBLISHED release the plugin
# self-installs at runtime. requirements-dev.txt deliberately does NOT carry it
# (installing the wheel would shadow the in-repo source); the invariant is
# instead `pin == packages/validator/pyproject.toml version`, enforced by
# tests/pipeline_builder/test_contract_enforcement.py.
VALIDATOR_PIN = "analitiq-validator==1.0.0rc13"

_REEXEC_SENTINEL = "ANALITIQ_PIPELINE_VALIDATOR_BOOTSTRAPPED"

# Set by this repo's root conftest.py. In the monorepo the validator is SOURCE
# (packages/validator/src) on sys.path, not an installed distribution, so
# `importlib.metadata` finds nothing and the bootstrap below would build a venv,
# install the published wheel, and os.execv into it — replacing the pytest
# process mid-run and testing the published release instead of the source.
#
# Deliberately an explicit opt-in rather than "is it importable?": end users have
# no checkout, and the version-exactness guarantee for them must not soften into
# a heuristic. Absent this variable, behaviour is unchanged.
_FROM_SOURCE = "ANALITIQ_VALIDATOR_FROM_SOURCE"


def _pinned_version() -> str:
    return VALIDATOR_PIN.split("==", 1)[1]


def _importable(version: str) -> bool:
    try:
        from importlib.metadata import PackageNotFoundError, version as _v
    except Exception:  # pragma: no cover
        return False
    try:
        return _v("analitiq-validator") == version
    except PackageNotFoundError:
        return False


def _managed_venv_python() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return cache / "analitiq" / "pipeline-validator" / "venv" / "bin" / "python"


def _venv_has_pin(py: Path, version: str) -> bool:
    if not py.exists():
        return False
    probe = (
        "import sys; from importlib.metadata import version as v;"
        f"sys.exit(0 if v('analitiq-validator') == {version!r} else 1)"
    )
    return subprocess.run([str(py), "-c", probe], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def ensure_deps_or_reexec(script_path: str) -> None:
    """Guarantee the pinned validator is importable, re-exec'ing `script_path`
    under a managed venv if the current interpreter lacks it. Raises RuntimeError
    if the managed-venv install fails (no network / pip unavailable) or the package
    is still missing after the re-exec."""
    if os.environ.get(_FROM_SOURCE):
        import importlib.util
        try:
            spec = importlib.util.find_spec("analitiq.validator")
        except ModuleNotFoundError:
            # `find_spec` RAISES rather than returning None when the parent
            # `analitiq` namespace is itself absent - which is the most likely
            # breakage this branch exists to diagnose, so it must not escape as
            # a bare traceback.
            spec = None
        # `find_spec` alone is satisfied by a BARE DIRECTORY: a leftover or
        # half-deleted `analitiq/validator/` resolves as a namespace package with
        # `origin` None, so this would report success and the real failure would
        # surface much later as an opaque ImportError inside validate.py.
        if spec is None or spec.origin in (None, "namespace"):
            raise RuntimeError(
                f"{_FROM_SOURCE} is set but `analitiq.validator` has no importable "
                f"source (spec={spec!r}). Expected the in-repo tree on sys.path — "
                f"see the repo-root conftest.py — or unset {_FROM_SOURCE} to install "
                f"the pinned {VALIDATOR_PIN}.")
        # Say so. This branch disables the version-exactness guarantee, and a
        # variable inherited from a parent process would otherwise do that
        # invisibly — an arbitrary installed build would satisfy it and every
        # validation would still report "passed".
        print(f"[analitiq] {_FROM_SOURCE}=1 — using {spec.origin}, "
              f"NOT the pinned {VALIDATOR_PIN}", file=sys.stderr)
        return
    version = _pinned_version()
    if _importable(version):
        return
    py = _managed_venv_python()
    if not _venv_has_pin(py, version):
        try:
            py.parent.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([sys.executable, "-m", "venv", str(py.parent.parent)],
                           check=True, stdout=sys.stderr, stderr=sys.stderr)
            subprocess.run([str(py), "-m", "pip", "install", "--quiet",
                            "--disable-pip-version-check", "--pre", VALIDATOR_PIN],
                           check=True, stdout=sys.stderr, stderr=sys.stderr)
        except (subprocess.CalledProcessError, OSError) as exc:
            raise RuntimeError(
                f"could not install {VALIDATOR_PIN} into a managed venv ({exc}); "
                f"install it manually with: pip install --pre {VALIDATOR_PIN}") from exc
    if os.environ.get(_REEXEC_SENTINEL):
        raise RuntimeError(
            "analitiq-validator is not importable after bootstrap; install it "
            f"manually with: pip install --pre {VALIDATOR_PIN}")
    os.environ[_REEXEC_SENTINEL] = "1"
    # skipcq: BAN-B606 — the re-exec IS the mechanism: replace this process with
    # the managed venv's interpreter running the same script (argv preserved).
    os.execv(str(py), [str(py), os.path.abspath(script_path), *sys.argv[1:]])
