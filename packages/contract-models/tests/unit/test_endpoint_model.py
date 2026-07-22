"""Tests for analitiq.contracts.endpoints Pydantic models (PR #366 / issue #364).

Covers the per-kind dispatch (api vs database, selected by the owning
connector — the document carries no top-level `kind`), x-* extension policy,
$schema-required rule, the four cross-field validators added in this PR
(param-binding uniqueness, response.records ↔ response.schema traversal,
replication cursor_field schema-presence, pagination expression shape), and
the discriminated-union refactors of `Predicate` and `CursorMapping`.
"""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from analitiq.contracts.endpoints import (
    _RESERVED_ENDPOINT_FIELDS,
    RESOLUTION_SCOPES,
    ApiEndpointDoc,
    Column,
    ColumnFieldSpec,
    DatabaseEndpointDoc,
    Param,
    PredicateAnd,
    PredicateEq,
    PredicateMissing,
    PredicateNot,
    RefExpression,
    SingleCursorMapping,
    TemplateExpression,
    WindowCursorMapping,
    WriteResponse,
    parse_endpoint,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
API_ENDPOINT_SCHEMA_PATH = REPO_ROOT / "schemas" / "api-endpoint" / "4.0.0.json"
DATABASE_ENDPOINT_SCHEMA_PATH = REPO_ROOT / "schemas" / "database-endpoint" / "4.0.0.json"
LATEST_API_ENDPOINT_SCHEMA_PATH = REPO_ROOT / "schemas" / "api-endpoint" / "latest.json"
LATEST_DATABASE_ENDPOINT_SCHEMA_PATH = (
    REPO_ROOT / "schemas" / "database-endpoint" / "latest.json"
)

API_SCHEMA_URL = "https://schemas.analitiq.ai/api-endpoint/latest.json"
DB_SCHEMA_URL = "https://schemas.analitiq.ai/database-endpoint/latest.json"
JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


# ---------------------------------------------------------------------------
# Fixtures / payload factories
# ---------------------------------------------------------------------------


def _minimal_api_payload(**overrides):
    """Return a spec-§Examples 'Minimal API Read Endpoint' payload with overrides."""
    payload = {
        "$schema": API_SCHEMA_URL,
        "endpoint_id": "records",
        "operations": {
            "read": {
                "request": {"method": "GET", "path": "/v1/records"},
                "params": {},
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {"$schema": JSON_SCHEMA, "type": "array", "items": {"type": "object"}},
                },
            },
        },
    }
    payload.update(overrides)
    return payload


def _minimal_database_payload(**overrides):
    payload = {
        "$schema": DB_SCHEMA_URL,
        "endpoint_id": "public_orders",
        "database_object": {"schema": "public", "name": "orders", "object_type": "table"},
        "columns": [{"name": "id", "native_type": "uuid", "arrow_type": "Utf8"}],
    }
    payload.update(overrides)
    return payload


def _read_op_with(params, request_extras=None, response=None):
    """Build a read operation block with declared params and a wired request."""
    base_request = {"method": "GET", "path": "/v1/x"}
    if request_extras:
        base_request.update(request_extras)
    return {
        "request": base_request,
        "params": params,
        "response": response or {
            "records": {"ref": "response.body"},
            "schema": {"type": "array", "items": {"type": "object"}},
        },
    }


# ---------------------------------------------------------------------------
# Per-kind dispatch (no top-level `kind` on the document)
# ---------------------------------------------------------------------------


class TestPerKindDispatch:
    def test_api_payload_returns_api_doc(self):
        result = parse_endpoint(_minimal_api_payload())
        assert isinstance(result, ApiEndpointDoc)

    def test_database_payload_returns_database_doc(self):
        result = parse_endpoint(_minimal_database_payload())
        assert isinstance(result, DatabaseEndpointDoc)

    def test_top_level_kind_field_rejected_on_api(self):
        # `kind` is no longer a permitted top-level field; it must be
        # rejected by the x-* extension policy on both per-kind shapes.
        with pytest.raises(ValidationError):
            ApiEndpointDoc.model_validate(_minimal_api_payload(kind="api"))

    def test_top_level_kind_field_rejected_on_database(self):
        with pytest.raises(ValidationError):
            DatabaseEndpointDoc.model_validate(_minimal_database_payload(kind="database"))


# ---------------------------------------------------------------------------
# Schema URL required (§Top-Level Fields)
# ---------------------------------------------------------------------------


class TestSchemaUrlRequired:
    def test_missing_dollar_schema_rejected(self):
        payload = _minimal_database_payload()
        del payload["$schema"]
        with pytest.raises(ValidationError):
            DatabaseEndpointDoc.model_validate(payload)

    def test_empty_dollar_schema_rejected(self):
        with pytest.raises(ValidationError):
            DatabaseEndpointDoc.model_validate(_minimal_database_payload(**{"$schema": ""}))

    def test_wrong_dollar_schema_rejected(self):
        with pytest.raises(ValidationError):
            DatabaseEndpointDoc.model_validate(_minimal_database_payload(**{"$schema": "https://example.com/schema.json"}))

    def test_api_url_on_database_payload_rejected(self):
        # Each per-kind class only accepts its own per-kind URL.
        with pytest.raises(ValidationError):
            DatabaseEndpointDoc.model_validate(_minimal_database_payload(**{"$schema": API_SCHEMA_URL}))

    def test_database_url_on_api_payload_rejected(self):
        with pytest.raises(ValidationError):
            ApiEndpointDoc.model_validate(_minimal_api_payload(**{"$schema": DB_SCHEMA_URL}))

    def test_present_dollar_schema_accepted(self):
        result = DatabaseEndpointDoc.model_validate(_minimal_database_payload())
        assert result.schema_url == DB_SCHEMA_URL


# ---------------------------------------------------------------------------
# x-* extension policy (§Extension Policy)
# ---------------------------------------------------------------------------


class TestExtensionPolicyClosed:
    """Closed contract: no `x-*` extensions; unknown fields rejected."""

    def test_unknown_field_at_root_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            parse_endpoint(_minimal_database_payload(unknown_field="oops"))

    def test_x_prefixed_field_at_root_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            parse_endpoint(_minimal_database_payload(**{"x-vendor-id": "v1"}))

    def test_x_prefixed_field_on_database_object_rejected(self):
        payload = _minimal_database_payload()
        payload["database_object"]["x-vendor-extension"] = {"beta": True}
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            parse_endpoint(payload)

    def test_alias_fields_not_treated_as_unknown(self):
        # `$schema` (alias for schema_url), `schema` (alias for DatabaseObject.schema_),
        # and `in` (alias for Param.location) must round-trip cleanly.
        result = parse_endpoint(_minimal_database_payload())
        assert result.database_object.schema_ == "public"


# ---------------------------------------------------------------------------
# §Cross-Field Validation: every declared param referenced exactly once
# ---------------------------------------------------------------------------


class TestParamBindingUniqueness:
    def test_unreferenced_param_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"unused": {"in": "query", "type": "string", "required": False, "operators": ["eq"]}},
            )},
        )
        with pytest.raises(ValidationError, match="not referenced"):
            parse_endpoint(payload)

    def test_singly_referenced_param_accepted(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"status": {"in": "query", "type": "string", "required": False, "operators": ["eq"]}},
                request_extras={"query": {"status": {"from_param": "status"}}},
            )},
        )
        parse_endpoint(payload)

    def test_doubly_referenced_param_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"p": {"in": "query", "type": "string", "required": False, "operators": ["eq"]}},
                request_extras={"query": {"a": {"from_param": "p"}, "b": {"from_param": "p"}}},
            )},
        )
        with pytest.raises(ValidationError, match="referenced by 2 request bindings"):
            parse_endpoint(payload)

    def test_two_distinct_params_each_bound_once_accepted(self):
        # Spec-recommended pattern when the same value goes to two locations.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={
                    "qa": {"in": "query", "type": "string", "required": False, "operators": ["eq"]},
                    "qb": {"in": "query", "type": "string", "required": False, "operators": ["eq"]},
                },
                request_extras={"query": {"a": {"from_param": "qa"}, "b": {"from_param": "qb"}}},
            )},
        )
        parse_endpoint(payload)


# ---------------------------------------------------------------------------
# §API Response Extraction: records resolves to an array node
# ---------------------------------------------------------------------------


class TestRecordsInResponseSchema:
    def test_root_array_accepted(self):
        parse_endpoint(_minimal_api_payload())

    def test_records_must_be_ref_expression(self):
        payload = _minimal_api_payload()
        payload["operations"]["read"]["response"]["records"] = {"template": "${response.body}"}
        with pytest.raises(ValidationError):
            parse_endpoint(payload)

    def test_nested_array_accepted(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={},
                response={
                    "records": {"ref": "response.body.data.items"},
                    "schema": {
                        "type": "object",
                        "properties": {
                            "data": {"type": "object", "properties": {
                                "items": {"type": "array", "items": {"type": "object"}},
                            }},
                        },
                    },
                },
            )},
        )
        parse_endpoint(payload)

    def test_non_array_node_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={},
                response={
                    "records": {"ref": "response.body.meta"},
                    "schema": {"type": "object", "properties": {"meta": {"type": "object"}}},
                },
            )},
        )
        with pytest.raises(ValidationError, match="non-array node"):
            parse_endpoint(payload)

    def test_unresolvable_path_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={},
                response={
                    "records": {"ref": "response.body.missing.items"},
                    "schema": {"type": "object", "properties": {
                        "data": {"type": "array", "items": {"type": "object"}},
                    }},
                },
            )},
        )
        with pytest.raises(ValidationError, match="traversal failed"):
            parse_endpoint(payload)


# ---------------------------------------------------------------------------
# §Cross-Field Validation: replication cursor_field exists in record-shape
# ---------------------------------------------------------------------------


class TestCursorFieldsInRecordShape:
    def _payload_with_cursor_field(self, cursor_field, record_props):
        return _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"u": {"from_param": "u"}}},
                "params": {"u": {"in": "query", "type": "string", "required": False, "controlled_by": "replication"}},
                "replication": {
                    "supported_methods": ["incremental"],
                    "cursor_mappings": [{"cursor_field": cursor_field, "param": "u", "operator": "gte"}],
                },
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {"type": "array", "items": {"type": "object", "properties": record_props}},
                },
            }},
        )

    def test_cursor_field_present_accepted(self):
        parse_endpoint(self._payload_with_cursor_field("updated_at", {"updated_at": {"type": "string"}}))

    def test_cursor_field_absent_rejected(self):
        with pytest.raises(ValidationError, match="not declared in response.schema record-shape branch"):
            parse_endpoint(self._payload_with_cursor_field("nonexistent", {"id": {"type": "string"}}))

    def test_dotted_cursor_field_traverses_nested_objects(self):
        parse_endpoint(self._payload_with_cursor_field(
            "metadata.updated_at",
            {"metadata": {"type": "object", "properties": {"updated_at": {"type": "string"}}}},
        ))

    def test_items_missing_rejected(self):
        # `items` not declared on the array — cursor field cannot be verified.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"u": {"from_param": "u"}}},
                "params": {"u": {"in": "query", "type": "string", "required": False, "controlled_by": "replication"}},
                "replication": {
                    "supported_methods": ["incremental"],
                    "cursor_mappings": [{"cursor_field": "updated_at", "param": "u", "operator": "gte"}],
                },
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {"type": "array"},
                },
            }},
        )
        with pytest.raises(ValidationError, match="cannot be verified"):
            parse_endpoint(payload)


# ---------------------------------------------------------------------------
# §Cross-Field Validation: cursor.next_cursor / link.next_url shape
# ---------------------------------------------------------------------------


class TestPaginationExpressions:
    def _cursor_pagination_payload(self, next_cursor):
        return _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"cursor": {"from_param": "cursor"}}},
                "params": {"cursor": {"in": "query", "type": "string", "required": False, "controlled_by": "pagination"}},
                "pagination": {
                    "type": "cursor",
                    "cursor": {"param": "cursor", "next_cursor": next_cursor},
                    "stop_when": {"missing": {"ref": "response.body.next"}},
                },
                "response": {
                    "records": {"ref": "response.body.data"},
                    "schema": {"type": "object", "properties": {
                        "data": {"type": "array", "items": {"type": "object"}},
                    }},
                },
            }},
        )

    def test_next_cursor_with_ref_accepted(self):
        parse_endpoint(self._cursor_pagination_payload({"ref": "response.body.next_cursor"}))

    def test_next_cursor_with_template_accepted(self):
        parse_endpoint(self._cursor_pagination_payload({"template": "${response.body.next_cursor}"}))

    def test_next_cursor_string_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._cursor_pagination_payload("response.body.next_cursor"))

    def test_next_cursor_response_path_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._cursor_pagination_payload({"response_path": "body.next_cursor"}))

    def test_pagination_without_type_rejected(self):
        """Pagination `type` is the REQUIRED union discriminator (#712): a default
        would render `{"const":X,"default":X}` and collapse the union's Zod
        routing. A pagination block omitting `type` must be rejected."""
        payload = self._cursor_pagination_payload({"ref": "response.body.next_cursor"})
        del payload["operations"]["read"]["pagination"]["type"]
        with pytest.raises(ValidationError):
            parse_endpoint(payload)


