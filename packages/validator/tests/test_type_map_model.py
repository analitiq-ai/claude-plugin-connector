"""Cross-field rules of the type-map contract models — the single-document
validity the validator delegates to. The PR premise ("the model rejects it, so
the validator catches it") rests on these, so they are pinned directly.
"""
import pytest
from pydantic import TypeAdapter, ValidationError

from analitiq.contracts.type_map import (
    TypeMapReadDoc,
    TypeMapWriteDoc,
    normalize_native_type,
)

READ = TypeAdapter(TypeMapReadDoc)
WRITE = TypeAdapter(TypeMapWriteDoc)


@pytest.mark.parametrize("raw,expected", [
    ("varchar", "VARCHAR"),
    ("VARCHAR", "VARCHAR"),
    ("  character  varying ", "CHARACTER VARYING"),
    ("timestamp\twithout time  zone", "TIMESTAMP WITHOUT TIME ZONE"),
    ("Int64", "INT64"),
])
def test_normalize_native_type_canonical(raw, expected):
    """The platform's single source of truth for read-match normalization:
    trim → collapse internal whitespace runs → uppercase."""
    assert normalize_native_type(raw) == expected


def _accepts(adapter, rules):
    adapter.validate_python(rules)


def _rejects(adapter, rules):
    with pytest.raises(ValidationError):
        adapter.validate_python(rules)


def test_empty_array_rejected():
    _rejects(READ, [])
    _rejects(WRITE, [])


def test_match_enum_and_required_keys():
    _rejects(READ, [{"match": "fuzzy", "native": "X", "canonical": "Utf8"}])
    _rejects(READ, [{"match": "exact", "native": "X"}])           # missing canonical
    _rejects(READ, [{"match": "exact", "native": "X", "canonical": "Utf8", "extra": 1}])


def test_exact_canonical_vocabulary():
    _accepts(READ, [{"match": "exact", "native": "STRING", "canonical": "Utf8"}])
    _rejects(READ, [{"match": "exact", "native": "STRING", "canonical": "NotArrow"}])


def test_canonical_rejects_bare_parameterized_types():
    # Must match the endpoint arrow vocabulary: parameterized types carry params,
    # nested types use <> not () (issue #424).
    for bad in ("Timestamp", "Decimal128", "Struct", "List(Int64)"):
        _rejects(READ, [{"match": "exact", "native": "X", "canonical": bad}])
    for ok in ("Timestamp(MICROSECOND)", "Decimal128(38, 9)", "List<Int64>", "Json"):
        _accepts(READ, [{"match": "exact", "native": "X", "canonical": ok}])


def test_canonical_rejects_trailing_newline():
    # `$` matches before a final newline; use fullmatch so `"Utf8\n"` is rejected
    # (consistent with the endpoint arrow_type check).
    _rejects(READ, [{"match": "exact", "native": "X", "canonical": "Utf8\n"}])


def test_templated_canonical_needs_literal_head():
    # A whole-value placeholder is invalid — substitutions are parameter-only.
    _rejects(READ, [{"match": "regex", "native": r"(?<type>\w+)", "canonical": "${type}"}])
    _accepts(READ, [{"match": "regex", "native": r"DEC\((?<p>\d+),(?<s>\d+)\)",
                     "canonical": "Decimal128(${p}, ${s})"}])


def test_templated_canonical_covers_all_temporal_enums():
    # The parameter-aware dummies must accept every temporal enum family, not
    # just microsecond-based ones (Time32 is SECOND/MILLISECOND; Interval is its own).
    for base in ("Time32", "Time64", "Timestamp", "Duration", "Interval"):
        _accepts(READ, [{"match": "regex", "native": r"X\((?<u>\w+)\)",
                         "canonical": f"{base}(${{u}})"}])


def test_write_native_may_carry_column_hints():
    # `native` is free-form DDL: per-column hint placeholders are allowed on both
    # exact and regex write rules (the contract permits `VARCHAR(${length})`).
    _accepts(WRITE, [{"match": "exact", "canonical": "Utf8", "native": "VARCHAR(${length})"}])
    _accepts(WRITE, [{"match": "regex", "canonical": r"^Decimal128\((?<p>\d+)\)",
                      "native": "NUMERIC(${p}, ${length})"}])  # capture + free hint mixed


def test_exact_must_not_template():
    _rejects(READ, [{"match": "exact", "native": "X", "canonical": "Decimal128(${p})"}])


def test_exact_write_native_render_placeholders_validated():
    # A write exact `native` render may carry `${length}` hints, but malformed
    # placeholders (empty / unclosed) must be rejected too — Codex round 5.
    _accepts(WRITE, [{"match": "exact", "canonical": "Utf8", "native": "VARCHAR(${length})"}])
    _rejects(WRITE, [{"match": "exact", "canonical": "Utf8", "native": "VARCHAR(${})"}])
    _rejects(WRITE, [{"match": "exact", "canonical": "Utf8", "native": "VARCHAR(${length)"}])


def test_regex_rejects_python_named_group():
    _rejects(READ, [{"match": "regex", "native": "(?P<p>.*)", "canonical": "Utf8"}])


