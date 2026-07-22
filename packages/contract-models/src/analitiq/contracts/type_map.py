"""Type-map contract models — the on-disk `type-map-read.json` /
`type-map-write.json` files a connector ships under its `definition/`.

Each file is a top-level JSON array of `{match, native, canonical}` rules,
order significant (first match wins), non-empty. The two directions share the
rule shape but invert which key is the *matcher* and which is *rendered*:

- **read**  (`native → canonical`): match on `native`, render `canonical`
  (the canonical side is the Apache Arrow vocabulary).
- **write** (`canonical → native`): match on `canonical`, render `native`
  (the native side is free-form dialect DDL).

Source of truth for both the published `type-map-read` / `type-map-write` JSON
Schemas and the connector validator (which validates via `model_validate`).
Only *error*-level rules live here — things that make a document invalid.
Advisory quality checks that the contract tolerates (duplicate rules, dead
uppercase-only patterns, write-vocabulary coverage gaps) are not contract
violations and stay in the validator as warnings.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, RootModel, model_validator

from analitiq.contracts.endpoints import ARROW_TYPE_PATTERN
from analitiq.contracts.shared.common import StrictModel

# A literal `canonical` uses the SAME strict Arrow vocabulary the endpoint
# `arrow_type` does (`ARROW_TYPE_PATTERN`, incl. the `Json`/`Object`/`List`
# markers): one source of truth, so a type map cannot render a canonical an
# endpoint would reject. The pattern requires parameterized types to carry their
# parameters (`Timestamp(MICROSECOND)`, not bare `Timestamp`; `List<Int64>`, not
# `List(Int64)`) — issue #424.
_ARROW_TYPE_RE = re.compile(ARROW_TYPE_PATTERN)

# `${name}` render-side substitution (empty `${}` captured too, flagged below).
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")

# Canonical read-match normalization — one collapsing-whitespace regex reused.
_NATIVE_WS_RUN = re.compile(r"\s+")


def normalize_native_type(value: str) -> str:
    r"""Canonical normalization for type-map READ matching, platform-wide.

    Trim leading/trailing whitespace, collapse internal whitespace runs to a
    single space, then uppercase. This is the single source of truth every
    read-side consumer normalizes with, so a native resolves identically
    wherever it is matched:

    * **exact** rules — applied SYMMETRICALLY to the rule's `native` (at
      map-build time) and to the probed native (at lookup), so a rule authored
      `varchar` or `CHARACTER  VARYING` still matches `VARCHAR` /
      `character varying`. SQL type names are case-insensitive and drivers
      report inconsistent casing/spacing, so verbatim matching is a
      silent-miss footgun.
    * **regex** rules — applied to the probed native only; the pattern is
      authored verbatim (uppercasing it would corrupt classes like `\d`→`\D`).

    Consumers that cannot import this module (the engine's in-container reader,
    the alq-common `alq.db.type_map` layer) MIRROR this body byte-for-byte
    under a conformance test rather than diverging.
    """
    return _NATIVE_WS_RUN.sub(" ", value.strip()).upper()


def _validate_type_map_canonical(value: str) -> None:
    """Validate a `canonical` Arrow type against the strict Arrow pattern.

    A literal is `fullmatch`ed directly. A templated canonical
    (`Decimal128(${p}, ${s})`, `Timestamp(${unit})`) has each `${...}`
    substituted with a dummy first, then `fullmatch`ed — so the whole SHAPE is
    validated (a scalar carrying parameters like `Utf8(${x})`, trailing garbage,
    or a typo'd head all fail), not just the head. Two dummies are tried because
    the grammar is parameter-specific (numeric precision vs a keyword unit); a
    templated canonical is valid if either substitution yields a real Arrow type.
    Substitutions are parameter-positional, so one placeholder per parameter
    (`Decimal128(${p}, ${s})`, not `Decimal128(${p})`)."""
    if _PLACEHOLDER_RE.search(value):
        # Dummies span the parameter grammars: a number (Decimal/FixedSize*),
        # and one keyword from each temporal enum family — SECOND (Time32/
        # Timestamp/Duration), MICROSECOND (Time64/Timestamp/Duration), YEAR_MONTH
        # (Interval). A templated canonical is valid if ANY dummy resolves it.
        candidates = [_PLACEHOLDER_RE.sub(d, value)
                      for d in ("1", "SECOND", "MICROSECOND", "YEAR_MONTH")]
    else:
        candidates = [value]
    # `fullmatch`, not `match`, so a trailing newline (which Python `$` allows)
    # is rejected — matching the endpoint model's arrow_type check.
    if not any(_ARROW_TYPE_RE.fullmatch(c) for c in candidates):
        raise ValueError(f"canonical {value!r} is not a valid Arrow type")
# ECMA-262 named group `(?<name>…)` + named backreference `\k<name>` — the only
# named forms the contract allows; translated to Python's `(?P<name>…)` / `(?P=name)`
# spellings only to compile-check.
_ECMA_NAMED_GROUP = re.compile(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>")
_ECMA_NAMED_BACKREF = re.compile(r"\\k<([A-Za-z_][A-Za-z0-9_]*)>")
# Any non-ECMA `(?P…` extension: Python stdlib `(?P<>)` / `(?P=)`, PyPI regex
# `(?P>)`. None are valid ECMA-262.
_PYTHON_REGEX_FEATURE = re.compile(r"\(\?P[<=>]")

# Canonical (Arrow) container heads — the engine's own vocabulary, so listing
# them is DB-agnostic. A read rule that maps a structured native to a scalar
# canonical (not one of these) silently drops the value's structure. Covers every
# nested Arrow head `ARROW_TYPE_PATTERN` accepts (incl. the union/dictionary/
# run-end-encoded families), so a structured native → nested canonical isn't
# wrongly flagged as a structure-dropping scalar mapping.
_CONTAINER_CANONICAL_HEADS = {
    "Json", "Object", "List", "LargeList", "FixedSizeList", "Struct", "Map",
    "DenseUnion", "SparseUnion", "Dictionary", "RunEndEncoded",
}


def _to_python_regex(pattern: str) -> str:
    """ECMA `(?<name>…)`/`\\k<name>` → Python `(?P<name>…)`/`(?P=name)`, for the
    compile check only. Both the declaration AND the backreference must be
    translated, or an ECMA rule using `\\k<name>` fails to compile and is rejected."""
    pattern = _ECMA_NAMED_GROUP.sub(r"(?P<\1>", pattern)
    return _ECMA_NAMED_BACKREF.sub(r"(?P=\1)", pattern)


def _strip_regex_meta(pattern: str) -> str:
    """Approximate literal text of a regex: drop named groups + named backrefs +
    class/anchor escapes, keep other escaped characters as literals. Backrefs
    (`\\k<name>`) contribute no literal text and MUST be dropped whole — otherwise
    the trailing `<name>` is mistaken for container `<…>` syntax."""
    without_groups = _ECMA_NAMED_GROUP.sub("(", pattern)
    without_backrefs = _ECMA_NAMED_BACKREF.sub("", without_groups)
    without_class_escapes = re.sub(r"\\[dDsSwWbBAZfnrtvux0]", "", without_backrefs)
    return re.sub(r"\\(.)", r"\1", without_class_escapes)


def _canonical_head(canonical: str) -> str:
    """Leading PascalCase Arrow type name (empty if it opens with `${…}`)."""
    m = re.match(r"\s*([A-Za-z][A-Za-z0-9]*)", canonical)
    return m.group(1) if m else ""


def _native_is_schemaless_container(native: str, match: str) -> bool:
    """Best-effort, DB-agnostic detection of a structured/container native from
    its SYNTAX — never a vendor type-name list. Container natives are recognised
    by shape: angle-bracket parameterization (`array<int>`, `struct<...>`,
    `map<k, v>`) or a SQL array suffix (`integer[]`). Bare vendor scalars-for-
    JSON (`JSONB`, `VARIANT`, …) are intentionally not special-cased; if their
    structure matters the author writes it with `<...>` or `[]`."""
    probe = _strip_regex_meta(native) if match == "regex" else native
    if "<" in probe and ">" in probe:
        return True
    return probe.replace("\\", "").rstrip("$").endswith("[]")


def _validate_render_placeholders(render: str) -> None:
    """Reject malformed `${...}` in a render template: an empty `${}` or an
    unclosed `${` (missing `}`). Applies to any render that may carry `${name}`
    substitutions — a regex render, or an exact WRITE rule's `native` DDL."""
    if any(not name.strip() for name in _PLACEHOLDER_RE.findall(render)):
        raise ValueError(f"render value {render!r} contains an empty ${{}} placeholder")
    if "${" in _PLACEHOLDER_RE.sub("", render):
        raise ValueError(f"render value {render!r} has an unclosed '${{' (missing '}}')")


def _compile_ecma_matcher(matcher: str) -> "re.Pattern[str]":
    """Compile an ECMA-262 matcher, rejecting Python-only `(?P…)` regex syntax."""
    if _PYTHON_REGEX_FEATURE.search(matcher):
        raise ValueError(
            "matcher uses Python-only '(?P…)' regex syntax; the contract "
            "requires ECMA-262 (use '(?<name>…)' for named groups)"
        )
    try:
        return re.compile(_to_python_regex(matcher))
    except re.error as exc:
        raise ValueError(f"matcher is not a valid regex ({exc})") from exc


def _guard_container_not_collapsed(native: str, match: str, canonical: str) -> None:
    """A schemaless/structured native must not resolve to a scalar canonical
    (which would silently drop the value's structure). Read direction only."""
    if _native_is_schemaless_container(native, match):
        head = _canonical_head(canonical)
        if head and head not in _CONTAINER_CANONICAL_HEADS:
            raise ValueError(
                f"native {native!r} is a schemaless/structured container but "
                f"resolves to scalar canonical {canonical!r}; map it to a container "
                "canonical (`Json`, or `Object`/`List` for endpoint narrowings)"
            )


# An EXACT rule's `canonical` is a literal Arrow type. `ARROW_TYPE_PATTERN` both
# validates the Arrow grammar AND forbids `${…}` (the grammar admits no `$`), so
# this one field constraint replaces the runtime canonical-vocabulary + no-template
# checks — and, unlike a `@model_validator`, it publishes into the generated JSON
# Schema, so external consumers enforce it too. (A REGEX rule's canonical is a
# render template or matcher and is validated at runtime instead — see below.)
_ExactCanonical = Annotated[str, Field(min_length=1, pattern=ARROW_TYPE_PATTERN)]
_NonEmptyStr = Annotated[str, Field(min_length=1)]


class _TypeMapRuleBase(StrictModel):
    """Shared fields for every `{match, native, canonical}` rule. `match` is the
    discriminator selecting the exact/regex variant."""

    native: _NonEmptyStr
    canonical: _NonEmptyStr


class TypeMapReadExactRule(_TypeMapRuleBase):
    """Read exact rule: literal `native` matches, literal Arrow `canonical` renders."""

    match: Literal["exact"]
    canonical: _ExactCanonical

    @model_validator(mode="after")
    def _check(self) -> "TypeMapReadExactRule":
        _guard_container_not_collapsed(self.native, "exact", self.canonical)
        return self


class TypeMapReadRegexRule(_TypeMapRuleBase):
    """Read regex rule: ECMA-262 `native` matches, Arrow `canonical` render template
    (its `${name}` placeholders draw from the native's named captures)."""

    match: Literal["regex"]

    @model_validator(mode="after")
    def _check(self) -> "TypeMapReadRegexRule":
        # `canonical` is a (possibly templated) Arrow type — validate its shape.
        _validate_type_map_canonical(self.canonical)
        compiled = _compile_ecma_matcher(self.native)
        _validate_render_placeholders(self.canonical)
        _guard_container_not_collapsed(self.native, "regex", self.canonical)

        # Every `${name}` in the canonical render must name a native capture.
        capture_names = set(compiled.groupindex.keys())
        placeholders = _PLACEHOLDER_RE.findall(self.canonical)
        for name in placeholders:
            if name not in capture_names:
                raise ValueError(
                    f"render references ${{{name}}} but the matcher has no matching "
                    f"(?<{name}>…) capture group"
                )
        # Reverse correspondence: a native that CAPTURES parameters must not map to
        # a FULLY hardcoded parameterized canonical that discards them (every match
        # would collapse to that one by-example constant). A literal parameterized
        # Arrow type carries `(...)`; match-and-discard is expressed with a
        # non-capturing group `(?:…)`. Scope (issue #917): the detector keys on `(`
        # — paren-parameterized scalars (`Decimal128(…)`, `Timestamp(…)`).
        # Deliberately out of scope: a partially-templated canonical
        # (`Decimal128(${p}, 9)`, guarded by `not placeholders`) and a capture
        # discarded into a hardcoded nested `<…>`/`[…]` canonical (governed by the
        # schemaless-container rule instead).
        if capture_names and not placeholders and "(" in self.canonical:
            raise ValueError(
                f"native {self.native!r} captures {sorted(capture_names)} but canonical "
                f"{self.canonical!r} is a hardcoded parameterized type that discards them; "
                "reference the captures (e.g. `Decimal128(${p}, ${s})`) or use a "
                "non-capturing group `(?:…)` if the parameter is intentionally dropped"
            )
        return self


class TypeMapWriteExactRule(_TypeMapRuleBase):
    """Write exact rule: literal Arrow `canonical` matches, `native` DDL renders."""

    match: Literal["exact"]
    canonical: _ExactCanonical

    @model_validator(mode="after")
    def _check(self) -> "TypeMapWriteExactRule":
        # A write `native` render may carry `${length}` DDL hints — they must be
        # syntactically valid (no empty `${}` / unclosed `${`).
        _validate_render_placeholders(self.native)
        return self


class TypeMapWriteRegexRule(_TypeMapRuleBase):
    """Write regex rule: ECMA-262 `canonical` matches, `native` DDL render template.
    `canonical` is the matcher here, so it is NOT held to the Arrow vocabulary."""

    match: Literal["regex"]

    @model_validator(mode="after")
    def _check(self) -> "TypeMapWriteRegexRule":
        _compile_ecma_matcher(self.canonical)
        _validate_render_placeholders(self.native)
        return self


# `match`-discriminated unions: the exact branch carries the Arrow `pattern` on
# `canonical` (published into the JSON Schema); the regex branch keeps its
# runtime-only render/capture checks. Both directions render a `oneOf` with a
# `match` discriminator, so external validators reject exactly what the model does.
TypeMapReadRule = Annotated[
    TypeMapReadExactRule | TypeMapReadRegexRule,
    Field(discriminator="match"),
]
TypeMapWriteRule = Annotated[
    TypeMapWriteExactRule | TypeMapWriteRegexRule,
    Field(discriminator="match"),
]

TypeMapReadDoc = RootModel[Annotated[list[TypeMapReadRule], Field(min_length=1)]]
TypeMapWriteDoc = RootModel[Annotated[list[TypeMapWriteRule], Field(min_length=1)]]
