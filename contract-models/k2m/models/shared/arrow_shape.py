"""Shared enforcement for authored-shape Arrow container markers.

The Arrow `arrow_type` vocabulary admits three bare markers that declare
JSON-container intent without a fully-typed `Struct<…>` / `List<…>` spec:

- ``Object`` — JSON object with a sibling ``properties`` map (recursive).
- ``List``   — JSON array with a sibling ``items`` field spec (recursive).
- ``Json``   — opaque JSON object or array; no inner declaration permitted.

The sibling-key contract is enforced at three sites that all share this
helper:

- :class:`k2m.models.stream.ArrowFieldSpec` / ``AssignmentTarget`` /
  ``ConstantValue`` — stream mapping authored shapes.
- :class:`k2m.models.endpoints.ColumnFieldSpec` / ``Column`` — database
  endpoint column schemas.
- ``k2m.models.endpoints._validate_arrow_type_in_json_schema`` — API
  endpoint response/input JSON Schemas, which already use ``properties`` /
  ``items`` as standard JSON Schema keywords. The walker re-implements the
  rules in JSON-Schema terms but defers semantics here.

Spec: the published Analitiq schema documentation §Assignment and
the published Analitiq schema documentation
§Native and Arrow Types.
"""
from __future__ import annotations

from typing import Any


def enforce_container_shape(
    arrow_type: str,
    properties: Any,
    items: Any,
) -> None:
    """Validate sibling-key presence for the authored-shape markers.

    Raises ``ValueError`` so Pydantic model validators surface the message as
    a ``ValidationError`` at the owning field path. Non-raising callers — see
    ``k2m.models.endpoints._validate_arrow_type_in_json_schema`` — re-implement
    the same matrix in their own error-list dialect because they operate on
    raw dicts rather than Pydantic-coerced instances.
    """
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
