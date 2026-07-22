"""
Endpoint schema models.

This module owns the *endpoint document* contract: the JSON shape persisted
to the catalog (public connector endpoints) or materialized as a snapshot
(private database endpoints). Catalog storage paths and ``schema_hash``
canonicalization belong to the runtime layer; they are not fields on the
endpoint document.

Fields typed ``Any`` and described as value-expressions accept the shared
value-expression grammar: refs, templates, literals, and functions.

Endpoint documents have no top-level ``kind`` field. The owning connector's
``kind`` selects the per-kind document class:

  * connector ``kind == "api"`` → :class:`ApiEndpointDoc`
  * connector ``kind in {"database", "nosql", "document"}`` →
    :class:`DatabaseEndpointDoc`

Stream-side endpoint references (``EndpointRef``) live in ``analitiq.contracts.stream``.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Annotated, Any, Literal, Union, get_args

from pydantic import (
    ConfigDict,
    Discriminator,
    Field,
    Tag as UnionTag,
    field_validator,
    model_validator,
)

from analitiq.contracts.shared.advisory import AdvisoryValidated
from analitiq.contracts.shared.arrow_shape import (
    ARROW_CONTAINER_SCHEMA_RULES,
    enforce_container_shape,
)
from analitiq.contracts.shared.common import (
    DESCRIPTION_MAX,
    DISPLAY_NAME_MAX,
    DISPLAY_NAME_MIN,
    NO_EDGE_WHITESPACE_PATTERN,
    SLUG_PATTERN,
    StrictModel,
    TAGS_MAX,
    TrimmedTag,
    schema_url_for,
    validate_display_name,
    validate_tags,
)
from analitiq.contracts.value_expression import (
    RESOLUTION_SCOPES,
    iter_expression_strings,
    template_placeholders,
)


# ---------------------------------------------------------------------------
# Constants & regex
# ---------------------------------------------------------------------------

PATH_PLACEHOLDER_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
# Record field paths preserve segment spelling and casing. The pattern only
# enforces the dotted non-empty-segment shape; identifier chars are
# provider-owned.
RECORD_FIELD_PATH_PATTERN = (
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
METADATA_KEY_PATTERN = r"^[a-z][a-z0-9_]*$"

# Canonical Apache Arrow type vocabulary. The Pydantic models are the single
# source of truth for the canonical vocabulary (the connector validator enforces
# it via the models; the published schemas are generated from them). Issue #424:
# parameterized canonical types (`Timestamp`,
# `Decimal128/256`, `Time32/64`, `FixedSizeBinary`, `Duration`, `Interval`,
# and the angle-bracketed nested types `List<>`, `LargeList<>`,
# `FixedSizeList<>[n]`, `Struct<>`, `Map<,>`, `SparseUnion<>`, `DenseUnion<>`,
# `Dictionary<,>`, `RunEndEncoded<,>`) must carry their parameters — bare
# base names like `Timestamp`, `Decimal128`, `Struct`, or `List` are
# unbuildable by PyArrow and must fail at author time, not at sync time.
_ARROW_SCALARS = (
    r"Null|Boolean|Int8|Int16|Int32|Int64|UInt8|UInt16|UInt32|UInt64"
    r"|Float16|Float32|Float64|Utf8|LargeUtf8|Binary|LargeBinary"
    r"|Date32|Date64"
)
_ARROW_FIXED_SIZE_BINARY = r"FixedSizeBinary\([1-9][0-9]*\)"
_ARROW_TIME32 = r"Time32\((?:SECOND|MILLISECOND)\)"
_ARROW_TIME64 = r"Time64\((?:MICROSECOND|NANOSECOND)\)"
# Timestamp tz is optional. When present it is one of: `null`, an IANA-like
# token, an `Etc/GMT±N` form, or a fixed `±HH:MM` offset.
_ARROW_TIMESTAMP = (
    r"Timestamp\((?:SECOND|MILLISECOND|MICROSECOND|NANOSECOND)"
    r"(?:\s*,\s*(?:null|[A-Za-z_][A-Za-z0-9_/\-]*"
    r"|Etc/GMT[+\-][0-9]{1,2}|[+\-](?:0[0-9]|1[0-4]):[0-5][0-9]))?\)"
)
_ARROW_DURATION = r"Duration\((?:SECOND|MILLISECOND|MICROSECOND|NANOSECOND)\)"
_ARROW_INTERVAL = r"Interval\((?:YEAR_MONTH|DAY_TIME|MONTH_DAY_NANO)\)"
_ARROW_DECIMAL128 = r"Decimal128\((?:[1-9]|[12][0-9]|3[0-8])\s*,\s*-?[0-9]+\)"
_ARROW_DECIMAL256 = r"Decimal256\((?:[1-9]|[1-6][0-9]|7[0-6])\s*,\s*-?[0-9]+\)"
# Nested types — outer shape only; inner canonical-type validation is the
# validator API's responsibility (matches canonical-types.json scope note).
_ARROW_NESTED = (
    r"List<.+>|LargeList<.+>|FixedSizeList<.+>\[[1-9][0-9]*\]"
    r"|Struct<.+>|Map<.+,\s*.+>"
    r"|SparseUnion<.+>|DenseUnion<.+>"
    r"|Dictionary<.+,\s*.+>|RunEndEncoded<.+,\s*.+>"
)
# Bare authored-shape JSON container markers. Spelled without angle brackets
# so the outer `^…$` anchors on ARROW_TYPE_PATTERN keep them distinct from
# the parameterized forms in `_ARROW_NESTED` — bare `List` does not collide
# with `List<Int64>` because the nested alternatives require `<` after the
# base name.
#   Object — JSON object with declared shape; requires sibling `properties`.
#   List   — JSON array with declared element shape; requires sibling `items`.
#   Json   — opaque JSON object or array; no inner declaration permitted.
# Recursion + sibling rules are enforced at the model layer
# (analitiq.contracts.stream.ArrowFieldSpec, analitiq.contracts.endpoints.Column) and at the
# JSON Schema walker for API endpoint response/input schemas.
_ARROW_AUTHORED_SHAPE = r"Object|List|Json"
ARROW_TYPE_PATTERN = (
    r"^(?:"
    + _ARROW_SCALARS
    + r"|" + _ARROW_FIXED_SIZE_BINARY
    + r"|" + _ARROW_TIME32
    + r"|" + _ARROW_TIME64
    + r"|" + _ARROW_TIMESTAMP
    + r"|" + _ARROW_DURATION
    + r"|" + _ARROW_INTERVAL
    + r"|" + _ARROW_DECIMAL128
    + r"|" + _ARROW_DECIMAL256
    + r"|" + _ARROW_NESTED
    + r"|" + _ARROW_AUTHORED_SHAPE
    + r")$"
)

SLUG_RE = re.compile(SLUG_PATTERN)
PATH_PLACEHOLDER_NAME_RE = re.compile(PATH_PLACEHOLDER_NAME_PATTERN)
PATH_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
RECORD_FIELD_PATH_RE = re.compile(RECORD_FIELD_PATH_PATTERN)
METADATA_KEY_RE = re.compile(METADATA_KEY_PATTERN)
ARROW_TYPE_RE = re.compile(ARROW_TYPE_PATTERN)

# Sentinel so the arrow_type walker can distinguish "key absent" from
# "key present with value null"; `null` on either annotation counts as
# "not declared" for pairing purposes.
_MISSING = object()

RESERVED_RESPONSE_SCOPES: frozenset[str] = frozenset(
    {"body", "headers", "status", "records", "record_count", "metadata"}
)

# Declarative mirror of the read/write `response.metadata` key rules: every key
# matches `METADATA_KEY_PATTERN` and none collides with a reserved response
# scope. `propertyNames` validates each key and passes vacuously on the null
# branch of the nullable `metadata` field. Defined once so the read
# (`ResponseExtraction`) and write (`WriteResponse`) contracts cannot drift.
_METADATA_PROPERTY_NAMES: dict[str, Any] = {
    "propertyNames": {
        "pattern": METADATA_KEY_PATTERN,
        "not": {"enum": sorted(RESERVED_RESPONSE_SCOPES)},
    }
}

# Published JSON-Schema pattern for a typed `RefExpression.ref`: the value must
# begin with one of the resolution scopes (imported from the resolver so the
# vocabulary has one home). The `(?:\.|$)` boundary rejects a longer look-alike
# token — `responseX` fails while `response` and `response.body` pass. Only the
# leading scope is contract-checked; sub-path existence and per-phase
# availability are the runtime resolver's concern.
_RESOLUTION_SCOPE_PATTERN = r"^(?:" + "|".join(RESOLUTION_SCOPES) + r")(?:\.|$)"


def _has_known_scope(token: str) -> bool:
    """True when a ref/placeholder token's leading scope is a known resolution
    scope. Stripped like the resolver, which strips before resolving."""
    return token.strip().split(".", 1)[0] in RESOLUTION_SCOPES

# Single source of truth for the destination write-mode vocabulary. The
# `Literal` is the type used wherever a mode is keyed/validated (`operations.write`
# below, the endpoint-schema `write_modes` list); the tuple is derived from it so
# the two can never drift.
WriteMode = Literal["insert", "upsert"]
WRITE_MODES: tuple[str, ...] = get_args(WriteMode)
READ_METHODS: tuple[str, ...] = ("GET", "POST")
WRITE_METHODS: tuple[str, ...] = ("POST", "PUT", "PATCH")
API_ENDPOINT_SCHEMA_URL = schema_url_for("api-endpoint")
DATABASE_ENDPOINT_SCHEMA_URL = schema_url_for("database-endpoint")


# ---------------------------------------------------------------------------
# Shared base — x-* extension policy + frozen instances
# ---------------------------------------------------------------------------


_RESERVED_ENDPOINT_FIELDS: frozenset[str] = frozenset({
    "connector_id",
    "connector_version",
    "connection_id",
    "schema_hash",
})


class _EndpointModel(StrictModel):
    """Endpoint-module base: `StrictModel` plus alias handling and immutability.

    ``frozen=True`` prevents post-construction mutation, so the cross-field
    invariants checked in model validators stay valid for the lifetime of an
    instance.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        frozen=True,
        # Default `model_dump()` to wire-format names. Without this, dumps emit
        # Python attribute names (`schema_url`, `schema_`, `location`,
        # `and_`/`or_`/`not_`) and round-trip via `parse_endpoint(model.model_dump())`
        # would fail because none of those are valid spec keys.
        serialize_by_alias=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_reserved_fields(cls, data: Any) -> Any:
        """Spec-specific error for reserved-field smuggling.

        Reserved names (`connector_id`, `connector_version`, `connection_id`,
        `schema_hash`) are catalog metadata stored alongside the document,
        never in it. `endpoint_id` is the endpoint's own identifier and is
        declared on `_EndpointBase`, so it is allowed. Surface a clear
        message; Pydantic's own `extra="forbid"` would still catch them but
        with a less precise hint.
        """
        if not isinstance(data, dict):
            return data
        declared = _declared_field_names(cls)
        smuggled = sorted(
            k for k in data
            if k in _RESERVED_ENDPOINT_FIELDS and k not in declared
        )
        if smuggled:
            raise ValueError(
                f"reserved field names cannot be authored on endpoint documents: "
                f"{smuggled!r} (spec: §Reserved Fields)"
            )
        return data


# ---------------------------------------------------------------------------
# Value expressions (refs / templates / literals / functions)
# ---------------------------------------------------------------------------


class RefExpression(_EndpointModel):
    """``{"ref": "<scope>.<dotted-path>"}`` value expression."""

    ref: str = Field(
        ...,
        min_length=1,
        pattern=_RESOLUTION_SCOPE_PATTERN,
        description=(
            "Must begin with a known resolution scope: "
            + ", ".join(RESOLUTION_SCOPES)
            + " (spec: §Value Expressions)."
        ),
    )


