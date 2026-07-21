"""Gate the prose *outside* generated blocks (issue #24).

`test_generated_blocks_in_sync` verifies the contents of every marked region.
Nothing verified a fact was **in** a region — delete a marker pair, hand-type a
wrong vocabulary in its place, and the suite stayed green.

Two checks close that, and it is worth being precise about which does the work:

1. `test_docs_carry_their_required_blocks` — the load-bearing one. Every doc must
   still carry the blocks it is supposed to carry. Deleting a block from a doc is
   the actual #24 scenario, and no content heuristic can catch it reliably,
   because what replaces the block is by definition wrong in an unpredictable
   way. Pinning the manifest catches it regardless of what the replacement says.

2. `test_no_undeclared_vocabulary_restatement` — the heuristic. It finds a doc
   that names a whole closed vocabulary outside a block, which must then be
   declared in ALLOWED_RESTATEMENTS with a reason and its exact expected member
   set. It catches a *correct, complete* copy appearing somewhere new, and — via
   the staleness check — an allow-listed copy that LOSES a member. It does not
   catch a copy that gains one; see the coverage table below.

Why the manifest is primary. The heuristic is a full-set match, so it is blind to
drift by *omission or rename*: a hand-typed table missing a member is not a
superset and does not match. Loosening it to "names >= 2 members" would fire on
ordinary guidance that contrasts two members, which this prose does constantly.
So the heuristic cannot be the gate for the headline case, and is not asked to be.

Detection limits of the heuristic, stated so nobody over-trusts it:
  * members must appear in backticks. Dropping that is not an option — members
    include `in`, `range`, `pattern`, `required`, `fail` and `skip`, ordinary
    words that saturate this prose.
  * a restatement inside a fenced code block using bare (unticked) members is not
    seen. `FENCED_RESTATEMENTS` records the ones that exist today.
  * scope is `*.md` under the plugin root.
  * `_sections()` does not track fenced blocks, so a column-0 `#` inside a fence
    would split a section early. Zero occurrences across the 26 fenced blocks in `src/`
    today; the failure mode is a missed detection, never a false one.

What each check does and does not cover, measured:

  | drift                                          | caught by                |
  |------------------------------------------------|--------------------------|
  | a generated block deleted from a doc            | REQUIRED_BLOCKS          |
  | a new hand-typed FULL copy appearing            | undeclared / count check |
  | a new hand-typed SUBSET copy appearing          | NOTHING — see below      |
  | an allow-listed copy DROPPING a member          | staleness check          |
  | the contract moving under a declared member set | contract-match check     |
  | an allow-listed copy INVENTING a member in place| NOTHING — see below      |

The subset row is the same blind spot as the invention row, from the other side:
detection needs the whole vocabulary present, so a new doc asserting
`replication.method` is only `full_refresh` is invisible. REQUIRED_BLOCKS is what
covers the docs that matter; a brand-new doc is required to carry nothing.

One more thing this gate does not cover at all, by design: it polices closed
VOCABULARIES and block presence, not every contract fact prose can express.
"Minimum is 15" written beside an intact `fields-schedule` block passes, even
though the block emits `min=1` two lines above. Bounds, defaults and requiredness
asserted in sentences are only protected by being generated in the first place —
which is why REQUIRED_BLOCKS matters more than the heuristic.

The last row is a real hole, stated rather than papered over.
`test_allow_listed_restatements_still_match_the_contract` compares the DECLARED
member set to the contract; it never reads the document. Adding `hourly` beside
`manual`/`interval`/`cron` inside an already-allow-listed section keeps the set a
superset and the occurrence count at 1, so it passes. Closing it would mean
anchoring each entry to a hash of its section's backticked tokens, which trips on
every unrelated wording edit in that section — churn that would train people to
re-baseline the hash without reading, which is worse than the hole. Eight
sections are exposed; they are the eight in ALLOWED_RESTATEMENTS.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "plugins" / "analitiq-pipeline-builder"
sys.path.insert(0, str(ROOT / "scripts"))

pytest.importorskip("analitiq.validator",
                    reason="requires: pip install -r requirements-dev.txt")
import gen_contract_docs as G  # noqa: E402

# Which generated blocks each doc must carry. Deleting one is issue #24's exact
# scenario, so this manifest — not a content heuristic — is what catches it.
# Plugin policy (which doc shows what), not a contract fact, and asserted against
# reality in both directions so it cannot rot.
REQUIRED_BLOCKS = {
    "skills/connection-spec/SKILL.md": {"fields-connection", "schema-urls"},
    "skills/connection-spec/spec-envelope.md": {"secret-ref-grammar"},
    "skills/endpoint-spec/SKILL.md": {"fields-database-endpoint", "schema-urls"},
    "skills/endpoint-spec/spec-columns.md": {"advisory-endpoint", "arrow-types", "fields-column"},
    "skills/endpoint-spec/spec-database-object.md": {"endpoint-id-derivation", "fields-database-object"},
    "skills/pipeline-builder/SKILL.md": {"enum-vocabulary"},
    "skills/pipeline-builder/references/enum-mappers.md": {"enum-vocabulary"},
    "skills/pipeline-builder/references/identity-and-versioning.md": {"shared-vocabulary"},
    "skills/pipeline-builder/references/io-contracts.md": {"validator-ids"},
    "skills/pipeline-builder/references/schema-hosts.md": {"schema-urls"},
    "skills/pipeline-spec/SKILL.md": {"advisory-pipeline", "fields-pipeline", "schema-urls"},
    "skills/pipeline-spec/spec-connections.md": {"fields-pipeline-connections"},
    "skills/pipeline-spec/spec-engine-runtime.md": {
        "fields-batching", "fields-engine", "fields-error-handling", "fields-logging", "fields-runtime"},
    "skills/pipeline-spec/spec-schedule.md": {"fields-schedule"},
    "skills/stream-spec/SKILL.md": {
        "advisory-stream", "enum-vocabulary", "fields-stream", "schema-urls"},
    "skills/stream-spec/spec-destinations.md": {
        "fields-stream-destination", "fields-stream-execution", "fields-stream-write"},
    "skills/stream-spec/spec-endpoint-refs.md": {
        "endpoint-id-derivation", "fields-connection-endpoint-ref", "fields-connector-endpoint-ref"},
    "skills/stream-spec/spec-filter-operators.md": {"filter-operators"},
    "skills/stream-spec/spec-mapping.md": {
        "fields-assignment-target", "fields-assignment-value", "fields-stream-mapping"},
    "skills/stream-spec/spec-source.md": {"fields-stream-source"},
    "skills/stream-spec/spec-validation-rules.md": {"fields-validation-rule"},
}

# (doc, vocabulary) -> (occurrences, expected members, why a hand-typed copy is right here).
# The member set is recorded so an allow-listed copy cannot quietly gain an
# invented member or lose a real one; the count so a second copy cannot hide
# behind the first. An entry is a decision, not a backlog.
ALLOWED_RESTATEMENTS = {
    ("skills/pipeline-builder/references/enum-mappers.md", "schedule.type"):
        (1, {"manual", "interval", "cron"},
         "the phrasing->member mapping table's right-hand column IS the member"),
    ("skills/pipeline-builder/references/enum-mappers.md", "replication.method"):
        (1, {"full_refresh", "incremental"}, "same: mapper table target column"),
    ("skills/pipeline-builder/references/enum-mappers.md", "write.mode"):
        (1, {"insert", "upsert"}, "same: mapper table target column"),
    ("skills/pipeline-spec/spec-schedule.md", "schedule.type"):
        (1, {"manual", "interval", "cron"},
         "the §`timezone` section names all three types while explaining that "
         "timezone is validated for every one of them — an incidental co-mention"),
    ("skills/pipeline-spec/spec-schedule.md", "status"):
        (1, {"draft", "active", "inactive"},
         "the status->scheduling-effect table explains behaviour per member"),
    ("skills/pipeline-spec/spec-streams-and-status.md", "status"):
        (1, {"draft", "active", "inactive"}, "the status->runnability table"),
    ("skills/stream-spec/spec-validation-rules.md", "validation_rule.type"):
        (1, {"required", "not_null", "min_length", "max_length", "pattern", "range", "in_list"},
         "per-member semantics (which types take a `value`)"),
    ("agents/stream-creator.md", "write.mode"):
        (2, {"insert", "upsert"},
         "§`Process` and §`Hard rules` both state the conflict_keys rule "
         "(ADV-STRM-011), which distinguishes the two modes and so names both"),
}

# Restatements inside fenced code blocks using bare, unticked members. The
# heuristic cannot see these (a fence body is illustrative JSON, where quoting
# every member would be wrong), so they are recorded explicitly rather than left
# to look like clean prose.
FENCED_RESTATEMENTS = {
    ("skills/pipeline-builder/references/io-contracts.md", "replication.method"):
        "the PipelineFacts jsonc example annotates the field with its vocabulary",
    ("skills/pipeline-builder/references/io-contracts.md", "schedule.type"):
        "same example, schedule field",
    ("skills/pipeline-builder/references/io-contracts.md", "write.mode"):
        "the DriftVerdict example shows a write_mode_changed entry, which needs a "
        "concrete from/to pair to illustrate the shape",
}

# Vocabularies whose members are ordinary domain nouns, so a full-set match
# carries no signal. Keyed to a written reason, like the allow-list — the gate's
# escape hatch must cost as much to widen as the thing it gates.
EXCLUDED_FROM_PROSE_GATE = {
    "endpoint_ref.scope":
        "members are `connector` and `connection`, ordinary nouns that appear in "
        "backticks throughout for unrelated reasons. Measured with the exclusion "
        "lifted, exactly two sections trip and both are incidental co-mentions "
        "contrasting the two ref shapes. Still covered by the emission test.",
}

_TICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)


def _sections(text: str):
    """Yield (start_line, text) per markdown section — heading to next heading.

    The section, not the paragraph, is the unit: a paragraph unit is evaded by
    spreading members over consecutive paragraphs under one heading, which reads
    as a single enumeration. Measured on this repo, section scope finds exactly
    the same restatements, so the wider net costs nothing in false positives.
    """
    current, start = [], 1
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.startswith("#") and current:
            yield start, "\n".join(current)
            current, start = [], lineno
        current.append(line)
    if current:
        yield start, "\n".join(current)


def _gated_vocabularies():
    return {
        key: set(vocab["members"])
        for key, vocab in G.published_vocabularies().items()
        # A single-member "vocabulary" would match almost any mention.
        if len(vocab["members"]) > 1 and key not in EXCLUDED_FROM_PROSE_GATE
    }


def _restatements():
    """Every (doc, vocab, line) where a full vocabulary appears outside a block."""
    vocabularies = _gated_vocabularies()
    found = []
    for path in sorted(G.DOCS_ROOT.rglob("*.md")):
        # Replace each block with its own newline count rather than "": keeps
        # reported line numbers aligned with the real file, and stops prose that
        # abuts a block from being merged into one section.
        outside = G._BLOCK_RE.sub(lambda m: "\n" * m.group(0).count("\n"),
                                  path.read_text())
        rel = path.relative_to(G.DOCS_ROOT).as_posix()
        for start, section in _sections(outside):
            ticked = set(_TICKED.findall(section))
            for key, members in vocabularies.items():
                if members <= ticked:
                    found.append((rel, key, start))
    return found


def test_docs_carry_their_required_blocks():
    """The #24 scenario: a block deleted from the doc that is supposed to carry it.

    No content check can catch this reliably — whatever replaces the block is
    wrong in an unpredictable way. Pinning which doc carries which block does.
    """
    actual = {}
    for path in G.generated_docs():
        actual[path.relative_to(G.DOCS_ROOT).as_posix()] = {
            m.group("id") for m in G._BLOCK_RE.finditer(path.read_text())
        }
    missing = {
        doc: sorted(required - actual.get(doc, set()))
        for doc, required in REQUIRED_BLOCKS.items()
        if required - actual.get(doc, set())
    }
    assert not missing, (
        f"docs no longer carry blocks they are required to: {missing}. "
        "If a block was deliberately moved, update REQUIRED_BLOCKS; otherwise a "
        "generated fact has been replaced by hand-typed prose.")


def test_required_blocks_manifest_is_current():
    """The manifest must describe reality, or it silently stops requiring things."""
    actual = {}
    for path in G.generated_docs():
        actual[path.relative_to(G.DOCS_ROOT).as_posix()] = {
            m.group("id") for m in G._BLOCK_RE.finditer(path.read_text())
        }
    unlisted = {
        doc: sorted(ids - REQUIRED_BLOCKS.get(doc, set()))
        for doc, ids in actual.items() if ids - REQUIRED_BLOCKS.get(doc, set())
    }
    assert not unlisted, (
        f"docs carry blocks the manifest does not require: {unlisted}. Add them, "
        "so deleting one later is caught.")
    stale = sorted(set(REQUIRED_BLOCKS) - set(actual))
    assert not stale, f"REQUIRED_BLOCKS names docs with no generated block: {stale}"


def test_every_vocabulary_is_emitted_by_some_block():
    """An authoritative generated copy of each vocabulary must exist in the docs."""
    bodies = [
        match.group("body")
        for path in G.generated_docs()
        for match in G._BLOCK_RE.finditer(path.read_text())
    ]
    assert any(b.strip() for b in bodies), "no generated block content — vacuous pass"
    # Per block, not concatenated: a union would let a vocabulary's members be
    # scattered over unrelated blocks and still "pass" while no single block
    # presents it to a reader.
    ticked_per_block = [set(_TICKED.findall(b)) for b in bodies]
    missing = sorted(
        key for key, vocab in G.published_vocabularies().items()
        if not any(set(vocab["members"]) <= t for t in ticked_per_block)
    )
    assert not missing, (
        f"vocabularies no single generated block presents in full: {missing}. "
        "Wire a block that emits them, or an agent has only hand-typed copies to read.")


def test_every_member_is_matchable_by_the_detector():
    """The gate silently assumes every member is a backtickable identifier.

    A member containing `-` or `.` would be invisible to `_TICKED`, and the
    emission test above would then report a phantom missing block. Fail on the
    real cause instead.
    """
    unmatchable = {
        key: [m for m in vocab["members"] if not _TICKED.fullmatch(f"`{m}`")]
        for key, vocab in G.published_vocabularies().items()
        if any(not _TICKED.fullmatch(f"`{m}`") for m in vocab["members"])
    }
    assert not unmatchable, (
        f"published members the detector cannot match: {unmatchable}. Widen "
        "_TICKED — otherwise these are silently ungated.")


def test_no_undeclared_vocabulary_restatement():
    """A hand-typed copy of a whole vocabulary must be a recorded decision."""
    counts = Counter((doc, key) for doc, key, _ in _restatements())
    lines = {(doc, key): line for doc, key, line in _restatements()}
    undeclared = sorted(k for k in counts if k not in ALLOWED_RESTATEMENTS)
    assert not undeclared, (
        "these docs restate a full closed vocabulary outside any generated block:\n"
        + "\n".join(f"  {doc}:{lines[(doc, key)]}  [{key}]" for doc, key in undeclared)
        + "\n\nEither wire a generated block that emits it (preferred — then the "
          "copy cannot drift), or add an entry to ALLOWED_RESTATEMENTS in this "
          "file saying why a hand-typed copy is correct there."
    )
    # A second copy must not hide behind the first entry.
    wrong_count = {
        k: (counts[k], expected)
        for k, (expected, _members, _why) in ALLOWED_RESTATEMENTS.items()
        if counts.get(k, 0) != expected
    }
    assert not wrong_count, (
        f"allow-listed restatement count changed (found, expected): {wrong_count}. "
        "A new hand-typed copy appeared, or one was removed.")


def test_allow_listed_restatements_still_match_the_contract():
    """The DECLARED member set must still equal the contract's.

    Note what this does and does not read: it compares the set written in
    ALLOWED_RESTATEMENTS to the published contract. It never opens the document.
    So it catches the contract moving out from under a declaration — a member
    renamed or retired upstream — and nothing about the prose itself. An
    allow-listed copy that LOSES a member is caught by
    test_allow_list_has_no_stale_entries (the full set stops matching); one that
    GAINS an invented member is caught by nothing, which the module docstring's
    coverage table states outright.
    """
    published = G.published_vocabularies()
    # Unknown keys are test_declared_lists_name_real_vocabularies' business —
    # indexing them here would raise a bare KeyError and mask its message.
    wrong = {
        key: (declared, set(published[key[1]]["members"]))
        for key, (_n, declared, _why) in ALLOWED_RESTATEMENTS.items()
        if key[1] in published and declared != set(published[key[1]]["members"])
    }
    assert not wrong, (
        f"allow-listed member sets no longer match the contract (declared, published): "
        f"{wrong}. The doc's hand-typed copy has drifted, or the contract moved — "
        "check the doc before updating the entry.")


def test_fenced_restatements_are_declared():
    """Bare-member restatements inside fenced examples, recorded not hidden."""
    vocabularies = _gated_vocabularies()
    found = set()
    for path in sorted(G.DOCS_ROOT.rglob("*.md")):
        outside = G._BLOCK_RE.sub("", path.read_text())
        rel = path.relative_to(G.DOCS_ROOT).as_posix()
        for fence in _FENCE.findall(outside):
            words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", fence))
            for key, members in vocabularies.items():
                if members <= words:
                    found.add((rel, key))
    undeclared = sorted(found - set(FENCED_RESTATEMENTS))
    assert not undeclared, (
        f"fenced code blocks restate a vocabulary without being declared: {undeclared}. "
        "Add to FENCED_RESTATEMENTS with a reason, or rewrite the example.")
    stale = sorted(set(FENCED_RESTATEMENTS) - found)
    assert not stale, f"FENCED_RESTATEMENTS entries that no longer match: {stale}"


def test_exclusion_list_names_real_vocabularies():
    """A stale exclusion key silently gates nothing — the dead-constant failure."""
    unknown = sorted(set(EXCLUDED_FROM_PROSE_GATE) - set(G.published_vocabularies()))
    assert not unknown, (
        f"EXCLUDED_FROM_PROSE_GATE names vocabularies the contract does not "
        f"publish: {unknown}. Remove them — they exempt nothing.")


def test_declared_lists_name_real_vocabularies():
    """ALLOWED_/FENCED_RESTATEMENTS must name published vocabularies.

    Without this a renamed key surfaces as a bare KeyError from the contract-match
    test. Every other failure in this file carries an actionable message; these
    two lists should not be the exception.
    """
    published = set(G.published_vocabularies())
    unknown = sorted(
        {key for _doc, key in ALLOWED_RESTATEMENTS} - published
        | {key for _doc, key in FENCED_RESTATEMENTS} - published
    )
    assert not unknown, (
        f"declared restatements name vocabularies the contract does not publish: "
        f"{unknown}. The contract renamed or retired them — check the docs those "
        "entries exempt before editing the lists.")


def test_allow_list_has_no_stale_entries():
    """An allow-list that outlives its restatement quietly licenses a future one."""
    actual = {(doc, key) for doc, key, _ in _restatements()}
    stale = sorted(set(ALLOWED_RESTATEMENTS) - actual)
    assert not stale, (
        f"ALLOWED_RESTATEMENTS entries match no restatement: {stale}.\n"
        "Two possible causes, and they need opposite fixes:\n"
        "  (a) the hand-typed copy was removed or wired into a block — delete the entry;\n"
        "  (b) the doc LOST or RENAMED a member, so the full set no longer matches "
        "— that is drift. Check the doc before touching this list.")