# ---------------------------------------------------------------------------
# Predicate discriminated union (§Stop Conditions)
# ---------------------------------------------------------------------------


class TestPredicate:
    def _offset_with_stop_when(self, stop_when):
        return _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"o": {"from_param": "o"}}},
                "params": {"o": {"in": "query", "type": "integer", "required": False, "controlled_by": "pagination"}},
                "pagination": {
                    "type": "offset",
                    "offset": {
                        "param": "o",
                        "initial": 0,
                        "increment_by": {"ref": "response.record_count"},
                    },
                    "stop_when": stop_when,
                },
                "response": {
                    "records": {"ref": "response.body.data"},
                    "schema": {"type": "object", "properties": {
                        "data": {"type": "array", "items": {"type": "object"}},
                    }},
                },
            }},
        )

    def test_eq_predicate_accepted(self):
        parse_endpoint(self._offset_with_stop_when({"eq": [{"ref": "response.body.has_more"}, False]}))

    def test_missing_predicate_accepted(self):
        parse_endpoint(self._offset_with_stop_when({"missing": {"ref": "response.body.next"}}))

    def test_nested_and_or_not_accepted(self):
        parse_endpoint(self._offset_with_stop_when({
            "and": [
                {"missing": {"ref": "response.body.next"}},
                {"not": {"empty": {"ref": "response.records"}}},
            ],
        }))

    def test_two_operator_keys_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._offset_with_stop_when({
                "eq": [{"ref": "response.body.has_more"}, False],
                "missing": {"ref": "response.body.next"},
            }))

    def test_zero_operator_keys_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._offset_with_stop_when({}))


# ---------------------------------------------------------------------------
# CursorMapping discriminated union (§Replication)
# ---------------------------------------------------------------------------


class TestCursorMapping:
    def test_single_form_accepted(self):
        cm = SingleCursorMapping(cursor_field="updated_at", param="updated_since", operator="gte")
        assert cm.param == "updated_since"

    def test_window_form_accepted(self):
        cm = WindowCursorMapping(
            cursor_field="updated_at",
            start_param="from", end_param="to",
            start_operator="gte", end_operator="lt",
        )
        assert cm.start_param == "from"

    def test_invalid_cursor_field_pattern_rejected(self):
        with pytest.raises(ValidationError):
            SingleCursorMapping(cursor_field="0bad_path", param="p", operator="gte")

    def test_mixed_form_via_parse_rejected(self):
        # When both forms' fields are present, `Replication._reject_mixed_cursor_forms`
        # raises before discrimination runs (spec §Replication forbids mixing).
        # `TestCursorMappingMixedError` below pins the exact error message;
        # this test only asserts the dispatch is a hard reject.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"a": {"from_param": "a"}, "b": {"from_param": "b"}}},
                "params": {
                    "a": {"in": "query", "type": "string", "required": False, "controlled_by": "replication"},
                    "b": {"in": "query", "type": "string", "required": False, "controlled_by": "replication"},
                },
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {"type": "array", "items": {"type": "object", "properties": {"updated_at": {"type": "string"}}}},
                },
                "replication": {
                    "supported_methods": ["incremental"],
                    "cursor_mappings": [{
                        "cursor_field": "updated_at",
                        "param": "a", "operator": "gte",
                        "start_param": "b",
                    }],
                },
            }},
        )
        with pytest.raises(ValidationError):
            parse_endpoint(payload)


# ---------------------------------------------------------------------------
# Param mutual exclusion + query serialization rules
# ---------------------------------------------------------------------------


class TestParamValidate:
    def test_controlled_by_and_operators_mutex(self):
        with pytest.raises(ValidationError, match="must not declare `operators`"):
            Param(**{
                "in": "query", "type": "string", "required": False,
                "controlled_by": "pagination", "operators": ["eq"],
            })

    def test_query_array_requires_style_and_explode(self):
        with pytest.raises(ValidationError, match="`style` and `explode`"):
            Param(**{"in": "query", "type": "array", "required": False})

    def test_query_string_does_not_require_style(self):
        Param(**{"in": "query", "type": "string", "required": False})

    def test_default_must_not_use_from_input(self):
        with pytest.raises(ValidationError, match="from_input is invalid"):
            Param(**{
                "in": "body", "type": "object", "required": False,
                "default": {"from_input": "record"},
            })


# ---------------------------------------------------------------------------
# §API Endpoint Shape: operations / write modes
# ---------------------------------------------------------------------------


class TestOperationsAtLeastOne:
    def test_empty_operations_rejected(self):
        with pytest.raises(ValidationError, match="at least one of"):
            parse_endpoint(_minimal_api_payload(endpoint_id="x", operations={}))

    def test_empty_write_dict_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(_minimal_api_payload(endpoint_id="x", operations={"write": {}}))

    def test_write_only_accepted(self):
        parse_endpoint(_minimal_api_payload(
            endpoint_id="x",
            operations={"write": {"insert": {
                "request": {"method": "POST", "path": "/v1/x", "headers": {"Content-Type": "application/json"}, "body": {"r": {"from_input": "record"}}},
                "params": {},
                "input": {"schema": {"type": "object"}},
            }}},
        ))

    def test_unknown_write_mode_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(_minimal_api_payload(
                endpoint_id="x",
                operations={"write": {"upsert_with_replace": {
                    "request": {"method": "POST", "path": "/v1/x", "headers": {"Content-Type": "application/json"}, "body": {"r": {"from_input": "record"}}},
                    "params": {},
                    "input": {"schema": {"type": "object"}},
                }}},
            ))


# ---------------------------------------------------------------------------
# §Request Bodies: GET no body, Content-Type can be supplied by transport defaults
# ---------------------------------------------------------------------------


class TestRequestBody:
    def test_get_with_body_rejected(self):
        # A GET read is the `GetReadRequest` branch, which declares no `body`
        # field; extra=forbid rejects it structurally (published as a `oneOf`).
        with pytest.raises(ValidationError, match=r"request\.GET\.body"):
            parse_endpoint(_minimal_api_payload(
                endpoint_id="x",
                operations={"read": {
                    "request": {"method": "GET", "path": "/v1/x", "headers": {"Content-Type": "application/json"}, "body": {"foo": 1}},
                    "params": {},
                    "response": {"records": {"ref": "response.body"}, "schema": {"type": "array", "items": {"type": "object"}}},
                }},
            ))

    def test_write_body_without_content_type_accepted_for_transport_default(self):
        parse_endpoint(_minimal_api_payload(
            endpoint_id="x",
            operations={"write": {"insert": {
                "request": {"method": "POST", "path": "/v1/x", "body": {"r": {"from_input": "record"}}},
                "params": {},
                "input": {"schema": {"type": "object"}},
            }}},
        ))

    def test_non_batched_write_without_from_input_rejected(self):
        with pytest.raises(ValidationError, match="non-batched write request body must reference"):
            parse_endpoint(_minimal_api_payload(
                endpoint_id="x",
                operations={"write": {"insert": {
                    "request": {"method": "POST", "path": "/v1/x", "body": {"static": True}},
                    "params": {},
                    "input": {"schema": {"type": "object"}},
                }}},
            ))

    def test_batched_body_using_record_rejected(self):
        with pytest.raises(ValidationError, match="batched write must not use"):
            parse_endpoint(_minimal_api_payload(
                endpoint_id="x",
                operations={"write": {"insert": {
                    "request": {"method": "POST", "path": "/v1/x", "headers": {"Content-Type": "application/json"}, "body": {"r": {"from_input": "record"}}},
                    "params": {},
                    "input": {"schema": {"type": "object"}},
                    "batching": {"max_records": 100},
                }}},
            ))

    def test_non_batched_body_using_records_rejected(self):
        with pytest.raises(ValidationError, match="non-batched write must not use"):
            parse_endpoint(_minimal_api_payload(
                endpoint_id="x",
                operations={"write": {"insert": {
                    "request": {"method": "POST", "path": "/v1/x", "headers": {"Content-Type": "application/json"}, "body": {"r": {"from_input": "records"}}},
                    "params": {},
                    "input": {"schema": {"type": "object"}},
                }}},
            ))


# ---------------------------------------------------------------------------
# §Cross-Field Validation: from_input record.<field> field existence (#949)
# ---------------------------------------------------------------------------


class TestWriteFromInputFieldExistence:
    """A `from_input: record.<field>` write-body path must address a field the
    write mode's declared `input.schema` contains, wherever the record shape is
    knowable — the same membership rule `conflict_keys` enforces. A shape that
    declares no object `properties` map is unknowable and skipped, not rejected
    (the engine owns the resolved shape at configure time)."""

    @staticmethod
    def _payload(body, properties=None, batching=None):
        schema = {"type": "object"}
        if properties is not None:
            schema["properties"] = properties
        op = {
            "request": {
                "method": "POST",
                "path": "/v1/x",
                "headers": {"Content-Type": "application/json"},
                "body": body,
            },
            "params": {},
            "input": {"schema": schema},
        }
        if batching is not None:
            op["batching"] = batching
        return _minimal_api_payload(endpoint_id="x", operations={"write": {"insert": op}})

    def test_record_field_present_accepted(self):
        parse_endpoint(self._payload(
            body={"contact_email": {"from_input": "record.email"}},
            properties={"email": {"type": "string"}},
        ))

    def test_record_field_absent_rejected(self):
        with pytest.raises(ValidationError, match="undeclared input.schema field"):
            parse_endpoint(self._payload(
                body={"contact_email": {"from_input": "record.emial"}},
                properties={"email": {"type": "string"}},
            ))

    def test_nested_record_field_present_accepted(self):
        parse_endpoint(self._payload(
            body={"city": {"from_input": "record.address.city"}},
            properties={"address": {"type": "object", "properties": {"city": {"type": "string"}}}},
        ))

    def test_nested_record_field_absent_rejected(self):
        with pytest.raises(ValidationError, match=r"record\.address\.zip"):
            parse_endpoint(self._payload(
                body={"z": {"from_input": "record.address.zip"}},
                properties={"address": {"type": "object", "properties": {"city": {"type": "string"}}}},
            ))

    def test_whole_record_not_field_checked(self):
        # `record` addresses the whole record — no field path to verify.
        parse_endpoint(self._payload(
            body={"from_input": "record"},
            properties={"email": {"type": "string"}},
        ))

    def test_batched_records_not_field_checked(self):
        # `records` carries no field path; batched writes require it.
        parse_endpoint(self._payload(
            body={"items": {"from_input": "records"}},
            properties={"email": {"type": "string"}},
            batching={"max_records": 100},
        ))

    def test_unchecked_when_input_schema_has_no_properties(self):
        # No object `properties` map → membership unknowable, engine owns it.
        parse_endpoint(self._payload(
            body={"x": {"from_input": "record.anything"}},
            properties=None,
        ))

    def test_rejected_when_input_schema_has_empty_properties(self):
        # Explicit `properties: {}` declares zero fields — any path is invalid.
        with pytest.raises(ValidationError, match="undeclared input.schema field"):
            parse_endpoint(self._payload(
                body={"x": {"from_input": "record.email"}},
                properties={},
            ))

    def test_unknowable_intermediate_segment_skips(self):
        # `address` is declared but models no `properties` — the nested segment
        # is unknowable, so the path is accepted rather than rejected.
        parse_endpoint(self._payload(
            body={"c": {"from_input": "record.address.city"}},
            properties={"address": {"type": "object"}},
        ))

    def test_boolean_subschema_intermediate_skips(self):
        # A boolean subschema (valid Draft 2020-12) is not walkable — a deeper
        # segment is unknowable, not rejected, and must not crash validation.
        parse_endpoint(self._payload(
            body={"c": {"from_input": "record.meta.tag"}},
            properties={"meta": True},
        ))

    def test_per_field_map_with_one_absent_rejected(self):
        # Per-field placement: every from_input path is checked; one bad path
        # fails the whole template even when a sibling is valid.
        with pytest.raises(ValidationError, match="undeclared input.schema field"):
            parse_endpoint(self._payload(
                body={
                    "e": {"from_input": "record.email"},
                    "n": {"from_input": "record.name"},
                },
                properties={"email": {"type": "string"}},
            ))


# ---------------------------------------------------------------------------
# §Write Modes: upsert conflict_keys (endpoint-declared, #853)
# ---------------------------------------------------------------------------