class TemplateExpression(_EndpointModel):
    """``{"template": "...${scope.path}..."}`` value expression."""

    template: str = Field(..., min_length=1)

    @field_validator("template")
    @classmethod
    def _placeholders_qualified(cls, value: str) -> str:
        # Every `${...}` placeholder must begin with a known resolution scope;
        # an unqualified `${name}` would resolve to "" at runtime (a silent bug).
        # Placeholders are parsed by the shared resolver grammar
        # (`template_placeholders`), so this agrees with the resolver by
        # construction. Model-enforced only — not a published JSON-Schema
        # pattern, so the validator, not `latest.json`, is the complete gate.
        for placeholder in template_placeholders(value):
            if not _has_known_scope(placeholder):
                raise ValueError(
                    f"template placeholder ${{{placeholder}}} must begin with a "
                    "known resolution scope "
                    f"({', '.join(RESOLUTION_SCOPES)}); unqualified placeholders "
                    "are invalid (spec: §Value Expressions)"
                )
        return value


class LiteralExpression(_EndpointModel):
    """``{"literal": <any-json>}`` value expression — opt out of expression interpretation."""

    literal: Any = Field(...)


class FunctionExpression(_EndpointModel):
    """``{"function": <name>, ...}`` registered-function value expression."""

    function: str = Field(..., min_length=1)
    input: Any = Field(default=None)
    map: dict[str, Any] | None = Field(default=None)
    safe: str | None = Field(default=None)


_EXPRESSION_KEYS: tuple[str, ...] = ("ref", "template", "literal", "function")


def _expression_discriminator(v: Any) -> str | None:
    """Pick the expression branch by inspecting which expression key is present."""
    if isinstance(v, dict):
        for k in _EXPRESSION_KEYS:
            if k in v:
                return k
        return None
    if isinstance(v, RefExpression):
        return "ref"
    if isinstance(v, TemplateExpression):
        return "template"
    if isinstance(v, LiteralExpression):
        return "literal"
    if isinstance(v, FunctionExpression):
        return "function"
    return None


Expression = Annotated[
    Union[
        Annotated[RefExpression, UnionTag("ref")],
        Annotated[TemplateExpression, UnionTag("template")],
        Annotated[LiteralExpression, UnionTag("literal")],
        Annotated[FunctionExpression, UnionTag("function")],
    ],
    Discriminator(_expression_discriminator),
]


# ---------------------------------------------------------------------------
# Param contract
# ---------------------------------------------------------------------------


# Declarative mirror of `Param._validate`'s cross-field rules for the published
# schema: a `query` param of `array`/`object` type must declare `style` and
# `explode` (non-null); a `controlled_by` param must not declare `operators`.
# Keyed on the wire name `in` (the `location` alias). `then` pins the required
# fields to non-null types because they render nullable and the runtime demands
# a value.
_PARAM_SCHEMA_RULES: dict[str, Any] = {
    "allOf": [
        {
            "if": {
                "required": ["in", "type"],
                "properties": {
                    "in": {"const": "query"},
                    "type": {"enum": ["array", "object"]},
                },
            },
            "then": {
                "required": ["style", "explode"],
                "properties": {
                    "style": {"type": "string"},
                    "explode": {"type": "boolean"},
                },
            },
        },
        {
            "if": {
                "required": ["controlled_by"],
                "properties": {"controlled_by": {"not": {"type": "null"}}},
            },
            "then": {"properties": {"operators": {"type": "null"}}},
        },
    ],
}


class Param(_EndpointModel):
    """One operation-input contract."""

    model_config = ConfigDict(json_schema_extra=_PARAM_SCHEMA_RULES)

    location: Literal["path", "query", "header", "body"] = Field(
        ..., alias="in", description="Where the param is sent in the request.",
    )
    type: Literal["string", "integer", "number", "boolean", "array", "object"] = Field(
        ..., description="JSON-style validation type for the request input.",
    )
    required: bool = Field(..., description="Whether the param must resolve to a value.")
    description: str | None = Field(default=None)
    default: Any | None = Field(default=None, description="Default value (literal or value expression).")
    enum: list[Any] | None = Field(default=None)
    format: str | None = Field(default=None)
    pattern: str | None = Field(default=None)
    minimum: float | None = Field(default=None)
    maximum: float | None = Field(default=None)
    min_length: int | None = Field(default=None, alias="minLength", ge=0)
    max_length: int | None = Field(default=None, alias="maxLength", ge=0)
    min_items: int | None = Field(default=None, alias="minItems", ge=0)
    max_items: int | None = Field(default=None, alias="maxItems", ge=0)
    operators: list[Literal[
        "eq", "neq", "gt", "gte", "lt", "lte",
        "in", "not_in", "contains", "starts_with", "ends_with",
    ]] | None = Field(
        default=None,
        description=(
            "Subset of the Analitiq operator vocabulary stream filters may use. "
            "Absence means the param is not stream-filterable."
        ),
    )
    controlled_by: Literal["pagination", "replication"] | None = Field(
        default=None,
        description="Marks the param as owned by pagination or replication.",
    )
    style: str | None = Field(default=None, description="OpenAPI query serialization style.")
    explode: bool | None = Field(default=None)

    @model_validator(mode="after")
    def _validate(self) -> "Param":
        if _collect_singleton_values(self.default, "from_input"):
            raise ValueError(
                "from_input is invalid in params.<name>.default "
                "(spec: §Cross-Field Validation)"
            )
        if self.controlled_by is not None and self.operators is not None:
            raise ValueError(
                "params with `controlled_by` must not declare `operators` "
                "(spec: §Parameter Validation and Operators)"
            )
        if (self.location == "query" and self.type in ("array", "object")
                and (self.style is None or self.explode is None)):
            raise ValueError(
                    f"query params with type={self.type!r} must declare `style` and `explode` "
                    "(spec: §Parameter Validation and Operators)"
                )
        return self


# ---------------------------------------------------------------------------
# Pagination strategies
# ---------------------------------------------------------------------------


class PageSize(_EndpointModel):
    """Optional ``limit`` block shared by paginated strategies that accept page size."""

    param: str | None = Field(default=None)
    default: Any | None = Field(default=None, description="Default page size value or expression.")
    max: int | None = Field(default=None, ge=1)


class OffsetCursor(_EndpointModel):
    param: str = Field(..., min_length=1, description="Param that receives the offset/start index.")
    initial: Any = Field(..., description="Initial offset/start index value.")
    increment_by: Annotated[int, Field(gt=0)] | Expression = Field(  # type: ignore[valid-type]
        ...,
        description=(
            "Per-page offset step. Required, with no default: the two offset "
            "families cannot be told apart from the document, so any default "
            "silently breaks one of them. A positive-integer literal is a fixed "
            "step (`1` for page-index-style offsets). A value expression lets the "
            "engine advance by a per-page value (analitiq-engine #346/#347): "
            "`{ref: response.record_count}` when `offset` counts records returned "
            "(resolved against that page's response); when it counts the requested "
            "window, step by the *effective* request limit — the page size "
            "actually sent — which is `{ref: runtime.batch_size}` only where no "
            "smaller `limit.max` clamps it; with a cap, use the clamped value so "
            "the step matches the window requested (a raw batch size would "
            "overshoot and skip rows). Spec: §Value Expressions."
        ),
    )


class PageCursor(_EndpointModel):
    param: str = Field(..., min_length=1, description="Param that receives the page number.")
    initial: Any = Field(..., description="Initial page number.")
    increment_by: Any | None = Field(default=None, description="Increment per page (defaults to 1).")


class Cursor(_EndpointModel):
    param: str = Field(..., min_length=1, description="Param that receives the cursor/token.")
    next_cursor: Expression = Field(  # type: ignore[valid-type]
        ...,
        description=(
            "Value expression resolving to the next cursor/token. Spec "
            "§Cross-Field Validation: must be a value expression "
            "(``{ref}``/``{template}``/``{literal}``/``{function}``); "
            "``response_path`` is invalid."
        ),
    )


class Link(_EndpointModel):
    next_url: Expression = Field(  # type: ignore[valid-type]
        ...,
        description=(
            "Value expression resolving to the next absolute URL. Spec "
            "§Cross-Field Validation: must be a value expression; "
            "``response_path`` is invalid."
        ),
    )


class Keyset(_EndpointModel):
    param: str = Field(..., min_length=1, description="Param that receives the last seen key value.")
    order_by_field: str = Field(
        ...,
        pattern=RECORD_FIELD_PATH_PATTERN,
        description=(
            "Dotted record field path used for page ordering. Spec §Cross-Field "
            "Validation requires the dotted-path regex."
        ),
    )
    initial: Any | None = Field(
        default=None,
        description="Initial keyset value. Omit to send no keyset on the first request.",
    )


class OffsetPagination(_EndpointModel):
    """Offset/start-index pagination strategy."""

    type: Literal["offset"] = Field(...)
    offset: OffsetCursor = Field(...)
    limit: PageSize | None = Field(default=None)
    stop_when: "Predicate" = Field(...)


class PagePagination(_EndpointModel):
    """Page-number pagination strategy."""

    type: Literal["page"] = Field(...)
    page: PageCursor = Field(...)
    limit: PageSize | None = Field(default=None)
    stop_when: "Predicate" = Field(...)


class CursorPagination(_EndpointModel):
    """Opaque-cursor pagination strategy."""

    type: Literal["cursor"] = Field(...)
    cursor: Cursor = Field(...)
    limit: PageSize | None = Field(default=None)
    stop_when: "Predicate" = Field(...)


class LinkPagination(_EndpointModel):
    """Next-URL pagination strategy."""

    type: Literal["link"] = Field(...)
    link: Link = Field(...)
    stop_when: "Predicate" = Field(...)


class KeysetPagination(_EndpointModel):
    """Keyset (advance-from-last-key) pagination strategy."""

    type: Literal["keyset"] = Field(...)
    keyset: Keyset = Field(...)
    limit: PageSize | None = Field(default=None)
    stop_when: "Predicate" = Field(...)


Pagination = Annotated[
    Union[
        Annotated[OffsetPagination, UnionTag("offset")],
        Annotated[PagePagination, UnionTag("page")],
        Annotated[CursorPagination, UnionTag("cursor")],
        Annotated[LinkPagination, UnionTag("link")],
        Annotated[KeysetPagination, UnionTag("keyset")],
    ],
    Discriminator("type"),
]


# ---------------------------------------------------------------------------
# Predicate grammar (spec §Stop Conditions)
# ---------------------------------------------------------------------------
#
# Encoded as a discriminated union over the operator key. Per spec §Stop
# Conditions: "A predicate object must contain exactly one operator key." The
# discriminator returns the single non-``x-*`` key and Pydantic dispatches to
# the matching branch — there is no separate "exactly one" validator because
# the type system enforces it.


class PredicateEq(_EndpointModel):
    eq: list[Any] = Field(..., min_length=2, max_length=2)


class PredicateNeq(_EndpointModel):
    neq: list[Any] = Field(..., min_length=2, max_length=2)


class PredicateLt(_EndpointModel):
    lt: list[Any] = Field(..., min_length=2, max_length=2)


class PredicateLte(_EndpointModel):
    lte: list[Any] = Field(..., min_length=2, max_length=2)


class PredicateGt(_EndpointModel):
    gt: list[Any] = Field(..., min_length=2, max_length=2)


class PredicateGte(_EndpointModel):
    gte: list[Any] = Field(..., min_length=2, max_length=2)


class PredicateExists(_EndpointModel):
    exists: Any = Field(...)


class PredicateMissing(_EndpointModel):
    missing: Any = Field(...)


class PredicateEmpty(_EndpointModel):
    empty: Any = Field(...)


class PredicateNotEmpty(_EndpointModel):
    not_empty: Any = Field(...)


class PredicateAnd(_EndpointModel):
    and_: list["Predicate"] = Field(..., alias="and", min_length=1)  # type: ignore[valid-type]


class PredicateOr(_EndpointModel):
    or_: list["Predicate"] = Field(..., alias="or", min_length=1)  # type: ignore[valid-type]


class PredicateNot(_EndpointModel):
    not_: "Predicate" = Field(..., alias="not")  # type: ignore[valid-type]


