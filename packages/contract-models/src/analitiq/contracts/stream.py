"""Stream models and validators (schema v1)."""
from __future__ import annotations

from typing import Annotated, Any, Literal, get_args
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from analitiq.contracts.endpoints import ARROW_TYPE_PATTERN, DatabaseObject
from analitiq.contracts.endpoint_identity import derive_db_endpoint_id
from analitiq.contracts.shared.advisory import AdvisoryValidated, find_duplicates
from analitiq.contracts.shared.arrow_shape import (
    ARROW_CONTAINER_SCHEMA_RULES,
    enforce_container_shape,
)
from analitiq.contracts.shared.common import (
    DESCRIPTION_MAX,
    DISPLAY_NAME_MAX,
    DISPLAY_NAME_MIN,
    NO_EDGE_WHITESPACE_PATTERN,
    NonEmptyStr,
    RetryErrorHandlingBase,
    StrictModel,
    TAGS_MAX,
    TrimmedTag,
    schema_url_for,
    validate_display_name,
    validate_tags,
)
from analitiq.contracts.shared.types import UUID_PATTERN


STREAM_SCHEMA_URL = schema_url_for("stream")

SCOPE_CONNECTOR = "connector"
SCOPE_CONNECTION = "connection"




def _check_unique_destinations(
    destinations: list["StreamDestination"],
) -> list["StreamDestination"]:
    """Reject duplicate destinations by `(scope, connection_id, endpoint_id)`.

    The public authored contract (`StreamAuthored`) enforces this via the
    advisory registry (ADV-STRM-001). This importable shim is retained for a
    downstream caller that reuses it directly; both paths share the
    `find_duplicates` primitive, so the algorithm is defined once.
    """
    dups = find_duplicates(
        destinations,
        key=lambda d: (
            d.endpoint_ref.scope,
            d.endpoint_ref.connection_id,
            d.endpoint_ref.endpoint_id,
        ),
    )
    if dups:
        raise ValueError(
            "destinations[].endpoint_ref must be unique by "
            f"(scope, connection_id, endpoint_id); duplicates: {dups!r}"
        )
    return destinations


# ---------------------------------------------------------------------------
# Endpoint reference
# ---------------------------------------------------------------------------


class _EndpointRefBase(StrictModel):
    """Fields shared by both endpoint-reference variants.

    `endpoint_id` lives on each variant, not here: a `connector` ref's id is
    the connector registry key you author, while a `connection` ref's id is a
    handle derived from `database_object`.
    """

    connection_id: NonEmptyStr = Field(
        ...,
        description=(
            "Connection reference selected in the parent pipeline. Typically a "
            "versioned connection ID (e.g. 'uuid_v1'); the schema accepts any "
            "non-empty string — engines resolve the reference at runtime."
        ),
        examples=["00000000-0000-4000-8000-000000000001_v1"],
    )


class ConnectorEndpointRef(_EndpointRefBase):
    """Public connector endpoint reference (`scope='connector'`).

    Pinned by the connection's connector_version. Carries NO `database_object`
    — an API endpoint's locator lives in its endpoint document
    (`operations.*.request.path`), not on the ref.
    """

    scope: Literal["connector"] = Field(
        ..., description="Endpoint reference scope; always 'connector' here."
    )
    endpoint_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Connector endpoint identifier — the registry key selected from "
            "endpoint discovery (e.g. 'transfers'). Client-authored."
        ),
    )


class ConnectionEndpointRef(_EndpointRefBase):
    """Private connection-scoped database endpoint reference (`scope='connection'`).

    Carries the verbatim provider-native object locator in `database_object`.
    `endpoint_id` is an opaque handle DERIVED from that locator by
    `analitiq.contracts.endpoint_identity.derive_db_endpoint_id` — a pure
    function shipped in this package, so you can compute it yourself. It is
    never decoded back to a target. The locator is the identity; hence it is
    REQUIRED and non-null here.
    """

    scope: Literal["connection"] = Field(
        ..., description="Endpoint reference scope; always 'connection' here."
    )
    database_object: DatabaseObject = Field(
        ...,
        description=(
            "Verbatim provider-native object locator (catalog/schema/name), "
            "round-tripped from endpoint discovery. Required — the opaque "
            "`endpoint_id` cannot be parsed for identity."
        ),
    )
    endpoint_id: str | None = Field(
        default=None,
        description=(
            "Opaque handle derived from `database_object` "
            "(`slug(schema)__slug(table)[__slug(catalog)]__<hash8>`). Omit it "
            "and it is derived from the locator; supply it and it must equal "
            "the derived handle."
        ),
    )

    @model_validator(mode="after")
    def _derive_or_verify_endpoint_id(self) -> "ConnectionEndpointRef":
        # `endpoint_id` is a pure function of the verbatim locator via the single
        # shared `derive_db_endpoint_id`, so there is no second implementation to
        # drift. Omitted → derive; supplied → verify it matches, fail loud on a
        # mismatch.
        obj = self.database_object
        canonical = derive_db_endpoint_id(obj.catalog, obj.schema_, obj.name)
        if self.endpoint_id is None:
            self.endpoint_id = canonical
        elif self.endpoint_id != canonical:
            raise ValueError(
                f"endpoint_id {self.endpoint_id!r} does not match the id derived "
                f"from database_object ({canonical!r}); it is derived from the "
                "locator and cannot be chosen independently"
            )
        return self