class TestWriteConflictKeys:
    """The upsert conflict key is an endpoint-declared, provider-defined
    property of the write mode: `upsert` must declare it, other modes must
    not, and the fields must exist in the mode's `input.schema`."""

    @staticmethod
    def _write_op(conflict_keys=None, properties=None):
        op = {
            "request": {
                "method": "POST",
                "path": "/v1/x",
                "headers": {"Content-Type": "application/json"},
                "body": {"r": {"from_input": "record"}},
            },
            "params": {},
            "input": {
                "schema": {
                    "type": "object",
                    "properties": properties
                    if properties is not None
                    else {"email": {"type": "string"}},
                }
            },
        }
        if conflict_keys is not None:
            op["conflict_keys"] = conflict_keys
        return op

    def _payload(self, write):
        return _minimal_api_payload(endpoint_id="x", operations={"write": write})

    def test_upsert_with_conflict_keys_accepted(self):
        parse_endpoint(self._payload({"upsert": self._write_op(conflict_keys=["email"])}))

    def test_upsert_without_conflict_keys_rejected(self):
        with pytest.raises(ValidationError, match="operations.write.upsert.conflict_keys is required"):
            parse_endpoint(self._payload({"upsert": self._write_op()}))

    def test_insert_with_conflict_keys_rejected(self):
        with pytest.raises(ValidationError, match="conflict_keys is not allowed"):
            parse_endpoint(self._payload({"insert": self._write_op(conflict_keys=["email"])}))

    def test_insert_without_conflict_keys_accepted(self):
        parse_endpoint(self._payload({"insert": self._write_op()}))

    def test_composite_conflict_keys_accepted(self):
        parse_endpoint(self._payload({"upsert": self._write_op(
            conflict_keys=["email", "tenant"],
            properties={"email": {"type": "string"}, "tenant": {"type": "string"}},
        )}))

    def test_conflict_keys_must_reference_input_schema_fields(self):
        with pytest.raises(ValidationError, match="unknown input.schema fields"):
            parse_endpoint(self._payload({"upsert": self._write_op(
                conflict_keys=["nope"],
                properties={"email": {"type": "string"}},
            )}))

    def test_conflict_keys_unchecked_when_input_schema_has_no_properties(self):
        # A record schema without an object `properties` map carries no names
        # to check against — membership is unknowable, not violated.
        op = self._write_op(conflict_keys=["anything"])
        op["input"]["schema"] = {"type": "object"}
        parse_endpoint(self._payload({"upsert": op}))

    def test_conflict_keys_rejected_when_input_schema_has_empty_properties(self):
        # An explicit `properties: {}` declares zero fields — distinct from an
        # absent map — so any conflict key is invalid.
        op = self._write_op(conflict_keys=["id"])
        op["input"]["schema"] = {"type": "object", "properties": {}}
        with pytest.raises(ValidationError, match="unknown input.schema fields"):
            parse_endpoint(self._payload({"upsert": op}))

    def test_empty_conflict_keys_rejected(self):
        # Field-level min_length=1 — an empty key set is meaningless.
        with pytest.raises(ValidationError):
            parse_endpoint(self._payload({"upsert": self._write_op(conflict_keys=[])}))

    def test_empty_string_conflict_key_rejected(self):
        # Per-item min_length=1 — an empty field name is meaningless.
        with pytest.raises(ValidationError):
            parse_endpoint(self._payload({"upsert": self._write_op(conflict_keys=["email", ""])}))


class TestPublishedSchemaConflictKeysRule:
    """The PUBLISHED api-endpoint JSON Schema (what external connector authors
    validate against) must enforce the same per-mode `conflict_keys` rule the
    Pydantic model does — including rejecting the field's nullable default
    (`conflict_keys: null`), which `required` alone would let through (#853)."""

    @staticmethod
    def _validator():
        schema = json.loads(LATEST_API_ENDPOINT_SCHEMA_PATH.read_text())
        return Draft202012Validator(schema)

    @staticmethod
    def _doc(mode, conflict_keys="omit"):
        block = {
            "request": {"method": "POST", "path": "/v1/x",
                        "headers": {"Content-Type": "application/json"},
                        "body": {"r": {"from_input": "record"}}},
            "params": {},
            "input": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}},
        }
        if conflict_keys != "omit":
            block["conflict_keys"] = conflict_keys
        return {"$schema": API_SCHEMA_URL, "endpoint_id": "x",
                "operations": {"write": {mode: block}}}

    @pytest.mark.parametrize("mode,conflict_keys,valid", [
        ("upsert", ["email"], True),
        ("upsert", "omit", False),
        ("upsert", None, False),      # nullable default must not satisfy the rule
        ("upsert", [], False),
        ("insert", "omit", True),
        ("insert", None, True),       # null/absent is fine on a non-upsert mode
        ("insert", ["email"], False),
    ])
    def test_published_schema_matches_model(self, mode, conflict_keys, valid):
        errors = list(self._validator().iter_errors(self._doc(mode, conflict_keys)))
        assert (not errors) == valid, (
            f"{mode} conflict_keys={conflict_keys!r}: "
            f"expected {'valid' if valid else 'invalid'}, got {errors[:1]}"
        )
        # The published contract must agree with the source-of-truth model.
        model_ok = True
        try:
            parse_endpoint(self._doc(mode, conflict_keys))
        except ValidationError:
            model_ok = False
        assert model_ok == valid, f"model/schema disagree for {mode} {conflict_keys!r}"


# ---------------------------------------------------------------------------
# §Write Modes: idempotency-key placement (#890)
# ---------------------------------------------------------------------------


class TestWriteIdempotency:
    """`idempotency` declares provider idempotency-key *placement* only (the
    key value is engine-owned): allowed on any write mode, forbidden together
    with `batching` — a per-record key cannot ride a multi-record request."""

    @staticmethod
    def _write_op(idempotency=None, batched=False, conflict_keys=None):
        op = {
            "request": {
                "method": "POST",
                "path": "/v1/x",
                "headers": {"Content-Type": "application/json"},
                "body": {"r": {"from_input": "records" if batched else "record"}},
            },
            "params": {},
            "input": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}},
        }
        if idempotency is not None:
            op["idempotency"] = idempotency
        if batched:
            op["batching"] = {"max_records": 100}
        if conflict_keys is not None:
            op["conflict_keys"] = conflict_keys
        return op

    def _payload(self, write):
        return _minimal_api_payload(endpoint_id="x", operations={"write": write})

    def test_header_idempotency_on_insert_accepted(self):
        parse_endpoint(self._payload({"insert": self._write_op(
            idempotency={"in": "header", "name": "Idempotency-Key"})}))

    def test_body_idempotency_on_insert_accepted(self):
        parse_endpoint(self._payload({"insert": self._write_op(
            idempotency={"in": "body", "name": "idempotency_key"})}))

    def test_idempotency_on_upsert_accepted(self):
        # Not insert-only: some providers require the key on upsert too
        # (e.g. Square UpsertCatalogObject).
        parse_endpoint(self._payload({"upsert": self._write_op(
            idempotency={"in": "header", "name": "Idempotency-Key"},
            conflict_keys=["email"])}))

    def test_idempotency_with_batching_rejected(self):
        with pytest.raises(ValidationError, match="idempotency cannot be combined with batching"):
            parse_endpoint(self._payload({"insert": self._write_op(
                idempotency={"in": "header", "name": "Idempotency-Key"}, batched=True)}))

    def test_batching_without_idempotency_accepted(self):
        parse_endpoint(self._payload({"insert": self._write_op(batched=True)}))

    def test_unknown_in_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._payload({"insert": self._write_op(
                idempotency={"in": "query", "name": "K"})}))

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._payload({"insert": self._write_op(
                idempotency={"in": "header", "name": ""})}))

    def test_missing_name_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._payload({"insert": self._write_op(
                idempotency={"in": "header"})}))

    def test_value_expression_rejected(self):
        # The key value is engine-owned; a `value` (or any other) key must not
        # be authorable — closed contract.
        with pytest.raises(ValidationError):
            parse_endpoint(self._payload({"insert": self._write_op(
                idempotency={"in": "header", "name": "K", "value": {"ref": "record.id"}})}))

    def test_body_idempotency_with_non_object_body_rejected(self):
        # A list body template can never resolve to a JSON object, so there is
        # no top-level field to inject the key into — fail at author time.
        op = self._write_op(idempotency={"in": "body", "name": "idempotency_key"})
        op["request"]["body"] = [{"from_input": "record"}]
        with pytest.raises(ValidationError, match="requires the write request body template"):
            parse_endpoint(self._payload({"insert": op}))

    def test_body_idempotency_with_whole_record_body_accepted(self):
        # `{"from_input": "record"}` resolves to the record: input.schema
        # declares `type: object` and no field named `idempotency_key`, so
        # both static rules pass.
        op = self._write_op(idempotency={"in": "body", "name": "idempotency_key"})
        op["request"]["body"] = {"from_input": "record"}
        parse_endpoint(self._payload({"insert": op}))

    def test_body_idempotency_colliding_with_record_field_rejected(self):
        # Whole-record body whose input.schema declares a field with the
        # idempotency name — the record's own value would race the injected
        # engine-owned key.
        op = self._write_op(idempotency={"in": "body", "name": "idempotency_key"})
        op["request"]["body"] = {"from_input": "record"}
        op["input"]["schema"]["properties"]["idempotency_key"] = {"type": "string"}
        with pytest.raises(ValidationError, match="declared field of the record"):
            parse_endpoint(self._payload({"insert": op}))

    def test_body_idempotency_with_scalar_record_field_body_rejected(self):
        # `{"from_input": "record.email"}` resolves to a string per
        # input.schema — a scalar body has no top-level field to inject into.
        op = self._write_op(idempotency={"in": "body", "name": "idempotency_key"})
        op["request"]["body"] = {"from_input": "record.email"}
        with pytest.raises(ValidationError, match="resolves to input.schema type 'string'"):
            parse_endpoint(self._payload({"insert": op}))

    def test_body_idempotency_with_object_record_field_body_accepted(self):
        op = self._write_op(idempotency={"in": "body", "name": "idempotency_key"})
        op["request"]["body"] = {"from_input": "record.payload"}
        op["input"]["schema"]["properties"]["payload"] = {
            "type": "object", "properties": {"email": {"type": "string"}},
        }
        parse_endpoint(self._payload({"insert": op}))

    def test_body_idempotency_with_decorated_expression_body_rejected(self):
        # An `x-*` sibling does not demote an expression body to a literal
        # template (§Extension Policy singleton tolerance) — the resolved-shape
        # rules still apply.
        op = self._write_op(idempotency={"in": "body", "name": "idempotency_key"})
        op["request"]["body"] = {"from_input": "record.email", "x-note": "decorated"}
        with pytest.raises(ValidationError, match="resolves to input.schema type 'string'"):
            parse_endpoint(self._payload({"insert": op}))

    def test_body_idempotency_with_unknowable_record_field_body_accepted(self):
        # input.schema declares no `properties` map, so the addressed field's
        # resolved shape is unknowable — both the idempotency static rules and
        # the record-field existence check (#949) defer to the engine's
        # configure-time gate. (A field absent from a *declared* properties map
        # is instead rejected up front; see TestWriteFromInputFieldExistence.)
        op = self._write_op(idempotency={"in": "body", "name": "idempotency_key"})
        op["request"]["body"] = {"from_input": "record.undeclared"}
        op["input"]["schema"] = {"type": "object"}
        parse_endpoint(self._payload({"insert": op}))

    def test_body_idempotency_colliding_with_body_template_key_rejected(self):
        op = self._write_op(idempotency={"in": "body", "name": "idempotency_key"})
        op["request"]["body"] = {"idempotency_key": "authored", "r": {"from_input": "record"}}
        with pytest.raises(ValidationError, match="top-level key of the request body template"):
            parse_endpoint(self._payload({"insert": op}))

    def test_header_idempotency_colliding_with_request_headers_rejected(self):
        # Case-insensitive, matching HTTP header semantics.
        op = self._write_op(idempotency={"in": "header", "name": "Idempotency-Key"})
        op["request"]["headers"]["idempotency-key"] = {"from_param": "key"}
        op["params"] = {"key": {"in": "header", "type": "string", "required": True,
                                "default": {"ref": "connection.selections.k"}}}
        with pytest.raises(ValidationError, match="also declared\\s+in request.headers"):
            parse_endpoint(self._payload({"insert": op}))

    def test_header_idempotency_with_unrelated_headers_accepted(self):
        # The helper already declares Content-Type; only a same-name header is
        # a collision.
        parse_endpoint(self._payload({"insert": self._write_op(
            idempotency={"in": "header", "name": "Idempotency-Key"})}))