# Single source of truth for predicate branches — `_PREDICATE_TAGS` and
# `_PREDICATE_INSTANCE_TAGS` derive from this tuple, and an import-time
# assertion below pins the explicit `Union[...]` member list to the same
# tuple so adding an operator can't drift any of the four structures.
_PRED_BRANCHES: tuple[tuple[str, type], ...] = (
    ("eq", PredicateEq),
    ("neq", PredicateNeq),
    ("lt", PredicateLt),
    ("lte", PredicateLte),
    ("gt", PredicateGt),
    ("gte", PredicateGte),
    ("exists", PredicateExists),
    ("missing", PredicateMissing),
    ("empty", PredicateEmpty),
    ("not_empty", PredicateNotEmpty),
    ("and", PredicateAnd),
    ("or", PredicateOr),
    ("not", PredicateNot),
)
_PREDICATE_TAGS: frozenset[str] = frozenset(t for t, _ in _PRED_BRANCHES)
_PREDICATE_INSTANCE_TAGS: dict[type, str] = {c: t for t, c in _PRED_BRANCHES}


def _predicate_discriminator(v: Any) -> str | None:
    """Pick the predicate branch from the single (non-``x-*``) operator key."""
    if isinstance(v, dict):
        op_keys = [
            k for k in v
            if isinstance(k, str) and not k.startswith("x-") and k in _PREDICATE_TAGS
        ]
        # Exactly one operator key is required by spec; any other count
        # produces an "Unable to extract tag" union error.
        if len(op_keys) == 1:
            return op_keys[0]
        return None
    return _PREDICATE_INSTANCE_TAGS.get(type(v))


Predicate = Annotated[
    Union[
        Annotated[PredicateEq, UnionTag("eq")],
        Annotated[PredicateNeq, UnionTag("neq")],
        Annotated[PredicateLt, UnionTag("lt")],
        Annotated[PredicateLte, UnionTag("lte")],
        Annotated[PredicateGt, UnionTag("gt")],
        Annotated[PredicateGte, UnionTag("gte")],
        Annotated[PredicateExists, UnionTag("exists")],
        Annotated[PredicateMissing, UnionTag("missing")],
        Annotated[PredicateEmpty, UnionTag("empty")],
        Annotated[PredicateNotEmpty, UnionTag("not_empty")],
        Annotated[PredicateAnd, UnionTag("and")],
        Annotated[PredicateOr, UnionTag("or")],
        Annotated[PredicateNot, UnionTag("not")],
    ],
    Discriminator(_predicate_discriminator),
]

def _union_tags(annotated_union: Any) -> frozenset[str]:
    """Extract `Tag(<name>)` values from an `Annotated[Union[Annotated[..., Tag(...)], ...], ...]`.

    Used to introspect the actual published `Union[...]` membership at import
    time so the drift guards detect any divergence between the explicit Union
    list, the `_PRED_BRANCHES`-style source-of-truth tuple, and downstream
    consumers like the discriminator's tag set.
    """
    tags: set[str] = set()
    union_arg, _discr = get_args(annotated_union)
    for member in get_args(union_arg):
        for meta in get_args(member)[1:]:
            if isinstance(meta, UnionTag):
                tags.add(meta.tag)
    return frozenset(tags)


# Drift guards — the actual `Union[...]` membership of each discriminated
# union must equal the source-of-truth tag list it derives from. Asserting
# at import time turns silent dispatch failures (`Unable to extract tag
# using discriminator`) into a clear ImportError when the structures
# diverge — including the case where a maintainer adds a branch to one
# list but not the other.
if _PREDICATE_TAGS != _union_tags(Predicate):
    raise AssertionError(
        f"Predicate Union members {sorted(_union_tags(Predicate))!r} do not match "
        f"_PRED_BRANCHES {sorted(_PREDICATE_TAGS)!r}")
if _union_tags(Expression) != frozenset(_EXPRESSION_KEYS):
    raise AssertionError(
        f"Expression Union members {sorted(_union_tags(Expression))!r} do not match "
        f"_EXPRESSION_KEYS {sorted(_EXPRESSION_KEYS)!r}")
if _union_tags(Pagination) != frozenset({"offset", "page", "cursor", "link", "keyset"}):
    raise AssertionError(
        f"Pagination Union members {sorted(_union_tags(Pagination))!r} do not match "
        "the expected pagination strategy set")


# Resolve forward refs: pagination → Predicate, plus Predicate's recursive
# and/or/not branches.
PredicateAnd.model_rebuild()
PredicateOr.model_rebuild()
PredicateNot.model_rebuild()
OffsetPagination.model_rebuild()
PagePagination.model_rebuild()
CursorPagination.model_rebuild()
LinkPagination.model_rebuild()
KeysetPagination.model_rebuild()


# ---------------------------------------------------------------------------
# Replication (spec §Replication)
# ---------------------------------------------------------------------------
#
# `CursorMapping` is a callable-discriminated union of single-param vs
# bounded-window forms. The wire format carries no tag; the discriminator
# picks the branch by detecting which form's fields are present. Mixed-form
# rejection lives on `Replication.cursor_mappings` as a `mode="before"`
# validator — keeping it there means the published JSON Schema's `oneOf`
# carries only the two real shapes, mirroring the runtime contract.


class SingleCursorMapping(_EndpointModel):
    """Single-param cursor mapping. Spec: §Replication."""

    cursor_field: str = Field(
        ...,
        pattern=RECORD_FIELD_PATH_PATTERN,
        description="Dotted record field path used as the incremental watermark.",
    )
    param: str = Field(..., min_length=1)
    operator: Literal["gt", "gte", "lt", "lte"]
    format: Literal["date-time", "date", "epoch_seconds", "epoch_milliseconds"] | None = Field(default=None)


class WindowCursorMapping(_EndpointModel):
    """Bounded-window cursor mapping (start/end provider params). Spec: §Replication."""

    cursor_field: str = Field(
        ...,
        pattern=RECORD_FIELD_PATH_PATTERN,
        description="Dotted record field path used as the incremental watermark.",
    )
    start_param: str = Field(..., min_length=1)
    end_param: str = Field(..., min_length=1)
    start_operator: Literal["gt", "gte", "lt", "lte"]
    end_operator: Literal["gt", "gte", "lt", "lte"]
    format: Literal["date-time", "date", "epoch_seconds", "epoch_milliseconds"] | None = Field(default=None)


_WINDOW_CM_FIELDS: tuple[str, ...] = ("start_param", "end_param", "start_operator", "end_operator")
_SINGLE_CM_FIELDS: tuple[str, ...] = ("param", "operator")


def _cursor_mapping_discriminator(v: Any) -> str | None:
    """Pick the single or window branch by detecting which form's fields are present.

    Pure-window inputs route to ``window``; everything else (pure-single,
    or input with neither form's fields) routes to ``single`` so common
    typos surface as "missing required field" errors. Mixed-form inputs
    are rejected upstream by ``Replication._reject_mixed_cursor_forms``,
    so the discriminator never has to handle that case.
    """
    if isinstance(v, dict):
        if any(f in v for f in _WINDOW_CM_FIELDS):
            return "window"
        return "single"
    if isinstance(v, WindowCursorMapping):
        return "window"
    if isinstance(v, SingleCursorMapping):
        return "single"
    return None


CursorMapping = Annotated[
    Union[
        Annotated[SingleCursorMapping, UnionTag("single")],
        Annotated[WindowCursorMapping, UnionTag("window")],
    ],
    Discriminator(_cursor_mapping_discriminator),
]