# Structured endpoint reference shared by source + destination sides, as a
# `scope`-discriminated union. The union structurally enforces "a `connection`
# ref carries a (non-null) `database_object`; a `connector` ref carries none" in
# BOTH the pydantic model and every generated artifact — the published JSON
# Schema renders a `oneOf` with a `scope` discriminator, and the
# @analitiq-ai/contracts Zod codegen preserves discriminated unions (unlike the
# `allOf if/then/else` conditional it used to strip). So external validators
# reject exactly what the service rejects, including the `database_object: null`
# edge (the connection variant has no null branch).
EndpointRef = Annotated[
    ConnectorEndpointRef | ConnectionEndpointRef,
    Field(discriminator="scope"),
]

_ENDPOINT_REF_ADAPTER = TypeAdapter(EndpointRef)


def validate_endpoint_ref(data: Any) -> ConnectorEndpointRef | ConnectionEndpointRef:
    """Validate a raw endpoint_ref dict into its concrete scope variant."""
    return _ENDPOINT_REF_ADAPTER.validate_python(data)


# ---------------------------------------------------------------------------
# Filters (spec §Filters)
# ---------------------------------------------------------------------------


# Filter-operator vocabulary (spec §Filter Operators). The `Filter.operator`
# Literal is the structural floor — the union of both scope vocabularies — and
# `StreamSource` narrows it to the scope-appropriate subset (database operators
# for a connection source, API operators for a connector source). The API set
# mirrors `endpoints.Param.operators`; the finer per-endpoint subset an API
# source may use is endpoint-owned and resolved at runtime.
_COMMON_FILTER_OPERATORS = ("eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in")
_DB_ONLY_FILTER_OPERATORS = ("is_null", "is_not_null", "like", "ilike")
_API_ONLY_FILTER_OPERATORS = ("contains", "starts_with", "ends_with")
_DB_FILTER_OPERATORS: frozenset[str] = frozenset(
    _COMMON_FILTER_OPERATORS + _DB_ONLY_FILTER_OPERATORS
)
_API_FILTER_OPERATORS: frozenset[str] = frozenset(
    _COMMON_FILTER_OPERATORS + _API_ONLY_FILTER_OPERATORS
)
FilterOperator = Literal[
    "eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in",  # common
    "is_null", "is_not_null", "like", "ilike",              # database-only
    "contains", "starts_with", "ends_with",                 # api-only
]

# Single-source guard: the field Literal must be exactly the union of the two
# scope vocabularies the StreamSource validator narrows to. Adding an operator to
# one place but not the other then fails loudly at import, not silently at runtime.
if set(get_args(FilterOperator)) != _DB_FILTER_OPERATORS | _API_FILTER_OPERATORS:
    raise AssertionError(
        "FilterOperator Literal must equal the union of the DB and API operator vocabularies")


_FILTER_CONDITIONAL_RULES: dict[str, Any] = {
    "allOf": [
        {
            "if": {
                "properties": {"operator": {"enum": ["is_null", "is_not_null"]}},
                "required": ["operator"],
            },
            "then": {"not": {"required": ["value"]}},
            "else": {"required": ["value"]},
        },
    ],
    "additionalProperties": False,
}


