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


def test_pattern_is_a_pure_derivation_of_the_manifest():
    """The published pattern is EXACTLY the composition of the per-family
    fragments in sorted family order — nothing hand-appended can hide in it,
    and each family behaves as its spec says (bare accepted iff no params)."""
    assert arrow_grammar.ARROW_TYPE_PATTERN == (
        "^(?:"
        + "|".join(
            arrow_grammar.family_pattern(name)
            for name in sorted(arrow_grammar.FAMILIES)
        )
        + ")$"
    )
    compiled = re.compile(arrow_grammar.ARROW_TYPE_PATTERN)
    for name, spec in arrow_grammar.FAMILIES.items():
        if spec.get("params"):
            assert not compiled.fullmatch(name), f"bare {name!r} must be rejected"
        else:
            assert compiled.fullmatch(name), f"bare {name!r} must be accepted"


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


@pytest.mark.parametrize("lo,hi", [(1, 38), (1, 76), (0, 38), (12, 45), (10, 10)])
def test_int_range_pattern_is_exhaustively_correct(lo, hi):
    """Property check over every value near the range (covers the decade
    decomposition for lo >= 10, which the manifest doesn't exercise yet)."""
    pattern = re.compile(arrow_grammar._int_range_pattern(lo, hi))
    for v in range(0, 130):
        assert (pattern.fullmatch(str(v)) is not None) == (lo <= v <= hi), (
            f"[{lo},{hi}] wrong at {v}"
        )


def test_int_range_pattern_unbounded_and_leading_zeros():
    unbounded = arrow_grammar._int_range_pattern(1, None)
    assert re.fullmatch(unbounded, "1") and re.fullmatch(unbounded, "12345")
    assert not re.fullmatch(unbounded, "0") and not re.fullmatch(unbounded, "01")
    zero = arrow_grammar._int_range_pattern(0, None)
    assert re.fullmatch(zero, "0") and re.fullmatch(zero, "10")
    assert not re.fullmatch(zero, "-1") and not re.fullmatch(zero, "007")
    bounded_zero = arrow_grammar._int_range_pattern(0, 38)
    assert not re.fullmatch(bounded_zero, "05")


def test_cross_ref_int_bound_resolves_to_referenced_ceiling():
    """`"max": "precision"` caps a literal scale at precision's own max — the
    satisfiable envelope — so `Decimal128(38, 99)` fails the pattern outright
    (and with it the unsatisfiable template `Decimal128(${p}, 99)`)."""
    compiled = re.compile(arrow_grammar.ARROW_TYPE_PATTERN)
    assert compiled.fullmatch("Decimal128(38, 38)")
    assert not compiled.fullmatch("Decimal128(38, 39)")
    assert compiled.fullmatch("Decimal256(76, 76)")
    assert not compiled.fullmatch("Decimal256(76, 77)")
    templated = re.compile(
        "^" + arrow_grammar.family_pattern("Decimal128", templated=True) + "$"
    )
    assert templated.fullmatch("Decimal128(${p}, 20)")
    assert not templated.fullmatch("Decimal128(${p}, 99)")


def test_unsupported_manifest_shapes_fail_loudly():
    with pytest.raises(ValueError):
        arrow_grammar._int_range_pattern(1, 100)  # beyond the two-digit builder
    with pytest.raises(ValueError):
        arrow_grammar._int_range_pattern(2, None)  # unsupported unbounded min
    with pytest.raises(ValueError):
        arrow_grammar._param_literal_pattern({"kind": "mystery", "name": "x"}, [])


@pytest.mark.parametrize("params", [
    # leading optional before a required param
    [
        {"kind": "int", "min": 1, "max": None, "name": "a", "optional": True},
        {"kind": "int", "min": 1, "max": None, "name": "b"},
    ],
    # ALL-optional multi-param list: `\((?:A)?(?:\s*,\s*B)?\)` would accept
    # the malformed `(,B)` — must refuse too (round-2 review finding)
    [
        {"kind": "int", "min": 1, "max": None, "name": "a", "optional": True},
        {"kind": "int", "min": 1, "max": None, "name": "b", "optional": True},
    ],
])
def test_non_trailing_optional_param_fails_loudly(monkeypatch, params):
    """A leading/middle optional param — or an all-optional multi-param list —
    would silently generate a wrong grammar (the comma rides inside each
    non-first piece); the generator must refuse instead."""
    monkeypatch.setattr(
        arrow_grammar, "FAMILIES", {**arrow_grammar.FAMILIES, "Bad": {"params": params}}
    )
    with pytest.raises(ValueError, match="non-trailing optional"):
        arrow_grammar.family_pattern("Bad")


def test_sole_optional_param_is_supported(monkeypatch):
    monkeypatch.setattr(
        arrow_grammar,
        "FAMILIES",
        {
            **arrow_grammar.FAMILIES,
            "Solo": {"params": [
                {"kind": "int", "min": 1, "max": None, "name": "a", "optional": True},
            ]},
        },
    )
    pattern = re.compile("^" + arrow_grammar.family_pattern("Solo") + "$")
    assert pattern.fullmatch("Solo()") and pattern.fullmatch("Solo(3)")
    assert not pattern.fullmatch("Solo(,3)")


def test_dangling_cross_ref_bound_fails_loudly():
    """A typo'd `"max": "presicion"` would otherwise disable the bound at BOTH
    layers — unbounded pattern here, silent skip in validate_cross_params."""
    with pytest.raises(ValueError, match="unknown sibling param"):
        arrow_grammar._param_literal_pattern(
            {"kind": "int", "min": 0, "max": "presicion", "name": "scale"},
            [{"kind": "int", "min": 1, "max": 38, "name": "precision"}],
        )


def test_wheel_packaging_declares_the_vendored_manifest():
    """`arrow_grammar.py` loads the JSON at import time, so the wheel MUST ship
    it; the only thing putting it there is the pyproject package-data stanza.
    Pin the declaration to the filename constant so neither can rot alone.
    (The release workflow additionally installs the built wheel and imports
    it — this is the offline half of that guard.)"""
    import tomllib

    pyproject = (
        arrow_grammar._GRAMMAR_PATH.parents[3] / "pyproject.toml"
    )
    config = tomllib.loads(pyproject.read_text())
    package_data = config["tool"]["setuptools"]["package-data"]
    assert arrow_grammar.ENGINE_GRAMMAR_FILENAME in package_data.get(
        "analitiq.contracts", []
    ), (
        "pyproject [tool.setuptools.package-data] must list "
        f"{arrow_grammar.ENGINE_GRAMMAR_FILENAME} under 'analitiq.contracts' — "
        "without it the published wheel cannot import"
    )
    assert arrow_grammar._GRAMMAR_PATH.name == arrow_grammar.ENGINE_GRAMMAR_FILENAME


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
