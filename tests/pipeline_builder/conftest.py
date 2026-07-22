"""Make this suite's `importorskip` guards fatal in CI.

Every contract-backed module here opens with
`pytest.importorskip("analitiq.validator", ...)` (prose-only modules need no
import), which is right for offline local work but wrong for a merge gate: if
the source tree were missing or renamed, they would all skip and the job would
go green having validated nothing.

`tests/connector_builder/test_schema_drift.py` already solves this with
`DRIFT_REQUIRE_CONTRACT_MODELS`; that variable was the only consumer, so the
same contract is extended here rather than inventing a second one. CI sets it
(see .github/workflows/tests.yml).
"""
from __future__ import annotations

import importlib.util
import os

import pytest


def pytest_collectstart(collector):
    if os.environ.get("DRIFT_REQUIRE_CONTRACT_MODELS") != "1":
        return
    spec = importlib.util.find_spec("analitiq.validator")
    # `origin` None means a bare namespace directory, not an importable module -
    # the same false positive the plugin's own bootstrap guards against.
    if spec is None or spec.origin in (None, "namespace"):
        raise pytest.UsageError(
            "DRIFT_REQUIRE_CONTRACT_MODELS=1 but `analitiq.validator` has no "
            f"importable source (spec={spec!r}). This suite would have skipped "
            "silently. Check the repo-root conftest.py put packages/*/src on "
            "sys.path."
        )