class Replication(_EndpointModel):
    """Replication block for API read operations. Spec: §Replication."""

    supported_methods: list[Literal["full_refresh", "incremental"]] = Field(
        ..., min_length=1,
    )
    cursor_mappings: list[CursorMapping] = Field(  # type: ignore[type-arg]
        ..., min_length=1,
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_mixed_cursor_forms(cls, data: Any) -> Any:
        # Spec §Replication forbids mixing single-param (`param`/`operator`)
        # with bounded-window (`start_param`/`end_param`/`start_operator`/
        # `end_operator`) fields in one cursor mapping. Catching this here
        # (mode="before", on the parent) keeps the published JSON Schema's
        # CursorMapping `oneOf` to two real branches; encoding the rule as a
        # synthetic third union branch would leak a permissively-shaped
        # `$def` that external JSON-Schema validators silently accept.
        if not isinstance(data, dict):
            return data
        cms = data.get("cursor_mappings")
        if not isinstance(cms, list):
            return data
        for i, cm in enumerate(cms):
            if not isinstance(cm, dict):
                continue
            single_keys = sorted(k for k in cm if k in _SINGLE_CM_FIELDS)
            window_keys = sorted(k for k in cm if k in _WINDOW_CM_FIELDS)
            if single_keys and window_keys:
                raise ValueError(
                    f"cursor_mappings[{i}] must not mix single-param and "
                    f"bounded-window forms; got single={single_keys!r} and "
                    f"window={window_keys!r} (spec: §Replication — declare "
                    "exactly one form)"
                )
        return data


# ---------------------------------------------------------------------------
# Request, Response, Operation blocks
# ---------------------------------------------------------------------------


# Declarative mirror of `_RequestBase._validate`'s presence correlation: a
# `path` that declares `{placeholder}`s requires a `path_params` object; a
# `path` with none forbids `path_params` (absent or null). The exact key-set
# equality (path_params keys == placeholder names) is instance-relative set
# logic that stock JSON Schema cannot express — it is enforced by
# `_RequestBase._validate` and catalogued in the advisory registry (ADV-ENDP-001).
_REQUEST_SCHEMA_RULES: dict[str, Any] = {
    "allOf": [
        {
            "if": {"required": ["path"], "properties": {"path": {"pattern": r"\{[^{}]+\}"}}},
            "then": {"required": ["path_params"], "properties": {"path_params": {"type": "object"}}},
        },
        {
            "if": {"required": ["path"], "properties": {"path": {"not": {"pattern": r"\{[^{}]+\}"}}}},
            "then": {"properties": {"path_params": {"type": "null"}}},
        },
    ],
}


class _RequestBase(AdvisoryValidated, _EndpointModel):
    """Common request fields shared by read and write operations."""

    model_config = ConfigDict(json_schema_extra=_REQUEST_SCHEMA_RULES)

    transport_ref: str | None = Field(
        default=None,
        description="Named transport this operation dispatches through; defaults to `default_transport`.",
    )
    path: str = Field(
        ...,
        min_length=1,
        description="Path or relative URL on the selected transport.",
        json_schema_extra={"not": {"pattern": r"\$\{"}},
    )
    path_params: dict[str, Any] | None = Field(
        default=None,
        description="Bindings for `{name}` placeholders in `path`. Values are `{from_param}` expressions.",
    )
    headers: dict[str, Any] | None = Field(
        default=None,
        description="Endpoint-declared request headers; values may be literals or `{from_param}`/`{ref}`/`{template}`.",
    )
    headers_remove: list[str] | None = Field(
        default=None,
        description="Header names to delete from inherited transport defaults (case-insensitive).",
    )
    query: dict[str, Any] | None = Field(
        default=None,
        description="Endpoint-declared query parameters; values may be literals or expressions.",
    )

    @model_validator(mode="after")
    def _validate(self) -> "_RequestBase":
        placeholders = PATH_PLACEHOLDER_RE.findall(self.path)
        if len(placeholders) != len(set(placeholders)):
            raise ValueError(
                f"request.path contains duplicate placeholders in {self.path!r} "
                "(spec: §Request Parameter Binding)"
            )
        for ph in placeholders:
            if not PATH_PLACEHOLDER_NAME_RE.match(ph):
                raise ValueError(
                    f"path placeholder {ph!r} must match "
                    f"{PATH_PLACEHOLDER_NAME_PATTERN!r} (spec: §Request Parameter Binding)"
                )
        if "${" in self.path:
            raise ValueError(
                "request.path must not contain ${...} template expressions "
                "(spec: §Request Parameter Binding)"
            )
        placeholder_set = set(placeholders)
        # Use explicit `is None`: `path_params={}` is meaningfully different
        # from omitted, and the falsy-check version treats them the same.
        if placeholder_set and self.path_params is None:
            raise ValueError(
                f"request.path declares placeholders {sorted(placeholder_set)!r} but "
                "request.path_params is missing (spec: §Request Parameter Binding)"
            )
        if not placeholder_set and self.path_params is not None:
            raise ValueError(
                "request.path_params is present but request.path has no placeholders "
                "(spec: §Request Parameter Binding)"
            )
        if self.path_params is not None:
            extra = set(self.path_params) - placeholder_set
            missing = placeholder_set - set(self.path_params)
            if extra or missing:
                raise ValueError(
                    f"request.path_params keys must equal placeholders in path; "
                    f"extra={sorted(extra)!r}, missing={sorted(missing)!r} "
                    "(spec: §Request Parameter Binding)"
                )
        return self


class GetReadRequest(_RequestBase):
    """Provider request for a GET-method API read operation. GET declares no body."""

    method: Literal["GET"] = Field(..., description="Read HTTP method.")


class PostReadRequest(_RequestBase):
    """Provider request for a POST-method API read operation (query-in-body reads)."""

    method: Literal["POST"] = Field(..., description="Read HTTP method.")
    body: Any | None = Field(
        default=None,
        description="JSON request body. May mix literals with `{from_param}`.",
    )


# `method`-discriminated read request: only the POST branch declares `body`, so
# the published JSON Schema structurally forbids a body on a GET read (the rule
# formerly enforced only by a `@model_validator`). Both branches share the
# `_RequestBase` fields.
ReadRequest = Annotated[
    GetReadRequest | PostReadRequest,
    Field(discriminator="method"),
]


class WriteRequest(_RequestBase):
    """Provider request for an API write mode."""

    method: Literal["POST", "PUT", "PATCH"] = Field(
        ..., description="Write HTTP method (closed v1 enum).",
    )
    body: Any | None = Field(
        default=None,
        description="JSON request body. May mix literals with `{from_param}` (and `{from_input}` for writes).",
    )


# JSON Schema 2020-12 keywords whose values are themselves schemas (or maps/
# lists of schemas). Used by the arrow_type walker to recurse only through
# structural positions — never through `default`, `examples`, `const`, etc.,
# which can legally carry arbitrary user data shaped like a schema.
_JSON_SCHEMA_SUBSCHEMA_KEYS: frozenset[str] = frozenset({
    "properties", "patternProperties", "$defs", "definitions",
    "dependentSchemas",
})
_JSON_SCHEMA_LIST_OF_SCHEMA_KEYS: frozenset[str] = frozenset({
    "allOf", "anyOf", "oneOf", "prefixItems",
})
_JSON_SCHEMA_SINGLE_SCHEMA_KEYS: frozenset[str] = frozenset({
    "items", "contains", "additionalProperties", "propertyNames",
    "unevaluatedItems", "unevaluatedProperties",
    "not", "if", "then", "else",
})


def _validate_arrow_type_in_json_schema(
    schema: Any, path: str, errors: list[str]
) -> None:
    """Walk a JSON Schema document and enforce arrow_type contract rules.

    Issue #424 / spec §Native and Arrow Types:
      (1) any subschema carrying `arrow_type` must match the canonical Arrow
          type vocabulary — bare parameterized forms like 'Timestamp' or
          'Decimal128' are rejected at author time.
      (2) any subschema declaring `native_type` or `arrow_type` must declare
          both. Pairing is enforced per node; the walker does not distinguish
          leaf and inner subschemas.
    """
    # JSON Schema 2020-12 permits `true` / `false` as a whole-schema short-form
    # ("anything" / "nothing"). Those are valid but carry no arrow_type, so
    # walk past them. Non-bool, non-dict values in a schema position are
    # malformed JSON Schema; surface them rather than silently skipping.
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        errors.append(
            f"{path} is not a JSON Schema object/boolean (got "
            f"{type(schema).__name__}); cannot validate arrow_type "
            "(spec: §Native and Arrow Types)"
        )
        return

    native_value = schema.get("native_type", _MISSING)
    arrow_value = schema.get("arrow_type", _MISSING)
    has_native = native_value is not _MISSING and native_value is not None
    has_arrow = arrow_value is not _MISSING and arrow_value is not None
    if arrow_value is not _MISSING and arrow_value is not None:
        # fullmatch (not match) — `$` in ARROW_TYPE_PATTERN matches before a
        # trailing `\n` under Python re default flags, so `"Utf8\n"` would
        # slip through match() but is correctly rejected by Pydantic's
        # rust-regex field-level pattern. Use fullmatch here for parity.
        if not isinstance(arrow_value, str) or not ARROW_TYPE_RE.fullmatch(arrow_value):
            errors.append(
                f"{path}.arrow_type={arrow_value!r} is not a canonical Arrow "
                "type. Parameterized canonical types must carry their "
                "parameters: e.g. 'Timestamp(MICROSECOND)', "
                "'Decimal128(38, 9)', 'FixedSizeBinary(16)', "
                "'Struct<id:Int64>' (spec: §Native and Arrow Types)"
            )
    if has_native ^ has_arrow:
        missing = "arrow_type" if has_native else "native_type"
        errors.append(
            f"{path} declares only one of native_type/arrow_type; typed field "
            f"schemas must carry both (missing {missing!r}; spec: §Native and "
            "Arrow Types)"
        )

    # Authored-shape JSON container markers (Object/List/Json) require
    # specific sibling keys. `properties` and `items` are standard JSON
    # Schema keywords already meaningful at this node, so we enforce
    # presence/absence inline here rather than constructing a model.
    #
    # Why this is not just `enforce_container_shape(...)`: the walker
    # validates response/input JSON-Schema slots — raw dicts — whereas
    # `analitiq.contracts.shared.arrow_shape.enforce_container_shape` runs after
    # Pydantic has coerced sibling keys into typed `ArrowFieldSpec` /
    # `ColumnFieldSpec` instances. Pydantic's type coercion implicitly
    # rejects JSON Schema 2020-12 shorthands (`items: true|false`,
    # tuple-form `items: [...]`) at the model layer, but the walker has
    # no such coercion and must reject them explicitly. The two paths
    # cover the same matrix but in different dialects; do not collapse
    # them without preserving the dialect-specific rejections below.
    if has_arrow and isinstance(arrow_value, str):
        properties_value = schema.get("properties", _MISSING)
        items_value = schema.get("items", _MISSING)
        # `null` siblings count as "not declared" so error messages are
        # precise rather than recursing into None downstream.
        has_properties = (
            properties_value is not _MISSING and properties_value is not None
        )
        has_items = items_value is not _MISSING and items_value is not None
        if arrow_value == "Object":
            if not has_properties:
                errors.append(
                    f"{path}.arrow_type='Object' requires sibling 'properties' "
                    "(spec: §Native and Arrow Types)"
                )
            elif not isinstance(properties_value, dict) or not properties_value:
                # Empty dict or non-dict shape is structurally meaningless for
                # a declared Object.
                errors.append(
                    f"{path}.arrow_type='Object' requires non-empty "
                    "'properties' map (spec: §Native and Arrow Types)"
                )
            if has_items:
                errors.append(
                    f"{path}.arrow_type='Object' must not carry 'items' "
                    "(spec: §Native and Arrow Types)"
                )
        elif arrow_value == "List":
            if not has_items:
                errors.append(
                    f"{path}.arrow_type='List' requires sibling 'items' "
                    "(spec: §Native and Arrow Types)"
                )
            elif not isinstance(items_value, dict):
                # Reject JSON Schema boolean shorthand (`items: true/false`)
                # and tuple-form (`items: [...]`) — both contradict the
                # single-spec contract that Column / ArrowFieldSpec enforce.
                errors.append(
                    f"{path}.arrow_type='List' requires 'items' to be a "
                    "single field spec (object); boolean and tuple forms "
                    "are not permitted (spec: §Native and Arrow Types)"
                )
            if has_properties:
                errors.append(
                    f"{path}.arrow_type='List' must not carry 'properties' "
                    "(spec: §Native and Arrow Types)"
                )
        elif arrow_value == "Json":
            if has_properties or has_items:
                errors.append(
                    f"{path}.arrow_type='Json' is opaque and must not carry "
                    "'properties' or 'items' (spec: §Native and Arrow Types)"
                )
        else:
            # Scalar or parameterized arrow_type (Utf8, Int64, Struct<…>,
            # List<…>, etc.): JSON-container siblings are not legal. The
            # Pydantic helper rejects this on the model side; the walker
            # must mirror it on the JSON Schema side per spec §Native and
            # Arrow Types ("must not appear on scalar or parameterized
            # arrow_type values").
            if has_properties or has_items:
                errors.append(
                    f"{path}.arrow_type={arrow_value!r} must not carry "
                    "'properties' or 'items'; those are only valid for the "
                    "bare authored-shape markers 'Object' / 'List' "
                    "(spec: §Native and Arrow Types)"
                )

    # Each traversal always re-enters the walker so its entry-point bool/dict
    # check (above) runs on every visited slot — that's the only place
    # malformed schema positions (e.g. `items: "Int64"`) get surfaced.
    for key in _JSON_SCHEMA_SUBSCHEMA_KEYS:
        child = schema.get(key)
        if isinstance(child, dict):
            for sub_key, sub_schema in child.items():
                _validate_arrow_type_in_json_schema(
                    sub_schema, f"{path}.{key}.{sub_key}", errors
                )
    for key in _JSON_SCHEMA_LIST_OF_SCHEMA_KEYS:
        child = schema.get(key)
        if isinstance(child, list):
            for idx, sub_schema in enumerate(child):
                _validate_arrow_type_in_json_schema(
                    sub_schema, f"{path}.{key}[{idx}]", errors
                )
    for key in _JSON_SCHEMA_SINGLE_SCHEMA_KEYS:
        if key not in schema:
            continue
        child = schema[key]
        # Draft 2019-09 tuple-form `items: [...]` is still authored in
        # parts of the catalog; iterate per position. Draft 2020-12 uses
        # `prefixItems` for the same purpose (handled by the list-keyword
        # block above).
        if isinstance(child, list):
            for idx, sub_schema in enumerate(child):
                _validate_arrow_type_in_json_schema(
                    sub_schema, f"{path}.{key}[{idx}]", errors
                )
        else:
            _validate_arrow_type_in_json_schema(child, f"{path}.{key}", errors)


class ResponseExtraction(_EndpointModel):
    """Read operation ``response`` block."""

    # Mirror of `_validate`: `records.ref` is anchored at `response.body[.<path>]`
    # and `metadata` keys obey the shared key rules. The records anchor is a
    # model-level `allOf` (not a sibling of the `records` `$ref`) so it composes
    # portably across draft-07/2020-12 consumers.
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "properties": {
                        "records": {
                            "properties": {"ref": {"pattern": r"^response\.body(?:\..+)?$"}}
                        },
                        "metadata": _METADATA_PROPERTY_NAMES,
                    }
                }
            ]
        }
    )

    records: RefExpression = Field(
        ...,
        description=(
            "Expression that resolves to the iterable record collection. Must "
            "be a `{ref}` whose path starts with `response.body`."
        ),
    )
    schema_: dict[str, Any] = Field(
        ...,
        alias="schema",
        description="JSON Schema Draft 2020-12 document describing the full response body.",
    )
    metadata: dict[str, Expression] | None = Field(  # type: ignore[valid-type]
        default=None,
        description="Optional named metadata extractions; each value is a value expression.",
    )

    @model_validator(mode="after")
    def _validate(self) -> "ResponseExtraction":
        ref = self.records.ref
        if not isinstance(ref, str) or not (ref == "response.body" or ref.startswith("response.body.")):
            raise ValueError(
                "response.records must be `{ref: response.body[.<path>]}` "
                "(spec: §API Response Extraction)"
            )
        if self.metadata is not None:
            for key in self.metadata:
                if not METADATA_KEY_RE.match(key):
                    raise ValueError(
                        f"response.metadata key {key!r} must match {METADATA_KEY_PATTERN!r} "
                        "(spec: §API Response Extraction)"
                    )
                if key in RESERVED_RESPONSE_SCOPES:
                    raise ValueError(
                        f"response.metadata key {key!r} collides with reserved response-scope name "
                        f"{sorted(RESERVED_RESPONSE_SCOPES)!r} (spec: §API Response Extraction)"
                    )
        errors: list[str] = []
        _validate_arrow_type_in_json_schema(self.schema_, "response.schema", errors)
        if errors:
            raise ValueError("; ".join(errors))
        return self


class WriteInput(_EndpointModel):
    """Write mode input shape (one provider-facing destination record)."""

    schema_: dict[str, Any] = Field(
        ...,
        alias="schema",
        description="JSON Schema Draft 2020-12 for one provider-facing destination record.",
    )

    @model_validator(mode="after")
    def _validate_arrow_types(self) -> "WriteInput":
        errors: list[str] = []
        _validate_arrow_type_in_json_schema(self.schema_, "input.schema", errors)
        if errors:
            raise ValueError("; ".join(errors))
        return self


