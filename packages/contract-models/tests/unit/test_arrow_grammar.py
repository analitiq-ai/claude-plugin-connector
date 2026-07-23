"""Guards for `analitiq.contracts.arrow_grammar` — the vendored engine manifest.

Three concerns, all offline:

1. **The pin**: the vendored `arrow_type_grammar.json` must hash to the sha256
   stated next to it. An edited/swapped vendored file fails here in any plain
   pytest run; the network half (published object byte-compare, conversion-
   matrix parity) lives in `scripts/check_engine_grammar_pin.py` (CI job
   `engine-grammar-pin-guard`).
2. **Derivation**: everything the contract derives from the manifest —
   `ARROW_TYPE_PATTERN`, container heads, template dummies — must be a pure
   function of the manifest's families, so a pin bump re-derives all of it.
3. **The generators' failure modes**: unsupported manifest shapes (int ranges
   the builder can't render, unknown param kinds) must fail loudly, never
   silently misparse.

Acceptance/rejection of concrete type strings is exercised where the pattern
is consumed (test_endpoint_model.py, test_canonical_types_schema.py,
validator's test_type_map_model.py) — not restated here.
"""
from __future__ import annotations

import hashlib
import re

import pytest

from analitiq.contracts import arrow_grammar


def test_vendored_grammar_hashes_to_the_pin():
    digest = hashlib.sha256(arrow_grammar._GRAMMAR_PATH.read_bytes()).hexdigest()
    assert digest == arrow_grammar.ENGINE_GRAMMAR_SHA256, (
        f"vendored {arrow_grammar._GRAMMAR_PATH.name} hashes to {digest}, but "
        f"the pin says {arrow_grammar.ENGINE_GRAMMAR_SHA256}. The vendored "
        "file and the pin constants must move together (re-vendor the "
        "published object, then re-render schemas + regenerate docs)."
    )


def test_every_family_appears_in_the_pattern():
    """The pattern is a pure derivation: each family name is present, and no
    alternative smuggles in a head the manifest doesn't know."""
    compiled = re.compile(arrow_grammar.ARROW_TYPE_PATTERN)
    for name, spec in arrow_grammar.FAMILIES.items():
        if spec.get("params"):
            # A parameterized family's bare name must NOT match…
            assert not compiled.fullmatch(name), f"bare {name!r} must be rejected"
        else:
            assert compiled.fullmatch(name), f"bare {name!r} must be accepted"
    # …and every head the pattern can accept is a manifest family.
    heads = set(re.findall(r"[A-Za-z][A-Za-z0-9]*", arrow_grammar.ARROW_TYPE_PATTERN))
    known_heads = {h for h in heads if h in arrow_grammar.FAMILIES}
    assert known_heads == set(arrow_grammar.FAMILY_NAMES)


def test_no_dead_families_in_the_manifest():
    """The issue #81 trim: none of the families the engine cannot execute may
    reappear in the vendored manifest without the engine shipping them first —
    at which point this list is consciously edited, which is the point."""
    # Bare `List`/`Object` are the authored-shape structural markers and are
    # NOT in this set — the trimmed families are the typed/encoded ones.
    dead = {
        "Interval", "LargeList", "FixedSizeList", "Struct", "Map",
        "SparseUnion", "DenseUnion", "Dictionary", "RunEndEncoded",
    }
    present = dead & set(arrow_grammar.FAMILY_NAMES)
    assert not present, (
        f"families {sorted(present)} are back in the vendored manifest — if the "
        "engine now executes them, update this list together with the re-add "
        "(prose, examples, and canonical-types groups all need the same pass)"
    )


def test_container_heads_derive_from_structural_families():
    structural = {
        name for name, spec in arrow_grammar.FAMILIES.items() if spec.get("structural")
    }
    assert arrow_grammar.CONTAINER_CANONICAL_HEADS == structural | {"Json"}
    assert structural == {"Object", "List"}


def test_template_dummies_cover_every_param_kind():
    """Substituting each dummy for every placeholder must resolve at least one
    dummy per parameterized family — the property `_validate_type_map_canonical`
    relies on."""
    compiled = re.compile(arrow_grammar.ARROW_TYPE_PATTERN)
    for name in arrow_grammar.PARAMETERIZED_FAMILY_NAMES:
        params = arrow_grammar.FAMILIES[name]["params"]
        required = [p for p in params if not p.get("optional")]
        template = name + "(" + ", ".join("${x}" for _ in required) + ")"
        assert any(
            compiled.fullmatch(template.replace("${x}", dummy))
            for dummy in arrow_grammar.TEMPLATE_DUMMY_SUBSTITUTIONS
        ), f"no dummy resolves {template!r}"


def test_int_range_pattern_bounds():
    def matches(pattern: str, value: int) -> bool:
        return re.fullmatch(pattern, str(value)) is not None

    p138 = arrow_grammar._int_range_pattern(1, 38)
    assert all(matches(p138, v) for v in (1, 9, 10, 29, 30, 38))
    assert not any(matches(p138, v) for v in (0, 39, 40, 100))
    p176 = arrow_grammar._int_range_pattern(1, 76)
    assert all(matches(p176, v) for v in (1, 76))
    assert not any(matches(p176, v) for v in (0, 77))
    unbounded = arrow_grammar._int_range_pattern(1, None)
    assert matches(unbounded, 1) and matches(unbounded, 12345)
    assert not re.fullmatch(unbounded, "0") and not re.fullmatch(unbounded, "01")
    zero = arrow_grammar._int_range_pattern(0, None)
    assert matches(zero, 0) and matches(zero, 10)
    assert not re.fullmatch(zero, "-1") and not re.fullmatch(zero, "007")


def test_unsupported_manifest_shapes_fail_loudly():
    with pytest.raises(ValueError):
        arrow_grammar._int_range_pattern(1, 100)  # beyond the two-digit builder
    with pytest.raises(ValueError):
        arrow_grammar._int_range_pattern(2, None)  # unsupported unbounded min
    with pytest.raises(ValueError):
        arrow_grammar._param_literal_pattern({"kind": "mystery", "name": "x"})


def test_cross_params_checks_literals_only():
    with pytest.raises(ValueError):
        arrow_grammar.validate_cross_params("Decimal128(5, 6)")
    with pytest.raises(ValueError):
        arrow_grammar.validate_cross_params("Decimal256(10, 11)")
    # Equal is allowed; templated / non-cross-ref / non-parameterized are ignored.
    arrow_grammar.validate_cross_params("Decimal128(5, 5)")
    arrow_grammar.validate_cross_params("Decimal128(${p}, 9)")
    arrow_grammar.validate_cross_params("Timestamp(SECOND, UTC)")
    arrow_grammar.validate_cross_params("Utf8")
    arrow_grammar.validate_cross_params("not a type at all")


def test_timestamp_offset_uses_the_manifest_pattern_verbatim():
    """The fixed-offset grammar is manifest-owned; the derived pattern must
    embed it unchanged (no hand-tightened hour range — issue #81's deltas)."""
    tz_param = next(
        p
        for p in arrow_grammar.FAMILIES["Timestamp"]["params"]
        if p["kind"] == "timezone"
    )
    assert tz_param["fixed_offset_pattern"] in arrow_grammar.ARROW_TYPE_PATTERN