class Filter(StrictModel):
    """Stream-owned read predicate.

    Endpoint contracts own which fields/params are filterable and which
    operators are allowed. Per spec §Filters, `value` is required except
    when `operator` is a unary operator (`is_null` / `is_not_null`).
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_FILTER_CONDITIONAL_RULES,
    )

    field: str = Field(
        ...,
        min_length=1,
        description="Database field reference or API endpoint read parameter key.",
    )
    operator: FilterOperator = Field(
        ...,
        description="Operator selected from the applicable source capability.",
    )
    value: Any = Field(
        default=None,
        description="JSON value for the predicate; omit for unary operators.",
    )

    @model_validator(mode="after")
    def _validate_value_presence(self) -> "Filter":
        unary = {"is_null", "is_not_null"}
        if self.operator in unary and self.value is not None:
            raise ValueError(
                f"filters[].value must be omitted for unary operator {self.operator!r}"
            )
        # Non-unary operators can carry `value=None` here because Pydantic
        # cannot distinguish "omitted" from "explicit None"; cross-field
        # checks land at endpoint resolution.
        return self


# ---------------------------------------------------------------------------
# Replication (spec §Replication)
# ---------------------------------------------------------------------------


class _ReplicationBase(StrictModel):
    """Fields shared by both replication variants; `method` selects the variant."""

    safety_window_seconds: int | None = Field(
        default=None,
        ge=0,
        description="Non-negative late-arrival overlap window.",
    )
    tie_breaker_fields: list[str] | None = Field(
        default=None,
        description="Database-only deterministic cursor tie-breaker fields.",
    )


class FullRefreshReplication(_ReplicationBase):
    """Full-refresh replication: each run re-reads the whole source; no cursor."""

    method: Literal["full_refresh"] = Field(
        ...,
        description="Stream-selected replication method.",
    )


class IncrementalReplication(_ReplicationBase):
    """Incremental replication: resume from the last committed `cursor_field` value."""

    method: Literal["incremental"] = Field(
        ...,
        description="Stream-selected replication method.",
    )
    cursor_field: str = Field(
        ...,
        min_length=1,
        description="Source field reference tracking incremental progress.",
    )


# `method`-discriminated union: the incremental variant REQUIRES `cursor_field`
# and the full_refresh variant FORBIDS it (absent under additionalProperties:false).
# The published JSON Schema renders a `oneOf` with a `method` discriminator, so
# external validators reject exactly what the model does — the cross-field rule
# formerly enforced only in a `@model_validator` is now structural.
Replication = Annotated[
    FullRefreshReplication | IncrementalReplication,
    Field(discriminator="method"),
]


# ---------------------------------------------------------------------------
# Database pagination (spec §Database Pagination)
# ---------------------------------------------------------------------------


class _DatabasePaginationBase(StrictModel):
    """Fields shared by both database-pagination variants; `type` selects the variant."""

    page_size: int | None = Field(
        default=None,
        ge=1,
        description="Positive integer read page size; pipeline batch-size default applies when omitted.",
    )


class OffsetDatabasePagination(_DatabasePaginationBase):
    """Offset/limit read paging. `order_by_field` is optional."""

    type: Literal["offset"] = Field(
        ...,
        description="Database pagination strategy.",
    )
    order_by_field: str | None = Field(
        default=None,
        min_length=1,
        description="Source field reference for page ordering; optional for offset.",
    )


class KeysetDatabasePagination(_DatabasePaginationBase):
    """Keyset (seek) read paging. `order_by_field` defines the seek order and is required."""

    type: Literal["keyset"] = Field(
        ...,
        description="Database pagination strategy.",
    )
    order_by_field: str = Field(
        ...,
        min_length=1,
        description="Source field reference for page ordering; required for keyset.",
    )


# `type`-discriminated union: the keyset variant REQUIRES `order_by_field`; the
# offset variant leaves it optional (preserving the model's current semantics).
# The published JSON Schema renders a `oneOf` with a `type` discriminator.
DatabasePagination = Annotated[
    OffsetDatabasePagination | KeysetDatabasePagination,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Source (spec §Source)
# ---------------------------------------------------------------------------


class StreamSource(StrictModel):
    """Source endpoint binding and stream-owned read options."""

    endpoint_ref: EndpointRef = Field(..., description="Structured endpoint reference.")
    selected_columns: list[str] | None = Field(
        default=None,
        description="Ordered source field references; database sources only.",
    )
    filters: list[Filter] | None = Field(
        default=None,
        description="Stream-supplied read predicates.",
    )
    replication: Replication | None = Field(
        default=None,
        description=(
            "Stream-selected replication policy. Omission allowed only when "
            "the source supports full_refresh."
        ),
    )
    database_pagination: DatabasePagination | None = Field(
        default=None,
        description=(
            "Database source read-page configuration. Defaults to offset "
            "pagination with page size from pipeline.runtime.batching.batch_size "
            "when omitted for database sources."
        ),
    )
    primary_keys: list[str] | None = Field(
        default=None,
        description=(
            "Stream-owned source identity hint when the endpoint does not "
            "provide primary-key metadata."
        ),
    )

    @model_validator(mode="after")
    def _validate_filter_operator_scope(self) -> "StreamSource":
        # `Filter.operator` structurally allows the union of both vocabularies;
        # which subset is valid depends on the source scope, which only the
        # binding (endpoint_ref) knows. A database (connection) source may use
        # the database operators; an API (connector) source may use the API
        # operators — its finer per-endpoint subset is resolved at runtime.
        if not self.filters:
            return self
        allowed = (
            _DB_FILTER_OPERATORS
            if self.endpoint_ref.scope == SCOPE_CONNECTION
            else _API_FILTER_OPERATORS
        )
        for filt in self.filters:
            if filt.operator not in allowed:
                raise ValueError(
                    f"filters[].operator {filt.operator!r} is not valid for a "
                    f"{self.endpoint_ref.scope} source "
                    f"(allowed: {sorted(allowed)})"
                )
        return self


# ---------------------------------------------------------------------------
# Destination — write selection, execution overrides (spec §Destinations, §Write Selection, §Execution)
# ---------------------------------------------------------------------------


class Write(StrictModel):
    """Stream-selected write behavior for one destination."""

    mode: str = Field(
        ...,
        min_length=1,
        description=(
            "Write mode. API: selected endpoint operations.write key. "
            "Database: 'insert' or 'upsert'."
        ),
    )
    conflict_keys: list[Annotated[str, Field(min_length=1)]] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Database upsert conflict target — a single composite key set "
            "(non-empty list of destination field names). Required for a "
            "database (`scope=connection`) upsert; forbidden for an API "
            "(`scope=connector`) destination, whose conflict key is "
            "endpoint-owned (`operations.write.upsert.conflict_keys`). Presence "
            "is enforced by `StreamDestination`, which knows the destination "
            "scope. Multiple alternative key sets are out of scope until a "
            "connector needs them."
        ),
    )


class Execution(StrictModel):
    """Per-stream destination execution override for pipeline runtime batching defaults."""

    batch_size: int | None = Field(
        default=None,
        ge=1,
        le=100_000,
        description="Override pipeline.runtime.batching.batch_size for this binding.",
    )
    max_concurrent_batches: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Override pipeline.runtime.batching.max_concurrent_batches for this binding.",
    )


# The database (connection-scope) write-mode vocabulary is closed to
# {insert, upsert}. An API (connector-scope) destination's mode is the selected
# endpoint's `operations.write` key, resolved at runtime, so it is not
# constrained here. Enforced in pydantic by `_validate_db_write_mode` and
# mirrored into the published schema by the first `allOf` branch below.
_DB_WRITE_MODES: frozenset[str] = frozenset({"insert", "upsert"})

# Declarative mirror of `_validate_write_conflict_keys` and
# `_validate_db_write_mode`. The selecting field is a nested discriminator
# (`endpoint_ref.scope`) and `write.mode` is an open string on a connector
# destination, so this is stock if/then rather than a discriminated union:
#   scope=connection           ⇒ write.mode ∈ {insert, upsert}
#   scope=connector            ⇒ write.conflict_keys forbidden (null/absent)
#   scope=connection, upsert   ⇒ write.conflict_keys required, non-empty array
#   scope=connection, ¬upsert  ⇒ write.conflict_keys forbidden (null/absent)
_STREAM_DESTINATION_SCHEMA_RULES: dict[str, Any] = {
    "allOf": [
        {
            "if": {
                "required": ["endpoint_ref"],
                "properties": {
                    "endpoint_ref": {
                        "required": ["scope"],
                        "properties": {"scope": {"const": "connection"}},
                    }
                },
            },
            "then": {
                "properties": {
                    "write": {"properties": {"mode": {"enum": sorted(_DB_WRITE_MODES)}}}
                }
            },
        },
        {
            "if": {
                "required": ["endpoint_ref"],
                "properties": {
                    "endpoint_ref": {
                        "required": ["scope"],
                        "properties": {"scope": {"const": "connector"}},
                    }
                },
            },
            "then": {"properties": {"write": {"properties": {"conflict_keys": {"type": "null"}}}}},
        },
        {
            "if": {
                "required": ["endpoint_ref", "write"],
                "properties": {
                    "endpoint_ref": {
                        "required": ["scope"],
                        "properties": {"scope": {"const": "connection"}},
                    },
                    "write": {"required": ["mode"], "properties": {"mode": {"const": "upsert"}}},
                },
            },
            "then": {
                "properties": {
                    "write": {
                        "required": ["conflict_keys"],
                        "properties": {"conflict_keys": {"type": "array", "minItems": 1}},
                    }
                }
            },
        },
        {
            "if": {
                "required": ["endpoint_ref"],
                "properties": {
                    "endpoint_ref": {
                        "required": ["scope"],
                        "properties": {"scope": {"const": "connection"}},
                    },
                    "write": {"properties": {"mode": {"not": {"const": "upsert"}}}},
                },
            },
            "then": {"properties": {"write": {"properties": {"conflict_keys": {"type": "null"}}}}},
        },
    ],
}


class StreamDestination(StrictModel):
    """Destination endpoint binding and stream-owned destination options."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra=_STREAM_DESTINATION_SCHEMA_RULES
    )

    endpoint_ref: EndpointRef = Field(..., description="Structured endpoint reference.")
    write: Write = Field(
        ..., description="Stream-selected write behavior for this destination."
    )
    execution: Execution | None = Field(
        default=None,
        description="Stream-level destination execution override.",
    )

    @model_validator(mode="after")
    def _validate_write_conflict_keys(self) -> "StreamDestination":
        # Who owns the upsert conflict key differs by destination type, and the
        # type is the endpoint scope: `connector` is an API endpoint (the key is
        # provider-defined and declared on the endpoint —
        # `operations.write.upsert.conflict_keys`), `connection` is a database
        # endpoint (the key is the stream-selected `primary_keys` subset). So an
        # API destination must NOT carry stream-authored conflict_keys, and a
        # database upsert MUST. Spec: §Write Selection.
        if self.endpoint_ref.scope == "connector":
            if self.write.conflict_keys is not None:
                raise ValueError(
                    "destinations[].write.conflict_keys must not be set for an API "
                    "destination (endpoint_ref.scope='connector'); the upsert conflict "
                    "key is endpoint-owned (operations.write.upsert.conflict_keys)"
                )
        elif self.write.mode == "upsert":
            if not self.write.conflict_keys:
                raise ValueError(
                    "destinations[].write.conflict_keys is required for a database upsert "
                    "(endpoint_ref.scope='connection', write.mode='upsert')"
                )
        elif self.write.conflict_keys is not None:
            # conflict_keys are an upsert concept; a non-upsert database mode
            # (insert) must not carry them.
            raise ValueError(
                "destinations[].write.conflict_keys is only valid for a database upsert "
                f"(endpoint_ref.scope='connection', write.mode='upsert'); write.mode="
                f"{self.write.mode!r} must not declare it"
            )
        return self

    @model_validator(mode="after")
    def _validate_db_write_mode(self) -> "StreamDestination":
        # A database destination's write-mode vocabulary is closed to
        # {insert, upsert}. An API destination's mode is the selected endpoint's
        # `operations.write` key (endpoint-owned, resolved at runtime), so it is
        # not constrained here. Spec: §Write Selection.
        if (
            self.endpoint_ref.scope == SCOPE_CONNECTION
            and self.write.mode not in _DB_WRITE_MODES
        ):
            raise ValueError(
                f"destinations[].write.mode {self.write.mode!r} is not a valid "
                f"database write mode (allowed: {sorted(_DB_WRITE_MODES)}); an API "
                "destination's mode is the selected endpoint's operations.write key"
            )
        return self