class TestPublishedSchemaIdempotencyRule:
    """The PUBLISHED api-endpoint JSON Schema must enforce the same
    idempotency rules the Pydantic model does (#890) — the shape itself and
    the idempotency×batching exclusion, including agreeing with the model on
    the fields' nullable defaults (`idempotency: null` / `batching: null`)."""

    @staticmethod
    def _validator():
        schema = json.loads(LATEST_API_ENDPOINT_SCHEMA_PATH.read_text())
        return Draft202012Validator(schema)

    @staticmethod
    def _doc(idempotency="omit", batching="omit", body="default"):
        batched = isinstance(batching, dict)
        block = {
            "request": {"method": "POST", "path": "/v1/x",
                        "headers": {"Content-Type": "application/json"}},
            "params": {},
            "input": {"schema": {"type": "object", "properties": {"email": {"type": "string"}}}},
        }
        if body != "omit":
            block["request"]["body"] = (
                body if body != "default"
                else {"r": {"from_input": "records" if batched else "record"}}
            )
        if idempotency != "omit":
            block["idempotency"] = idempotency
        if batching != "omit":
            block["batching"] = batching
        return {"$schema": API_SCHEMA_URL, "endpoint_id": "x",
                "operations": {"write": {"insert": block}}}

    @pytest.mark.parametrize("idempotency,batching,valid", [
        ("omit", "omit", True),
        ({"in": "header", "name": "Idempotency-Key"}, "omit", True),
        ({"in": "body", "name": "idempotency_key"}, "omit", True),
        ("omit", {"max_records": 100}, True),
        ({"in": "header", "name": "Idempotency-Key"}, {"max_records": 100}, False),
        (None, {"max_records": 100}, True),   # nullable default ≠ declared placement
        ({"in": "header", "name": "Idempotency-Key"}, None, True),
        ({"in": "query", "name": "K"}, "omit", False),
        ({"in": "header", "name": ""}, "omit", False),
        ({"in": "header"}, "omit", False),
        ({"name": "Idempotency-Key"}, "omit", False),
        # Closed contract: the key value is engine-owned, so a smuggled `value`
        # expression must be unauthorable in the published schema too.
        ({"in": "header", "name": "K", "value": {"ref": "record.id"}}, "omit", False),
    ])
    def test_published_schema_matches_model(self, idempotency, batching, valid):
        self._assert_agreement(self._doc(idempotency, batching), valid,
                               f"idempotency={idempotency!r} batching={batching!r}")

    @pytest.mark.parametrize("idempotency,body,valid", [
        # Body-placement guard: a literal non-object body template is
        # unauthorable in the published schema too (template-level only —
        # expression bodies stay with the model/engine).
        ({"in": "body", "name": "idempotency_key"}, [{"from_input": "record"}], False),
        ({"in": "header", "name": "Idempotency-Key"}, [{"from_input": "record"}], True),
        # An absent body is not an object to inject into — `properties` alone
        # is vacuous on absent keys, so the guard branch requires `body`.
        ({"in": "body", "name": "idempotency_key"}, "omit", False),
    ])
    def test_published_schema_body_guard_matches_model(self, idempotency, body, valid):
        self._assert_agreement(self._doc(idempotency, body=body), valid,
                               f"idempotency={idempotency!r} body={body!r}")

    def _assert_agreement(self, doc, valid, label):
        errors = list(self._validator().iter_errors(doc))
        assert (not errors) == valid, (
            f"{label}: expected {'valid' if valid else 'invalid'}, got {errors[:1]}"
        )
        # The published contract must agree with the source-of-truth model.
        model_ok = True
        try:
            parse_endpoint(doc)
        except ValidationError:
            model_ok = False
        assert model_ok == valid, f"model/schema disagree for {label}"


# ---------------------------------------------------------------------------
# §Request Parameter Binding: path placeholders
# ---------------------------------------------------------------------------


class TestRequestPathPlaceholders:
    def test_placeholder_with_matching_path_params_accepted(self):
        parse_endpoint(_minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {
                    "method": "GET",
                    "path": "/v1/{account_id}/transactions",
                    "path_params": {"account_id": {"from_param": "account_id"}},
                },
                "params": {"account_id": {"in": "path", "type": "string", "required": True, "default": {"ref": "connection.selections.account_id"}}},
                "response": {"records": {"ref": "response.body"}, "schema": {"type": "array", "items": {"type": "object"}}},
            }},
        ))

    def test_duplicate_placeholder_rejected(self):
        with pytest.raises(ValidationError, match="duplicate placeholders"):
            parse_endpoint(_minimal_api_payload(
                endpoint_id="x",
                operations={"read": {
                    "request": {"method": "GET", "path": "/v1/{a}/{a}", "path_params": {"a": {"from_param": "a"}}},
                    "params": {"a": {"in": "path", "type": "string", "required": True, "default": {"ref": "connection.selections.x"}}},
                    "response": {"records": {"ref": "response.body"}, "schema": {"type": "array", "items": {"type": "object"}}},
                }},
            ))

    def test_dollar_brace_template_in_path_rejected(self):
        with pytest.raises(ValidationError, match=r"\$\{...\} template"):
            parse_endpoint(_minimal_api_payload(
                endpoint_id="x",
                operations={"read": {
                    "request": {"method": "GET", "path": "/v1/${account_id}"},
                    "params": {},
                    "response": {"records": {"ref": "response.body"}, "schema": {"type": "array", "items": {"type": "object"}}},
                }},
            ))


# ---------------------------------------------------------------------------
# DatabaseEndpointDoc cross-field validation
# ---------------------------------------------------------------------------


class TestDatabaseCrossField:
    def test_duplicate_column_names_rejected(self):
        with pytest.raises(ValidationError, match=r"ADV-DBEP-001|name must be unique"):
            parse_endpoint(_minimal_database_payload(
                columns=[
                    {"name": "id", "native_type": "uuid", "arrow_type": "Utf8"},
                    {"name": "id", "native_type": "uuid", "arrow_type": "Utf8"},
                ],
            ))

    def test_duplicate_ordinal_position_rejected(self):
        with pytest.raises(ValidationError, match="ordinal_position"):
            parse_endpoint(_minimal_database_payload(
                columns=[
                    {"name": "id", "native_type": "uuid", "arrow_type": "Utf8", "ordinal_position": 1},
                    {"name": "name", "native_type": "varchar", "arrow_type": "Utf8", "ordinal_position": 1},
                ],
            ))

    def test_primary_keys_unknown_column_rejected(self):
        with pytest.raises(ValidationError, match=r"ADV-DBEP-003|primary_keys must reference"):
            parse_endpoint(_minimal_database_payload(primary_keys=["missing"]))

    def test_invalid_arrow_type_case_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(_minimal_database_payload(
                columns=[{"name": "id", "native_type": "uuid", "arrow_type": "utf8"}],
            ))

    def test_missing_column_arrow_type_rejected(self):
        with pytest.raises(ValidationError, match="arrow_type"):
            parse_endpoint(_minimal_database_payload(
                columns=[{"name": "id", "native_type": "uuid"}],
            ))

    @pytest.mark.parametrize("bare", [
        "Timestamp", "Decimal128", "Decimal256",
        "Time32", "Time64", "FixedSizeBinary",
        "Duration", "Interval",
    ])
    def test_bare_parameterized_arrow_type_rejected(self, bare):
        with pytest.raises(ValidationError, match="arrow_type"):
            parse_endpoint(_minimal_database_payload(
                columns=[{"name": "c", "native_type": "x", "arrow_type": bare}],
            ))

    @pytest.mark.parametrize("canonical", [
        "Utf8", "LargeUtf8", "Boolean", "Int8", "Int32", "Int64", "UInt64",
        "Float32", "Float64", "Date32", "Date64", "Binary", "LargeBinary",
        "Timestamp(MICROSECOND)",
        "Timestamp(MICROSECOND, UTC)",
        "Timestamp(NANOSECOND, null)",
        "Timestamp(SECOND, Etc/GMT+5)",
        "Timestamp(MILLISECOND, +05:30)",
        "Decimal128(38, 9)",
        "Decimal128(1, -2)",
        "Decimal256(76, 0)",
        "Time32(SECOND)",
        "Time32(MILLISECOND)",
        "Time64(MICROSECOND)",
        "Time64(NANOSECOND)",
        "FixedSizeBinary(16)",
        "Duration(MICROSECOND)",
        "Interval(YEAR_MONTH)",
        "List<Int64>",
        "Struct<id:Int64,name:Utf8>",
        "Map<Utf8, Int64>",
    ])
    def test_canonical_arrow_type_accepted(self, canonical):
        parse_endpoint(_minimal_database_payload(
            columns=[{"name": "c", "native_type": "x", "arrow_type": canonical}],
        ))

    @pytest.mark.parametrize("bad", [
        "Decimal128(38)",        # missing scale
        "Decimal128(40, 0)",     # precision > 38
        "Decimal256(80, 0)",     # precision > 76
        "Time32(MICROSECOND)",   # wrong unit for Time32
        "Time64(SECOND)",        # wrong unit for Time64
        "FixedSizeBinary(0)",    # n must be ≥ 1
        "Timestamp()",           # empty parens
        "Int128",                # unknown base
        "decimal128(38, 9)",     # wrong case
    ])
    def test_invalid_arrow_type_forms_rejected(self, bad):
        with pytest.raises(ValidationError):
            parse_endpoint(_minimal_database_payload(
                columns=[{"name": "c", "native_type": "x", "arrow_type": bad}],
            ))

    def test_explicit_null_catalog_rejected(self):
        payload = _minimal_database_payload()
        payload["database_object"]["catalog"] = None
        with pytest.raises(ValidationError, match="explicit null is invalid"):
            parse_endpoint(payload)

    def test_explicit_null_schema_rejected(self):
        payload = _minimal_database_payload()
        payload["database_object"]["schema"] = None
        with pytest.raises(ValidationError, match="explicit null is invalid"):
            parse_endpoint(payload)


# ---------------------------------------------------------------------------
# ApiEndpointDoc name regex
# ---------------------------------------------------------------------------


class TestApiEndpointName:
    def test_uppercase_name_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(_minimal_api_payload(endpoint_id="BadName"))

    def test_leading_underscore_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(_minimal_api_payload(endpoint_id="_bad"))

    def test_lowercase_alphanum_accepted(self):
        parse_endpoint(_minimal_api_payload(endpoint_id="transfers_v2"))


# ---------------------------------------------------------------------------
# Issue #424 — canonical arrow_type inside API response.schema / input.schema
# ---------------------------------------------------------------------------


def _api_payload_with_response_schema(response_schema):
    """Wrap a JSON-Schema body in a minimal API read-endpoint payload."""
    return _minimal_api_payload(operations={
        "read": {
            "request": {"method": "GET", "path": "/v1/records"},
            "params": {},
            "response": {
                "records": {"ref": "response.body.data"},
                "schema": response_schema,
            },
        },
    })


def _api_payload_with_write_input_schema(input_schema):
    """Wrap a JSON-Schema body in a minimal API write-endpoint payload."""
    return _minimal_api_payload(operations={
        "write": {
            "insert": {
                "request": {"method": "POST", "path": "/v1/records", "body": {"from_input": "record"}},
                "params": {},
                "input": {"schema": input_schema},
            },
        },
    })


