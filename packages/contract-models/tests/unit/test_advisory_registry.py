"""Advisory rule registry — engine, registry integrity, and shared-fixture gate.

This is the drift-prevention linchpin that replaces the removed CUE layer: every
relational rule ships a corpus of valid/invalid instance fixtures, and this suite
asserts the registry-driven Pydantic enforcement agrees with them. A non-Python
re-implementation reconciles against the same JSON fixtures.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from analitiq.contracts import connection, connector, endpoints, stream, type_map
from analitiq.contracts.pipelines import config as pipeline_config
from analitiq.contracts.pipelines import data_sync
from analitiq.contracts.shared import common
from analitiq.contracts.shared.advisory import (
    CUSTOM_KIND,
    GENERIC_KINDS,
    AdvisoryValidated,
    all_rules,
)


# tests/unit/<this file> -> parents[1] is tests/, which holds the fixtures.
# (In the infra repo this reached up to the repo root and back down through
# contract-models/; here the test already lives inside the package.)
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "advisory"

_MODULES = (
    connection, connector, endpoints, stream, type_map,
    pipeline_config, data_sync, common,
)


def _model_index() -> dict[str, type[BaseModel]]:
    """Map every model class name reachable from the contract modules to the class."""
    index: dict[str, type[BaseModel]] = {}
    for module in _MODULES:
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, BaseModel):
                index[obj.__name__] = obj
    return index


MODEL_INDEX = _model_index()
RULES = {r.id: r for r in all_rules()}
GENERIC_RULES = [r for r in all_rules() if r.kind in GENERIC_KINDS]


def _iter_fixtures():
    for rule_dir in sorted(FIXTURES_DIR.glob("*")):
        if not rule_dir.is_dir():
            continue
        for group in ("valid", "invalid"):
            for path in sorted((rule_dir / group).glob("*.json")):
                yield pytest.param(rule_dir.name, group, path, id=f"{rule_dir.name}/{group}/{path.stem}")


# --- Registry integrity -----------------------------------------------------


def test_rule_ids_are_unique():
    ids = [r.id for r in all_rules()]
    assert len(ids) == len(set(ids)), "duplicate advisory rule ids"


def test_every_target_resolves_to_a_model():
    for rule in all_rules():
        for target in rule.targets:
            assert target in MODEL_INDEX, f"{rule.id}: unknown target class {target!r}"


def test_generic_targets_use_the_mixin():
    """A generic rule only fires if its models inherit the advisory mixin."""
    for rule in GENERIC_RULES:
        for target in rule.targets:
            cls = MODEL_INDEX[target]
            assert issubclass(cls, AdvisoryValidated), (
                f"{rule.id}: {target} does not inherit AdvisoryValidated, so the rule never runs"
            )


def test_custom_rules_name_a_real_enforcer():
    """A custom rule's enforcer must exist on EVERY target — it runs on each."""
    for rule in all_rules():
        if rule.kind != CUSTOM_KIND:
            continue
        missing = [t for t in rule.targets if not hasattr(MODEL_INDEX[t], rule.enforcer)]
        assert not missing, (
            f"{rule.id}: enforcer {rule.enforcer!r} missing on {missing}"
        )


# --- Shared fixture corpus --------------------------------------------------


def test_every_generic_rule_has_fixtures():
    """Fail closed: each generic rule carries >=2 valid and >=2 invalid fixtures."""
    for rule in GENERIC_RULES:
        for group, minimum in (("valid", 2), ("invalid", 2)):
            found = list((FIXTURES_DIR / rule.id / group).glob("*.json"))
            assert len(found) >= minimum, (
                f"{rule.id}: {len(found)} {group} fixtures, need >= {minimum}"
            )


def test_no_orphan_fixture_directories():
    for rule_dir in FIXTURES_DIR.glob("*"):
        if rule_dir.is_dir():
            assert rule_dir.name in RULES, f"fixtures for unknown rule {rule_dir.name!r}"


@pytest.mark.parametrize("rule_id, group, path", list(_iter_fixtures()))
def test_fixture_matches_enforcement(rule_id, group, path):
    rule = RULES[rule_id]
    model = MODEL_INDEX[rule.fixture_target]
    payload = json.loads(path.read_text())
    if group == "valid":
        model.model_validate(payload)  # must not raise
    else:
        with pytest.raises(ValidationError) as exc:
            model.model_validate(payload)
        if rule.kind in GENERIC_KINDS:
            assert rule_id in str(exc.value), (
                f"{rule_id} invalid fixture rejected, but not by this rule"
            )
