"""Guard: JSON examples embedded in the API spec docs must validate against
the live published contract schema.

This closes the drift gap issue #9 surfaced. The spec docs restated
schema-owned shapes as prose, and nothing held the two in step, so they
silently diverged from `api-endpoint/latest.json` — `spec-replication.md`
documented `cursor_mappings` keys that don't exist and a `"full"` method
that isn't in the enum; `spec-pagination.md` documented string `stop_when`
values where the contract requires a predicate object, and wrong
`link`/`keyset` shapes.

The fix is to stop trusting the prose. Every fenced ```json block in a
covered spec doc that is a complete instance of a named contract
definition is validated against that definition of the live schema, so a
doc example can't drift from the contract without turning this test red.

Coverage:
  - spec-replication.md -> #/$defs/Replication
  - spec-pagination.md  -> #/$defs/{Offset,Page,Cursor,Link,Keyset}Pagination
    (selected per the example's `type` discriminator)

Not covered, by design: `spec-transport.md` and `spec-auth-flows.md`. Their
fenced blocks are deliberately illustrative fragments — partial transports
that rely on `transport_defaults` inheritance, and bare `"Authorization":
{...}` header members that aren't standalone JSON — not complete contract
objects. Their real worked examples are the `examples/*/*.example.json`
connector bodies, which the suite already validates against the live
connector schema.

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
SPEC_DIR = REPO_ROOT / "skills" / "connector-spec-api"

_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)

# Pagination examples are bare strategy objects discriminated by `type`;
# map each to its contract definition.
_PAGINATION_DEF = {
    "offset": "OffsetPagination",
    "page": "PagePagination",
    "cursor": "CursorPagination",
    "link": "LinkPagination",
    "keyset": "KeysetPagination",
}


def _select_replication(block):
    """A `{ "replication": {...} }` example -> validate the inner block."""
    if isinstance(block, dict) and "replication" in block:
        return ("Replication", block["replication"])
    return None


def _select_pagination(block):
    """A bare pagination strategy object -> validate against its `type` def."""
    if isinstance(block, dict) and block.get("type") in _PAGINATION_DEF:
        return (_PAGINATION_DEF[block["type"]], block)
    return None


# (doc filename, selector, minimum number of validatable examples expected).
# `minimum` guards against a regex/extractor change silently matching nothing
# (which would collapse the parametrized network test to zero cases) or an
# example being added without being wired into a selector.
_DOC_GUARDS = [
    ("spec-replication.md", _select_replication, 2),
    ("spec-pagination.md", _select_pagination, 5),
]


def _parsed_json_blocks(doc_path: Path):
    """Parsed fenced ```json blocks. Blocks that aren't standalone JSON are
    deliberate illustrative fragments and are skipped."""
    for raw in _JSON_BLOCK.findall(doc_path.read_text()):
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def _collect():
    cases, counts = [], {}
    for fname, selector, _minimum in _DOC_GUARDS:
        found = 0
        for i, block in enumerate(_parsed_json_blocks(SPEC_DIR / fname)):
            selected = selector(block)
            if selected is None:
                continue
            def_name, instance = selected
            cases.append(pytest.param(def_name, instance, id=f"{fname}:{def_name}:{i}"))
            found += 1
        counts[fname] = found
    return cases, counts


_CASES, _COUNTS = _collect()


@pytest.mark.parametrize(
    "fname,minimum",
    [(fname, minimum) for fname, _selector, minimum in _DOC_GUARDS],
    ids=[fname for fname, _selector, _minimum in _DOC_GUARDS],
)
def test_doc_has_expected_examples(fname, minimum):
    """Each covered doc still yields at least its known set of validatable
    examples — otherwise the network test below would pass vacuously."""
    assert _COUNTS[fname] >= minimum, (
        f"expected >= {minimum} validatable examples in {fname}, found {_COUNTS[fname]}; "
        "did a regex/extractor change stop matching, or was an example added "
        "without wiring it into a selector?"
    )


@pytest.mark.network
@pytest.mark.parametrize("def_name,instance", _CASES)
def test_doc_example_matches_contract(def_name, instance):
    """Each spec-doc example validates against its contract `$defs` entry.

    The definitions' internal refs are relative JSON pointers, so re-rooting
    a `$ref` over the schema's own `$defs` resolves the whole closure
    (e.g. a pagination `stop_when` predicate, a replication `oneOf`) without
    any external registry."""
    schema = fetch_schema(API_ENDPOINT_SCHEMA_URL)
    sub_schema = {"$ref": f"#/$defs/{def_name}", "$defs": schema["$defs"]}
    errors = sorted(Draft202012Validator(sub_schema).iter_errors(instance), key=str)
    assert not errors, (
        f"example does not match #/$defs/{def_name}: {[e.message for e in errors]}"
    )
