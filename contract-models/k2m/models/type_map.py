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
Schemas (rendered by `scripts/render_schemas.py`) and the connector validator
(which validates via `model_validate`). Only *error*-level rules live here —
things that make a document invalid. Advisory quality checks that the contract
tolerates (duplicate rules, dead uppercase-only patterns, write-vocabulary
coverage gaps) are not contract violations and stay in the validator as
warnings. See the published Analitiq schema documentation.
"""
from __future__ import annotations

import re
from typing import Annotated, ClassVar, Literal

from pydantic import Field, RootModel, model_validator

from k2m.models.endpoints import ARROW_TYPE_PATTERN
from k2m.models.shared.common import StrictModel

# A literal `canonical` uses the SAME strict Arrow vocabulary the endpoint
# `arrow_type` does (`ARROW_TYPE_PATTERN`, incl. the `Json`/`Object`/`List`
# markers): one source of truth, so a type map cannot render a canonical an
# endpoint would reject. The pattern requires parameterized types to carry their
# parameters (`Timestamp(MICROSECOND)`, not bare `Timestamp`; `List<Int64>`, not
# `List(Int64)`) — issue #424.
_ARROW_TYPE_RE = re.compile(ARROW_TYPE_PATTERN)

# `${name}` render-side substitution (empty `${}` captured too, flagged below).
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")


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


class _TypeMapRule(StrictModel):
    """One `{match, native, canonical}` rule. Subclasses fix `_direction`,
    which selects the matcher/render orientation for the cross-field checks."""

    match: Literal["exact", "regex"]
    native: Annotated[str, Field(min_length=1)]
    canonical: Annotated[str, Field(min_length=1)]

    _direction: ClassVar[Literal["read", "write"]]

    @model_validator(mode="after")
    def _check_wiring(self) -> "_TypeMapRule":
        matcher, render = (
            (self.native, self.canonical)
            if self._direction == "read"
            else (self.canonical, self.native)
        )

        # `canonical` is an Arrow type in every case EXCEPT a write regex rule,
        # where it is the regex *matcher* (a pattern over canonicals, compiled
        # below). Validate it strictly — literal or templated — so a typo'd head
        # or a bare parameterized type is rejected.
        if not (self._direction == "write" and self.match == "regex"):
            _validate_type_map_canonical(self.canonical)

        if self.match == "exact":
            # An exact rule matches/renders a literal canonical; it must not carry
            # `${...}`. Only the canonical is checked — a write map's `native`
            # render may carry per-column DDL hints (`VARCHAR(${length})`) on an
            # exact rule (the contract allows it; `native` is free-form DDL).
            if _PLACEHOLDER_RE.search(self.canonical):
                raise ValueError(
                    f"exact rules must not use ${{...}} in canonical; got {self.canonical!r}"
                )
            # A write exact rule's `native` render may carry `${length}` DDL hints —
            # but they must be syntactically valid (no empty `${}` / unclosed `${`),
            # same as a regex render, or the destination DDL renderer gets a
            # malformed template. (A read exact `native` is a literal matcher.)
            if self._direction == "write":
                _validate_render_placeholders(self.native)
            return self

        # match == "regex": the matcher must be a valid ECMA-262 regex.
        if _PYTHON_REGEX_FEATURE.search(matcher):
            raise ValueError(
                f"matcher uses Python-only '(?P…)' regex syntax; the contract "
                "requires ECMA-262 (use '(?<name>…)' for named groups)"
            )
        try:
            compiled = re.compile(_to_python_regex(matcher))
        except re.error as exc:
            raise ValueError(f"matcher is not a valid regex ({exc})") from exc

        _validate_render_placeholders(render)
        placeholders = _PLACEHOLDER_RE.findall(render)
        if self._direction == "read" and _native_is_schemaless_container(matcher, "regex"):
            head = _canonical_head(render)
            if head and head not in _CONTAINER_CANONICAL_HEADS:
                raise ValueError(
                    f"native {matcher!r} is a schemaless/structured container but "
                    f"resolves to scalar canonical {render!r}; map it to a container "
                    "canonical (`Json`, or `Object`/`List` for endpoint narrowings)"
                )
        # On a READ rule the canonical render's `${name}` must come from a native
        # regex capture. On a WRITE rule the `native` render may carry both
        # captures AND free-form per-column hints (`${length}`), so the contract
        # does not require every placeholder to be a capture.
        if self._direction == "read":
            capture_names = set(compiled.groupindex.keys())
            for name in placeholders:
                if name not in capture_names:
                    raise ValueError(
                        f"render references ${{{name}}} but the matcher has no matching "
                        f"(?<{name}>…) capture group"
                    )
            # Reverse correspondence: a native that CAPTURES parameters must not
            # map to a FULLY hardcoded parameterized canonical that discards them.
            # A literal parameterized Arrow type carries `(...)` (`Decimal128(38, 9)`,
            # `Timestamp(MICROSECOND)`) — if the native declares named captures and
            # the canonical carries params but references NONE of them (`not
            # placeholders`), every match collapses to that one by-example constant.
            # Match-and-discard is expressed with a non-capturing group (`(?:\d+)`).
            # Scope note (issue #917): the detector keys on `(` — i.e. paren-
            # parameterized scalars (`Decimal128(…)`, `Timestamp(…)`). Deliberately
            # out of scope: a partially-templated canonical (`Decimal128(${p}, 9)`,
            # guarded by `not placeholders`) and a capture discarded into a hardcoded
            # nested `<…>`/`[…]` canonical (`Struct<id:Int64>`) — the latter is rare
            # and its element shape is governed by the schemaless-container rule.
            if capture_names and not placeholders and "(" in render:
                raise ValueError(
                    f"native {matcher!r} captures {sorted(capture_names)} but canonical "
                    f"{render!r} is a hardcoded parameterized type that discards them; "
                    f"reference the captures (e.g. `Decimal128(${{p}}, ${{s}})`) or use a "
                    "non-capturing group `(?:…)` if the parameter is intentionally dropped"
                )
        return self


class TypeMapReadRule(_TypeMapRule):
    """A read rule: `native` matches, `canonical` renders (Arrow vocabulary)."""

    _direction: ClassVar[Literal["read", "write"]] = "read"

    @model_validator(mode="after")
    def _check_exact_schemaless(self) -> "TypeMapReadRule":
        # Exact read rules also guard the schemaless-container→scalar collapse
        # (the regex branch is handled in the base by the stripped matcher).
        if self.match == "exact" and _native_is_schemaless_container(self.native, "exact"):
            head = _canonical_head(self.canonical)
            if head and head not in _CONTAINER_CANONICAL_HEADS:
                raise ValueError(
                    f"native {self.native!r} is a schemaless/structured container but "
                    f"resolves to scalar canonical {self.canonical!r}; map it to a "
                    "container canonical (`Json`, or `Object`/`List`)"
                )
        return self


class TypeMapWriteRule(_TypeMapRule):
    """A write rule: `canonical` matches, `native` renders (free-form DDL)."""

    _direction: ClassVar[Literal["read", "write"]] = "write"


TypeMapReadDoc = RootModel[Annotated[list[TypeMapReadRule], Field(min_length=1)]]
TypeMapWriteDoc = RootModel[Annotated[list[TypeMapWriteRule], Field(min_length=1)]]