# ---------------------------------------------------------------------------
# Mapping (spec §Mapping, §Assignment, §Mapping Expressions, §Assignment Validation)
# ---------------------------------------------------------------------------


class GetExpression(StrictModel):
    """`{"op": "get", "path": "<source field reference>"}` — read a source
    field."""

    op: Literal["get"] = Field(...)
    path: str = Field(
        ..., min_length=1, description="Source field reference."
    )


class FnExpression(StrictModel):
    """`{"op": "fn", "name": "<conversion fn>"}` — one conversion stage of a
    `pipe` (#887).

    Mirrors the engine's `fn` AST node. `name` is closed over the conversion
    functions the engine-published conversion matrix declares for `explicit`
    conversions — the matrix cell's `fn` is what an author (the FE mapping
    editor) wires in, so a name the engine would reject at transform build is
    not authorable. The engine's optional `version`/`args` node fields are
    intentionally not published: the engine defaults them (`version=1`,
    `args=[]`) and no declarable conversion takes arguments.
    """

    op: Literal["fn"] = Field(...)
    # Source of truth for the permitted names: the engine's
    # `cdk/cdk/type_map/conversion_matrix.json` (the `fn` of every `explicit`
    # cell). Widen this Literal when the matrix declares a new one.
    name: Literal["to_string"] = Field(
        ...,
        description=(
            "Conversion function name. Closed over the functions the "
            "engine-published conversion matrix declares for `explicit` "
            "conversions."
        ),
    )