class TestApiSchemaArrowType:
    def test_bare_timestamp_in_response_schema_rejected(self):
        payload = _api_payload_with_response_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "updated_at": {
                                "type": "string",
                                "native_type": "datetime",
                                "arrow_type": "Timestamp",
                            },
                        },
                    },
                },
            },
        })
        with pytest.raises(ValidationError, match="canonical Arrow type"):
            parse_endpoint(payload)

    def test_parameterized_timestamp_in_response_schema_accepted(self):
        parse_endpoint(_api_payload_with_response_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "updated_at": {
                                "type": "string",
                                "native_type": "datetime",
                                "arrow_type": "Timestamp(MICROSECOND, UTC)",
                            },
                        },
                    },
                },
            },
        }))

    def test_native_type_without_arrow_type_rejected(self):
        payload = _api_payload_with_response_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "native_type": "money",
                },
            },
        })
        with pytest.raises(ValidationError, match="native_type/arrow_type"):
            parse_endpoint(payload)

    def test_arrow_type_without_native_type_rejected(self):
        payload = _api_payload_with_response_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "arrow_type": "Utf8",
                },
            },
        })
        with pytest.raises(ValidationError, match="native_type/arrow_type"):
            parse_endpoint(payload)

    def test_bare_decimal_in_write_input_schema_rejected(self):
        payload = _api_payload_with_write_input_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "native_type": "money",
                    "arrow_type": "Decimal128",
                },
            },
        })
        with pytest.raises(ValidationError, match="canonical Arrow type"):
            parse_endpoint(payload)

    def test_canonical_decimal_in_write_input_schema_accepted(self):
        parse_endpoint(_api_payload_with_write_input_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "native_type": "money",
                    "arrow_type": "Decimal128(38, 9)",
                },
            },
        }))

    def test_nested_oneOf_branch_with_bare_arrow_type_rejected(self):
        # The walker must recurse through `oneOf` branches too.
        payload = _api_payload_with_response_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "value": {
                    "oneOf": [
                        {"type": "null"},
                        {
                            "type": "string",
                            "native_type": "datetime",
                            "arrow_type": "Timestamp",
                        },
                    ],
                },
            },
        })
        with pytest.raises(ValidationError, match="canonical Arrow type"):
            parse_endpoint(payload)

    # One bare-arrow_type case per JSON Schema schema-bearing keyword
    # category, so that a typo in _JSON_SCHEMA_*_KEYS or a missed traversal
    # branch shows up as a hard test failure.
    @pytest.mark.parametrize("wrapper", [
        # List-of-schema keywords (one each)
        {"allOf": [{"type": "object", "properties": {"x": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }}}]},
        {"anyOf": [{"type": "object", "properties": {"x": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }}}]},
        {"prefixItems": [{
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }]},
        # Map-of-schema keywords
        {"patternProperties": {"^a": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }}},
        {"$defs": {"MyDef": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }}},
        {"definitions": {"MyDef": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }}},
        {"dependentSchemas": {"x": {"type": "object", "properties": {"y": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }}}}},
        # Single-schema keywords
        {"contains": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }},
        {"additionalProperties": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }},
        {"if": {"type": "object", "properties": {"x": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }}}},
        {"then": {"type": "object", "properties": {"x": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }}}},
        {"else": {"type": "object", "properties": {"x": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }}}},
        {"not": {"type": "object", "properties": {"x": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }}}},
        {"propertyNames": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }},
        {"unevaluatedItems": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }},
        {"unevaluatedProperties": {
            "type": "string", "native_type": "datetime", "arrow_type": "Timestamp",
        }},
    ])
    def test_walker_recurses_every_schema_bearing_keyword(self, wrapper):
        payload = _api_payload_with_response_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            **wrapper,
        })
        with pytest.raises(ValidationError, match="canonical Arrow type"):
            parse_endpoint(payload)

    def test_walker_accepts_boolean_true_schema(self):
        # JSON Schema 2020-12: `true` is a valid whole-schema short-form
        # ("anything"). Authors using `additionalProperties: true` or a
        # boolean-true sub-schema elsewhere shouldn't have those blocks
        # tripped by the walker.
        parse_endpoint(_api_payload_with_response_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "id": {
                                "type": "string",
                                "native_type": "uuid",
                                "arrow_type": "Utf8",
                            },
                        },
                    },
                },
            },
        }))

    def test_walker_rejects_non_dict_non_bool_in_schema_slot(self):
        # `items: "Int64"` is malformed JSON Schema — string in a schema
        # position. The walker surfaces it explicitly rather than silently
        # skipping.
        payload = _api_payload_with_response_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "data": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nested": {"type": "array", "items": "Int64"},
                        },
                    },
                },
            },
        })
        with pytest.raises(ValidationError, match="not a JSON Schema"):
            parse_endpoint(payload)

    def test_walker_rejects_arrow_type_with_trailing_newline(self):
        # `re.match` would accept "Utf8\n" because `$` matches before a
        # trailing newline; fullmatch closes that gap.
        payload = _api_payload_with_response_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "native_type": "uuid",
                    "arrow_type": "Utf8\n",
                },
            },
        })
        with pytest.raises(ValidationError, match="canonical Arrow type"):
            parse_endpoint(payload)

    def test_null_native_type_treated_as_absent(self):
        # `{"native_type": null, "arrow_type": "Utf8"}` should be flagged
        # as unpaired (null counts as "not declared"), not silently
        # accepted just because the key is present.
        payload = _api_payload_with_response_schema({
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "native_type": None,
                    "arrow_type": "Utf8",
                },
            },
        })
        with pytest.raises(ValidationError, match="native_type/arrow_type"):
            parse_endpoint(payload)


# ---------------------------------------------------------------------------
# Frozen instance behavior
# ---------------------------------------------------------------------------


class TestFrozen:
    def test_constructed_instance_rejects_mutation(self):
        result = parse_endpoint(_minimal_database_payload())
        with pytest.raises(ValidationError):
            result.name = "mutated"

    def test_nested_instance_rejects_mutation(self):
        result = parse_endpoint(_minimal_database_payload())
        with pytest.raises(ValidationError):
            result.database_object.name = "mutated"


# ---------------------------------------------------------------------------
# Wire-format round-trip via model_dump(): default must use aliases
# ---------------------------------------------------------------------------


class TestWireFormatRoundTrip:
    def test_database_payload_round_trips_via_default_dump(self):
        original = DatabaseEndpointDoc.model_validate(_minimal_database_payload())
        # `exclude_none=True` because optional fields like
        # `database_object.{catalog,schema}` must not surface as `null` per spec.
        re_parsed = DatabaseEndpointDoc.model_validate(original.model_dump(exclude_none=True))
        assert isinstance(re_parsed, DatabaseEndpointDoc)
        assert re_parsed.database_object.schema_ == "public"

    def test_api_payload_round_trips_via_default_dump(self):
        original = ApiEndpointDoc.model_validate(_minimal_api_payload())
        re_parsed = ApiEndpointDoc.model_validate(original.model_dump())
        assert isinstance(re_parsed, ApiEndpointDoc)
        # `$schema` alias must round-trip cleanly (without serialize_by_alias=True
        # the dump would emit `schema_url` and re-parse would fail).
        assert re_parsed.schema_url == API_SCHEMA_URL

    def test_dump_emits_dollar_schema_alias(self):
        result = DatabaseEndpointDoc.model_validate(_minimal_database_payload())
        dumped = result.model_dump()
        assert "$schema" in dumped
        assert "schema_url" not in dumped


# ---------------------------------------------------------------------------
# x-* siblings on operator/expression dicts
# ---------------------------------------------------------------------------


class TestExtensionOnNestedDictsClosed:
    """Closed contract: `x-*` siblings on operator/expression dicts are rejected."""

    def test_x_extension_on_predicate_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            PredicateEq.model_validate({"eq": [{"ref": "response.body.has_more"}, False], "x-vendor": "wise"})

    def test_x_extension_on_expression_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RefExpression.model_validate({"ref": "response.body.next", "x-debug": True})


# ---------------------------------------------------------------------------
# Predicate construction via Python attribute and alias forms
# ---------------------------------------------------------------------------


class TestPredicateConstruction:
    def test_construct_via_python_attribute(self):
        # populate_by_name=True permits Python attr names.
        p = PredicateAnd(and_=[PredicateEq(eq=[1, 2]), PredicateMissing(missing={"ref": "response.body.next"})])
        assert len(p.and_) == 2

    def test_construct_via_alias(self):
        p = PredicateAnd(**{"and": [PredicateEq(eq=[1, 2]), PredicateEq(eq=[3, 4])]})
        assert len(p.and_) == 2

    def test_predicate_round_trip_by_default_dump(self):
        p = PredicateNot(not_=PredicateMissing(missing={"ref": "response.body.next"}))
        # serialize_by_alias=True default → dump uses wire-format `not`/`missing`.
        assert "not" in p.model_dump()
        assert "not_" not in p.model_dump()


# ---------------------------------------------------------------------------
# Malformed expression dicts in request bindings (F2 fix)
# ---------------------------------------------------------------------------


class TestExpressionShapeValidation:
    def test_from_param_with_non_x_sibling_in_query_rejected(self):
        # `_validate_expression_shapes` catches malformed expression dicts
        # that the singleton-check would silently drop.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"p": {"in": "query", "type": "string", "required": False, "operators": ["eq"]}},
                request_extras={"query": {"foo": {"from_param": "p", "rogue": 1}}},
            )},
        )
        with pytest.raises(ValidationError, match="unexpected siblings"):
            parse_endpoint(payload)

    def test_ref_with_non_x_sibling_in_header_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"h": {"in": "header", "type": "string", "required": False, "operators": ["eq"]}},
                request_extras={"headers": {"X-Token": {"ref": "secrets.api_key", "rogue": 1}}},
            )},
        )
        with pytest.raises(ValidationError, match="unexpected siblings"):
            parse_endpoint(payload)

    def test_multiple_expression_keys_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"p": {"in": "query", "type": "string", "required": False, "operators": ["eq"]}},
                request_extras={"query": {"foo": {"ref": "x", "template": "y"}}},
            )},
        )
        with pytest.raises(ValidationError, match="multiple expression keys"):
            parse_endpoint(payload)

    def test_x_sibling_on_expression_in_request_accepted(self):
        # The recognizer tolerates x-* siblings on otherwise-singleton expressions.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"p": {"in": "query", "type": "string", "required": False, "operators": ["eq"]}},
                request_extras={"query": {"foo": {"from_param": "p", "x-vendor": "wise"}}},
            )},
        )
        parse_endpoint(payload)


# ---------------------------------------------------------------------------
# Stream/state/runtime ref leak detection (`_has_disallowed_dynamic_refs`)
# ---------------------------------------------------------------------------


class TestDisallowedDynamicRefs:
    def test_stream_ref_in_header_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"h": {"in": "header", "type": "string", "required": False, "operators": ["eq"]}},
                request_extras={"headers": {"X-Token": {"ref": "stream.api_key"}}},
            )},
        )
        with pytest.raises(ValidationError, match="direct stream/state/runtime ref"):
            parse_endpoint(payload)

    def test_state_ref_in_query_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"p": {"in": "query", "type": "string", "required": False, "operators": ["eq"]}},
                request_extras={"query": {"foo": {"ref": "state.last_run"}}},
            )},
        )
        with pytest.raises(ValidationError, match="direct stream/state/runtime ref"):
            parse_endpoint(payload)

    def test_runtime_ref_nested_in_body_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"write": {"insert": {
                "request": {
                    "method": "POST", "path": "/v1/x",
                    "headers": {"Content-Type": "application/json"},
                    "body": {"records": [{"r": {"from_input": "record"}, "rid": {"ref": "runtime.invocation_id"}}]},
                },
                "params": {},
                "input": {"schema": {"type": "object"}},
            }}},
        )
        with pytest.raises(ValidationError, match="direct stream/state/runtime ref"):
            parse_endpoint(payload)


# ---------------------------------------------------------------------------
# `from_input: 'records.<dotted>'` rejection
# ---------------------------------------------------------------------------


class TestFromInputRecordsDotted:
    def test_records_dotted_path_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"write": {"insert": {
                "request": {
                    "method": "POST", "path": "/v1/x",
                    "headers": {"Content-Type": "application/json"},
                    "body": {"r": {"from_input": "records.id"}},
                },
                "params": {},
                "input": {"schema": {"type": "object"}},
                "batching": {"max_records": 50},
            }}},
        )
        with pytest.raises(ValidationError, match="dotted paths through batch arrays"):
            parse_endpoint(payload)


# ---------------------------------------------------------------------------
# Pagination strategy diversity (page / link / keyset)
# ---------------------------------------------------------------------------


