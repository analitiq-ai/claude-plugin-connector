"""Repo-root conftest: put the in-repo package source on sys.path.

This repo is the SOURCE of `analitiq-contract-models` and `analitiq-validator`.
Tests must therefore exercise `packages/*/src`, never a wheel that happens to be
installed in the environment.

Note what this CANNOT do: `analitiq/contracts/` has no `__init__.py` in source
(it is generated at build time), so it is a namespace *portion*, while an
installed wheel ships the generated file and is a *regular* package — which wins
regardless of sys.path. So this works only when nothing is installed, which is
why requirements-dev.txt forbids it and `_pins.assert_pinned_versions()` checks
provenance rather than trusting the path order.

A root conftest is the only place this works. `packages/validator/tests/conftest.py`
puts both source roots on `sys.path`, but pytest collects
`packages/contract-models/tests/` first: those modules import `analitiq.contracts`
before that conftest ever runs, the installed distribution lands in
`sys.modules`, and every later import gets the stale copy regardless of
`sys.path`. Root conftests are imported before any collection, so this wins.

`analitiq` is a PEP 420 namespace spanning both source trees, which is why both
roots go on the path rather than one package importing the other.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# The contract models bind DOMAIN at import time for the `$schema` host Literal.
# The published wheel pins it; from source the ambient value wins, so set the
# public host before the first import.
os.environ.setdefault("DOMAIN", "analitiq.ai")

for _src in (REPO_ROOT / "packages" / "contract-models" / "src",
             REPO_ROOT / "packages" / "validator" / "src"):
    # Fail loudly. Skipping a missing root would leave the suite importing
    # whatever happens to be installed while every downstream `importorskip`
    # turned green — a merge gate passing having validated nothing.
    if not _src.is_dir():
        raise RuntimeError(
            f"{_src} is missing. The suite must exercise in-repo source, not an "
            "installed wheel; run pytest from a complete checkout.")
    _p = str(_src)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The pipeline plugin's helpers bootstrap the PUBLISHED validator into a managed
# venv and `os.execv` into it when `importlib.metadata` can't find the pin. Source
# on sys.path has no metadata, so without this the bootstrap would replace the
# pytest process mid-run. See `_FROM_SOURCE` in
# plugins/analitiq-pipeline-builder/scripts/_analitiq.py.
os.environ["ANALITIQ_VALIDATOR_FROM_SOURCE"] = "1"