def _pipe_args_positional_grammar(schema: dict[str, Any]) -> None:
    """Publish `pipe.args` as `[<get seed>, <fn stage>, ...]` positionally.

    Pydantic renders `list[GetExpression | FnExpression]` as a uniform
    `items.anyOf`, which would let a published-schema-only author put a
    `get` in a stage position or an `fn` in the seed — shapes the model
    validator (and the engine transform build) reject. Restructured into
    `prefixItems` (seed) + `items` (stages) the published grammar is exactly
    the model's rule. The `$ref`s seen here are pydantic's internal defs refs;
    the generator remaps them to the public `#/$defs/...` refs afterwards.
    """
    by_position: dict[str, dict[str, Any]] = {}
    for variant in schema["items"]["anyOf"]:
        ref = variant["$ref"].rsplit("/", 1)[-1]
        if "GetExpression" in ref:
            by_position["seed"] = variant
        elif "FnExpression" in ref:
            by_position["stage"] = variant
    schema["prefixItems"] = [by_position["seed"]]
    schema["items"] = by_position["stage"]


class PipeExpression(StrictModel):
    """`{"op": "pipe", "args": [<get>, <fn>, ...]}` — a source read piped
    through one or more declared conversion functions (#887).

    Mirrors the engine's `pipe` AST node: `args[0]` is the seed expression —
    a `get` in the stream grammar (constants use `value.constant`, never an
    expression node) — and every later entry is an `fn` conversion stage
    applied left to right. This is how an assignment satisfies an `explicit`
    conversion-matrix pair (e.g. `Int64 → Utf8` needs `to_string`): the
    engine rejects a bare `get` for such a pair at both the transform build
    and the destination cast.
    """

    op: Literal["pipe"] = Field(...)
    args: list[GetExpression | FnExpression] = Field(
        ...,
        min_length=2,
        json_schema_extra=_pipe_args_positional_grammar,
        description=(
            "Seed `get` expression followed by one or more `fn` conversion "
            "stages, applied left to right."
        ),
    )

    @model_validator(mode="after")
    def _validate_positional_grammar(self) -> "PipeExpression":
        # Mirrored in the published schema by `_pipe_args_positional_grammar`
        # (prefixItems/items); keep the two in lockstep.
        if not isinstance(self.args[0], GetExpression):
            raise ValueError("pipe args[0] must be a 'get' expression (the seed)")
        if not all(isinstance(arg, FnExpression) for arg in self.args[1:]):
            raise ValueError("pipe args[1:] must all be 'fn' conversion stages")
        return self