class TestPaginationStrategies:
    def test_page_strategy_accepted(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"page": {"from_param": "p"}}},
                "params": {"p": {"in": "query", "type": "integer", "required": False, "controlled_by": "pagination"}},
                "pagination": {
                    "type": "page",
                    "page": {"param": "p", "initial": 1},
                    "stop_when": {"empty": {"ref": "response.records"}},
                },
                "response": {
                    "records": {"ref": "response.body.data"},
                    "schema": {"type": "object", "properties": {
                        "data": {"type": "array", "items": {"type": "object"}},
                    }},
                },
            }},
        )
        parse_endpoint(payload)

    def test_link_strategy_accepted(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x"},
                "params": {},
                "pagination": {
                    "type": "link",
                    "link": {"next_url": {"ref": "response.body.links.next"}},
                    "stop_when": {"missing": {"ref": "response.body.links.next"}},
                },
                "response": {
                    "records": {"ref": "response.body.data"},
                    "schema": {"type": "object", "properties": {
                        "data": {"type": "array", "items": {"type": "object"}},
                    }},
                },
            }},
        )
        parse_endpoint(payload)

    def test_keyset_strategy_accepted(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"after": {"from_param": "after"}}},
                "params": {"after": {"in": "query", "type": "string", "required": False, "controlled_by": "pagination"}},
                "pagination": {
                    "type": "keyset",
                    "keyset": {"param": "after", "order_by_field": "id"},
                    "stop_when": {"empty": {"ref": "response.records"}},
                },
                "response": {
                    "records": {"ref": "response.body.data"},
                    "schema": {"type": "object", "properties": {
                        "data": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}},
                    }},
                },
            }},
        )
        parse_endpoint(payload)

    def test_keyset_invalid_order_by_field_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"after": {"from_param": "after"}}},
                "params": {"after": {"in": "query", "type": "string", "required": False, "controlled_by": "pagination"}},
                "pagination": {
                    "type": "keyset",
                    "keyset": {"param": "after", "order_by_field": "0bad.path"},
                    "stop_when": {"empty": {"ref": "response.records"}},
                },
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {"type": "array", "items": {"type": "object"}},
                },
            }},
        )
        with pytest.raises(ValidationError):
            parse_endpoint(payload)

    @staticmethod
    def _offset_payload(offset):
        return _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"o": {"from_param": "o"}}},
                "params": {"o": {"in": "query", "type": "integer", "required": False, "controlled_by": "pagination"}},
                "pagination": {
                    "type": "offset",
                    "offset": offset,
                    "stop_when": {"empty": {"ref": "response.records"}},
                },
                "response": {
                    "records": {"ref": "response.body.data"},
                    "schema": {"type": "object", "properties": {
                        "data": {"type": "array", "items": {"type": "object"}},
                    }},
                },
            }},
        )

    def test_offset_increment_by_literal_accepted(self):
        # Fixed step / page-index-style offsets: a positive-integer literal.
        parse_endpoint(self._offset_payload({"param": "o", "initial": 0, "increment_by": 50}))
        parse_endpoint(self._offset_payload({"param": "o", "initial": 1, "increment_by": 1}))

    def test_offset_increment_by_record_count_ref_accepted(self):
        # Family A — `offset` counts records returned: step by the page's row count.
        parse_endpoint(self._offset_payload(
            {"param": "o", "initial": 0, "increment_by": {"ref": "response.record_count"}}
        ))

    def test_offset_increment_by_batch_size_ref_accepted(self):
        # Family B — `offset` counts the requested window: step by the effective limit.
        parse_endpoint(self._offset_payload(
            {"param": "o", "initial": 0, "increment_by": {"ref": "runtime.batch_size"}}
        ))

    def test_offset_increment_by_required(self):
        # No default is correct for both offset families, so the field is required.
        with pytest.raises(ValidationError, match="increment_by"):
            parse_endpoint(self._offset_payload({"param": "o", "initial": 0}))

    def test_offset_increment_by_literal_zero_rejected(self):
        # A bare-int literal 0 would re-request the same offset forever; the
        # `gt=0` guard on the int branch rejects it. (A `{literal: 0}` value
        # expression is opaque to this shape contract — see the runtime-gate
        # test below.)
        with pytest.raises(ValidationError, match="greater than 0"):
            parse_endpoint(self._offset_payload({"param": "o", "initial": 0, "increment_by": 0}))

    def test_offset_increment_by_non_integer_literal_rejected(self):
        # A non-integer numeric literal is rejected. The int branch is lax
        # (Pydantic's standard `int` coercion), so a stringy/bool/integer-valued
        # number would be coerced — the published JSON Schema (`type: integer`)
        # is the tighter author-time gate for those, and the authoritative
        # validator stays a superset of it (no "passes schema, rejected on
        # ingest").
        for bad in (1.5, 2.5):
            with pytest.raises(ValidationError):
                parse_endpoint(self._offset_payload(
                    {"param": "o", "initial": 0, "increment_by": bad}
                ))

    def test_offset_increment_by_unknown_scope_ref_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._offset_payload(
                {"param": "o", "initial": 0, "increment_by": {"ref": "bogus.path"}}
            ))

    def test_offset_increment_by_arbitrary_dict_rejected(self):
        # A dict bearing no expression key selects no `Expression` branch (the
        # discriminator returns None) and is not the int branch — rejected.
        with pytest.raises(ValidationError):
            parse_endpoint(self._offset_payload(
                {"param": "o", "initial": 0, "increment_by": {"foo": "bar"}}
            ))

    def test_offset_increment_by_literal_zero_accepted_at_contract_layer(self):
        # Boundary documentation, not an endorsement: a `{literal: 0}` (or a
        # `{ref}`/`{function}` that resolves to 0) passes the *shape* contract —
        # this model does not evaluate expression values (consistent with every
        # other Expression-typed field). Resolved-value positivity is the
        # engine's runtime gate (analitiq-engine #346/#347). Only the bare-int
        # branch is positivity-constrained here.
        parse_endpoint(self._offset_payload(
            {"param": "o", "initial": 0, "increment_by": {"literal": 0}}
        ))


# ---------------------------------------------------------------------------
# Read POST body; Content-Type may be inherited from transport defaults
# ---------------------------------------------------------------------------


class TestReadPostBody:
    def test_read_post_body_without_content_type_accepted_for_transport_default(self):
        parse_endpoint(_minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "POST", "path": "/v1/search", "body": {"q": "hello"}},
                "params": {},
                "response": {"records": {"ref": "response.body"}, "schema": {"type": "array", "items": {"type": "object"}}},
            }},
        ))

    def test_read_post_body_with_content_type_accepted(self):
        parse_endpoint(_minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "POST", "path": "/v1/search", "headers": {"Content-Type": "application/json"}, "body": {"q": "hello"}},
                "params": {},
                "response": {"records": {"ref": "response.body"}, "schema": {"type": "array", "items": {"type": "object"}}},
            }},
        ))


# ---------------------------------------------------------------------------
# Replication min-length on supported_methods / cursor_mappings
# ---------------------------------------------------------------------------


class TestReplicationMinLength:
    def _payload_with_replication(self, replication):
        return _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x"},
                "params": {},
                "replication": replication,
                "response": {"records": {"ref": "response.body"}, "schema": {"type": "array", "items": {"type": "object"}}},
            }},
        )

    def test_empty_supported_methods_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._payload_with_replication({"supported_methods": [], "cursor_mappings": [{"cursor_field": "u", "param": "u", "operator": "gte"}]}))

    def test_empty_cursor_mappings_rejected(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._payload_with_replication({"supported_methods": ["incremental"], "cursor_mappings": []}))


# ---------------------------------------------------------------------------
# WriteResponse contract (success_when, error, metadata)
# ---------------------------------------------------------------------------


class TestWriteResponse:
    def _write_payload(self, response_block):
        return _minimal_api_payload(
            endpoint_id="x",
            operations={"write": {"insert": {
                "request": {"method": "POST", "path": "/v1/x", "headers": {"Content-Type": "application/json"}, "body": {"r": {"from_input": "record"}}},
                "params": {},
                "input": {"schema": {"type": "object"}},
                "response": response_block,
            }}},
        )

    def test_success_when_predicate_accepted(self):
        parse_endpoint(self._write_payload({
            "success_when": {"eq": [{"ref": "response.body.status"}, "ok"]},
        }))

    def test_metadata_reserved_scope_rejected(self):
        with pytest.raises(ValidationError, match="reserved response-scope"):
            parse_endpoint(self._write_payload({
                "metadata": {"records": {"ref": "response.body.records"}},
            }))

    def test_metadata_invalid_key_pattern_rejected(self):
        with pytest.raises(ValidationError, match="must match"):
            parse_endpoint(self._write_payload({
                "metadata": {"BadKey": {"ref": "response.body.x"}},
            }))

    def test_unknown_error_field_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            parse_endpoint(self._write_payload({
                "error": {"provider_message": {"ref": "response.body.message"}},
            }))

    def test_affected_records_must_be_expression(self):
        with pytest.raises(ValidationError):
            parse_endpoint(self._write_payload({
                "affected_records": "response.body.count",
            }))


# ---------------------------------------------------------------------------
# CursorMapping mixed-form clear error
# ---------------------------------------------------------------------------


class TestFunctionExpressionInRequestBindings:
    """Per the `value_expression` contract model
    (`analitiq.contracts.value_expression`), `function`
    expressions carry `input`/`map`/`safe` siblings — they are NOT singletons
    like `ref`/`template`/`literal`/`from_param`. The shape validator must
    permit those siblings wherever the request bindings allow expressions.
    """

    def test_function_expression_with_input_in_query_accepted(self):
        # Spec example: lookup function with a ref input.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"region": {"in": "query", "type": "string", "required": False, "operators": ["eq"]}},
                request_extras={"query": {
                    "region": {"from_param": "region"},
                    "lookup": {
                        "function": "lookup",
                        "input": {"ref": "connection.parameters.region"},
                        "map": {"eu": "eu-central-1", "us": "us-east-1"},
                    },
                }},
            )},
        )
        parse_endpoint(payload)

    def test_function_expression_with_input_in_header_accepted(self):
        # basic_auth via derived: header literal that's a function expression.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={},
                request_extras={"headers": {"Authorization": {
                    "function": "basic_auth",
                    "input": {
                        "username": {"ref": "connection.parameters.client_id"},
                        "password": {"ref": "secrets.client_secret"},
                    },
                }}},
            )},
        )
        parse_endpoint(payload)

    def test_function_expression_with_safe_field_accepted(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={},
                request_extras={"query": {"q": {
                    "function": "url_encode",
                    "input": {"ref": "connection.parameters.tag"},
                    "safe": "/",
                }}},
            )},
        )
        parse_endpoint(payload)

    def test_malformed_nested_expression_inside_function_input_rejected(self):
        # Recursion still catches malformed expressions inside `function.input`.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={"r": {"in": "query", "type": "string", "required": False, "operators": ["eq"]}},
                request_extras={"query": {"q": {
                    "function": "lookup",
                    "input": {"from_param": "r", "rogue": 1},
                    "map": {"a": 1},
                }}},
            )},
        )
        with pytest.raises(ValidationError, match="unexpected siblings"):
            parse_endpoint(payload)

    def test_function_with_unrelated_extra_field_rejected(self):
        # Beyond `function`/`input`/`map`/`safe`/`x-*`, unrelated fields are rejected.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={},
                request_extras={"query": {"q": {"function": "url_encode", "input": "abc", "rogue": 1}}},
            )},
        )
        with pytest.raises(ValidationError, match="unexpected siblings"):
            parse_endpoint(payload)


# ---------------------------------------------------------------------------
# Tuple-form `items` in response.schema (cursor-field traversal across positions)
# ---------------------------------------------------------------------------


class TestRecordsArrayItemsTupleForm:
    def test_tuple_items_with_cursor_field_in_every_position_accepted(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"u": {"from_param": "u"}}},
                "params": {"u": {"in": "query", "type": "string", "required": False, "controlled_by": "replication"}},
                "replication": {
                    "supported_methods": ["incremental"],
                    "cursor_mappings": [{"cursor_field": "updated_at", "param": "u", "operator": "gte"}],
                },
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {"type": "array", "items": [
                        {"type": "object", "properties": {"updated_at": {"type": "string"}}},
                        {"type": "object", "properties": {"updated_at": {"type": "string"}}},
                    ]},
                },
            }},
        )
        parse_endpoint(payload)

    def test_tuple_items_with_non_dict_position_rejected(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"u": {"from_param": "u"}}},
                "params": {"u": {"in": "query", "type": "string", "required": False, "controlled_by": "replication"}},
                "replication": {
                    "supported_methods": ["incremental"],
                    "cursor_mappings": [{"cursor_field": "updated_at", "param": "u", "operator": "gte"}],
                },
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {"type": "array", "items": [
                        True,
                        {"type": "object", "properties": {"updated_at": {"type": "string"}}},
                    ]},
                },
            }},
        )
        with pytest.raises(ValidationError, match="not an object schema"):
            parse_endpoint(payload)


# ---------------------------------------------------------------------------
# LinkPagination has no request-binding params (spec: §Pagination Strategies)
# ---------------------------------------------------------------------------


class TestLinkPaginationNoParams:
    def test_link_strategy_without_pagination_params_accepted(self):
        # The `link` strategy replaces the entire request URL — it does not
        # require any `controlled_by='pagination'` params. Pin that
        # invariant so a future regression in `_validate_pagination_wiring`
        # cannot silently start requiring them.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x"},
                "params": {},
                "pagination": {
                    "type": "link",
                    "link": {"next_url": {"ref": "response.body.links.next"}},
                    "stop_when": {"missing": {"ref": "response.body.links.next"}},
                },
                "response": {
                    "records": {"ref": "response.body.data"},
                    "schema": {"type": "object", "properties": {
                        "data": {"type": "array", "items": {"type": "object"}},
                    }},
                },
            }},
        )
        parse_endpoint(payload)


# ---------------------------------------------------------------------------
# Published JSON Schema generation smoke + propagation
# ---------------------------------------------------------------------------


class TestPublishedJsonSchema:
    def test_endpoint_doc_closed_at_root_and_defs(self):
        api_schema = TypeAdapter(ApiEndpointDoc).json_schema(ref_template="#/$defs/{model}")
        # Closed contract: `additionalProperties: false` everywhere, no `x-*`
        # patternProperties. Check root + a representative nested def.
        assert api_schema.get("additionalProperties") is False
        assert "patternProperties" not in api_schema
        assert "$defs" in api_schema
        cursor_def = api_schema["$defs"].get("Cursor") or {}
        assert cursor_def.get("additionalProperties") is False
        assert "patternProperties" not in cursor_def
        # `CursorMapping` mixed-form rejection lives on `Replication` as a
        # `mode="before"` validator (not as a synthetic union branch), so the
        # published `$defs` must contain only `SingleCursorMapping` and
        # `WindowCursorMapping`. A synthetic third branch with permissive
        # shape would let JSON-Schema-only validators accept the very
        # payloads runtime rejects.
        assert "_MixedCursorMapping" not in api_schema["$defs"]
        # Predicate branch classes are public-named (`Pred*`) so their `$defs`
        # entries appear under stable, externally citable names. A leading
        # underscore would still emit them into `$defs` but signal "internal"
        # to humans browsing the published schema.
        assert not any(name.startswith("_Pred") for name in api_schema["$defs"])


class TestCursorMappingMixedError:
    def test_mixed_form_error_message_cites_replication(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": {
                "request": {"method": "GET", "path": "/v1/x", "query": {"a": {"from_param": "a"}, "b": {"from_param": "b"}}},
                "params": {
                    "a": {"in": "query", "type": "string", "required": False, "controlled_by": "replication"},
                    "b": {"in": "query", "type": "string", "required": False, "controlled_by": "replication"},
                },
                "response": {
                    "records": {"ref": "response.body"},
                    "schema": {"type": "array", "items": {"type": "object", "properties": {"updated_at": {"type": "string"}}}},
                },
                "replication": {
                    "supported_methods": ["incremental"],
                    "cursor_mappings": [{
                        "cursor_field": "updated_at",
                        "param": "a", "operator": "gte",
                        "start_param": "b",
                    }],
                },
            }},
        )
        with pytest.raises(ValidationError, match="must not mix single-param and bounded-window forms"):
            parse_endpoint(payload)