class WriteError(_EndpointModel):
    """Optional provider-declared write error extraction expressions."""

    code: Expression | None = Field(default=None)  # type: ignore[valid-type]
    message: Expression | None = Field(default=None)  # type: ignore[valid-type]
    details: Expression | None = Field(default=None)  # type: ignore[valid-type]


class Batching(_EndpointModel):
    """Batching declaration for a write mode."""

    max_records: int = Field(
        ..., ge=2,
        description="Provider's maximum records per request. Must be ≥ 2.",
    )


class Idempotency(_EndpointModel):
    """Idempotency-key placement declaration for a write mode (#890).

    The author declares only *where* the provider's idempotency key goes on
    the write request. The key *value* is engine-owned (the content-derived
    per-record id) — no value expression, no template, so engine-computed
    values stay out of the request value-expression grammar.
    """

    location: Literal["header", "body"] = Field(
        ...,
        alias="in",
        description=(
            "Where the engine sends the key: `header` — as HTTP request header "
            "`name` (Stripe `Idempotency-Key`, PayPal `PayPal-Request-Id`); "
            "`body` — injected as top-level JSON body field `name` (Square-style "
            "`idempotency_key`). `body` is only valid when the request body is a "
            "JSON object; the engine rejects non-object bodies at configure time."
        ),
    )
    name: str = Field(
        ...,
        min_length=1,
        description="Header name or top-level body field name that carries the key.",
    )


class WriteResponse(_EndpointModel):
    """Optional write-result extraction block."""

    # Mirror of `_metadata_keys`: write `metadata` follows the same key rules as
    # the read side (`§API Write Response Contract` defers to
    # `§API Response Extraction`). Same fragment as `ResponseExtraction.metadata`.
    model_config = ConfigDict(
        json_schema_extra={"allOf": [{"properties": {"metadata": _METADATA_PROPERTY_NAMES}}]}
    )

    success_when: Predicate | None = Field(default=None)  # type: ignore[valid-type]
    error: WriteError | None = Field(
        default=None,
        description="Optional `code`/`message`/`details` value expressions for failure parsing.",
    )
    affected_records: Expression | None = Field(default=None)  # type: ignore[valid-type]
    generated_keys: Expression | None = Field(default=None)  # type: ignore[valid-type]
    metadata: dict[str, Expression] | None = Field(default=None)  # type: ignore[valid-type]

    @model_validator(mode="after")
    def _metadata_keys(self) -> "WriteResponse":
        # Spec §API Write Response Contract delegates metadata key rules to
        # the read-side §API Response Extraction rules; same patterns apply.
        if self.metadata is None:
            return self
        for key in self.metadata:
            if not METADATA_KEY_RE.match(key):
                raise ValueError(
                    f"response.metadata key {key!r} must match {METADATA_KEY_PATTERN!r} "
                    "(spec: §API Write Response Contract — follows §API Response Extraction)"
                )
            if key in RESERVED_RESPONSE_SCOPES:
                raise ValueError(
                    f"response.metadata key {key!r} collides with reserved response-scope name "
                    "(spec: §API Write Response Contract — follows §API Response Extraction)"
                )
        return self

    @model_validator(mode="after")
    def _reject_record_count(self) -> "WriteResponse":
        # `response.record_count` is available only for read operations
        # (§API Write Response Contract); write-response expressions must not
        # reference it. `iter_expression_strings` (the shared resolver grammar)
        # reaches the `Any`-typed `success_when` operands and function inputs a
        # typed walk would miss — including bare-string templates — while skipping
        # `literal` subtrees (a `{"literal": {...}}` payload is protected data,
        # not an executable ref). Tokens are stripped like the resolver.
        def _is_record_count(token: str) -> bool:
            t = token.strip()
            return t == "response.record_count" or t.startswith("response.record_count.")

        for kind, s in iter_expression_strings(self.model_dump(by_alias=True)):
            hits = [s] if kind == "ref" else template_placeholders(s)
            if any(_is_record_count(h) for h in hits):
                raise ValueError(
                    "write-response expressions must not reference "
                    "`response.record_count` (read-only response scope; spec: "
                    "§API Write Response Contract)"
                )
        return self


class ReadOperation(_EndpointModel):
    """Read operation block."""

    # Mirror of `_wiring`'s GET check: a GET read must not declare any param with
    # `in: "body"`. Cross-block (`request.method` vs `params.*.in`), so it reads
    # the discriminated request's `method` const; the T1 request-union split
    # cannot reach across to the sibling `params` map.
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "required": ["request"],
                        "properties": {
                            "request": {
                                "required": ["method"],
                                "properties": {"method": {"const": "GET"}},
                            }
                        },
                    },
                    "then": {
                        "properties": {
                            "params": {
                                "additionalProperties": {
                                    "properties": {"in": {"not": {"const": "body"}}}
                                }
                            }
                        }
                    },
                }
            ]
        }
    )

    request: ReadRequest = Field(...)
    params: dict[str, Param] = Field(default_factory=dict)
    response: ResponseExtraction = Field(...)
    pagination: Pagination | None = Field(default=None)  # type: ignore[type-arg]
    replication: Replication | None = Field(default=None)

    @model_validator(mode="after")
    def _wiring(self) -> "ReadOperation":
        _validate_param_wiring(self.request, self.params, allow_from_input=False)
        _validate_param_binding_uniqueness(self.request, self.params)

        if self.request.method == "GET":
            for name, param in self.params.items():
                if param.location == "body":
                    raise ValueError(
                        f"read GET operation must not declare params with in='body' "
                        f"(found {name!r}; spec: §Request Bodies)"
                    )

        if self.pagination is not None:
            _validate_pagination_wiring(self.pagination, self.params)

        if self.replication is not None:
            _validate_replication_wiring(self.replication, self.params)

        # response.records → response.schema traversal raises directly.
        # When replication is declared, the same traversal feeds cursor-field
        # validation (avoiding a second walk of the same JSON Schema).
        records_array_node = _validate_records_in_response_schema(self.response)
        if self.replication is not None:
            _validate_cursor_fields_in_record_shape(self.replication, records_array_node)

        return self


def _json_schema_top_level_fields(schema: dict[str, Any]) -> set[str] | None:
    """Top-level object field names declared by a JSON Schema record shape.

    The names a write mode's `conflict_keys` can target. Returns `None` when the
    schema declares no object `properties` map (e.g. a `$ref` record) —
    "unknowable, skip the check" — distinct from an explicit empty `properties:
    {}`, which returns an empty set ("zero declared fields", so any conflict key
    is invalid).
    """
    props = schema.get("properties")
    return set(props) if isinstance(props, dict) else None


def _walk_input_schema_path(schema: dict[str, Any], from_input: str) -> dict[str, Any] | None:
    """Resolve the ``input.schema`` subschema a write-body `from_input` addresses.

    ``record`` → the schema itself; ``record.<a>.<b>`` → a ``properties`` walk.
    Returns ``None`` when the expression is not record-addressed or a segment
    is not declared — statically unknowable, per the contract's
    unknowable→skip convention (the engine owns the resolved shape at
    configure time).
    """
    if from_input == "record":
        return schema
    if not from_input.startswith("record."):
        return None
    node = schema
    for seg in from_input.removeprefix("record.").split("."):
        props = node.get("properties")
        if not isinstance(props, dict) or not isinstance(props.get(seg), dict):
            return None
        node = props[seg]
    return node


def _undeclared_from_input_field(schema: dict[str, Any], from_input: str) -> str | None:
    """The first ``record.<dotted>`` segment the declared ``input.schema``
    provably does not contain, or ``None`` when the path is not checkable.

    Walks ``properties`` maps segment by segment. A segment is a violation only
    when its parent declares an object ``properties`` map that omits it — a
    genuinely absent field. A parent that declares no ``properties`` map (a bare
    ``$ref``, an unconstrained object) is unknowable, so the walk stops and the
    path is accepted, per the contract's unknowable→skip convention (the engine
    owns the resolved shape at configure time). Whole-``record`` and ``records``
    expressions carry no field path and are always ``None``.
    """
    if not from_input.startswith("record."):
        return None
    node = schema
    walked = "record"
    for seg in from_input.removeprefix("record.").split("."):
        if not isinstance(node, dict):
            return None  # a non-object subschema (e.g. a boolean) is not walkable
        fields = _json_schema_top_level_fields(node)
        if fields is None:
            return None
        if seg not in fields:
            return f"{walked}.{seg}"
        node = node["properties"][seg]
        walked = f"{walked}.{seg}"
    return None


