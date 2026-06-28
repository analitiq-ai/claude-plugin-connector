"""Guard: JSON examples embedded in the API spec docs must validate against
the live published contract schema.

This closes the drift gap issue #9 surfaced. `spec-replication.md` carried
hand-written `cursor_mappings` / `supported_methods` shapes that no test
checked, so they silently diverged from `api-endpoint/latest.json` (wrong
property names, a `"full"` method that isn't in the enum, a `default_method`
key the contract forbids). The root cause was duplication: the doc restated
schema-owned shape as prose, and nothing held the two in step.

The fix is to stop trusting the prose. Every fenced ```json block in
`spec-replication.md` that carries a top-level `replication` key is now
validated against `#/$defs/Replication` of the live schema, so the doc
examples are checked against the contract rather than asserted by hand.

Network-marked (fetches the live schema); skipped offline like the other
Layer-1 network tests: `pytest -m "not network"`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from validate_connector import fetch_schema  # noqa: E402  (script dir added above)

API_ENDPOINT_SCHEMA_URL = "https://schemas.analitiq.ai/api-endpoint/latest.json"
SPEC_REPLICATION = REPO_ROOT / "skills" / "connector-spec-api" / "spec-replication.md"

_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def _replication_examples(doc: Path) -> list[dict]:
    """Replication blocks from every fenced ```json example in `doc` that
    carries a top-level `replication` key."""
    return [
        json.loads(raw)["replication"]
        for raw in _JSON_BLOCK.findall(doc.read_text())
        if "replication" in json.loads(raw)
    ]


REPLICATION_EXAMPLES = _replication_examples(SPEC_REPLICATION)


def test_spec_replication_has_examples():
    """Guard against the regex silently matching nothing — which would
    collapse the parametrized network test below to zero cases and let drift
    pass unnoticed. The doc ships a single-param and a window example."""
    assert len(REPLICATION_EXAMPLES) >= 2, (
        f"expected >= 2 replication examples in {SPEC_REPLICATION.name} "
        f"(single-param + window variants), found {len(REPLICATION_EXAMPLES)}"
    )


@pytest.mark.network
@pytest.mark.parametrize(
    "replication",
    REPLICATION_EXAMPLES,
    ids=[f"block{i}" for i in range(len(REPLICATION_EXAMPLES))],
)
def test_spec_replication_example_matches_contract(replication):
    """Each `replication` block in the spec doc validates against
    `#/$defs/Replication` of the live api-endpoint schema.

    The sub-schema's internal refs (`#/$defs/SingleCursorMapping`,
    `#/$defs/WindowCursorMapping` via the `oneOf`) are relative JSON
    pointers, so re-rooting a `$ref` over the schema's own `$defs` resolves
    the whole closure without any external registry."""
    schema = fetch_schema(API_ENDPOINT_SCHEMA_URL)
    sub_schema = {"$ref": "#/$defs/Replication", "$defs": schema["$defs"]}
    errors = sorted(Draft202012Validator(sub_schema).iter_errors(replication), key=str)
    assert not errors, (
        f"replication example does not match #/$defs/Replication in "
        f"{SPEC_REPLICATION.name}: {[e.message for e in errors]}"
    )
