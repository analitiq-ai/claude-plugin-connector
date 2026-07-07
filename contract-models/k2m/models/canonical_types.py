"""Canonical Arrow types for Analitiq pipelines.

The pipeline engine streams data using Apache Arrow, so the canonical
type vocabulary is the Arrow PascalCase type names (e.g. ``Int64``,
``Utf8``, ``Timestamp(MICROSECOND)``). This module is the single source
of truth used by both the API (Pydantic validation on stream payloads)
and the LLM type-mapping prompt.

Tokens are either bare base names (``Int64``, ``Utf8``) or a base name
followed by a parenthesised parameter list (``Timestamp(MICROSECOND)``,
``Decimal128(38, 9)``, ``List(Int64)``). The parameter body is not
recursively validated here — only the base name is checked against the
known Arrow set.
"""
from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

ARROW_BASE_TYPES: frozenset[str] = frozenset({
    "Null",
    "Boolean",
    "Int8", "Int16", "Int32", "Int64",
    "UInt8", "UInt16", "UInt32", "UInt64",
    "Float16", "Float32", "Float64",
    "Decimal128", "Decimal256",
    "Binary", "LargeBinary", "FixedSizeBinary",
    "Utf8", "LargeUtf8",
    "Date32", "Date64",
    "Time32", "Time64",
    "Timestamp",
    "Duration",
    "Interval",
    "List", "LargeList", "FixedSizeList",
    "Struct",
    "Union",
    "Dictionary",
    "Map",
})

# JSON-Schema-friendly pattern: PascalCase base, optional parenthesised body.
CANONICAL_TYPE_PATTERN = r"^[A-Z][A-Za-z0-9]*(\(.+\))?$"

_TOKEN_RE = re.compile(CANONICAL_TYPE_PATTERN)


def validate_canonical_type(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(
            f"canonical type must be a string, got {type(value).__name__}"
        )
    match = _TOKEN_RE.match(value)
    if not match:
        raise ValueError(
            f"{value!r} is not a valid Arrow PascalCase type "
            "(expected e.g. 'Int64', 'Utf8', 'Timestamp(MICROSECOND)')"
        )
    base = value.split("(", 1)[0]
    if base not in ARROW_BASE_TYPES:
        raise ValueError(
            f"{value!r} has unknown Arrow base type {base!r}. "
            f"Allowed bases: {sorted(ARROW_BASE_TYPES)}"
        )
    return value


CanonicalType = Annotated[str, AfterValidator(validate_canonical_type)]