# Spec: the shared metadata/slug definitions in `analitiq.contracts.shared.common`
# — `[a-z0-9][a-z0-9_-]*` allows hyphens.
class TestAliasAllowsHyphen:
    def test_api_endpoint_accepts_hyphen_alias(self):
        parse_endpoint(_minimal_api_payload(endpoint_id="transfers-v2"))

    def test_database_endpoint_accepts_hyphen_alias(self):
        parse_endpoint(_minimal_database_payload(endpoint_id="public-orders-v2"))


# --- Published authored-shape artifact pin ---------------------------------
#
# `api-endpoint/latest.json` and `database-endpoint/latest.json` are rendered
# from `ApiEndpointDoc` / `DatabaseEndpointDoc` (authored shapes). Reserved
# field names (`endpoint_id`, `connector_id`, `connector_version`,
# `connection_id`, `schema_hash`) are catalog metadata stored alongside the
# document, never inside it. Pin the rendered artifacts.

class TestPublishedSchemasExcludeReservedFields:
    def test_api_endpoint_excludes_reserved(self):
        schema = json.loads(API_ENDPOINT_SCHEMA_PATH.read_text())
        properties = set(schema.get("properties", {}).keys())
        leaked = _RESERVED_ENDPOINT_FIELDS & properties
        assert not leaked, (
            f"reserved fields leaked into api-endpoint authored schema: {sorted(leaked)}"
        )

    def test_database_endpoint_excludes_reserved(self):
        schema = json.loads(DATABASE_ENDPOINT_SCHEMA_PATH.read_text())
        properties = set(schema.get("properties", {}).keys())
        leaked = _RESERVED_ENDPOINT_FIELDS & properties
        assert not leaked, (
            f"reserved fields leaked into database-endpoint authored schema: {sorted(leaked)}"
        )


# --- Authored-shape JSON container markers ---------------------------------
#
# Object / List / Json on database columns (Pydantic enforcement) and on API
# endpoint response/input JSON Schemas (walker enforcement). Both layers must
# agree on the sibling-key contract — divergence is the classic "validates at
# one layer, breaks at the other" bug class.

def _api_payload_with_field_schema(field_name, field_schema):
    """Wrap one field schema in a minimal API read-endpoint response schema.

    The shared `_api_payload_with_response_schema` helper sets
    `records.ref = response.body.data`, so the response schema must declare
    `properties.data` for the ref traversal to succeed. The field under test
    lives at `properties.data.items.properties.<field_name>`.
    """
    return _api_payload_with_response_schema({
        "$schema": JSON_SCHEMA,
        "type": "object",
        "properties": {
            "data": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {field_name: field_schema},
                },
            },
        },
    })


class TestColumnAuthoredShapeMarkers:
    """Database `Column` carries the same Object / List / Json rules."""

    def test_column_object_with_properties_accepted(self):
        parse_endpoint(_minimal_database_payload(columns=[{
            "name": "meta",
            "native_type": "jsonb",
            "arrow_type": "Object",
            "properties": {"id": {"arrow_type": "Int64"}},
        }]))

    def test_column_list_with_items_accepted(self):
        parse_endpoint(_minimal_database_payload(columns=[{
            "name": "tags",
            "native_type": "array<text>",
            "arrow_type": "List",
            "items": {"arrow_type": "Utf8"},
        }]))

    def test_column_json_opaque_accepted(self):
        parse_endpoint(_minimal_database_payload(columns=[{
            "name": "payload",
            "native_type": "jsonb",
            "arrow_type": "Json",
        }]))

    def test_column_object_missing_properties_rejected(self):
        with pytest.raises(ValidationError, match="requires sibling 'properties'"):
            parse_endpoint(_minimal_database_payload(columns=[{
                "name": "meta", "native_type": "jsonb", "arrow_type": "Object",
            }]))

    def test_column_object_with_empty_properties_rejected(self):
        with pytest.raises(ValidationError, match="non-empty 'properties'"):
            parse_endpoint(_minimal_database_payload(columns=[{
                "name": "meta",
                "native_type": "jsonb",
                "arrow_type": "Object",
                "properties": {},
            }]))

    def test_column_list_missing_items_rejected(self):
        with pytest.raises(ValidationError, match="requires sibling 'items'"):
            parse_endpoint(_minimal_database_payload(columns=[{
                "name": "tags", "native_type": "array<text>", "arrow_type": "List",
            }]))

    def test_column_json_with_properties_rejected(self):
        with pytest.raises(ValidationError, match="must not carry 'properties' or 'items'"):
            parse_endpoint(_minimal_database_payload(columns=[{
                "name": "payload",
                "native_type": "jsonb",
                "arrow_type": "Json",
                "properties": {"id": {"arrow_type": "Int64"}},
            }]))

    def test_column_nested_malformed_marker_rejected(self):
        """Recursive `ColumnFieldSpec.properties` must enforce the same
        rules; an inner `List` without `items` should fail validation."""
        with pytest.raises(ValidationError, match="requires sibling 'items'"):
            parse_endpoint(_minimal_database_payload(columns=[{
                "name": "meta",
                "native_type": "jsonb",
                "arrow_type": "Object",
                "properties": {
                    "id": {"arrow_type": "Int64"},
                    "tags": {"arrow_type": "List"},
                },
            }]))


class TestApiEndpointWalkerAuthoredShapeMarkers:
    """The `_validate_arrow_type_in_json_schema` walker enforces the same
    sibling-key rules on API endpoint response/input JSON Schemas."""

    def test_object_with_properties_accepted(self):
        parse_endpoint(_api_payload_with_field_schema("checkAccount", {
            "arrow_type": "Object",
            "native_type": "object",
            "properties": {
                "id": {"arrow_type": "Int64", "native_type": "integer"},
                "objectName": {"arrow_type": "Utf8", "native_type": "string"},
            },
        }))

    def test_list_with_items_accepted(self):
        parse_endpoint(_api_payload_with_field_schema("tags", {
            "arrow_type": "List",
            "native_type": "array",
            "items": {"arrow_type": "Utf8", "native_type": "string"},
        }))

    def test_json_opaque_accepted(self):
        parse_endpoint(_api_payload_with_field_schema("metadata", {
            "arrow_type": "Json", "native_type": "object",
        }))

    def test_object_missing_properties_rejected(self):
        with pytest.raises(ValidationError, match="requires sibling 'properties'"):
            parse_endpoint(_api_payload_with_field_schema("checkAccount", {
                "arrow_type": "Object", "native_type": "object",
            }))

    def test_object_empty_properties_rejected(self):
        with pytest.raises(ValidationError, match="non-empty 'properties'"):
            parse_endpoint(_api_payload_with_field_schema("checkAccount", {
                "arrow_type": "Object",
                "native_type": "object",
                "properties": {},
            }))

    def test_list_missing_items_rejected(self):
        with pytest.raises(ValidationError, match="requires sibling 'items'"):
            parse_endpoint(_api_payload_with_field_schema("tags", {
                "arrow_type": "List", "native_type": "array",
            }))

    def test_list_with_boolean_items_rejected(self):
        """JSON Schema 2020-12 `items: false/true` shorthand contradicts a
        declared single-spec `List`."""
        with pytest.raises(ValidationError, match="single field spec"):
            parse_endpoint(_api_payload_with_field_schema("tags", {
                "arrow_type": "List", "native_type": "array", "items": False,
            }))

    def test_list_with_tuple_items_rejected(self):
        """Tuple-form `items: [...]` contradicts the single-spec contract."""
        with pytest.raises(ValidationError, match="single field spec"):
            parse_endpoint(_api_payload_with_field_schema("tags", {
                "arrow_type": "List",
                "native_type": "array",
                "items": [
                    {"arrow_type": "Utf8", "native_type": "string"},
                ],
            }))

    def test_json_with_properties_rejected(self):
        with pytest.raises(ValidationError, match="must not carry 'properties' or 'items'"):
            parse_endpoint(_api_payload_with_field_schema("metadata", {
                "arrow_type": "Json",
                "native_type": "object",
                "properties": {
                    "k": {"arrow_type": "Utf8", "native_type": "string"},
                },
            }))

    def test_scalar_with_properties_rejected(self):
        """Scalar arrow_type must not carry JSON-container siblings — mirror
        the Pydantic helper's rule on the walker side."""
        with pytest.raises(ValidationError, match="must not carry 'properties' or 'items'"):
            parse_endpoint(_api_payload_with_field_schema("id", {
                "arrow_type": "Int64",
                "native_type": "integer",
                "properties": {
                    "k": {"arrow_type": "Utf8", "native_type": "string"},
                },
            }))

    def test_parameterized_struct_with_items_rejected(self):
        """Parameterized Arrow nested types (Struct<…>, List<…>) carry their
        shape inline; JSON Schema `items` is not a valid sibling here."""
        with pytest.raises(ValidationError, match="must not carry 'properties' or 'items'"):
            parse_endpoint(_api_payload_with_field_schema("rec", {
                "arrow_type": "Struct<id:Int64>",
                "native_type": "struct",
                "items": {"arrow_type": "Int64", "native_type": "integer"},
            }))

    def test_nested_marker_inside_valid_object_rejected(self):
        """Outer Object is well-formed; inner child is `arrow_type: 'List'`
        without `items`. The walker must recurse into `properties.<child>`
        and surface the inner violation — short-circuiting on the outer
        Object would be a silent regression."""
        with pytest.raises(ValidationError, match="requires sibling 'items'"):
            parse_endpoint(_api_payload_with_field_schema("checkAccount", {
                "arrow_type": "Object",
                "native_type": "object",
                "properties": {
                    "id": {"arrow_type": "Int64", "native_type": "integer"},
                    "tags": {"arrow_type": "List", "native_type": "array"},
                },
            }))

    def test_nested_marker_inside_valid_list_items_rejected(self):
        """Outer List is well-formed; `items` child is `Object` without
        `properties`. Walker must recurse through `items`."""
        with pytest.raises(ValidationError, match="requires sibling 'properties'"):
            parse_endpoint(_api_payload_with_field_schema("rows", {
                "arrow_type": "List",
                "native_type": "array",
                "items": {"arrow_type": "Object", "native_type": "object"},
            }))




class TestEndpointMetadata:
    """Behavioral cover for `_EndpointBase`'s shared metadata contract.

    These rules are enforced declaratively (`list[Tag]`, `max_length=TAGS_MAX`,
    DISPLAY_NAME_MIN/MAX, DESCRIPTION_MAX) plus the shared
    `validate_tags`/`validate_display_name`. Both endpoint kinds subclass
    `_EndpointBase`, so both are exercised: a rule that held for only one kind
    would be a hole. Without these, severing either validator — or dropping a
    bound — is silent (the shared helpers' own unit tests pin the helpers, not
    the wiring).
    """

    @pytest.fixture(params=["api", "database"])
    def parse(self, request):
        if request.param == "api":
            return lambda **kw: ApiEndpointDoc.model_validate(_minimal_api_payload(**kw))
        return lambda **kw: DatabaseEndpointDoc.model_validate(_minimal_database_payload(**kw))

    # --- tags -------------------------------------------------------------
    def test_tags_at_50_accepted(self, parse):
        parse(tags=[f"t{i}" for i in range(50)])

    def test_tags_over_50_rejected(self, parse):
        with pytest.raises(ValidationError):
            parse(tags=[f"t{i}" for i in range(51)])

    def test_tag_at_64_chars_accepted(self, parse):
        parse(tags=["x" * 64])

    def test_tag_over_64_chars_rejected(self, parse):
        with pytest.raises(ValidationError):
            parse(tags=["x" * 65])

    def test_empty_tag_rejected(self, parse):
        with pytest.raises(ValidationError):
            parse(tags=[""])

    def test_duplicate_tags_rejected(self, parse):
        with pytest.raises(ValidationError, match="duplicate"):
            parse(tags=["finance", "finance"])

    def test_tag_leading_whitespace_rejected(self, parse):
        with pytest.raises(ValidationError, match="whitespace|should match pattern"):
            parse(tags=[" leading"])

    def test_tag_trailing_whitespace_rejected(self, parse):
        with pytest.raises(ValidationError, match="whitespace|should match pattern"):
            parse(tags=["trailing "])

    # --- display_name / description ---------------------------------------
    def test_display_name_at_120_accepted(self, parse):
        parse(display_name="x" * 120)

    def test_display_name_over_120_rejected(self, parse):
        with pytest.raises(ValidationError):
            parse(display_name="x" * 121)

    def test_display_name_outer_whitespace_rejected(self, parse):
        with pytest.raises(ValidationError, match="whitespace|should match pattern"):
            parse(display_name=" padded")

    def test_description_at_2000_accepted(self, parse):
        parse(description="x" * 2000)

    def test_description_over_2000_rejected(self, parse):
        with pytest.raises(ValidationError):
            parse(description="x" * 2001)


