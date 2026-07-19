"""Drift guard for the generated cross-field-rule reference.

`src/skills/connector-builder/references/advisory-rules.md` is rendered from the
pinned contract models' advisory registry so agent prose can cite a rule by id
instead of restating it. A generated copy is only safe while it is pinned: this
test regenerates it and fails when the checked-in file is stale, so a contract
change lands as a red build instead of silently-wrong authoring guidance.

Same environment contract as `test_schema_drift.py`: skipped when the pinned
package is absent (offline dev), hard-failed in CI via
`DRIFT_REQUIRE_CONTRACT_MODELS=1`.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

try:
    import analitiq.contracts  # noqa: F401
except ImportError:  # pragma: no cover - environment guard
    if os.environ.get("DRIFT_REQUIRE_CONTRACT_MODELS") == "1":
        raise
    pytest.skip(
        "analitiq-contract-models not installed — run `pip install --pre "
        '"analitiq-validator==1.0.0rc10" "analitiq-contract-models==1.0.0rc10"` '
        "to run the drift guards",
        allow_module_level=True,
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_advisory.py"


def _load_renderer():
    """Import the generator by path — `scripts/` is not an installed package."""
    spec = importlib.util.spec_from_file_location("render_advisory", SCRIPT_PATH)
    assert spec and spec.loader, f"cannot load {SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_advisory_reference_is_in_sync() -> None:
    renderer = _load_renderer()
    expected = renderer.render()
    output_path = renderer.OUTPUT_PATH

    assert output_path.exists(), (
        f"{output_path.relative_to(REPO_ROOT)} is missing — "
        "run `python scripts/render_advisory.py write`"
    )
    assert output_path.read_text(encoding="utf-8") == expected, (
        f"{output_path.relative_to(REPO_ROOT)} is stale — the contract's advisory "
        "registry changed. Run `python scripts/render_advisory.py write` and review "
        "any prose that cites the affected rule ids."
    )


def test_reference_covers_only_authored_resources() -> None:
    """The reference must not leak rules for documents this plugin never authors.

    Pipelines, streams, connection documents, and database endpoints belong to
    other tools; carrying their rules here would invite agents to enforce rules
    against documents they do not own.
    """
    from analitiq.contracts.shared.advisory import all_rules

    renderer = _load_renderer()
    rendered = renderer.OUTPUT_PATH.read_text(encoding="utf-8")

    foreign = [
        rule
        for rule in all_rules()
        if rule.resource not in renderer.PLUGIN_RESOURCES
    ]
    assert foreign, "expected the registry to carry rules outside the plugin's scope"

    leaked = sorted(rule.id for rule in foreign if rule.id in rendered)
    assert not leaked, f"reference leaked out-of-scope rule ids: {leaked}"