class ArrowFieldSpec(StrictModel):
    """Recursive field-shape declaration.

    Used to describe authored-shape JSON containers under `arrow_type` =
    `Object` / `List` / `Json`. Scalar and parameterized Arrow types reuse the
    same model with `properties` and `items` absent.
    """

    # Declarative mirror of `enforce_container_shape` — shared verbatim with the
    # endpoint `Column`/`ColumnFieldSpec` classes (see `arrow_shape.py`).
    model_config = ConfigDict(
        extra="forbid", json_schema_extra=ARROW_CONTAINER_SCHEMA_RULES
    )

    arrow_type: str = Field(
        ...,
        pattern=ARROW_TYPE_PATTERN,
        description=(
            "Arrow canonical type string from the shared type vocabulary. "
            "Parameterized canonical types must preserve the full string, "
            "e.g. 'Decimal128(38, 9)' — not 'Decimal128'. Bare authored-shape "
            "markers 'Object', 'List', and 'Json' declare JSON containers."
        ),
    )
    nullable: bool | None = Field(default=None)
    # Sibling-key rules (Object/List/Json) live in
    # `analitiq.contracts.shared.arrow_shape.enforce_container_shape`; do not duplicate
    # them in field descriptions, or they'll rot when the rules change.
    properties: dict[str, "ArrowFieldSpec"] | None = Field(default=None)
    items: "ArrowFieldSpec | None" = Field(default=None)

    @model_validator(mode="after")
    def _validate_container_shape(self) -> "ArrowFieldSpec":
        enforce_container_shape(
            self.arrow_type, self.properties, self.items
        )
        return self


# ConstantValue carries the shared container matrix PLUS a `value` JSON-kind
# pin against `arrow_type` (constants carry the actual payload). The container
# branches are reused verbatim from `ARROW_CONTAINER_SCHEMA_RULES`. `value:null`
# is the universal "no value" sentinel, so every branch also admits null.
_CONSTANT_VALUE_SCHEMA_RULES: dict[str, Any] = {
    "allOf": [
        *ARROW_CONTAINER_SCHEMA_RULES["allOf"],
        {
            "if": {"required": ["arrow_type"], "properties": {"arrow_type": {"const": "Object"}}},
            "then": {"properties": {"value": {"type": ["object", "null"]}}},
        },
        {
            "if": {"required": ["arrow_type"], "properties": {"arrow_type": {"const": "List"}}},
            "then": {"properties": {"value": {"type": ["array", "null"]}}},
        },
        {
            "if": {"required": ["arrow_type"], "properties": {"arrow_type": {"const": "Json"}}},
            "then": {"properties": {"value": {"type": ["object", "array", "null"]}}},
        },
        {
            "if": {
                "required": ["arrow_type"],
                "properties": {"arrow_type": {"not": {"enum": ["Object", "List", "Json"]}}},
            },
            "then": {"properties": {"value": {"not": {"type": ["object", "array"]}}}},
        },
    ],
}


