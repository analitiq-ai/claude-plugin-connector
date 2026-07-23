"""Pin the researcher's grounding instructions to the ProviderFacts fragment.

`io-contracts.md` owns the ProviderFacts JSON Schema fragment;
`connector-provider-researcher.md` restates several of its database-branch
field names as grounding instructions ("report ... `sqlalchemy_driver` ...").
Nothing else ties their field names together, so a partial rename would leave
the researcher grounding fields the fragment no longer names — #70's
`async_sqlalchemy_driver` → `sqlalchemy_driver` rename happened to land
consistently, but only by care, not by any check (issue #72 item 4).

Convention this guard enforces: inside the researcher's `- For databases:`
hard-rule bullets, a backticked snake_case token (`` `like_this` ``) or dotted
path (`` `tls.supported_modes` ``) is a ProviderFacts field reference and must
resolve in the fragment (dotted paths resolve through nested object
`properties`). Backticked text that is not a bare snake_case identifier or
dotted path (`dialect+driver`, `COPY FROM stdin`, `mysql+aiomysql`) is prose,
not a field reference, and is ignored. The researcher prose carries a
maintainer comment pointing back here.

Pure text-vs-text: no contract packages involved, so no `_pins` skip guard —
this always runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "analitiq-connector-builder"
IO_CONTRACTS = PLUGIN_ROOT / "skills" / "connector-builder" / "references" / "io-contracts.md"
RESEARCHER = PLUGIN_ROOT / "agents" / "connector-provider-researcher.md"

_FIELD_TOKEN = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)`")


def _provider_facts_schema() -> dict:
    """The first ```json fenced block inside the `## ProviderFacts` section.

    The search is bounded at the next `## ` heading so a missing fence fails
    here with a message about the fragment, instead of silently matching a
    later section's block (EndpointFacts) and misdiagnosing downstream.
    """
    text = IO_CONTRACTS.read_text(encoding="utf-8")
    section = re.search(
        r"^## ProviderFacts.*?$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    assert section, f"{IO_CONTRACTS}: no '## ProviderFacts' heading"
    fence = re.search(r"^```json\n(.*?)^```", section.group(1), re.MULTILINE | re.DOTALL)
    assert fence, f"{IO_CONTRACTS}: no ```json block inside the ProviderFacts section"
    return json.loads(fence.group(1))


def _known_fields(schema: dict) -> set[str]:
    """Dotted property paths from the top level plus the database branch.

    Nested object `properties` contribute dotted paths (`tls` and
    `tls.supported_modes` are both known), so a rename inside a sub-object is
    caught the same as a branch-level one.
    """
    fields: set[str] = set()

    def walk(props: dict, prefix: str) -> None:
        for name, sub in props.items():
            fields.add(prefix + name)
            if isinstance(sub, dict):
                walk(sub.get("properties", {}), f"{prefix}{name}.")

    walk(schema.get("properties", {}), "")
    for branch in schema.get("oneOf", []):
        props = branch.get("properties", {})
        if props.get("kind", {}).get("const") == "database":
            walk(props, "")
            return fields
    pytest.fail(f"{IO_CONTRACTS}: ProviderFacts has no kind=database oneOf branch")


def _database_bullets() -> list[str]:
    """Each `- For databases:` bullet in ## Hard rules, continuations joined."""
    text = RESEARCHER.read_text(encoding="utf-8")
    section = re.search(r"^## Hard rules$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert section, f"{RESEARCHER}: no '## Hard rules' section"

    bullets: list[str] = []
    current: list[str] | None = None
    for line in section.group(1).splitlines():
        if line.startswith("- "):
            if current:
                bullets.append("\n".join(current))
            current = [line]
        elif current and line.startswith("  "):
            current.append(line)
        else:
            if current:
                bullets.append("\n".join(current))
            current = None
    if current:
        bullets.append("\n".join(current))
    return [b for b in bullets if b.startswith("- For databases:")]


def test_extraction_finds_the_grounding_bullets() -> None:
    """Guard the extraction itself: if the researcher prose restructures its
    database bullets, this fails loudly instead of the field check passing
    vacuously on an empty or shrunken token set."""
    bullets = _database_bullets()
    # Exact count, not a floor: with a floor, one of N>2 bullets could be
    # reworded away and its tokens would silently leave the guard. Adding or
    # removing a `- For databases:` bullet is a recorded decision — update
    # this count with it.
    assert len(bullets) == 2, (
        f"expected exactly 2 '- For databases:' bullets under '## Hard rules' "
        f"in {RESEARCHER.name}, found {len(bullets)} — if the prose "
        "restructured deliberately, update this count."
    )
    tokens = {t for b in bullets for t in _FIELD_TOKEN.findall(b)}
    assert tokens, "no backticked field tokens extracted from the bullets"
    # Canaries: the very field #70 renamed, and a dotted nested path (proves
    # dotted extraction works). If either renames again, all three sites
    # (fragment, prose, these literals) move together as a recorded decision.
    assert "sqlalchemy_driver" in tokens
    assert "tls.supported_modes" in tokens


def test_prose_grounded_fields_exist_in_provider_facts() -> None:
    """Every field the researcher prose instructs grounding must exist in the
    ProviderFacts fragment — a rename landing in only one file fails here in
    both directions (prose keeps the old name, or prose moves ahead of the
    fragment)."""
    known = _known_fields(_provider_facts_schema())
    tokens = {t for b in _database_bullets() for t in _FIELD_TOKEN.findall(b)}
    unknown = sorted(tokens - known)
    assert not unknown, (
        f"researcher prose grounds field(s) {unknown} that the ProviderFacts "
        f"fragment in {IO_CONTRACTS.name} does not define. Either the fragment "
        "renamed a field without the prose following, or the prose references "
        "a field that was never added — fix whichever file is stale."
    )


def test_provider_facts_database_branch_still_names_the_driver_fields() -> None:
    """The reverse anchor: the fragment's database branch keeps the fields the
    pipeline depends on by name — including the nested TLS mode carrier, which
    three prose files reference (`spec-tls.md`, `db-connector-creator.md`,
    `connector-provider-researcher.md`). Removing one from the fragment
    without touching the prose would otherwise only fail once the prose is
    next edited."""
    known = _known_fields(_provider_facts_schema())
    expected = {"adbc_driver_package", "flight_sql_endpoint", "bulk_load_protocol",
                "sqlalchemy_driver", "tls", "tls.supported_modes"}
    missing = sorted(expected - known)
    assert not missing, (
        f"ProviderFacts database branch lost field(s) {missing} — if the "
        "rename/removal is intentional, update the researcher prose bullets "
        "and this expectation together."
    )