def is_valid_conflict_keys(value: Any) -> bool:
    """A raw ``conflict_keys`` value is well-formed iff it is a non-empty list of
    non-empty strings — the shape the Pydantic field enforces on parsed
    documents. The untyped read (discovery) and ingest (DIP webhook)
    paths never run the model, so they share this predicate rather than each
    re-spelling the rule (define once)."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(k, str) and k for k in value)
    )


class WriteOperation(_EndpointModel):
    """One write-mode block (insert or upsert)."""

    model_config = ConfigDict(
        json_schema_extra={
            # Published-schema mirror of the `_wiring` idempotency×batching rule
            # (#890): the key value is per-record, so a multi-record request
            # cannot carry one. `anyOf` over null-or-absent — not
            # `not: {required: [...]}` — so an explicit null (either field's
            # nullable default) still authors, matching the model's is-None check.
            "anyOf": [
                {"properties": {"idempotency": {"type": "null"}}},
                {"properties": {"batching": {"type": "null"}}},
            ],
            # Published-schema mirror of the `_wiring` body-placement guard:
            # `idempotency.in: "body"` needs an object request body to inject
            # into, so a literal non-object `request.body` template is
            # unauthorable. Template-level only — an expression body
            # (`{"from_input": ...}`) is an object template whose resolved
            # shape JSON Schema cannot see; the model's static resolution and
            # the engine's configure gate own those cases. A sibling `allOf`
            # (not `if`/`then`, and not folded into the anyOf above, which
            # would loosen it): the guard binds only documents using the new
            # 9.1.0 field, so it is semantically additive, and this is the
            # conjunction form the version classifier also reads as additive.
            "allOf": [
                {
                    "anyOf": [
                        {"properties": {"idempotency": {"anyOf": [
                            {"type": "null"},
                            {"properties": {"in": {"const": "header"}}},
                        ]}}},
                        {"properties": {"request": {
                            "properties": {"body": {"type": "object"}},
                            "required": ["body"],
                        }}},
                    ],
                },
            ],
        },
    )

    request: WriteRequest = Field(...)
    params: dict[str, Param] = Field(default_factory=dict)
    input: WriteInput = Field(...)
    conflict_keys: list[Annotated[str, Field(min_length=1)]] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Upsert conflict-target fields — the provider-defined natural key "
            "the upsert matches on (e.g. Salesforce `ExternalIdField`, HubSpot "
            "`idProperty`, Airtable `fieldsToMergeOn`, Elasticsearch `_id`, "
            "Algolia `objectID`). Each entry is a top-level field name in "
            "`input.schema`. A single composite key set — every listed field "
            "participates. Required on the `upsert` write mode; forbidden on "
            "`insert` (enforced by `operations`, which knows the mode key)."
        ),
    )
    batching: Batching | None = Field(default=None)
    idempotency: Idempotency | None = Field(
        default=None,
        description=(
            "Provider idempotency-key placement (e.g. Stripe `Idempotency-Key` "
            "header, Square `idempotency_key` body field). Allowed on both "
            "`insert` and `upsert`, required on neither — some providers require "
            "the key even on upsert (Square `UpsertCatalogObject`). Forbidden "
            "together with `batching`: the "
            "key value is per-record, and a resumed cursor re-batches different "
            "row compositions, so a multi-record request cannot carry a key "
            "that survives an engine restart."
        ),
    )
    response: WriteResponse | None = Field(default=None)

    @model_validator(mode="after")
    def _wiring(self) -> "WriteOperation":
        _validate_param_wiring(self.request, self.params, allow_from_input=True)
        _validate_param_binding_uniqueness(self.request, self.params)

        if self.conflict_keys is not None:
            known = _json_schema_top_level_fields(self.input.schema_)
            # Enforce membership whenever the input schema declares an object
            # `properties` map — including an explicit empty one (`properties:
            # {}` means zero fields, so any conflict key is invalid). Only a
            # record that models its fields some other way (a bare `$ref`, no
            # `properties` map → `None`) is unknowable and skipped.
            if known is not None:
                unknown = sorted(set(self.conflict_keys) - known)
                if unknown:
                    raise ValueError(
                        f"conflict_keys reference unknown input.schema fields {unknown!r} "
                        "(spec: §Cross-Field Validation)"
                    )

        if self.idempotency is not None:
            if self.batching is not None:
                raise ValueError(
                    "idempotency cannot be combined with batching — the key value is "
                    "per-record and a multi-record request cannot carry one "
                    "(spec: §Write Modes)"
                )
            if self.idempotency.location == "header":
                declared = {h.lower() for h in (self.request.headers or {})}
                if self.idempotency.name.lower() in declared:
                    raise ValueError(
                        f"idempotency header {self.idempotency.name!r} is also declared "
                        "in request.headers — the key value is engine-owned, so the "
                        "header must not carry an authored value "
                        "(spec: §Cross-Field Validation)"
                    )
            else:  # location == "body"
                # Only the statically-provable cases are rejected here; where
                # the resolved shape is unknowable, the engine rejects a
                # resolved non-object body at configure time.
                body = self.request.body
                if not isinstance(body, dict):
                    raise ValueError(
                        "idempotency.in='body' requires the write request body "
                        "template to be a JSON object — the engine injects the key "
                        "as a top-level body field (spec: §Cross-Field Validation)"
                    )
                if _matches_singleton(body, "from_input"):
                    # Expression body: the request body IS the addressed record
                    # (or record field). Apply the same two rules to the
                    # input.schema shape it resolves to, when declared.
                    node = _walk_input_schema_path(self.input.schema_, body["from_input"])
                    declared_type = node.get("type") if node is not None else None
                    if declared_type is not None and "object" not in (
                        declared_type if isinstance(declared_type, list) else [declared_type]
                    ):
                        raise ValueError(
                            "idempotency.in='body' requires the write request body to "
                            f"resolve to a JSON object — `from_input: {body['from_input']!r}` "
                            f"resolves to input.schema type {declared_type!r} "
                            "(spec: §Cross-Field Validation)"
                        )
                    fields = _json_schema_top_level_fields(node) if node is not None else None
                    if fields is not None and self.idempotency.name in fields:
                        raise ValueError(
                            f"idempotency body field {self.idempotency.name!r} is also a "
                            "declared field of the record the body resolves to — the key "
                            "value is engine-owned, so the field must not carry an "
                            "authored value (spec: §Cross-Field Validation)"
                        )
                elif self.idempotency.name in body:
                    raise ValueError(
                        f"idempotency body field {self.idempotency.name!r} is also a "
                        "top-level key of the request body template — the key value "
                        "is engine-owned, so the field must not carry an authored "
                        "value (spec: §Cross-Field Validation)"
                    )

        from_inputs = _collect_singleton_values(self.request.body, "from_input")
        if self.batching is None:
            if not from_inputs:
                raise ValueError(
                    "non-batched write request body must reference `from_input: 'record'` "
                    "or `record.<field>` (spec: §Cross-Field Validation)"
                )
            for fi in from_inputs:
                if fi == "records":
                    raise ValueError(
                        "non-batched write must not use `from_input: 'records'` "
                        "(spec: §Cross-Field Validation)"
                    )
        else:
            if not from_inputs:
                raise ValueError(
                    "batched write request body must reference `from_input: 'records'` "
                    "(spec: §Cross-Field Validation)"
                )
            for fi in from_inputs:
                if fi == "record" or fi.startswith("record."):
                    raise ValueError(
                        "batched write must not use `from_input: 'record'` or `record.<field>` "
                        "(spec: §Cross-Field Validation)"
                    )

        # A `record.<field>` path must address a field the declared `input.schema`
        # actually contains, where the shape is knowable — the same membership
        # rule `conflict_keys` enforces. Batched writes never reach here with a
        # `record.<field>` path (rejected above), so this only bites the
        # non-batched per-field placement form.
        for fi in from_inputs:
            missing = _undeclared_from_input_field(self.input.schema_, fi)
            if missing is not None:
                raise ValueError(
                    f"from_input {fi!r} references undeclared input.schema field "
                    f"{missing!r} (spec: §Cross-Field Validation)"
                )
        return self


class Operations(_EndpointModel):
    """``operations`` block for API endpoints. At least one of ``read``/``write`` is required."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
        json_schema_extra={
            "additionalProperties": False,
            "anyOf": [{"required": ["read"]}, {"required": ["write"]}],
        },
    )

    read: ReadOperation | None = Field(default=None)
    write: dict[WriteMode, WriteOperation] | None = Field(
        default=None,
        json_schema_extra={"minProperties": 1},
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> "Operations":
        if self.read is None and not self.write:
            raise ValueError(
                "operations must declare at least one of `read` or `write` "
                "(spec: §API Endpoint Shape)"
            )
        if self.write is not None and len(self.write) == 0:
            raise ValueError(
                "operations.write must contain at least one mode when present "
                "(spec: §API Endpoint Shape)"
            )
        return self

    @model_validator(mode="after")
    def _conflict_keys_by_mode(self) -> "Operations":
        # The conflict key lives on the endpoint because it is provider-defined,
        # but whether it is required is a property of the *mode* — and the mode
        # is the dict key, visible only here. `upsert` must declare it (there is
        # nothing to match on otherwise); every other mode must not (the concept
        # does not apply). Spec: §Write Modes.
        for mode, op in (self.write or {}).items():
            if mode == "upsert":
                if not op.conflict_keys:
                    raise ValueError(
                        "operations.write.upsert.conflict_keys is required — an "
                        "upsert must declare the provider's conflict target(s) "
                        "(spec: §Write Modes)"
                    )
            elif op.conflict_keys:
                raise ValueError(
                    f"operations.write.{mode}.conflict_keys is not allowed — "
                    "conflict_keys applies only to the upsert write mode "
                    "(spec: §Write Modes)"
                )
        return self


# ---------------------------------------------------------------------------
# Endpoint root models
# ---------------------------------------------------------------------------


class _EndpointBase(_EndpointModel):
    """Shared identity and metadata fields. Spec: §Top-Level Fields, §Shared Metadata."""

    endpoint_id: str = Field(
        ...,
        min_length=1,
        pattern=SLUG_PATTERN,
        description=(
            "Stable endpoint identifier within the owner. "
            "Matches `^[a-z0-9][a-z0-9_-]*$`."
        ),
    )
    display_name: str | None = Field(
        default=None,
        min_length=DISPLAY_NAME_MIN,
        max_length=DISPLAY_NAME_MAX,
        pattern=NO_EDGE_WHITESPACE_PATTERN,
        description="User-facing label (1-120 chars trimmed).",
    )
    description: str | None = Field(
        default=None,
        max_length=DESCRIPTION_MAX,
        description="User-facing summary (≤2000 chars).",
    )
    tags: list[TrimmedTag] | None = Field(
        default=None,
        max_length=TAGS_MAX,
        json_schema_extra={"uniqueItems": True},
        description="Grouping/search labels (max 50, each 1-64 chars, unique, trimmed).",
    )

    @field_validator("display_name")
    @classmethod
    def _validate_display_name_field(cls, v: str | None) -> str | None:
        return validate_display_name(v)

    @field_validator("tags")
    @classmethod
    def _validate_tags_field(cls, v: list[str] | None) -> list[str] | None:
        return validate_tags(v)


class ApiEndpointDoc(_EndpointBase):
    """API endpoint schema document."""

    schema_url: Literal[API_ENDPOINT_SCHEMA_URL] = Field(
        ...,
        alias="$schema",
        description=(
            "Per-kind schema URL declared by every persisted API endpoint "
            "document. Per spec §Schema URLs."
        ),
    )
    operations: Operations = Field(...)


class DatabaseObject(_EndpointModel):
    """Provider-native database object identity.

    Identifier strings are stored verbatim from introspection — no
    case-folding, quoting, or normalization.
    """

    catalog: str | None = Field(default=None, min_length=1)
    schema_: str | None = Field(default=None, alias="schema", min_length=1)
    name: str = Field(..., min_length=1, description="Provider-native object name.")
    object_type: str | None = Field(
        default=None,
        description=(
            "Open-string descriptive type (table, view, materialized_view, "
            "external_table, collection, …). Read execution must not branch on it."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null_namespaces(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ("catalog", "schema"):
                if key in data and data[key] is None:
                    raise ValueError(
                        f"database_object.{key} must be omitted when not applicable; "
                        "explicit null is invalid (spec: §Database Endpoint Shape)"
                    )
        return data


# The authored-shape container matrix — Object ⇒ non-empty `properties`, no
# `items`; List ⇒ `items`, no `properties`; anything else ⇒ neither. The shared
# `ARROW_CONTAINER_SCHEMA_RULES` (defined next to the runtime
# `enforce_container_shape` helper) is the declarative mirror, reused verbatim by
# the stream `ArrowFieldSpec`/`AssignmentTarget` classes (and its `allOf`
# branches by stream `ConstantValue`) so the two contracts cannot drift.
# `test_column_container_matrix` guards the fragment and the runtime validator.


class ColumnFieldSpec(_EndpointModel):
    """Recursive child field-shape declaration for declared-shape JSON
    containers under database columns.
    """

    model_config = ConfigDict(json_schema_extra=ARROW_CONTAINER_SCHEMA_RULES)

    arrow_type: str = Field(
        ...,
        pattern=ARROW_TYPE_PATTERN,
        description=(
            "Apache Arrow canonical transport type string. Bare authored-shape "
            "markers 'Object', 'List', and 'Json' declare JSON containers."
        ),
    )
    nullable: bool | None = Field(default=None)
    properties: dict[str, "ColumnFieldSpec"] | None = Field(default=None)
    items: ColumnFieldSpec | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_container_shape(self) -> "ColumnFieldSpec":
        enforce_container_shape(self.arrow_type, self.properties, self.items)
        return self


class Column(_EndpointModel):
    """Database column metadata."""

    model_config = ConfigDict(json_schema_extra=ARROW_CONTAINER_SCHEMA_RULES)

    name: str = Field(..., min_length=1)
    native_type: str = Field(
        ...,
        min_length=1,
        description="Provider-native database type label. Use 'unknown' when unavailable.",
    )
    arrow_type: str = Field(
        ...,
        pattern=ARROW_TYPE_PATTERN,
        description=(
            "Apache Arrow canonical transport type string. PascalCase base name "
            "plus parameters when the canonical type requires them — bare "
            "parameterized forms such as 'Timestamp', 'Decimal128', or 'Struct' "
            "are rejected. Examples: 'Utf8', 'Int64', 'Timestamp(MICROSECOND)' "
            "(zone-naive), 'Timestamp(MICROSECOND, UTC)' (zoned source; prefer "
            "UTC unless source-specific), 'Decimal128(38, 9)', "
            "'FixedSizeBinary(16)', 'Struct<id:Int64, name:Utf8>'. Bare markers "
            "'Object' / 'List' / 'Json' declare JSON containers; see spec "
            "§Native and Arrow Types."
        ),
    )
    nullable: bool | None = Field(default=None)
    default: Any | None = Field(default=None)
    comment: str | None = Field(default=None)
    ordinal_position: int | None = Field(default=None, ge=1)
    # `properties` here is a field-spec map (recursive ColumnFieldSpec),
    # distinct from JSON Schema `properties` blocks used by API endpoints.
    # Both are enforced by `enforce_container_shape` via the validator below.
    properties: dict[str, ColumnFieldSpec] | None = Field(default=None)
    items: ColumnFieldSpec | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_container_shape(self) -> "Column":
        enforce_container_shape(self.arrow_type, self.properties, self.items)
        return self


class DatabaseEndpointDoc(AdvisoryValidated, _EndpointBase):
    """Database endpoint schema document."""

    schema_url: Literal[DATABASE_ENDPOINT_SCHEMA_URL] = Field(
        ...,
        alias="$schema",
        description=(
            "Per-kind schema URL declared by every persisted database endpoint "
            "document. Per spec §Schema URLs."
        ),
    )
    database_object: DatabaseObject = Field(...)
    columns: list[Column] = Field(..., min_length=1)
    primary_keys: list[str] | None = Field(default=None, min_length=1)


def parse_endpoint(payload: Any) -> "ApiEndpointDoc | DatabaseEndpointDoc":
    """Dispatch an endpoint payload to the kind-specific validator.

    Endpoint documents carry no top-level ``kind``; the owning connector
    determines kind at runtime. This helper picks the right Pydantic model
    using (1) the ``$schema`` URL when present, then (2) a structural fall-
    back on the presence of database-only fields (``database_object`` /
    ``columns``). API is the default when neither hint is present.

    Raises:
        TypeError: payload is not a dict.
        ValueError: payload carries a ``$schema`` URL that is neither the
            api-endpoint nor the database-endpoint URL. Routing on an
            unrecognized schema would silently dispatch to ApiEndpointDoc and
            surface a misleading "extra field" error against the wrong model.
    """
    if not isinstance(payload, dict):
        raise TypeError(
            f"endpoint payload must be a dict, got {type(payload).__name__}"
        )
    schema = payload.get("$schema")
    if schema == DATABASE_ENDPOINT_SCHEMA_URL:
        return DatabaseEndpointDoc.model_validate(payload)
    if schema == API_ENDPOINT_SCHEMA_URL:
        return ApiEndpointDoc.model_validate(payload)
    if schema is not None:
        raise ValueError(
            f"unknown $schema {schema!r}; expected "
            f"{API_ENDPOINT_SCHEMA_URL!r} or {DATABASE_ENDPOINT_SCHEMA_URL!r}"
        )
    if "database_object" in payload or "columns" in payload:
        return DatabaseEndpointDoc.model_validate(payload)
    return ApiEndpointDoc.model_validate(payload)


# ---------------------------------------------------------------------------
# Internal validators
# ---------------------------------------------------------------------------


_DECLARED_FIELDS_CACHE: dict[type, frozenset[str]] = {}


def _declared_field_names(cls: type) -> frozenset[str]:
    """Per-class set of declared field names + aliases. Cached.

    Used by the ``_reject_non_x_extras`` mode-``before`` validator on every
    model construction; caching keeps the hot path cheap.
    """
    cached = _DECLARED_FIELDS_CACHE.get(cls)
    if cached is not None:
        return cached
    names: set[str] = set()
    for name, info in cls.model_fields.items():
        names.add(name)
        if info.alias:
            names.add(info.alias)
    frozen = frozenset(names)
    _DECLARED_FIELDS_CACHE[cls] = frozen
    return frozen


_BINDING_KEYS: frozenset[str] = frozenset({"from_param", "from_input"})
_VALUE_EXPRESSION_KEYS: frozenset[str] = frozenset(_EXPRESSION_KEYS)
_ALL_EXPRESSION_KEYS: frozenset[str] = _BINDING_KEYS | _VALUE_EXPRESSION_KEYS

# `function` expressions are *not* singletons — per the `FunctionExpression` model,
# they declare a `function` name plus optional argument fields. The shape validator at `_validate_expression_shapes`
# permits exactly the field set declared on `FunctionExpression` itself, so
# extending the model (e.g. adding a future `jwt_sign`-specific field) only
# touches one place — the validator follows automatically.
_FUNCTION_EXPRESSION_FIELDS: frozenset[str] = frozenset(FunctionExpression.model_fields.keys())


def _validate_expression_shapes(value: Any, where: str) -> None:
    """Walk ``value``; raise when a dict has expression-like keys but is structurally malformed.

    Spec §Extension Policy + §Value Expressions: an expression dict declares
    exactly one of ``ref``/``template``/``literal``/``function``/``from_param``/
    ``from_input``. ``ref``/``template``/``literal``/``from_param``/``from_input``
    are singleton-shaped — only the named key plus ``x-*`` siblings are
    permitted. ``function`` carries documented argument fields
    (``input``/``map``/``safe``) plus ``x-*``.

    Surfacing malformed expression dicts here gives the author a precise
    pointer to the bad fragment instead of a downstream "param not
    referenced" error from the param-binding walk that runs after.
    """
    if isinstance(value, dict):
        present_expr_keys = [k for k in value if k in _ALL_EXPRESSION_KEYS]
        if present_expr_keys:
            if len(present_expr_keys) > 1:
                raise ValueError(
                    f"{where}: expression dict declares multiple expression keys "
                    f"{sorted(present_expr_keys)!r}; spec requires exactly one "
                    "(spec: §Value Expressions)"
                )
            primary = present_expr_keys[0]
            allowed_siblings = (
                _FUNCTION_EXPRESSION_FIELDS if primary == "function" else {primary}
            )
            non_x_others = sorted(
                k for k in value
                if k not in allowed_siblings and not (
                    isinstance(k, str) and k.startswith("x-")
                )
            )
            if non_x_others:
                raise ValueError(
                    f"{where}: {primary!r} expression has unexpected siblings "
                    f"{non_x_others!r}; expressions must be the documented shape "
                    "(spec: §Value Expressions)"
                )
            # Recurse into the expression's argument(s). For `function`, this
            # walks `input` (which may itself be an expression) and `map`'s
            # values; for `template`/`ref`/`literal` the inner is leaf data.
            for v_inner in value.values():
                _validate_expression_shapes(v_inner, f"{where}.<{primary}>")
            return
        for k, v_inner in value.items():
            _validate_expression_shapes(v_inner, f"{where}.{k}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _validate_expression_shapes(item, f"{where}[{i}]")


def _matches_singleton(value: Any, key: str) -> bool:
    """True when ``value`` is a ``{key: <str>}`` dict, with optional ``x-*`` siblings.

    Tolerating ``x-*`` siblings (spec §Extension Policy) is required so an
    extension key on a binding expression does not hide it from the
    dangling-param walk that runs against the param-wiring.
    """
    if not isinstance(value, dict):
        return False
    if key not in value or not isinstance(value[key], str):
        return False
    for k in value:
        if k == key:
            continue
        if isinstance(k, str) and k.startswith("x-"):
            continue
        return False
    return True


def _collect_singleton_values(value: Any, key: str) -> list[str]:
    """Walk ``value``; return every string from a ``{key: str}`` singleton dict (x-* tolerant)."""
    found: list[str] = []
    if isinstance(value, dict):
        if _matches_singleton(value, key):
            found.append(value[key])
            return found
        for v in value.values():
            found.extend(_collect_singleton_values(v, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_singleton_values(item, key))
    return found


def _has_disallowed_dynamic_refs(value: Any) -> str | None:
    """Return the first ``stream.*``/``state.*``/``runtime.*`` ref encountered, or ``None``."""
    disallowed_prefixes = ("stream.", "state.", "runtime.")
    if isinstance(value, dict):
        if _matches_singleton(value, "ref"):
            ref = value["ref"]
            for prefix in disallowed_prefixes:
                if ref == prefix.rstrip(".") or ref.startswith(prefix):
                    return ref
            return None
        for v in value.values():
            r = _has_disallowed_dynamic_refs(v)
            if r is not None:
                return r
    elif isinstance(value, list):
        for item in value:
            r = _has_disallowed_dynamic_refs(item)
            if r is not None:
                return r
    return None


def _first_unscoped_expression(value: Any) -> str | None:
    """Return the first ref or `${...}` template placeholder whose leading token
    is not a known resolution scope, or ``None``. Complements ``RefExpression``'s
    published ``pattern`` (typed nodes) by reaching refs *and* templates buried in
    ``Any``-typed request slots, parsed via the shared resolver grammar
    (``iter_expression_strings`` skips protected ``literal`` subtrees)."""
    for kind, s in iter_expression_strings(value):
        tokens = [s] if kind == "ref" else template_placeholders(s)
        for token in tokens:
            if not _has_known_scope(token):
                return token
    return None


def _validate_param_wiring(
    request: _RequestBase,
    params: dict[str, Param],
    *,
    allow_from_input: bool,
) -> None:
    """Validate from_param/from_input usage and request-binding location rules."""
    # Reject malformed expression dicts (e.g. `{from_param: "x", "rogue": 1}`)
    # at their actual location before the per-binding walks. Without this,
    # the singleton check would fall through to recursion and the user would
    # only see a misleading "param not referenced" error rooted at the
    # param-binding-uniqueness validator — pointing at the wrong failure site.
    _validate_expression_shapes(request.path_params, "request.path_params")
    _validate_expression_shapes(request.headers, "request.headers")
    _validate_expression_shapes(request.query, "request.query")
    _validate_expression_shapes(getattr(request, "body", None), "request.body")

    for where, value in (
        ("request.path_params", request.path_params),
        ("request.headers", request.headers),
        ("request.query", request.query),
    ):
        if _collect_singleton_values(value, "from_input"):
            raise ValueError(
                f"from_input is invalid in {where}; it is allowed only in "
                "operations.write.<mode>.request.body (spec: §Cross-Field Validation)"
            )

    for placeholder, expr in (request.path_params or {}).items():
        names = _collect_singleton_values(expr, "from_param")
        if not names:
            raise ValueError(
                f"request.path_params[{placeholder!r}] must be a `{{from_param: <name>}}` expression "
                "(spec: §Request Parameter Binding)"
            )
        for name in names:
            param = params.get(name)
            if param is None:
                raise ValueError(
                    f"request.path_params[{placeholder!r}] references unknown param {name!r} "
                    "(spec: §Cross-Field Validation)"
                )
            if param.location != "path":
                raise ValueError(
                    f"request.path_params[{placeholder!r}] binds to param {name!r} which has "
                    f"in={param.location!r}; expected in='path' (spec: §Parameter Validation and Operators)"
                )

    for header_name, value in (request.headers or {}).items():
        for name in _collect_singleton_values(value, "from_param"):
            param = params.get(name)
            if param is None:
                raise ValueError(
                    f"request.headers[{header_name!r}] references unknown param {name!r}"
                )
            if param.location != "header":
                raise ValueError(
                    f"request.headers[{header_name!r}] binds to param {name!r} with "
                    f"in={param.location!r}; expected in='header'"
                )
        if _has_disallowed_dynamic_refs(value) is not None:
            raise ValueError(
                f"request.headers[{header_name!r}] uses a direct stream/state/runtime ref; "
                "route dynamic values through declared params (spec: §Request Parameter Binding)"
            )
        bad_scope = _first_unscoped_expression(value)
        if bad_scope is not None:
            raise ValueError(
                f"request.headers[{header_name!r}] uses {bad_scope!r} (a ref or template placeholder) whose leading token is "
                f"not a known resolution scope ({', '.join(RESOLUTION_SCOPES)}) (spec: §Value Expressions)"
            )

    for q_name, value in (request.query or {}).items():
        for name in _collect_singleton_values(value, "from_param"):
            param = params.get(name)
            if param is None:
                raise ValueError(
                    f"request.query[{q_name!r}] references unknown param {name!r}"
                )
            if param.location != "query":
                raise ValueError(
                    f"request.query[{q_name!r}] binds to param {name!r} with "
                    f"in={param.location!r}; expected in='query'"
                )
        if _has_disallowed_dynamic_refs(value) is not None:
            raise ValueError(
                f"request.query[{q_name!r}] uses a direct stream/state/runtime ref; "
                "route dynamic values through declared params"
            )
        bad_scope = _first_unscoped_expression(value)
        if bad_scope is not None:
            raise ValueError(
                f"request.query[{q_name!r}] uses {bad_scope!r} (a ref or template placeholder) whose leading token is "
                f"not a known resolution scope ({', '.join(RESOLUTION_SCOPES)}) (spec: §Value Expressions)"
            )

    body = getattr(request, "body", None)
    if body is not None:
        for name in _collect_singleton_values(body, "from_param"):
            param = params.get(name)
            if param is None:
                raise ValueError(
                    f"request.body references unknown param {name!r}"
                )
            if param.location != "body":
                raise ValueError(
                    f"request.body binds to param {name!r} with in={param.location!r}; expected in='body'"
                )
        from_inputs = _collect_singleton_values(body, "from_input")
        if not allow_from_input and from_inputs:
            raise ValueError(
                "from_input is allowed only in write request bodies "
                "(spec: §Cross-Field Validation)"
            )
        # Disjoint cases: 'record', 'records', or 'record.<dotted>'. Anything
        # else — including 'records.<dotted>' (dotted paths through batch
        # arrays) — is invalid in v1.
        for fi in from_inputs:
            if fi in ("record", "records"):
                continue
            if fi.startswith("record.") and not fi.startswith("records."):
                continue
            if fi.startswith("records."):
                raise ValueError(
                    f"from_input value {fi!r} is invalid; dotted paths through batch arrays "
                    "are unsupported in v1 (spec: §Cross-Field Validation)"
                )
            raise ValueError(
                f"from_input value {fi!r} must be 'record', 'records', or 'record.<dotted>' "
                "(spec: §Cross-Field Validation)"
            )
        if _has_disallowed_dynamic_refs(body) is not None:
            raise ValueError(
                "request.body uses a direct stream/state/runtime ref; "
                "route dynamic values through declared params"
            )
        bad_scope = _first_unscoped_expression(body)
        if bad_scope is not None:
            raise ValueError(
                f"request.body uses {bad_scope!r} (a ref or template placeholder) whose leading token is not a known "
                f"resolution scope ({', '.join(RESOLUTION_SCOPES)}) (spec: §Value Expressions)"
            )


def _validate_pagination_wiring(pagination: Any, params: dict[str, Param]) -> None:
    """Validate pagination param references and ``controlled_by`` markers."""
    referenced: list[str] = []
    if isinstance(pagination, OffsetPagination):
        referenced.append(pagination.offset.param)
        if pagination.limit and pagination.limit.param:
            referenced.append(pagination.limit.param)
    elif isinstance(pagination, PagePagination):
        referenced.append(pagination.page.param)
        if pagination.limit and pagination.limit.param:
            referenced.append(pagination.limit.param)
    elif isinstance(pagination, CursorPagination):
        referenced.append(pagination.cursor.param)
        if pagination.limit and pagination.limit.param:
            referenced.append(pagination.limit.param)
    elif isinstance(pagination, KeysetPagination):
        referenced.append(pagination.keyset.param)
        if pagination.limit and pagination.limit.param:
            referenced.append(pagination.limit.param)
    # LinkPagination declares no request-binding params (spec: §Pagination
    # Strategies — link replaces the entire URL, no params traverse).

    for name in referenced:
        param = params.get(name)
        if param is None:
            raise ValueError(
                f"pagination references unknown param {name!r} (spec: §Cross-Field Validation)"
            )
        if param.controlled_by != "pagination":
            raise ValueError(
                f"param {name!r} is referenced by pagination but does not declare "
                "controlled_by='pagination' (spec: §Cross-Field Validation)"
            )


def _validate_replication_wiring(replication: Replication, params: dict[str, Param]) -> None:
    """Validate replication param references and ``controlled_by`` markers."""
    referenced: list[str] = []
    for cm in replication.cursor_mappings:
        if isinstance(cm, SingleCursorMapping):
            referenced.append(cm.param)
        elif isinstance(cm, WindowCursorMapping):
            referenced.append(cm.start_param)
            referenced.append(cm.end_param)
    for name in referenced:
        param = params.get(name)
        if param is None:
            raise ValueError(
                f"replication references unknown param {name!r} (spec: §Cross-Field Validation)"
            )
        if param.controlled_by != "replication":
            raise ValueError(
                f"param {name!r} is referenced by replication but does not declare "
                "controlled_by='replication' (spec: §Cross-Field Validation)"
            )


def _validate_param_binding_uniqueness(
    request: _RequestBase, params: dict[str, Param]
) -> None:
    """Every declared param must be referenced by exactly one request binding.

    Spec: §Cross-Field Validation — "Every declared param must be referenced
    by exactly one request binding" + "If a provider requires the same
    resolved value in two request locations, declare two params with the
    same default/source and bind each param once."
    """
    refs: list[str] = []
    refs.extend(_collect_singleton_values(request.path_params, "from_param"))
    refs.extend(_collect_singleton_values(request.headers, "from_param"))
    refs.extend(_collect_singleton_values(request.query, "from_param"))
    refs.extend(_collect_singleton_values(getattr(request, "body", None), "from_param"))
    counts = Counter(refs)

    for name in params:
        n = counts.get(name, 0)
        if n == 0:
            raise ValueError(
                f"declared param {name!r} is not referenced by any request binding "
                "(spec: §Cross-Field Validation — every declared param must be "
                "referenced by exactly one request binding)"
            )
        if n > 1:
            raise ValueError(
                f"declared param {name!r} is referenced by {n} request bindings; "
                "every declared param must be referenced exactly once "
                "(spec: §Cross-Field Validation — declare two params if the "
                "same value is needed in two request locations)"
            )


def _walk_response_schema(
    response_schema: dict[str, Any], dotted_path: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Walk JSON Schema following dotted ``properties.<seg>`` chain.

    Returns ``(node, error_segment)``: ``node`` is the resolved subschema or
    ``None``; ``error_segment`` is the failing segment when traversal stops.
    Spec: §API Response Extraction — schema traversal rule.
    """
    node: Any = response_schema
    if not dotted_path:
        return (node if isinstance(node, dict) else None), None
    for seg in dotted_path.split("."):
        if not isinstance(node, dict):
            return None, seg
        props = node.get("properties")
        if not isinstance(props, dict) or seg not in props:
            return None, seg
        node = props[seg]
    return (node if isinstance(node, dict) else None), None


def resolve_read_record_schema(response: Any, response_schema: Any) -> Any:
    """Resolve ``operations.read.response.records`` to the record-shape subschema.

    ``response.schema`` describes the FULL provider response body, not just the
    records; ``records`` is a ``{ref}`` selecting the record collection inside
    it. Per the api-endpoint contract the ref is anchored at ``response.body``
    (the schema root), and each dotted segment after it maps to
    ``properties.<segment>`` (via :func:`_walk_response_schema`). The addressed
    node's ``items`` (when it is an ``array``) is the record shape — so a
    nested collection like ``response.body.objects`` yields the real record
    columns, not the wrapper key ``objects``.

    Falls back to ``response_schema`` when ``records`` is absent, non-spec, or
    does not resolve to a schema node — preserving extraction for schemas whose
    record shape already sits at the root.

    The single record-locator shared by every consumer of the read contract
    (field extraction and arrow_type stamping): they MUST target the same
    fields, so they call one function.
    """
    if not isinstance(response_schema, dict):
        return response_schema
    records = response.get("records") if isinstance(response, dict) else response
    ref = records.get("ref") if isinstance(records, dict) else records
    if not isinstance(ref, str):
        return response_schema
    if ref == "response.body":
        node: Any = response_schema
    elif ref.startswith("response.body."):
        node, _ = _walk_response_schema(response_schema, ref[len("response.body."):])
    else:
        # Non-spec ref (e.g. a bare JSONPath) — cannot map onto the schema.
        return response_schema
    if isinstance(node, dict) and node.get("type") == "array" and isinstance(node.get("items"), dict):
        return node["items"]
    return node if isinstance(node, dict) else response_schema


def find_record_field_properties(record_schema: Any) -> dict[str, Any] | None:
    """Return a record schema's top-level field-descriptor map, or ``None``.

    Walks the ``items`` chain (an array-of-records envelope may nest the record
    object under ``items``) until the first ``properties`` map — the record's
    mappable fields, one level deep. The single field-enumerator shared by the
    read contract's consumers (field extraction and column derivation) so
    they enumerate identical fields. Consumers read type/annotations off
    these top-level descriptors
    only; nested sub-properties, ``items`` elements and composition branches
    are not separately mappable.
    """
    current = record_schema
    while isinstance(current, dict):
        props = current.get("properties")
        if isinstance(props, dict):
            return props
        items = current.get("items")
        if not isinstance(items, dict):
            return None
        current = items
    return None


def _validate_records_in_response_schema(
    response: ResponseExtraction,
) -> dict[str, Any]:
    """Validate ``response.records`` resolves to an array node in ``response.schema``.

    Returns the array subschema (caller drills into ``items.*``). Always
    raises on failure — never returns ``None``. Spec: §Cross-Field Validation
    — ``response.records`` must resolve to a path represented in
    ``response.schema``, and that schema location must be an array.
    """
    ref: str = response.records.ref  # validated upstream to start with response.body
    suffix = ref[len("response.body"):]
    if suffix.startswith("."):
        suffix = suffix[1:]
    node, error_segment = _walk_response_schema(response.schema_, suffix)
    if node is None:
        if error_segment is None:
            raise ValueError(
                f"response.records ref {ref!r} resolved to a non-object schema location "
                "(spec: §API Response Extraction — schema traversal rule)"
            )
        raise ValueError(
            f"response.records ref {ref!r} traversal failed at segment "
            f"{error_segment!r}; not declared as `properties.{error_segment}` in "
            "response.schema (spec: §API Response Extraction — schema traversal rule)"
        )
    if node.get("type") != "array":
        raise ValueError(
            f"response.records ref {ref!r} resolves to a non-array node in "
            f"response.schema (got type={node.get('type')!r}); spec requires "
            "the schema location to be an array (spec: §Cross-Field Validation)"
        )
    return node


def _validate_cursor_fields_in_record_shape(
    replication: Replication, array_node: dict[str, Any]
) -> None:
    """Each ``cursor_field`` path must exist under the array's ``items`` subschema.

    Spec: §Cross-Field Validation — "Each replication ``cursor_field`` must
    correspond to a field path in ``response.schema`` under the extracted
    record-shape branch."
    """
    items = array_node.get("items")
    cursor_fields = [_cursor_field_of(cm) for cm in replication.cursor_mappings]

    if items is None or items is True:
        raise ValueError(
            "replication is declared but response.schema array node has no "
            f"`items` subschema; cursor_fields {cursor_fields!r} cannot be "
            "verified — tighten the response schema "
            "(spec: §Cross-Field Validation)"
        )
    if items is False:
        raise ValueError(
            "response.schema array node disallows items (`items: false`) but "
            "replication is declared (spec: §Cross-Field Validation)"
        )
    if isinstance(items, list):
        # Tuple validation: every cursor_field must exist in every position.
        for idx, sub in enumerate(items):
            if not isinstance(sub, dict):
                raise ValueError(
                    f"replication is declared but response.schema array `items[{idx}]` "
                    f"is {type(sub).__name__}, not an object schema; cursor_fields "
                    f"{cursor_fields!r} cannot be verified at that position "
                    "(spec: §Cross-Field Validation)"
                )
            for cf in cursor_fields:
                _check_cursor_field_in_node(cf, sub, where=f"items[{idx}]")
        return
    if not isinstance(items, dict):
        raise ValueError(
            f"response.schema array node `items` has unexpected type "
            f"{type(items).__name__}; cannot validate cursor fields"
        )

    for cf in cursor_fields:
        _check_cursor_field_in_node(cf, items, where="items")


def _cursor_field_of(cm: Any) -> str:
    if isinstance(cm, (SingleCursorMapping, WindowCursorMapping)):
        return cm.cursor_field
    raise TypeError(
        f"unsupported cursor mapping type {type(cm).__name__}; expected "
        "SingleCursorMapping or WindowCursorMapping"
    )


def _check_cursor_field_in_node(
    cursor_field: str, items_node: dict[str, Any], *, where: str
) -> None:
    node: Any = items_node
    segments = cursor_field.split(".")
    for i, seg in enumerate(segments):
        if not isinstance(node, dict):
            walked = ".".join(segments[: i + 1])
            raise ValueError(
                f"replication cursor_field {cursor_field!r} traversal failed at "
                f"{walked!r} (under {where!r}); intermediate node is not an object "
                "(spec: §Cross-Field Validation)"
            )
        props = node.get("properties")
        if not isinstance(props, dict) or seg not in props:
            walked = ".".join(segments[: i + 1])
            raise ValueError(
                f"replication cursor_field {cursor_field!r} not declared in "
                f"response.schema record-shape branch at {walked!r} (under {where!r}) "
                "(spec: §Cross-Field Validation)"
            )
        node = props[seg]