class ConstantValue(StrictModel):
    """Typed constant — alternative to expression."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra=_CONSTANT_VALUE_SCHEMA_RULES
    )

    arrow_type: str = Field(
        ...,
        pattern=ARROW_TYPE_PATTERN,
        description=(
            "Arrow canonical type string from the shared type vocabulary. "
            "Parameterized canonical types must preserve the full string, "
            "e.g. 'Decimal128(38, 9)' — not 'Decimal128'. Bare authored-shape "
            "markers 'Object', 'List', and 'Json' declare JSON containers."
        ),
    )
    value: Any = Field(
        ...,
        description=(
            "JSON literal value to assign. May be a JSON object when "
            "arrow_type is 'Object' or 'Json', a JSON array when arrow_type "
            "is 'List' or 'Json', or a JSON scalar for scalar Arrow types."
        ),
    )
    properties: dict[str, ArrowFieldSpec] | None = Field(default=None)
    items: ArrowFieldSpec | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_container_shape(self) -> "ConstantValue":
        enforce_container_shape(
            self.arrow_type, self.properties, self.items
        )
        # Constants carry the actual payload, so we additionally pin the
        # JSON kind of `value` against the declared `arrow_type`. Object →
        # dict; List → list; Json → dict or list; everything else → scalar
        # (anything but dict/list).
        #
        # `value: null` is permitted for any arrow_type as the universal
        # "no value" sentinel — destination nullability is enforced at write
        # time against the destination field, not here. Skip the kind check
        # for null so e.g. `{arrow_type: "Int64", value: null}` and
        # `{arrow_type: "Object", value: null, properties: {...}}` both pass.
        # Sibling-key requirements (`properties` for Object, `items` for List)
        # are still enforced above by `enforce_container_shape` regardless
        # of `value`, so a null Object without `properties` still fails.
        if self.value is None:
            return self
        if self.arrow_type == "Object" and not isinstance(self.value, dict):
            raise ValueError(
                "constant.value must be a JSON object when arrow_type is 'Object'"
            )
        if self.arrow_type == "List" and not isinstance(self.value, list):
            raise ValueError(
                "constant.value must be a JSON array when arrow_type is 'List'"
            )
        if self.arrow_type == "Json" and not isinstance(self.value, (dict, list)):
            raise ValueError(
                "constant.value must be a JSON object or array when "
                "arrow_type is 'Json'"
            )
        if self.arrow_type not in ("Object", "List", "Json") and isinstance(
            self.value, (dict, list)
        ):
            raise ValueError(
                f"constant.value must be a JSON scalar when arrow_type is "
                f"{self.arrow_type!r}; got {type(self.value).__name__}"
            )
        return self


class AssignmentValue(StrictModel):
    """Exactly one of `expression` or `constant` per spec §Assignment."""

    # Declarative mirror of `_validate_one_of`. No discriminator field exists, so
    # the two branches pin the *other* field to null (rather than relying on
    # `required`, since both default to null).
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["expression"],
                    "properties": {
                        "expression": {"not": {"type": "null"}},
                        "constant": {"type": "null"},
                    },
                },
                {
                    "required": ["constant"],
                    "properties": {
                        "constant": {"not": {"type": "null"}},
                        "expression": {"type": "null"},
                    },
                },
            ]
        },
    )

    expression: GetExpression | PipeExpression | None = Field(default=None)
    constant: ConstantValue | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_one_of(self) -> "AssignmentValue":
        if (self.expression is None) == (self.constant is None):
            raise ValueError(
                "value must contain exactly one of 'expression' or 'constant'"
            )
        return self


class AssignmentTarget(StrictModel):
    """Destination field specification."""

    # Declarative mirror of `enforce_container_shape` — shared with the other
    # authored-shape classes (see `arrow_shape.py`).
    model_config = ConfigDict(
        extra="forbid", json_schema_extra=ARROW_CONTAINER_SCHEMA_RULES
    )

    path: str = Field(
        ..., min_length=1, description="Destination field reference."
    )
    arrow_type: str = Field(
        ...,
        pattern=ARROW_TYPE_PATTERN,
        description=(
            "Arrow canonical type string from the shared type vocabulary. "
            "Parameterized canonical types must preserve the full string, "
            "e.g. 'Decimal128(38, 9)' — not 'Decimal128'. Bare authored-shape "
            "markers 'Object', 'List', and 'Json' declare JSON containers."
        ),
    )
    native_type: str | None = Field(
        default=None,
        description="Destination-native type override (e.g., 'NUMERIC(12,2)').",
    )
    nullable: bool = Field(default=True)
    # See ArrowFieldSpec for the recursive child shape and
    # enforce_container_shape for the sibling-key rules.
    properties: dict[str, ArrowFieldSpec] | None = Field(default=None)
    items: ArrowFieldSpec | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_container_shape(self) -> "AssignmentTarget":
        enforce_container_shape(
            self.arrow_type, self.properties, self.items
        )
        return self


# Declarative mirror of `_validate_value_for_rule`. It resembles the
# `_FILTER_CONDITIONAL_RULES` precedent but must be null-aware, because
# `ValidationRule`'s runtime is stricter than `Filter`'s: `Filter` defers the
# value check (a null `value` is allowed for binary operators), so key-presence
# suffices there; `ValidationRule` requires a NON-NULL `value` for the
# value-taking rules and forbids a non-null `value` for the unary ones. So each
# branch pins the null-ness, not just key presence:
#   type in {required, not_null}  ⇒ value absent or null
#   otherwise                     ⇒ value present and non-null
_VALIDATION_RULE_CONDITIONAL_RULES: dict[str, Any] = {
    "allOf": [
        {
            "if": {
                "properties": {"type": {"enum": ["required", "not_null"]}},
                "required": ["type"],
            },
            "then": {"properties": {"value": {"type": "null"}}},
            "else": {
                "required": ["value"],
                "properties": {"value": {"not": {"type": "null"}}},
            },
        },
    ],
    "additionalProperties": False,
}


class ValidationRule(StrictModel):
    """Stream record validation rule — see §Assignment Validation."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra=_VALIDATION_RULE_CONDITIONAL_RULES
    )

    type: Literal[
        "required", "not_null", "min_length", "max_length", "pattern", "range", "in_list"
    ] = Field(...)
    field: str = Field(
        ..., min_length=1, description="Mapped output field path validated by this rule."
    )
    value: Any = Field(
        default=None,
        description=(
            "Rule parameter. Required for min_length/max_length/pattern/range/in_list; "
            "must be omitted for required/not_null."
        ),
    )
    message: str | None = Field(default=None, description="Custom validation error message.")

    @model_validator(mode="after")
    def _validate_value_for_rule(self) -> "ValidationRule":
        unary = {"required", "not_null"}
        needs_value = {"min_length", "max_length", "pattern", "range", "in_list"}
        if self.type in unary and self.value is not None:
            raise ValueError(
                f"validation rule {self.type!r} must omit 'value'"
            )
        if self.type in needs_value and self.value is None:
            raise ValueError(
                f"validation rule {self.type!r} requires 'value'"
            )
        return self


