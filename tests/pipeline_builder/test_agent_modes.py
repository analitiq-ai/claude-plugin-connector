"""Pin the private-endpoint-creator sub-mode names across their call sites.

The orchestrator (`skills/pipeline-builder/SKILL.md`) dispatches
`private-endpoint-creator` by sub-mode name, and the agent declares the modes
as `### Mode N: `<name>`` headers. The names are prose on both sides, so a
rename in one file would strand the other silently — this pins the vocabulary
in both places (the repeated value is unavoidable, so the copy is a test's
assertion target per `.claude/rules/no-drift-surfaces.md`).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "analitiq-pipeline-builder"
AGENT = PLUGIN / "agents" / "private-endpoint-creator.md"
ORCHESTRATOR = PLUGIN / "skills" / "pipeline-builder" / "SKILL.md"

MODES = (
    "discover-schemas",
    "discover-tables",
    "create-endpoints",
    "author-new-table",
)

# Return-envelope keys the orchestrator dispatches on — same drift class as
# the mode names: prose on both sides, and a rename in one file silently
# strands the other (the interview branch just never fires).
ENVELOPE_KEYS = ("write_render_choices", "write_gaps")


def test_agent_declares_exactly_the_pinned_modes():
    declared = re.findall(
        r"^### Mode \d+: `([^`]+)`",
        AGENT.read_text(encoding="utf-8"),
        flags=re.M,
    )
    assert tuple(declared) == MODES


def test_orchestrator_references_every_mode():
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    missing = [m for m in MODES if f"`{m}`" not in text]
    assert not missing, (
        f"pipeline-builder SKILL.md never names sub-mode(s) {missing!r}"
    )


def _names_in_backticks(text: str, token: str) -> bool:
    # Match the token inside any backticked span, so dotted forms like
    # `type_maps.write_gaps` count as naming `write_gaps`.
    return re.search(rf"`[^`]*{re.escape(token)}[^`]*`", text) is not None


def test_both_sides_name_every_envelope_key():
    agent = AGENT.read_text(encoding="utf-8")
    orchestrator = ORCHESTRATOR.read_text(encoding="utf-8")
    missing = [
        (name, key)
        for name, text in (("agent", agent), ("orchestrator", orchestrator))
        for key in ENVELOPE_KEYS
        if not _names_in_backticks(text, key)
    ]
    assert not missing, f"envelope key(s) unnamed on one side: {missing!r}"