class TestPublishedMetadataConstraints:
    """Pin the metadata bounds the PUBLISHED endpoint schemas declare.

    These schemas used to enforce the tag rules in Python while declaring
    `tags: {"type": "string"}` with no bounds at all — a consumer reading the
    contract saw "any list of strings" and got a rejection. Schema parity only
    proves the artifact matches the model, and its documented remedy is
    "re-run render_schemas.py write" — which would happily regenerate a
    weakened contract. This pins the values, so dropping a bound costs an
    explicit test edit and a version-bump conversation.
    """

    @pytest.fixture(params=["api-endpoint", "database-endpoint"])
    def schema(self, request):
        path = (
            LATEST_API_ENDPOINT_SCHEMA_PATH
            if request.param == "api-endpoint"
            else LATEST_DATABASE_ENDPOINT_SCHEMA_PATH
        )
        assert path.exists(), f"published schema missing: {path}"
        return json.loads(path.read_text())

    @staticmethod
    def _branch(node, key):
        """The non-null branch of an `X | None` anyOf."""
        for b in node.get("anyOf", [node]):
            if b.get("type") != "null" and key in b:
                return b
        raise AssertionError(f"no branch declaring {key!r} in {node}")

    def test_tags_bounds_published(self, schema):
        tags = self._branch(schema["properties"]["tags"], "maxItems")
        assert tags["maxItems"] == 50
        assert tags["items"]["minLength"] == 1
        assert tags["items"]["maxLength"] == 64

    def test_display_name_bounds_published(self, schema):
        dn = self._branch(schema["properties"]["display_name"], "maxLength")
        assert dn["minLength"] == 1
        assert dn["maxLength"] == 120

    def test_description_bound_published(self, schema):
        desc = self._branch(schema["properties"]["description"], "maxLength")
        assert desc["maxLength"] == 2000


# ---------------------------------------------------------------------------
# §Native and Arrow Types: container-shape matrix (issue #977)
# ---------------------------------------------------------------------------
#
# The Object/List/plain sibling-key matrix is enforced at runtime by
# `enforce_container_shape` AND published into the JSON Schema via the
# `_ARROW_CONTAINER_SCHEMA_RULES` if/then fragment. This guards the two against
# drift: every case must get the SAME verdict from the Pydantic model and from a
# stock JSON-Schema validator built from the model's own generated schema. If the
# json_schema_extra fragment is ever dropped, the schema verdict diverges here.

_COLUMN_CONTAINER_MATRIX = [
    ({"arrow_type": "Utf8"}, True),
    ({"arrow_type": "Utf8", "properties": {"a": {"arrow_type": "Int64"}}}, False),
    ({"arrow_type": "Utf8", "items": {"arrow_type": "Int64"}}, False),
    ({"arrow_type": "Utf8", "properties": None, "items": None}, True),
    ({"arrow_type": "Object", "properties": {"a": {"arrow_type": "Int64"}}}, True),
    ({"arrow_type": "Object"}, False),
    ({"arrow_type": "Object", "properties": {}}, False),
    ({"arrow_type": "Object", "properties": {"a": {"arrow_type": "Int64"}}, "items": {"arrow_type": "Int64"}}, False),
    ({"arrow_type": "Object", "properties": {"a": {"arrow_type": "Int64"}}, "items": None}, True),
    ({"arrow_type": "List", "items": {"arrow_type": "Int64"}}, True),
    ({"arrow_type": "List"}, False),
    ({"arrow_type": "List", "items": None}, False),
    ({"arrow_type": "List", "items": {"arrow_type": "Int64"}, "properties": {"a": {"arrow_type": "Int64"}}}, False),
    ({"arrow_type": "List", "items": {"arrow_type": "Int64"}, "properties": None}, True),
    ({"arrow_type": "Json"}, True),
    ({"arrow_type": "Json", "properties": {"a": {"arrow_type": "Int64"}}}, False),
    # Nested: the matrix must be enforced at depth, via the recursive
    # ColumnFieldSpec `$ref`, not only on the top-level node. An inner `List`
    # with no `items` inside an Object's properties is invalid; a valid deep
    # Object must not be over-rejected.
    ({"arrow_type": "Object", "properties": {"a": {"arrow_type": "List"}}}, False),
    ({"arrow_type": "Object",
      "properties": {"a": {"arrow_type": "Object", "properties": {"b": {"arrow_type": "Int64"}}}}}, True),
]

_COLUMN_JSON_SCHEMA = TypeAdapter(Column).json_schema(ref_template="#/$defs/{model}")
_COLUMN_JSON_SCHEMA["$schema"] = JSON_SCHEMA

_COLUMN_FIELD_SPEC_JSON_SCHEMA = TypeAdapter(ColumnFieldSpec).json_schema(ref_template="#/$defs/{model}")
_COLUMN_FIELD_SPEC_JSON_SCHEMA["$schema"] = JSON_SCHEMA


@pytest.mark.parametrize("node, accepted", _COLUMN_CONTAINER_MATRIX)
def test_column_container_matrix(node, accepted):
    payload = {"name": "c", "native_type": "x", **node}

    try:
        Column.model_validate(payload)
        runtime_ok = True
    except ValidationError:
        runtime_ok = False

    schema_ok = Draft202012Validator(_COLUMN_JSON_SCHEMA).is_valid(payload)

    assert runtime_ok is accepted, f"runtime verdict {runtime_ok} != expected {accepted}"
    assert schema_ok is accepted, f"published-schema verdict {schema_ok} != expected {accepted}"


@pytest.mark.parametrize("node, accepted", _COLUMN_CONTAINER_MATRIX)
def test_column_field_spec_container_matrix(node, accepted):
    """The recursive child spec `ColumnFieldSpec` carries the same if/then
    fragment as `Column`. Guard it directly so a regression that drops the
    fragment from `ColumnFieldSpec` alone (with `Column` still carrying it) is
    caught here, not only by the published-file parity test."""
    try:
        ColumnFieldSpec.model_validate(node)
        runtime_ok = True
    except ValidationError:
        runtime_ok = False

    schema_ok = Draft202012Validator(_COLUMN_FIELD_SPEC_JSON_SCHEMA).is_valid(node)

    assert runtime_ok is accepted, f"runtime verdict {runtime_ok} != expected {accepted}"
    assert schema_ok is accepted, f"published-schema verdict {schema_ok} != expected {accepted}"


# ---------------------------------------------------------------------------
# Value-expression grammar migrated from prose residue (#994)
# ---------------------------------------------------------------------------


class TestRefResolutionScope:
    """Rule 2a — `RefExpression.ref` must begin with a known resolution scope."""

    @pytest.mark.parametrize(
        "ref",
        [
            "response.body",
            "connection.selections.profile_id",
            "secrets.api_key",  # sub-path existence is NOT statically checked
            "runtime.oauth.code",
            "auth.refresh_token",
            "connector.base_url",
            "request.path",
            "state.cursor",
            "stream.id",
            "response",  # a bare scope is a valid leading token
        ],
    )
    def test_known_scope_accepted(self, ref):
        assert RefExpression(ref=ref).ref == ref

    @pytest.mark.parametrize(
        "ref",
        ["foo.bar", "responseX.y", "record.id", "profile_id", "missing.path", ".response"],
    )
    def test_unknown_leading_scope_rejected(self, ref):
        with pytest.raises(ValidationError):
            RefExpression(ref=ref)

    def test_scope_pattern_rendered_into_published_schema(self):
        # Rule 2a is structural, so it must appear in the published JSON Schema.
        schema = json.loads(LATEST_API_ENDPOINT_SCHEMA_PATH.read_text())
        ref_def = schema["$defs"]["RefExpression"]["properties"]["ref"]
        assert "pattern" in ref_def
        for scope in RESOLUTION_SCOPES:
            assert scope in ref_def["pattern"]

    # The `ref` pattern only reaches typed Expression nodes; refs buried in the
    # `Any`-typed request slots (headers/query/body) are caught by the walk. Body
    # is exercised via the same `_first_unknown_scope_ref(body)` call site;
    # headers/query are GET-safe here.
    @pytest.mark.parametrize("slot", ["headers", "query"])
    def test_unknown_scope_ref_in_any_request_slot_rejected(self, slot):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={}, request_extras={slot: {"X": {"ref": "foo.bar"}}},
            )},
        )
        with pytest.raises(ValidationError, match="resolution scope"):
            parse_endpoint(payload)

    def test_known_scope_ref_in_header_accepted(self):
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={}, request_extras={"headers": {"X": {"ref": "secrets.api_key"}}},
            )},
        )
        parse_endpoint(payload)

    def test_literal_wrapped_unknown_ref_in_header_accepted(self):
        # `literal` opts the object out of expression interpretation → not a ref.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={}, request_extras={"headers": {"X": {"literal": {"ref": "foo.bar"}}}},
            )},
        )
        parse_endpoint(payload)

    def test_unqualified_template_placeholder_in_any_slot_rejected(self):
        # Templates in Any-typed slots are scope-checked too, not only refs.
        payload = _minimal_api_payload(
            endpoint_id="x",
            operations={"read": _read_op_with(
                params={}, request_extras={"headers": {"X": {"template": "${name}"}}},
            )},
        )
        with pytest.raises(ValidationError, match="resolution scope"):
            parse_endpoint(payload)


class TestTemplatePlaceholderScope:
    """Rule 2b — every `${...}` placeholder must be a fully-qualified scope ref."""

    @pytest.mark.parametrize(
        "template",
        [
            "welcome-email",  # no placeholder → passes vacuously
            "Bearer ${secrets.api_key}",
            "${connection.parameters.region}-${response.body.host}",
            "${runtime.oauth.code}",
            "${secrets.missing}",  # sub-path not checked
        ],
    )
    def test_qualified_or_placeholderless_accepted(self, template):
        assert TemplateExpression(template=template).template == template

    @pytest.mark.parametrize(
        "template",
        ["${name}", "${foo.bar}", "prefix ${profile_id} suffix", "${ spaced }"],
    )
    def test_unqualified_placeholder_rejected(self, template):
        with pytest.raises(ValidationError, match="resolution scope"):
            TemplateExpression(template=template)

    @pytest.mark.parametrize("template", ["key=${}", "Bearer ${secrets.api_key"])
    def test_malformed_opener_matches_resolver_and_passes(self, template):
        # `${}` and an unclosed `${` are not placeholders under the shared
        # resolver grammar (`_TEMPLATE_RE` requires `[^}]+`), so — matching the
        # runtime — they are inert literal text, not a validation error. The model
        # deliberately does not invent a stricter rule than the resolver.
        assert TemplateExpression(template=template).template == template


class TestWriteResponseRecordCountBan:
    """Rule 1 — write-response expressions must not reference `response.record_count`."""

    def test_record_count_ref_rejected(self):
        with pytest.raises(ValidationError, match="record_count"):
            WriteResponse(affected_records={"ref": "response.record_count"})

    def test_record_count_subpath_ref_rejected(self):
        with pytest.raises(ValidationError, match="record_count"):
            WriteResponse(generated_keys={"ref": "response.record_count.total"})

    def test_record_count_template_rejected(self):
        with pytest.raises(ValidationError, match="record_count"):
            WriteResponse(metadata={"n": {"template": "${response.record_count}"}})

    def test_record_count_nested_in_success_when_rejected(self):
        # `success_when` operands are `Any`; the walk must reach nested expression
        # dicts a typed field walk would miss.
        with pytest.raises(ValidationError, match="record_count"):
            WriteResponse(success_when={"eq": [{"ref": "response.record_count"}, 0]})

    def test_record_count_bare_string_template_operand_rejected(self):
        # The resolver treats every string as a template, so a bare-string operand
        # carrying the placeholder must be caught too.
        with pytest.raises(ValidationError, match="record_count"):
            WriteResponse(success_when={"eq": ["${response.record_count}", 0]})

    def test_other_response_refs_accepted(self):
        # `response.record_count` is the only banned target; siblings are fine.
        WriteResponse(affected_records={"ref": "response.body.count"})
        WriteResponse(metadata={"n": {"ref": "response.headers.x_total"}})

    def test_literal_wrapped_record_count_accepted(self):
        # `literal` protects the object from expression interpretation, so the
        # inner `{"ref": ...}` is opaque data, not a banned reference.
        WriteResponse(affected_records={"literal": {"ref": "response.record_count"}})
        WriteResponse(metadata={"n": {"literal": {"template": "${response.record_count}"}}})

    def test_whitespace_padded_record_count_ref_rejected(self):
        # The resolver strips a ref before resolving, so the ban strips too.
        with pytest.raises(ValidationError, match="record_count"):
            WriteResponse(affected_records={"ref": "response.record_count "})

    def test_whitespace_padded_record_count_placeholder_rejected(self):
        with pytest.raises(ValidationError, match="record_count"):
            WriteResponse(generated_keys={"template": "${response.record_count }"})
