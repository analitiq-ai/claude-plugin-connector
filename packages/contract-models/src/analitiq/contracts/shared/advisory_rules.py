"""The advisory rule registry — authored data, the single source for the
relational (cross-field) contract tier.

Every entry is enforced at runtime by :mod:`advisory` (generic kinds) or by the
bespoke method it names (``kind="custom"``). Generic rules additionally carry a
valid/invalid fixture corpus (``contract-models/tests/fixtures/advisory``) a
non-Python second system reconciles against; custom rules are enforced only
in-process by their named validator and may carry no fixtures. Adding a
relational rule is a data edit here — never new imperative code.

IDs are ``ADV-<AREA>-NNN``, stable and never reused. ``targets`` name model
classes by string; a rule on a base class covers its subclasses (MRO match).
"""
from __future__ import annotations

from .advisory import AdvisoryRule, register

ADVISORY_RULES: list[AdvisoryRule] = [
    # --- HTTP request blocks (endpoints + connector share the intent) -------
    AdvisoryRule(
        id="ADV-HTTP-001",
        kind="disjoint",
        resource="shared",
        prose=(
            "A header named in headers_remove must not also be declared in "
            "headers by the same block (case-insensitive)."
        ),
        targets=(
            "_RequestBase",
            "AuthOperationTemplate",
            "HttpTransport",
            "TransportDefaults",
        ),
        fields=("headers", "headers_remove"),
        options={"case_insensitive": True},
        fixture_model="AuthOperationTemplate",
    ),
    # --- api-endpoint request binding ---------------------------------------
    AdvisoryRule(
        id="ADV-ENDP-001",
        kind="custom",
        resource="api-endpoint",
        prose=(
            "request.path_params keys must equal the {placeholder} names in "
            "request.path, and path_params is present exactly when the path "
            "declares placeholders."
        ),
        targets=("_RequestBase",),
        enforcer="_validate",
        fixture_model="WriteRequest",
    ),
    # --- connector connection_contract inputs -------------------------------
    AdvisoryRule(
        id="ADV-CONN-001",
        kind="set_equal",
        resource="connector",
        prose="ui.options must enumerate exactly the same value set as enum.",
        targets=("ConnectionContractInput",),
        fields=("ui.options[].value", "enum"),
    ),
    AdvisoryRule(
        id="ADV-CONN-002",
        kind="member_of",
        resource="connector",
        prose="default must be a member of enum when both are present.",
        targets=("ConnectionContractInput",),
        fields=("default", "enum"),
    ),
    # --- stream destinations + mapping --------------------------------------
    AdvisoryRule(
        id="ADV-STRM-001",
        kind="unique_by",
        resource="stream",
        prose=(
            "destinations must be unique by "
            "(endpoint_ref.scope, endpoint_ref.connection_id, endpoint_ref.endpoint_id)."
        ),
        targets=("StreamAuthored",),
        fields=("destinations",),
        options={
            "key": [
                "endpoint_ref.scope",
                "endpoint_ref.connection_id",
                "endpoint_ref.endpoint_id",
            ]
        },
        fixture_model="StreamInput",
    ),
    AdvisoryRule(
        id="ADV-STRM-002",
        kind="unique_by",
        resource="stream",
        prose="mapping.assignments[].target.path must be unique within the mapping.",
        targets=("StreamMapping",),
        fields=("assignments",),
        options={"key": ["target.path"]},
    ),
    # --- database-endpoint columns ------------------------------------------
    AdvisoryRule(
        id="ADV-DBEP-001",
        kind="unique_by",
        resource="database-endpoint",
        prose="columns[].name must be unique.",
        targets=("DatabaseEndpointDoc",),
        fields=("columns",),
        options={"key": ["name"]},
    ),
    AdvisoryRule(
        id="ADV-DBEP-002",
        kind="unique_by",
        resource="database-endpoint",
        prose="columns[].ordinal_position must be unique where present.",
        targets=("DatabaseEndpointDoc",),
        fields=("columns",),
        options={"key": ["ordinal_position"], "skip_null": True},
    ),
    AdvisoryRule(
        id="ADV-DBEP-003",
        kind="subset_of",
        resource="database-endpoint",
        prose="primary_keys must reference declared columns[].name.",
        targets=("DatabaseEndpointDoc",),
        fields=("primary_keys", "columns[].name"),
    ),
    # --- connector document -------------------------------------------------
    AdvisoryRule(
        id="ADV-CTOR-001",
        kind="member_of",
        resource="connector",
        prose="default_transport must name a key declared in transports.",
        targets=("ConnectorBase",),
        fields=("default_transport", "transports"),
        fixture_model="ApiConnector",
    ),
    # --- pipeline connections -----------------------------------------------
    AdvisoryRule(
        id="ADV-PIPE-001",
        kind="unique_by",
        resource="pipeline",
        prose="connections.destinations must not contain duplicate connection IDs.",
        targets=("PipelineConnections",),
        fields=("destinations",),
    ),
    # ======================================================================
    # CUSTOM catalog — value-conditioned / compound / recursive rules whose
    # enforcement is irreducibly bespoke. The named model validator stays the
    # enforcer; these entries make the census complete (every relational rule
    # declares its enforcer) and drive the generated docs + checklist.
    # ======================================================================
    # --- api-endpoint -------------------------------------------------------
    AdvisoryRule(
        id="ADV-ENDP-002", kind="custom", resource="api-endpoint",
        prose="A controlled_by parameter must not also declare operators.",
        targets=("Param",), enforcer="_validate",
    ),
    AdvisoryRule(
        id="ADV-ENDP-003", kind="custom", resource="api-endpoint",
        prose="A query parameter of type array or object must declare style and explode.",
        targets=("Param",), enforcer="_validate",
    ),
    AdvisoryRule(
        id="ADV-ENDP-004", kind="custom", resource="api-endpoint",
        prose="A cursor mapping must not mix the single-parameter form with the window (start/end) form.",
        targets=("Replication",), enforcer="_reject_mixed_cursor_forms",
    ),
    AdvisoryRule(
        id="ADV-ENDP-005", kind="custom", resource="api-endpoint",
        prose="Every node in response.schema must pair native_type/arrow_type and match the Object/List/scalar container shape.",
        targets=("ResponseExtraction",), enforcer="_validate",
    ),
    AdvisoryRule(
        id="ADV-ENDP-006", kind="custom", resource="api-endpoint",
        prose="Every node in input.schema must pair native_type/arrow_type and match its container shape.",
        targets=("WriteInput",), enforcer="_validate_arrow_types",
    ),
    AdvisoryRule(
        id="ADV-ENDP-007", kind="custom", resource="api-endpoint",
        prose="A GET read operation must not declare a parameter located in the body.",
        targets=("ReadOperation",), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-008", kind="custom", resource="api-endpoint",
        prose="Every {from_param} binding must reference a declared parameter whose location matches the binding site.",
        targets=("ReadOperation", "WriteOperation"), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-009", kind="custom", resource="api-endpoint",
        prose="Every declared parameter must be referenced by exactly one request binding.",
        targets=("ReadOperation", "WriteOperation"), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-010", kind="custom", resource="api-endpoint",
        prose="Pagination parameter references must exist and declare controlled_by='pagination'.",
        targets=("ReadOperation",), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-011", kind="custom", resource="api-endpoint",
        prose="Replication cursor parameter references must exist and declare controlled_by='replication'.",
        targets=("ReadOperation",), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-012", kind="custom", resource="api-endpoint",
        prose="response.records.ref must resolve to an array node inside response.schema.",
        targets=("ReadOperation",), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-013", kind="custom", resource="api-endpoint",
        prose="Each replication cursor_field must resolve to a field in the record shape of response.schema.",
        targets=("ReadOperation",), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-014", kind="custom", resource="api-endpoint",
        prose="write.conflict_keys must reference top-level fields declared in input.schema.",
        targets=("WriteOperation",), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-015", kind="custom", resource="api-endpoint",
        prose="idempotency and batching are mutually exclusive on a write operation.",
        targets=("WriteOperation",), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-016", kind="custom", resource="api-endpoint",
        prose="An idempotency key name must not collide with a declared header or body field.",
        targets=("WriteOperation",), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-017", kind="custom", resource="api-endpoint",
        prose="batching selects the from_input arity in request.body (records when batching, record otherwise), and the referenced field must exist in input.schema.",
        targets=("WriteOperation",), enforcer="_wiring",
    ),
    AdvisoryRule(
        id="ADV-ENDP-018", kind="custom", resource="api-endpoint",
        prose="An operations block must declare at least one of read or write.",
        targets=("Operations",), enforcer="_at_least_one",
    ),
    AdvisoryRule(
        id="ADV-ENDP-019", kind="custom", resource="api-endpoint",
        prose="An upsert write mode requires conflict_keys; any other write mode forbids it.",
        targets=("Operations",), enforcer="_conflict_keys_by_mode",
    ),
    AdvisoryRule(
        id="ADV-ENDP-020", kind="custom", resource="api-endpoint",
        prose="A column field's arrow_type must match its container shape: Object declares properties, List declares items, scalars neither.",
        targets=("ColumnFieldSpec",), enforcer="_validate_container_shape",
    ),
    AdvisoryRule(
        id="ADV-ENDP-021", kind="custom", resource="api-endpoint",
        prose="A column's arrow_type must match its container shape: Object declares properties, List declares items, scalars neither.",
        targets=("Column",), enforcer="_validate_container_shape",
    ),
    AdvisoryRule(
        id="ADV-ENDP-022", kind="custom", resource="api-endpoint",
        prose="Every expression dict in a request slot must declare exactly one primary key (ref/template/literal/function/from_param/from_input) alongside only x-* siblings.",
        targets=("ReadOperation", "WriteOperation"), enforcer="_wiring",
    ),
    # --- stream -------------------------------------------------------------
    AdvisoryRule(
        id="ADV-STRM-003", kind="custom", resource="stream",
        prose="A supplied endpoint_id must equal derive_db_endpoint_id(database_object).",
        targets=("ConnectionEndpointRef",), enforcer="_derive_or_verify_endpoint_id",
    ),
    AdvisoryRule(
        id="ADV-STRM-004", kind="custom", resource="stream",
        prose="A unary filter operator (is_null/is_not_null) must omit value; every other operator requires it.",
        targets=("Filter",), enforcer="_validate_value_presence",
    ),
    AdvisoryRule(
        id="ADV-STRM-005", kind="custom", resource="stream",
        prose="A pipe expression must start with a get step and be followed only by fn steps.",
        targets=("PipeExpression",), enforcer="_validate_positional_grammar",
    ),
    AdvisoryRule(
        id="ADV-STRM-006", kind="custom", resource="stream",
        prose="An arrow field's arrow_type must match its container shape: Object declares properties, List declares items, scalars neither.",
        targets=("ArrowFieldSpec",), enforcer="_validate_container_shape",
    ),
    AdvisoryRule(
        id="ADV-STRM-007", kind="custom", resource="stream",
        prose="constant.value's JSON kind must match arrow_type, and the Object/List/scalar container shape rule applies.",
        targets=("ConstantValue",), enforcer="_validate_container_shape",
    ),
    AdvisoryRule(
        id="ADV-STRM-008", kind="custom", resource="stream",
        prose="An assignment value must declare exactly one of expression or constant.",
        targets=("AssignmentValue",), enforcer="_validate_one_of",
    ),
    AdvisoryRule(
        id="ADV-STRM-009", kind="custom", resource="stream",
        prose="A validation rule requires value for value-taking types and omits it for required/not_null.",
        targets=("ValidationRule",), enforcer="_validate_value_for_rule",
    ),
    AdvisoryRule(
        id="ADV-STRM-010", kind="custom", resource="stream",
        prose="An assignment target's arrow_type must match its container shape: Object declares properties, List declares items, scalars neither.",
        targets=("AssignmentTarget",), enforcer="_validate_container_shape",
    ),
    AdvisoryRule(
        id="ADV-STRM-011", kind="custom", resource="stream",
        prose="conflict_keys is required for a connection-scope upsert destination and forbidden for a connector-scope or non-upsert destination.",
        targets=("StreamDestination",), enforcer="_validate_write_conflict_keys",
    ),
    AdvisoryRule(
        id="ADV-STRM-012", kind="custom", resource="stream",
        prose="A filter operator must belong to the source scope's vocabulary: the database operator set for a connection source, the API operator set for a connector source.",
        targets=("StreamSource",), enforcer="_validate_filter_operator_scope",
    ),
    AdvisoryRule(
        id="ADV-STRM-013", kind="custom", resource="stream",
        prose="A database (connection-scope) destination's write.mode must be one of {insert, upsert}; an API (connector-scope) destination's mode is an endpoint-declared operations.write key.",
        targets=("StreamDestination",), enforcer="_validate_db_write_mode",
    ),
    # --- connector (ConnectionContractInput + connector document) -----------
    AdvisoryRule(
        id="ADV-CONN-003", kind="custom", resource="connector",
        prose="secret must be true if and only if storage is 'secrets'.",
        targets=("ConnectionContractInput",), enforcer="_consistency",
    ),
    AdvisoryRule(
        id="ADV-CTOR-002", kind="custom", resource="connector",
        prose="A user_selection post-auth output requires options_request and forbids discovery_request; an auto_discovery output requires discovery_request and forbids options_request/options_path/label_path; storage is constrained by mode.",
        targets=("PostAuthOutput",), enforcer="_mode_consistency",
    ),
    AdvisoryRule(
        id="ADV-CTOR-003", kind="custom", resource="connector",
        prose="A connector_plugin resource discovery requires an entrypoint; a builtin forbids it.",
        targets=("ResourceDiscoveryImplementation",), enforcer="_entrypoint_matches_type",
    ),
    AdvisoryRule(
        id="ADV-CTOR-004", kind="custom", resource="connector",
        prose="An ADBC transport must declare at least one of dsn or db_kwargs.",
        targets=("AdbcTransport",), enforcer="_require_dsn_or_kwargs",
    ),
    AdvisoryRule(
        id="ADV-CTOR-005", kind="custom", resource="connector",
        prose="Every transport_ref (auth operations, resource discovery, post-auth requests) must resolve to a declared transport.",
        targets=("ConnectorBase",), enforcer="_transport_refs_resolvable",
    ),
    AdvisoryRule(
        id="ADV-CTOR-006", kind="custom", resource="connector",
        prose="No two connection_contract declarations may write the same secrets storage path.",
        targets=("ConnectorBase",), enforcer="_connection_contract_internal_refs",
    ),
    AdvisoryRule(
        id="ADV-CTOR-007", kind="custom", resource="connector",
        prose="required_for_activation must reference declared input or post-auth-output storage paths.",
        targets=("ConnectorBase",), enforcer="_connection_contract_internal_refs",
    ),
    AdvisoryRule(
        id="ADV-CTOR-008", kind="custom", resource="connector",
        prose="validation.rules[].when.field must reference a declared input.",
        targets=("ConnectorBase",), enforcer="_connection_contract_internal_refs",
    ),
    AdvisoryRule(
        id="ADV-CTOR-009", kind="custom", resource="connector",
        prose="validation.rules[].require and .forbid must reference declared inputs.",
        targets=("ConnectorBase",), enforcer="_connection_contract_internal_refs",
    ),
    AdvisoryRule(
        id="ADV-CTOR-010", kind="custom", resource="connector",
        prose="file/s3/stdout connectors must not declare post_auth_outputs or required_for_activation.",
        targets=("FileConnector", "S3Connector", "StdoutConnector"),
        enforcer="_validate_no_post_auth_contract",
    ),
    AdvisoryRule(
        id="ADV-CTOR-011", kind="custom", resource="connector",
        prose="Every {placeholder} in a url_template DSN must have a matching entry in bindings, and every binding must be referenced by the template.",
        targets=("UrlTemplateDsn",), enforcer="_validate_placeholder_bindings",
    ),
    AdvisoryRule(
        id="ADV-CTOR-012", kind="custom", resource="connector",
        prose="A connection condition predicate must declare field and exactly one operator key (eq/in/not_in/present/regex).",
        targets=("ConnectionConditionPredicate",), enforcer="_exactly_one_operator",
    ),
    # --- type-map -----------------------------------------------------------
    AdvisoryRule(
        id="ADV-TMAP-001", kind="custom", resource="type-map",
        prose="A schemaless or structured native type must not resolve to a scalar canonical type.",
        targets=("TypeMapReadExactRule",), enforcer="_check",
    ),
    AdvisoryRule(
        id="ADV-TMAP-002", kind="custom", resource="type-map",
        prose="A schemaless or structured native pattern must not resolve to a scalar canonical type.",
        targets=("TypeMapReadRegexRule",), enforcer="_check",
    ),
    AdvisoryRule(
        id="ADV-TMAP-003", kind="custom", resource="type-map",
        prose="Every ${name} in the canonical render must name a capture group in the native pattern.",
        targets=("TypeMapReadRegexRule",), enforcer="_check",
    ),
    AdvisoryRule(
        id="ADV-TMAP-004", kind="custom", resource="type-map",
        prose="A native pattern with named captures must not map to a canonical that discards them.",
        targets=("TypeMapReadRegexRule",), enforcer="_check",
    ),
    AdvisoryRule(
        id="ADV-TMAP-005", kind="custom", resource="type-map",
        prose="A regex read rule's native must compile as an ECMA-262 regex; Python-only (?P…) syntax and otherwise-invalid patterns are rejected.",
        targets=("TypeMapReadRegexRule",), enforcer="_check",
    ),
    AdvisoryRule(
        id="ADV-TMAP-006", kind="custom", resource="type-map",
        prose="A regex read rule's canonical must be a valid (optionally ${name}-templated) Arrow type matched full-string, so a trailing newline is rejected.",
        targets=("TypeMapReadRegexRule",), enforcer="_check",
    ),
    AdvisoryRule(
        id="ADV-TMAP-007", kind="custom", resource="type-map",
        prose="A ${...} placeholder in a canonical render must be well-formed: no empty ${} and no unclosed ${.",
        targets=("TypeMapReadRegexRule",), enforcer="_check",
    ),
    # --- pipeline -----------------------------------------------------------
    AdvisoryRule(
        id="ADV-PIPE-002", kind="custom", resource="pipeline",
        prose="schedule.type gates its fields: manual forbids interval/cron, interval requires interval_minutes, cron requires cron_expression.",
        targets=("Schedule",), enforcer="_validate_schedule_fields",
    ),
    AdvisoryRule(
        id="ADV-PIPE-003", kind="custom", resource="pipeline",
        prose="streams must be unique by version-stripped base id.",
        targets=("PipelineAuthored",), enforcer="_validate_streams_unique_base",
    ),
    AdvisoryRule(
        id="ADV-PIPE-004", kind="custom", resource="pipeline",
        prose="An active pipeline must reference at least one stream.",
        targets=("PipelineAuthored",), enforcer="_check_active_requires_streams",
    ),
    # --- data-sync ----------------------------------------------------------
    AdvisoryRule(
        id="ADV-DSYNC-001", kind="custom", resource="data-sync-run-status",
        prose="error is present only for failed/partial runs, and a failed run always carries error.",
        targets=("PipelineRunStatusData",), enforcer="_error_matches_status",
    ),
    # --- shared -------------------------------------------------------------
    AdvisoryRule(
        id="ADV-RETRY-001", kind="custom", resource="shared",
        prose="retry_delay_seconds must be omitted or 0 when max_retries is 0.",
        targets=("RetryErrorHandlingBase",), enforcer="_validate_retry_fields",
    ),
]

register(ADVISORY_RULES)