def test_regex_ecma_named_backreference_accepted():
    # An ECMA named backreference `\k<name>` is valid contract syntax; it must be
    # translated to Python's `(?P=name)` (not rejected as uncompilable) — Codex r4.
    _accepts(READ, [{"match": "regex", "native": r"(?<x>\w+)_\k<x>", "canonical": "Utf8"}])


def test_regex_must_compile():
    _rejects(READ, [{"match": "regex", "native": "([", "canonical": "Utf8"}])


def test_placeholder_needs_matching_capture():
    _rejects(READ, [{"match": "regex", "native": "NUMERIC", "canonical": "Timestamp(${unit})"}])
    _accepts(READ, [{"match": "regex", "native": r"TS\((?<unit>\w+)\)",
                     "canonical": "Timestamp(${unit})"}])


def test_read_captured_native_must_not_discard_params_to_hardcoded_canonical():
    # Issue #917 Gap 1: a native that NAMES captures but maps to a literal
    # parameterized canonical silently coerces every source precision/scale/unit
    # to a by-example constant. Flag it (reverse of the placeholder→capture check).
    _rejects(READ, [{"match": "regex", "native": r"NUMERIC\((?<p>\d+),(?<s>\d+)\)",
                     "canonical": "Decimal128(38, 9)"}])
    _rejects(READ, [{"match": "regex", "native": r"TS\((?<u>\d+)\)",
                     "canonical": "Timestamp(MICROSECOND)"}])
    # Referencing the captures (templated canonical) is the correct mapping.
    _accepts(READ, [{"match": "regex", "native": r"DEC\((?<p>\d+),(?<s>\d+)\)",
                     "canonical": "Decimal128(${p}, ${s})"}])
    # A non-capturing group is the escape hatch for match-and-discard.
    _accepts(READ, [{"match": "regex", "native": r"NUMERIC\((?:\d+),(?:\d+)\)",
                     "canonical": "Decimal128(38, 9)"}])
    # A named capture mapping to a NON-parameterized canonical drops nothing.
    _accepts(READ, [{"match": "regex", "native": r"VARCHAR\((?<n>\d+)\)", "canonical": "Utf8"}])
    # Structural container canonicals (`<>`, not `()`) are not param-lossy here.
    _accepts(READ, [{"match": "regex", "native": r"ARR<(?<t>\w+)>", "canonical": "List<Int64>"}])
    # The reverse check is read-only: a write rule renders free-form native DDL,
    # so a canonical-side capture unused in the native is allowed.
    _accepts(WRITE, [{"match": "regex", "canonical": r"^Decimal128\((?<p>\d+)\)",
                      "native": "NUMERIC(20, 4)"}])


def test_templated_read_canonical_head_validated():
    # The non-placeholder head + shape of a templated read canonical is validated
    # (one placeholder per parameter position).
    _rejects(READ, [{"match": "regex", "native": r"DEC\((?<p>\d+),(?<s>\d+)\)", "canonical": "Decmal128(${p}, ${s})"}])
    _accepts(READ, [{"match": "regex", "native": r"DEC\((?<p>\d+),(?<s>\d+)\)", "canonical": "Decimal128(${p}, ${s})"}])


def test_schemaless_container_must_not_collapse_to_scalar():
    # Detection is DB-agnostic (by shape): a native written with container syntax
    # (`<...>` or `[]`) must not map to a scalar canonical. Bare vendor names
    # (`JSONB`) are intentionally not special-cased.
    _rejects(READ, [{"match": "exact", "native": "array<int>", "canonical": "Utf8"}])
    _accepts(READ, [{"match": "exact", "native": "array<int>", "canonical": "List<Int64>"}])
    _rejects(READ, [{"match": "exact", "native": "integer[]", "canonical": "Utf8"}])
    _accepts(READ, [{"match": "exact", "native": "JSONB", "canonical": "Utf8"}])  # bare name: not flagged


def test_schemaless_native_may_map_to_any_nested_arrow_container():
    # A structured native mapping to ANY nested Arrow head that ARROW_TYPE_PATTERN
    # accepts — including the union/dictionary/run-end-encoded families, not just
    # List/Struct — must be accepted (Codex round 3).
    for canonical in ("DenseUnion<a:Int64>", "SparseUnion<a:Int64>",
                      "Dictionary<Int32, Utf8>", "RunEndEncoded<Int32, Int64>"):
        _accepts(READ, [{"match": "exact", "native": "union<int, str>", "canonical": canonical}])
    # ...but a structured native → scalar canonical is still flagged.
    _rejects(READ, [{"match": "exact", "native": "union<int, str>", "canonical": "Utf8"}])


def test_write_direction_matches_canonical_renders_native():
    # write: canonical is the matcher (vocabulary-checked on exact), native is free-form DDL.
    _accepts(WRITE, [{"match": "exact", "canonical": "Int64", "native": "BIGINT"}])
    _rejects(WRITE, [{"match": "exact", "canonical": "NotArrow", "native": "BIGINT"}])
    _accepts(WRITE, [{"match": "regex", "canonical": r"^Decimal128\((?<p>\d+)\)",
                      "native": "NUMERIC(${p})"}])
