"""Shared enforcement for authored-shape Arrow container markers.

The Arrow `arrow_type` vocabulary is authored-shape only for nested data: three
bare markers declare JSON-container intent (fully-typed angle-bracket forms are
not part of the executable vocabulary — see ``analitiq.contracts.arrow_grammar``):

- ``Object`` — JSON object with a sibling ``properties`` map (recursive).
- ``List``   — JSON array with a sibling ``items`` field spec (recursive).
- ``Json``   — opaque JSON object or array; no inner declaration permitted.

The sibling-key contract is enforced at three sites that all share this
helper:

- :class:`analitiq.contracts.stream.ArrowFieldSpec` / ``AssignmentTarget`` /
  ``ConstantValue`` — stream mapping authored shapes.
- :class:`analitiq.contracts.endpoints.ColumnFieldSpec` / ``Column`` — database
  endpoint column schemas.
- ``analitiq.contracts.endpoints._validate_arrow_type_in_json_schema`` — API
  endpoint response/input JSON Schemas, which already use ``properties`` /
  ``items`` as standard JSON Schema keywords. The walker re-implements the
  rules in JSON-Schema terms but defers semantics here.
"""
from __future__ import annotations

from typing import Any

from analitiq.contracts.arrow_grammar import validate_cross_params


def enforce_container_shape(
    arrow_type: str,
    properties: Any,
    items: Any,
) -> None:
    """Validate the model-layer ``arrow_type`` semantics beyond the regex:
    sibling-key presence for the authored-shape markers, plus cross-parameter
    bounds the pattern cannot express (Decimal scale <= precision).

    This is the single chokepoint every model-layer ``arrow_type`` field runs
    through, so no acceptance site can forget either rule.

    Raises ``ValueError`` so Pydantic model validators surface the message as
    a ``ValidationError`` at the owning field path. Non-raising callers — see
    ``analitiq.contracts.endpoints._validate_arrow_type_in_json_schema`` — re-implement
    the same matrix in their own error-list dialect because they operate on
    raw dicts rather than Pydantic-coerced instances.
    """
    validate_cross_params(arrow_type)
    if arrow_type == "Object":
        if properties is None:
            raise ValueError("arrow_type 'Object' requires sibling 'properties'")
        if not properties:
            raise ValueError(
                "arrow_type 'Object' requires non-empty 'properties' map"
            )
        if items is not None:
            raise ValueError("arrow_type 'Object' must not carry 'items'")
        return
    if arrow_type == "List":
        if items is None:
            raise ValueError("arrow_type 'List' requires sibling 'items'")
        if properties is not None:
            raise ValueError("arrow_type 'List' must not carry 'properties'")
        return
    if properties is not None or items is not None:
        raise ValueError(
            f"arrow_type {arrow_type!r} must not carry 'properties' or "
            "'items'; those are only valid for 'Object' / 'List'"
        )


# Declarative mirror of `enforce_container_shape` for the published JSON Schema:
# Object ⇒ non-empty `properties`, no `items`; List ⇒ `items`, no `properties`;
# anything else ⇒ neither. Encoded as JSON-Schema if/then so external consumers
# enforce exactly what the runtime validator does. A forbidden sibling may be
# absent OR explicit null (the runtime coerces null to absent); a sibling
# carrying a value is rejected. `arrow_type` is an open pattern (not a
# `Literal`), so if/then is used rather than a discriminated union. Defined once
# here — next to the runtime helper — and attached via `json_schema_extra`
# directly to `ArrowFieldSpec` / `AssignmentTarget` (stream) and `Column` /
# `ColumnFieldSpec` (endpoint). `ConstantValue` does not attach it directly; it
# reuses these `allOf` branches inside a larger fragment
# (`_CONSTANT_VALUE_SCHEMA_RULES`) that also pins `value` by kind. Single-sourcing
# the branches keeps the container contract from drifting across those sites.
ARROW_CONTAINER_SCHEMA_RULES: dict[str, Any] = {
    "allOf": [
        {
            "if": {"required": ["arrow_type"], "properties": {"arrow_type": {"const": "Object"}}},
            "then": {
                "required": ["properties"],
                "properties": {
                    "properties": {"type": "object", "minProperties": 1},
                    "items": {"type": "null"},
                },
            },
        },
        {
            "if": {"required": ["arrow_type"], "properties": {"arrow_type": {"const": "List"}}},
            "then": {
                "required": ["items"],
                "properties": {
                    "items": {"not": {"type": "null"}},
                    "properties": {"type": "null"},
                },
            },
        },
        {
            "if": {
                "required": ["arrow_type"],
                "properties": {"arrow_type": {"not": {"enum": ["Object", "List"]}}},
            },
            "then": {"properties": {"properties": {"type": "null"}, "items": {"type": "null"}}},
        },
    ],
}