class StreamValidationErrorHandling(RetryErrorHandlingBase):
    """Mirror of the shared error-handling shape (`pipeline.ErrorHandling` in `analitiq.contracts.pipelines`)."""

    # Inherits the full error-handling contract — fields, bounds, defaulting, the
    # cross-field rule, and the JSON-Schema conditional rules — from
    # `RetryErrorHandlingBase`, shared with the pipeline block so the two cannot
    # drift (#938). The stream block adds no field descriptions, so it re-declares
    # nothing; this subclass exists to name the stream schema `$def`.


class Validation(StrictModel):
    """Per-assignment validation block."""

    rules: list[ValidationRule] = Field(default_factory=list)
    error_handling: StreamValidationErrorHandling | None = Field(
        default=None,
        description=(
            "Validation failure handling override. When omitted, the pipeline "
            "runtime.error_handling default applies."
        ),
    )


class Assignment(StrictModel):
    """Single field assignment — writes one target field from expression or constant."""

    target: AssignmentTarget = Field(...)
    value: AssignmentValue = Field(...)
    # Field name is `validate` per spec; aliased to avoid shadowing Pydantic's
    # `BaseModel.validate` legacy attribute.
    validation: Validation | None = Field(
        default=None,
        alias="validate",
        description="Assignment validation rules.",
    )


class StreamMapping(AdvisoryValidated, StrictModel):
    """Source-to-destination assignment rules. Optional — omit for default mapping."""

    assignments: list[Assignment] = Field(
        default_factory=list,
        description="Ordered list of field assignments. Order is significant.",
    )


# ---------------------------------------------------------------------------
# Authored shared base + read/write split
# ---------------------------------------------------------------------------


class StreamAuthored(AdvisoryValidated, BaseModel):
    """Authored stream fields shared between input and persisted models."""

    schema_url: Literal[STREAM_SCHEMA_URL] | None = Field(
        default=None,
        alias="$schema",
        description="Stream schema URL (optional in API payloads).",
    )

    display_name: str | None = Field(
        default=None,
        min_length=DISPLAY_NAME_MIN,
        max_length=DISPLAY_NAME_MAX,
        pattern=NO_EDGE_WHITESPACE_PATTERN,
        description="User-facing stream label.",
    )
    description: str | None = Field(
        default=None, max_length=DESCRIPTION_MAX, description="User-facing summary."
    )
    pipeline_id: NonEmptyStr = Field(
        ...,
        description=(
            "Parent pipeline reference. Typically the base pipeline UUID; the "
            "schema accepts any non-empty string — engines resolve the "
            "reference at runtime. Immutable after creation."
        ),
        examples=["b4904c77-0a4a-4a8d-a768-4a8b5f2f2414"],
    )

    source: StreamSource = Field(...)
    destinations: list[StreamDestination] = Field(
        ..., min_length=1, description="Non-empty array of destination bindings."
    )
    mapping: StreamMapping | None = Field(
        default=None,
        description=(
            "Explicit source-to-destination field mapping. Omit for runtime "
            "default mapping."
        ),
    )

    status: Literal["draft", "active", "inactive"] = Field(
        default="draft",
        description=(
            "Lifecycle status."
        ),
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

class StreamInput(StreamAuthored):
    """Strict API input variant — the source of truth for the `stream/latest.json` published JSON Schema.

    This model declares exactly what an author may write, so anything Analitiq
    assigns is not merely rejected here — it is unrepresentable. `extra="forbid"`
    does the rest.

    `stream_id` is an optional authored identifier (RFC-4122 UUID) so
    externally-authored stream definitions can supply their own UUID; the
    service assigns one when the create payload omits it.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    stream_id: str | None = Field(
        default=None,
        pattern=UUID_PATTERN,
        description=(
            "Stream UUID (RFC-4122). Optional in authored definitions so "
            "external publishers can assign their own identifier; "
            "service-assigned on ingest when omitted."
        ),
    )

