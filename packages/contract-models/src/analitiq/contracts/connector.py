"""
Connector models.

A connector definition declares how a provider is authenticated, configured,
and reached: its `kind` discriminator, an auth block, a connection contract
(the inputs a connection must supply), and a transport contract.

Fields typed `Any` and described as value-expressions accept the shared
value-expression grammar — refs, templates, literals, and functions —
resolved at runtime against the connection's stored values. The authored
contract is closed: `x-*` extension keys are rejected at every level.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    Tag as UnionTag,
    TypeAdapter,
    field_validator,
    model_validator,
)

from analitiq.contracts.shared.advisory import AdvisoryValidated
from analitiq.contracts.shared.common import (
    DESCRIPTION_MAX,
    DISPLAY_NAME_MAX,
    DISPLAY_NAME_MIN,
    NO_EDGE_WHITESPACE_PATTERN,
    SEMVER_PATTERN,
    SLUG_PATTERN,
    StrictModel,
    TrimmedTag,
    TAGS_MAX,
    schema_url_for,
    schema_url_pattern,
    validate_display_name,
    validate_tags,
)

CONNECTOR_SCHEMA_URL = schema_url_for("connector")
# Host-tolerant matcher for the `$schema` field: a connector authored against
# the canonical `schemas.analitiq.ai` URL must still validate when the engine
# checks it against a per-environment schema (`schemas.analitiq.work` / `.dev`).
_CONNECTOR_SCHEMA_URL_PATTERN = schema_url_pattern("connector")



# --- Enums ---


class ConnectorKind(str, Enum):
    """Closed connector-kind discriminator."""
    API = "api"
    DATABASE = "database"
    NOSQL = "nosql"
    DOCUMENT = "document"
    FILE = "file"
    S3 = "s3"
    STDOUT = "stdout"


# --- Supporting Models ---


class FormFieldOption(StrictModel):
    """Option for select-style widgets."""

    value: str = Field(..., description="Option value")
    label: str = Field(..., description="Display label")


# --- Auth Models (discriminated union) ---


class AuthOperationTemplate(AdvisoryValidated, StrictModel):
    """Operation template for auth `authorize` / `token_exchange` / `refresh` / `test`.

    The HTTP `base_url` lives on the named transport; this template selects the
    transport via `transport_ref` (omit to use `default_transport`) and supplies
    the per-operation `path`, headers, and body.
    """

    transport_ref: str | None = Field(
        default=None,
        description="Named transport this request is dispatched through; defaults to `default_transport`",
    )
    method: str | None = Field(
        default=None,
        description="HTTP method. Engines treat absence as GET; declare it explicitly for non-HTTP transports.",
    )
    path: str = Field(
        ...,
        min_length=1,
        description="Path or relative URL on the selected transport",
    )
    headers: dict[str, Any] | None = Field(
        default=None,
        description="Request headers; values may be literals, `{ref}`, `{template}`, or `{function}`",
    )
    headers_remove: list[str] | None = Field(
        default=None,
        description="Header names to delete from inherited transport defaults",
    )
    body: Any | None = Field(default=None, description="Request body; type depends on transport/encoding")


class ApiKeyAuth(StrictModel):
    """API key authentication. Required values declared in `connection_contract.inputs`."""

    type: Literal["api_key"] = Field(..., description="Auth type")
    test: AuthOperationTemplate | None = Field(default=None, description="Optional connection test template")


class BasicAuth(StrictModel):
    """Basic (username/password) authentication.

    Required user-entered values are declared in `connection_contract.inputs`
    (typically `username` and `password`). The connector should not declare
    field names here.
    """

    type: Literal["basic_auth"] = Field(..., description="Auth type")
    test: AuthOperationTemplate | None = Field(default=None, description="Optional connection test template")


class OAuth2AuthorizationCodeAuth(StrictModel):
    """OAuth2 authorization code flow.

    Spec: §Authentication — requires `authorize` and `token_exchange`; `refresh`
    optional.
    """

    type: Literal["oauth2_authorization_code"] = Field(..., description="Auth type")
    authorize: AuthOperationTemplate = Field(..., description="Authorization request template")
    token_exchange: AuthOperationTemplate = Field(..., description="Token exchange request template")
    refresh: AuthOperationTemplate | None = Field(default=None, description="Token refresh request template")
    test: AuthOperationTemplate | None = Field(default=None, description="Optional connection test template")


class OAuth2ClientCredentialsAuth(StrictModel):
    """OAuth2 client credentials flow.

    Spec: §Authentication — requires `token_exchange`; must omit `authorize`.
    """

    type: Literal["oauth2_client_credentials"] = Field(..., description="Auth type")
    token_exchange: AuthOperationTemplate = Field(..., description="Token exchange request template")
    refresh: AuthOperationTemplate | None = Field(default=None, description="Token refresh request template")
    test: AuthOperationTemplate | None = Field(default=None, description="Optional connection test template")


class JwtAuth(StrictModel):
    """JWT-based authentication. Signing inputs are declared in `connection_contract.inputs`."""

    type: Literal["jwt"] = Field(..., description="Auth type")
    test: AuthOperationTemplate | None = Field(default=None, description="Optional connection test template")


class DbAuth(StrictModel):
    """Database connection authentication. Inputs declared in `connection_contract.inputs`."""

    type: Literal["db"] = Field(..., description="Auth type")
    test: AuthOperationTemplate | None = Field(default=None, description="Optional connection test template")


class CredentialsAuth(StrictModel):
    """Provider-specific credentials bundle (use only when no narrower type fits)."""

    type: Literal["credentials"] = Field(..., description="Auth type")
    test: AuthOperationTemplate | None = Field(default=None, description="Optional connection test template")


class AwsIamAuth(StrictModel):
    """AWS IAM / role / profile / credential-chain auth.

    Spec: §Authentication — must not declare OAuth-specific children. User
    values (account, role, profile, …) live in `connection_contract.inputs`.
    """

    type: Literal["aws_iam"] = Field(..., description="Auth type")
    test: AuthOperationTemplate | None = Field(default=None, description="Optional connection test template")


class NoneAuth(StrictModel):
    """Marker for connectors that need no authentication workflow.

    Spec: §Authentication — must not declare `authorize`, `token_exchange`, or
    `refresh`. `test` remains optional per the shared `Auth object fields`
    table.
    """

    type: Literal["none"] = Field(..., description="Auth type")
    test: AuthOperationTemplate | None = Field(default=None, description="Optional connection test template")


Auth = Annotated[
    Union[
        Annotated[ApiKeyAuth, UnionTag("api_key")],
        Annotated[BasicAuth, UnionTag("basic_auth")],
        Annotated[OAuth2AuthorizationCodeAuth, UnionTag("oauth2_authorization_code")],
        Annotated[OAuth2ClientCredentialsAuth, UnionTag("oauth2_client_credentials")],
        Annotated[JwtAuth, UnionTag("jwt")],
        Annotated[DbAuth, UnionTag("db")],
        Annotated[CredentialsAuth, UnionTag("credentials")],
        Annotated[AwsIamAuth, UnionTag("aws_iam")],
        Annotated[NoneAuth, UnionTag("none")],
    ],
    Discriminator("type"),
]


# --- Connection Contract (spec: §Connection Contract) ---


class InputSource(str, Enum):
    """How a connection contract input is provisioned.

    Closed enum: `user` or `platform`. Values produced after authentication
    belong in `post_auth_outputs`, not in `inputs` with a post-auth source.
    """
    USER = "user"
    PLATFORM = "platform"


class InputPhase(str, Enum):
    """Lifecycle phase a connection contract input is provisioned in.

    Closed enum: `pre_auth` or `auth`. Values produced after authentication
    belong in `post_auth_outputs`.
    """
    PRE_AUTH = "pre_auth"
    AUTH = "auth"


# Where a connection contract input is stored — closed enum:
# `connection.parameters` (non-secret) or `secrets` (referenced via `secret_refs`).
ContractInputStorage = Literal["connection.parameters", "secrets"]


# Where a post-auth output is stored — closed enum: `connection.selections`,
# `connection.discovered`, or `secrets`.
PostAuthOutputStorage = Literal[
    "connection.selections",
    "connection.discovered",
    "secrets",
]


class PostAuthOutputMode(str, Enum):
    """How a post-auth output's value is produced. Spec: §Post-Auth Outputs."""
    USER_SELECTION = "user_selection"
    AUTO_DISCOVERY = "auto_discovery"


class ConnectionContractInputUI(StrictModel):
    """UI hints for a connection contract input. Spec: §Connection Inputs.

    The spec calls these "Label, help text, widget, defaults, validation hints".
    `default` here is the displayed-default-in-the-form value (which may differ
    from the contract-resolution default on `ConnectionContractInput.default`).
    """


    label: str | None = Field(default=None, description="Display label for the input")
    help_text: str | None = Field(default=None, description="Inline help text shown to the user")
    widget: str | None = Field(default=None, description="Widget hint (text, password, select, textarea, number, ...)")
    placeholder: str | None = Field(default=None, description="Placeholder text shown in empty inputs")
    default: Any | None = Field(default=None, description="Default value to pre-fill in the form widget")
    options: list[FormFieldOption] | None = Field(
        default=None,
        description=(
            "Options for select-style widgets. When both `options` and the "
            "input's `enum` are provided they must enumerate the same value "
            "set — `ui.options` may not omit any `enum` value or add a value "
            "not in `enum` (spec: §Connection Inputs)."
        ),
    )


class ConnectionContractInput(AdvisoryValidated, StrictModel):
    """One submitted/provisioned value declared by the connection contract.

    Spec: §Connection Inputs. The combination of `name` and `storage` determines
    the runtime reference path (e.g. `connection.parameters.host`,
    `secrets.api_key`).
    """


    source: InputSource = Field(
        ...,
        description="How the value is provisioned (closed enum: user, platform).",
    )
    phase: InputPhase = Field(
        ...,
        description=(
            "Lifecycle phase the value is provisioned in (closed enum: "
            "pre_auth, auth)."
        ),
    )
    storage: ContractInputStorage = Field(
        ...,
        description=(
            "Where the resolved value is durably stored (closed enum: "
            "connection.parameters, secrets)."
        ),
    )
    type: Literal["string", "integer", "number", "boolean", "array", "object"] = Field(
        ...,
        description=(
            "JSON value type used for validation and coercion. Spec: "
            "§Connection Inputs — closed vocabulary."
        ),
    )
    required: bool = Field(..., description="Whether resolution must produce a value")
    default: Any | None = Field(default=None, description="Connector-defined default for optional inputs")
    enum: list[Any] | None = Field(
        default=None,
        description="Authoritative allowed-value list for scalar inputs (non-empty when present).",
    )
    secret: bool | None = Field(
        default=None,
        description=(
            "Required when `storage` is `secrets`; must be `true` iff `storage` "
            "is `secrets`. Otherwise omitted or `false`."
        ),
    )
    format: str | None = Field(default=None, description="Format hint (e.g. 'uri', 'date-time')")
    pattern: str | None = Field(default=None, description="Regex pattern for string validation")
    ui: ConnectionContractInputUI | None = Field(default=None, description="UI rendering hints")

    @model_validator(mode="after")
    def _consistency(self) -> "ConnectionContractInput":
        is_secret_storage = self.storage == "secrets"
        if is_secret_storage and self.secret is not True:
            raise ValueError(
                "storage='secrets' requires secret=true "
                "(spec: §Connection Inputs — secret iff storage='secrets')"
            )
        if not is_secret_storage and self.secret is True:
            raise ValueError(
                "secret=true requires storage='secrets' "
                "(spec: §Connection Inputs — secret iff storage='secrets')"
            )
        # ui.options ≡ enum (ADV-CONN-001) and default ∈ enum (ADV-CONN-002) are
        # relational rules enforced by the advisory registry, not here.
        if self.enum is not None and len(self.enum) == 0:
            raise ValueError(
                "enum must be non-empty when present "
                "(spec: §Connection Inputs — enum is the authoritative "
                "allowed-value list)"
            )
        return self


# Python attribute names of the five mutually-exclusive operator keys; `in_`
# is aliased to the `in` grammar key.
_CONDITION_OPERATOR_FIELDS = ("eq", "in_", "not_in", "present", "regex")
_CONDITION_OPERATOR_ALIASES = {"in_": "in"}

# A predicate operand is a JSON scalar — never a container, null, or a
# non-finite float. NaN/Infinity are not JSON numbers and the published schema
# rejects them; `model_dump_json` would also emit them as `null`, silently
# dropping the operator (see `_exactly_one_operator`). A null operand does not
# count as a declared operator either.
ConditionScalar = str | int | Annotated[float, Field(allow_inf_nan=False)] | bool

# Structural mirror of `_exactly_one_operator` for the published JSON Schema, so
# author-time schema validation agrees with the model. `oneOf` matches exactly
# one branch; each branch requires its operator present AND non-null, so a
# predicate with zero or multiple non-null operators fails schema validation too
# — not only the Pydantic model. Keys are the wire aliases (`in`, not `in_`).
_PREDICATE_EXACTLY_ONE_OPERATOR: dict[str, Any] = {
    "oneOf": [
        {"required": [alias], "properties": {alias: {"not": {"type": "null"}}}}
        for alias in ("eq", "in", "not_in", "present", "regex")
    ]
}


class ConnectionConditionPredicate(StrictModel):
    """A single connection-input test used in
    `connection_contract.validation.rules[].when`: declares `field` and exactly
    one operator key (`eq`/`in`/`not_in`/`present`/`regex`).
    """

    model_config = ConfigDict(
        extra="forbid", json_schema_extra=_PREDICATE_EXACTLY_ONE_OPERATOR
    )

    field: str = Field(
        ...,
        min_length=1,
        description="Connection input key or saved connection field path the predicate tests.",
    )
    eq: ConditionScalar | None = Field(
        default=None, description="Scalar value the field must equal."
    )
    in_: list[ConditionScalar] | None = Field(
        default=None,
        alias="in",
        min_length=1,
        description="Non-empty array of allowed scalar values.",
    )
    not_in: list[ConditionScalar] | None = Field(
        default=None,
        min_length=1,
        description="Non-empty array of disallowed scalar values.",
    )
    present: Literal[True] | None = Field(
        default=None,
        description="Boolean literal true; the field must resolve to a non-empty value.",
    )
    regex: str | None = Field(
        default=None,
        min_length=1,
        description="Regular expression matched against the resolved string value.",
    )

    @field_validator("present", mode="before")
    @classmethod
    def _present_is_strictly_boolean(cls, v: Any) -> Any:
        # `Literal[True]` compares by equality, so `1`/`1.0` (== True) would slip
        # through; the wire grammar and the schema's `const: true` require a real
        # boolean. Reject any non-bool before the Literal check.
        if v is not None and not isinstance(v, bool):
            raise ValueError("present must be the boolean literal true")
        return v

    @model_validator(mode="after")
    def _exactly_one_operator(self) -> "ConnectionConditionPredicate":
        # Count operators by usable (non-null) value, not key presence. A
        # serialized predicate (a `by_alias` dump) carries the four unused
        # operators as explicit `null`, and an authored `eq: null` is not a
        # usable test — connection inputs are strings/enums/secrets, never JSON
        # null — so a null-valued operator key never counts as declared.
        declared = sorted(
            _CONDITION_OPERATOR_ALIASES.get(f, f)
            for f in _CONDITION_OPERATOR_FIELDS
            if getattr(self, f) is not None
        )
        if len(declared) != 1:
            raise ValueError(
                "a connection condition predicate must declare exactly one "
                "operator key (eq/in/not_in/present/regex); "
                f"got {declared or 'none'}"
            )
        return self


class ConnectionContractValidationRule(StrictModel):
    """Cross-input declarative validation rule. Spec: §Cross-Input Validation."""

    when: ConnectionConditionPredicate = Field(
        ..., description="Predicate that decides whether the rule applies"
    )
    require: list[str] | None = Field(default=None, description="Fields required when predicate matches")
    forbid: list[str] | None = Field(default=None, description="Fields forbidden when predicate matches")
    message: str | None = Field(default=None, description="Human-readable validation error")


class ConnectionContractValidation(StrictModel):
    """Cross-input validation block. Spec: §Cross-Input Validation."""


    rules: list[ConnectionContractValidationRule] = Field(
        default_factory=list,
        description="Cross-input validation rules; per-input rules belong on the input itself",
    )


class PostAuthOperationRequest(StrictModel):
    """Request template used by `options_request` / `discovery_request` to populate
    a post-auth output. Spec: §Post-Auth Outputs.
    """


    transport_ref: str | None = Field(
        default=None,
        description="Named transport this request is dispatched through; defaults to `default_transport`",
    )
    method: str | None = Field(
        default=None,
        description=(
            "HTTP method. Engines treat absence as GET; declare it explicitly to "
            "signal the intent to non-HTTP transports."
        ),
    )
    path: str = Field(
        ...,
        min_length=1,
        description="Path or relative URL on the selected transport",
    )
    headers: dict[str, Any] | None = Field(
        default=None,
        description="Request headers; values may be literals, `{ref}`, `{template}`, or `{function}`",
    )
    body: Any | None = Field(default=None, description="Request body; type depends on transport/encoding")


class PostAuthOutput(StrictModel):
    """Durable post-auth output produced after authentication. Spec: §Post-Auth Outputs.

    The post-auth output field set is closed except for `x-*` extension
    metadata: `mode`, `storage`, `type`, `format`, `ui`, `options_request`,
    `options_path`, `discovery_request`, `value_path`, `label_path`. `source`,
    `phase`, `required`, and `secret` are explicitly NOT valid post-auth
    output fields — `source`/`phase` are inherent to the enclosing
    `post_auth_outputs` map; activation enforcement lives on
    `required_for_activation`; secrecy is determined by `storage`.
    """

    mode: PostAuthOutputMode = Field(
        ..., description="Closed enum: `user_selection` or `auto_discovery`."
    )
    storage: PostAuthOutputStorage = Field(
        ...,
        description=(
            "Closed enum. Must be `connection.selections` for `user_selection`, "
            "or `connection.discovered`/`secrets` for `auto_discovery`."
        ),
    )
    type: Literal["string", "integer", "number", "boolean", "array", "object"] = Field(
        ...,
        description=(
            "JSON value type used for validation and coercion. Spec: "
            "§Post-Auth Outputs — same closed vocabulary as input `type`."
        ),
    )
    format: str | None = Field(default=None, description="Optional format constraint such as `uri`.")
    ui: ConnectionContractInputUI | None = Field(default=None, description="UI rendering hints")
    options_request: PostAuthOperationRequest | None = Field(
        default=None,
        description=(
            "Request that returns selectable options. Required for "
            "`user_selection`; forbidden for `auto_discovery`."
        ),
    )
    options_path: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Response body path to the option array. Defaults to the response "
            "body root. Optional for `user_selection`; forbidden for "
            "`auto_discovery`."
        ),
    )
    discovery_request: PostAuthOperationRequest | None = Field(
        default=None,
        description=(
            "Request that returns the value to auto-discover. Required for "
            "`auto_discovery`; forbidden for `user_selection`."
        ),
    )
    value_path: str = Field(
        ...,
        min_length=1,
        description="Response path used to extract the stored value",
    )
    label_path: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Response path used as the option label. Optional for "
            "`user_selection`; forbidden for `auto_discovery`."
        ),
    )

    @model_validator(mode="after")
    def _mode_consistency(self) -> "PostAuthOutput":
        if self.mode is PostAuthOutputMode.USER_SELECTION:
            if self.options_request is None:
                raise ValueError(
                    "mode='user_selection' requires `options_request` "
                    "(spec: §Post-Auth Outputs)"
                )
            if self.discovery_request is not None:
                raise ValueError(
                    "mode='user_selection' must omit `discovery_request` "
                    "(spec: §Post-Auth Outputs)"
                )
            if self.storage != "connection.selections":
                raise ValueError(
                    "mode='user_selection' requires storage='connection.selections' "
                    "(spec: §Post-Auth Outputs)"
                )
        else:
            if self.discovery_request is None:
                raise ValueError(
                    "mode='auto_discovery' requires `discovery_request` "
                    "(spec: §Post-Auth Outputs)"
                )
            for forbidden, name in (
                (self.options_request, "options_request"),
                (self.options_path, "options_path"),
                (self.label_path, "label_path"),
            ):
                if forbidden is not None:
                    raise ValueError(
                        f"mode='auto_discovery' must omit `{name}` "
                        "(spec: §Post-Auth Outputs)"
                    )
            if self.storage not in ("connection.discovered", "secrets"):
                raise ValueError(
                    "mode='auto_discovery' requires storage='connection.discovered' "
                    "or 'secrets' (spec: §Post-Auth Outputs)"
                )
        return self


class ConnectionContract(StrictModel):
    """Connector-level contract for what a saved connection must contribute.

    Source of truth for connection form rendering, save-time validation, drift
    detection, and template reference validation. Spec: §Connection Contract.

    No standalone `version` field — drift detection rides on `connector_version`
    semver: patch = no shape change, minor = additive shape change, major =
    breaking shape change.
    """

    inputs: dict[str, ConnectionContractInput] = Field(
        default_factory=dict,
        description="Declared submitted/provisioned inputs keyed by their runtime `name`",
    )
    post_auth_outputs: dict[str, PostAuthOutput] = Field(
        default_factory=dict,
        description="Declared post-auth outputs keyed by their runtime `name`",
    )
    required_for_activation: list[str] = Field(
        default_factory=list,
        description=(
            "Runtime reference paths that must resolve before the connection can "
            "be marked active (e.g. 'connection.parameters.host', 'secrets.password')"
        ),
    )
    validation: ConnectionContractValidation | None = Field(
        default=None,
        description="Cross-input validation block; per-input rules belong on the input",
    )


# --- Resource Discovery (spec: §Resource Discovery) ---


class ResourceDiscoveryImplementation(StrictModel):
    """Discovery strategy implementation source. Spec: §Resource Discovery."""


    type: Literal["builtin", "connector_plugin"] = Field(..., description="Implementation kind")
    entrypoint: str | None = Field(
        default=None,
        description=(
            "Plugin entrypoint string (e.g. `analitiq_acme.discovery:AcmeCatalogDiscovery`). "
            "Required when type=connector_plugin; must be omitted when type=builtin."
        ),
    )

    @model_validator(mode="after")
    def _entrypoint_matches_type(self) -> "ResourceDiscoveryImplementation":
        if self.type == "connector_plugin" and not self.entrypoint:
            raise ValueError(
                "type='connector_plugin' requires `entrypoint` (spec: §Resource Discovery)"
            )
        if self.type == "builtin" and self.entrypoint is not None:
            raise ValueError(
                "type='builtin' must not declare `entrypoint` (spec: §Resource Discovery)"
            )
        return self


class ResourceDiscoveryTriggers(StrictModel):
    """When list/describe discovery actions run."""


    list_resources: Literal[
        "on_activation",
        "on_connection_selected",
        "on_resource_selected",
        "on_demand",
        "scheduled",
    ] | None = Field(default=None, description="Trigger for the list-resources action")
    describe_resource: Literal[
        "on_activation",
        "on_connection_selected",
        "on_resource_selected",
        "on_demand",
        "scheduled",
    ] | None = Field(default=None, description="Trigger for the describe-resource action")


class ResourceDiscovery(StrictModel):
    """Declarative resource discovery for connection-scoped private endpoints.

    Spec: §Resource Discovery. Produces connection-scoped endpoints and type
    maps under `connection.endpoints` / `connection.type_map`.
    """


    transport_ref: str | None = Field(
        default=None,
        description="Named transport used for discovery; defaults to `default_transport`",
    )
    strategy: str = Field(..., description="Registered discovery strategy ID")
    implementation: ResourceDiscoveryImplementation | None = Field(
        default=None,
        description="Strategy implementation source; omit to use a builtin strategy",
    )
    triggers: ResourceDiscoveryTriggers | None = Field(
        default=None,
        description="When list/describe discovery actions run",
    )
    produces: list[Literal["connection.endpoints", "connection.type_map"]] = Field(
        default_factory=list,
        description="Artifacts written by discovery",
    )
    options: dict[str, Any] | None = Field(
        default=None,
        description="Strategy-specific declarative options (e.g. exclude_schemas)",
    )


# --- Function Expressions ---


class BasicAuthDerivedInput(StrictModel):
    """Input shape for `basic_auth` function expression."""

    username: Any = Field(..., description="Username value-expression (`{ref}`, `{template}`, or literal)")
    password: Any = Field(..., description="Password value-expression (`{ref}`, `{template}`, or literal)")


class BasicAuthDerived(StrictModel):
    """`basic_auth` — build a Basic auth credential from username/password."""

    function: Literal["basic_auth"] = Field(description="Function discriminator")
    input: BasicAuthDerivedInput = Field(..., description="Username/password value-expressions")


class Base64EncodeDerived(StrictModel):
    """`base64_encode` — base64-encode a string or bytes value."""

    function: Literal["base64_encode"] = Field(description="Function discriminator")
    input: Any = Field(..., description="Scalar value-expression to encode")


class LookupDerived(StrictModel):
    """`lookup` — map an input value through a connector-declared inline map."""

    function: Literal["lookup"] = Field(description="Function discriminator")
    input: Any = Field(..., description="Scalar value-expression (typically `{ref: connection.parameters.<X>}`)")
    map: dict[str, Any] = Field(
        ...,
        description="Inline value map: input-value → mapped-output JSON value",
    )


class UrlEncodeDerived(StrictModel):
    """`url_encode` — percent-encode a scalar for use inside a URL component."""

    function: Literal["url_encode"] = Field(description="Function discriminator")
    input: Any = Field(..., description="Scalar value-expression to encode")
    safe: str | None = Field(
        default=None,
        description="Characters to leave unencoded (default empty string — encode everything)",
    )


# `pkce_challenge_s256` and `jwt_sign` are `planned` in the callable-function
# catalog: connectors must not reference them yet — validation rejects unknown
# function names. Add their Pydantic shapes when the engine ships them; until
# then they intentionally have no model.


DerivedValue = Annotated[
    Union[
        Annotated[BasicAuthDerived, UnionTag("basic_auth")],
        Annotated[Base64EncodeDerived, UnionTag("base64_encode")],
        Annotated[LookupDerived, UnionTag("lookup")],
        Annotated[UrlEncodeDerived, UnionTag("url_encode")],
    ],
    Discriminator("function"),
]
"""Resolution-time function expression, discriminated by `function`.

Per-function input shapes are enforced at the model level — connectors that
reference a function must use that function's required input shape, and `map`
is exclusive to `lookup`.
"""


# --- String-valued value expressions (spec: §Value Expressions) ---
#
# A field that must resolve to a non-empty URL string models its
# value-expression object forms as these typed models — NOT a bare
# `dict[str, Any]` — so the PUBLISHED JSON Schema constrains the shape exactly
# as the Pydantic model does. Schema and validator stay aligned by
# construction; there is no imperative `@field_validator` the schema misses.


class TemplateExpression(StrictModel):
    """`{template}` form: a `${scope.path}`-bearing string resolved at runtime."""

    template: Annotated[str, StringConstraints(min_length=1)] = Field(
        description="Template string carrying `${scope.path}` placeholders."
    )


class RefExpression(StrictModel):
    """`{ref}` form: a dotted path into the resolution context."""

    ref: Annotated[str, StringConstraints(min_length=1)] = Field(
        description="Dotted reference path, e.g. `connection.parameters.host`."
    )


class LiteralStringExpression(StrictModel):
    """`{literal}` form constrained to a non-empty string — the shape a
    URL-valued field accepts (a general `{literal}` may wrap any value, but a
    URL must be a string)."""

    literal: Annotated[str, StringConstraints(min_length=1)] = Field(
        description="A verbatim, non-empty string."
    )


UrlValueExpression = Union[
    Annotated[str, StringConstraints(min_length=1)],
    TemplateExpression,
    RefExpression,
    LiteralStringExpression,
    DerivedValue,
]
"""A value expression that resolves to a non-empty URL string: a literal string
or one of the typed object forms above. Used where a bare `dict[str, Any]`
would leave the published schema unconstrained."""


# --- Transport Contracts (spec: §Transport Contracts) ---


class TransportRateLimit(StrictModel):
    """Rate limit declaration for a transport. Spec: §Transport Contracts."""


    max_requests: int = Field(..., ge=1, description="Maximum requests allowed per window")
    time_window_seconds: Any = Field(..., description="Window length in seconds (int or value-expression)")


class HttpTransport(AdvisoryValidated, StrictModel):
    """HTTP transport contract. Spec: §Transport Contracts."""

    transport_type: Literal["http"] = Field(description="Transport type discriminator")
    base_url: "UrlValueExpression | None" = Field(
        default=None,
        description=(
            "Base URL: a non-empty literal string, or a value-expression "
            "(`{template}`/`{ref}`/`{literal}`/function) resolving to one at "
            "connection-materialization time (e.g. a per-tenant host taken from "
            "`connection.parameters` or discovered post-auth via "
            "`connection.discovered`). May be omitted when this entry exists "
            "only to extend `transport_defaults`."
        ),
    )
    headers: dict[str, Any] | None = Field(
        default=None,
        description="Default request headers; values may be literals or expressions",
    )
    headers_remove: list[str] | None = Field(
        default=None,
        description="Header names to delete from inherited defaults (case-insensitive)",
    )
    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Request timeout in seconds",
    )
    rate_limit: "TransportRateLimit | None" = Field(
        default=None, description="Rate-limit policy"
    )


class DsnBinding(StrictModel):
    """Single binding entry inside a `url_template` DSN. Spec: §Transport Contracts."""

    value: Any = Field(..., description="Value-expression resolving to the raw binding value")
    encoding: Literal[
        "raw", "host", "url_userinfo", "url_path_segment", "url_query_key", "url_query_value"
    ] = Field(..., description="Generic encoding applied before substitution into the template")


class UrlTemplateDsn(StrictModel):
    """Connector-authored URL template DSN with structured bindings."""

    kind: Literal["url_template"] = Field(description="DSN kind")
    template: str = Field(
        ...,
        min_length=1,
        description="Connector-authored URL template containing `{binding}` placeholders.",
        # `{binding}` placeholders are the DSN grammar; the value-expression
        # `${...}` template syntax is not permitted in a DSN URL template. The
        # ECMA negative-lookahead pattern lives in json_schema_extra ONLY —
        # pydantic-core's rust regex rejects lookahead, so it cannot be a
        # `pattern=` StringConstraint; `_reject_template_expressions` is the
        # runtime mirror.
        json_schema_extra={"pattern": r"^(?![\s\S]*\$\{)[\s\S]*$"},
    )
    bindings: dict[str, DsnBinding] = Field(
        ...,
        min_length=1,
        description="Map keyed by placeholder name; each binding declares value + encoding.",
    )

    @field_validator("template")
    @classmethod
    def _reject_template_expressions(cls, v: str) -> str:
        if "${" in v:
            raise ValueError(
                "DSN template must not contain ${...} value-expression syntax; "
                "use {binding} placeholders declared in `bindings`"
            )
        return v

    @model_validator(mode="after")
    def _validate_placeholder_bindings(self) -> "UrlTemplateDsn":
        # Every `{placeholder}` in the template must resolve to a binding, and
        # every binding must be referenced by the template. `${...}` is already
        # rejected by `_reject_template_expressions`, so a bare `{name}` is
        # unambiguous. Spec: §Transport Contracts — DSN url_template.
        placeholders = set(re.findall(r"\{([^{}]+)\}", self.template))
        binding_keys = set(self.bindings)
        missing = placeholders - binding_keys
        if missing:
            raise ValueError(
                f"url_template references placeholder(s) {sorted(missing)} with "
                "no matching entry in `bindings`"
            )
        unused = binding_keys - placeholders
        if unused:
            raise ValueError(
                f"url_template declares binding(s) {sorted(unused)} not "
                "referenced by the template"
            )
        return self


class DatabaseTls(StrictModel):
    """Database transport TLS declaration. Spec: §Transport Contracts.

    Both fields resolve to plain strings at runtime. The interpretation of
    those strings (libpq vocabulary, MySQL vocabulary, etc.) is owned by the
    connector package's dialect — the schema is vocabulary-agnostic. No
    canonical mode set is enforced here; the connector's
    ``connection_contract.inputs[<field>].enum`` is the user-facing constraint.
    """

    mode: Any = Field(
        ...,
        description=(
            "TLS mode value-expression. Resolves to a plain string at runtime; "
            "interpretation (e.g. libpq `verify-full` vs MySQL `REQUIRED`) is "
            "owned by the connector package's dialect."
        ),
    )
    ca_certificate: Any | None = Field(
        default=None,
        description=(
            "CA certificate value-expression. Resolves to a plain string at "
            "runtime; required when `mode` implies certificate verification."
        ),
    )


class SqlAlchemyTransport(StrictModel):
    """SQLAlchemy database transport contract. Spec: §Transport Contracts."""

    transport_type: Literal["sqlalchemy"] = Field(description="Transport type discriminator")
    driver: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*\+[a-z][a-z0-9_]*$",
        description=(
            "SQLAlchemy driver in `dialect+driver` form "
            "(e.g. `postgresql+asyncpg`, `mysql+aiomysql`, "
            "`redshift+redshift_connector`). May name a sync or an async "
            "DBAPI — no driver allow-list is imposed. The named driver must "
            "be a real SQLAlchemy dialect registration; that is checked at "
            "transport build, not here. Optional — SQLAlchemy can derive "
            "the driver from the DSN scheme — but declare it so the "
            "sync/async choice is explicit to a reader."
        ),
    )
    dsn: UrlTemplateDsn | None = Field(
        default=None,
        description="Structured URL-template DSN with bindings and encodings.",
    )
    tls: DatabaseTls | None = Field(
        default=None,
        description="Generic TLS declaration; runtime materializes driver-specific args.",
    )
    options: dict[str, Any] | None = Field(
        default=None,
        description="Engine options (pool_size, isolation_level, etc.)",
    )


class AdbcTransport(StrictModel):
    """ADBC (Arrow Database Connectivity) database transport contract. Spec: §Transport Contracts."""

    model_config = ConfigDict(
        extra="forbid",
        # Surface the `_require_dsn_or_kwargs` model-validator constraint
        # in the published JSON Schema too, so external consumers
        # validating against the JSON Schema alone (FE, connector-author
        # tooling, third-party validators) catch the empty-transport case
        # before it reaches Pydantic runtime.
        #
        # JSON Schema `required` only checks property existence — it
        # accepts `{"dsn": null}`. Mirror Pydantic's `_require_dsn_or_kwargs`
        # by also asserting the present branch is not null (and, for
        # `db_kwargs`, not empty), so a schema-only validator rejects the
        # same payloads Pydantic does instead of letting them slip
        # through and explode at the backend.
        json_schema_extra={
            "anyOf": [
                {
                    "required": ["dsn"],
                    "properties": {"dsn": {"not": {"type": "null"}}},
                },
                {
                    "required": ["db_kwargs"],
                    "properties": {
                        "db_kwargs": {
                            "not": {"type": "null"},
                            "minProperties": 1,
                        },
                    },
                },
            ],
        },
    )

    transport_type: Literal["adbc"] = Field(description="Transport type discriminator")
    driver: Literal["postgresql", "snowflake", "bigquery"] = Field(
        ...,
        description=(
            "ADBC driver family identifier. Closed to the drivers the engine "
            "actually ships. Required at the connector layer "
            "(unlike `SqlAlchemyTransport.driver`); the ADBC dispatcher "
            "selects the matching driver and cannot "
            "fall back. Not to be confused with the format-dialect inputs "
            "on file/s3 connectors."
        ),
    )
    dsn: UrlTemplateDsn | None = Field(
        default=None,
        description=(
            "Structured URL-template DSN with bindings and encodings. "
            "Connector-authored layout; the pipeline runtime renders and "
            "substitutes bindings before invoking the ADBC driver. "
            "Note: schema introspection takes a separate path — it rebuilds "
            "the URI from canonical credential fields and does NOT consume "
            "the connector-authored DSN template (symmetric to the SQLAlchemy "
            "introspection path). Optional individually, but `AdbcTransport` "
            "requires at least one of `dsn` / `db_kwargs`; ADBC drivers "
            "that accept all connection state via `db_kwargs` (e.g. "
            "Snowflake) may omit `dsn`."
        ),
    )
    db_kwargs: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Driver-specific keyword arguments passed to the ADBC driver on "
            "connect (e.g. Snowflake account/warehouse, Postgres options). "
            "Values may be literals or value expressions (`{\"ref\": \"...\"}`, "
            "`{\"template\": \"...\"}`, `{\"function\": \"...\"}`); the "
            "pipeline runtime resolves them before invoking the driver. "
            "Note: schema introspection (`list_schemas`, `test_credentials`, "
            "…) takes a separate path — it constructs driver kwargs from "
            "canonical credential fields (`account`, `warehouse`, `username`, "
            "…) and does NOT consume connector-authored `db_kwargs`. This "
            "matches the SQLAlchemy introspection path, which ignores the "
            "connector DSN template and rebuilds the URI from credential fields. "
            "Optional individually, but `AdbcTransport` requires at least "
            "one of `dsn` / `db_kwargs`."
        ),
    )

    @model_validator(mode="after")
    def _require_dsn_or_kwargs(self) -> "AdbcTransport":
        if self.dsn is None and not self.db_kwargs:
            raise ValueError(
                "AdbcTransport requires at least one of `dsn` or `db_kwargs` "
                "— the transport must carry some connection state."
            )
        return self


class S3CredentialsBlock(StrictModel):
    """Static S3 credentials block (access key + secret access key). Spec: §Transport Contracts."""

    access_key_id: str = Field(..., min_length=1, description="Access key id")
    secret_access_key: Any = Field(..., description="Secret access key value-expression")
    session_token: Any | None = Field(default=None, description="Optional session token value-expression")


class S3Transport(StrictModel):
    """S3 transport contract."""

    transport_type: Literal["s3"] = Field(description="Transport type discriminator")
    bucket: str = Field(..., min_length=1, description="Bucket name")
    region: Any = Field(..., description="Region value-expression")
    prefix: Any | None = Field(
        default=None,
        description=(
            "Object key prefix or template value-expression. May embed "
            "`{stream_alias}` / `{run_id}` / `{date}` placeholders."
        ),
    )
    format: Any = Field(
        ...,
        description=(
            "Output format value-expression. Closed value-level enum: "
            "`csv` | `jsonl`."
        ),
    )
    dialect: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Format-specific dialect options as a value-expression bundle. "
            "Schema is `format`-dependent — see spec §Output Format."
        ),
    )
    credentials: S3CredentialsBlock = Field(
        ...,
        description="Static AWS credentials; required for the v1 contract.",
    )


class FileTransport(StrictModel):
    """Filesystem transport contract."""

    transport_type: Literal["file"] = Field(description="Transport type discriminator")
    path: str = Field(
        ...,
        min_length=1,
        description=(
            "Target output path. May embed `{stream_alias}` / `{run_id}` / "
            "`{date}` placeholders."
        ),
    )
    format: Any = Field(
        ...,
        description=(
            "Output format value-expression. Closed value-level enum: "
            "`csv` | `jsonl`."
        ),
    )
    dialect: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Format-specific dialect options as a value-expression bundle. "
            "Schema is `format`-dependent — see spec §Output Format."
        ),
    )


class StdoutTransport(StrictModel):
    """Stdout transport contract."""

    transport_type: Literal["stdout"] = Field(description="Transport type discriminator")
    format: Any | None = Field(
        default=None,
        description=(
            "Output format value-expression. Closed value-level enum: "
            "`csv` | `jsonl`. Default `jsonl`."
        ),
    )


Transport = Annotated[
    Union[
        Annotated[HttpTransport, UnionTag("http")],
        Annotated[SqlAlchemyTransport, UnionTag("sqlalchemy")],
        Annotated[AdbcTransport, UnionTag("adbc")],
        Annotated[S3Transport, UnionTag("s3")],
        Annotated[FileTransport, UnionTag("file")],
        Annotated[StdoutTransport, UnionTag("stdout")],
    ],
    Discriminator("transport_type"),
]
"""Named transport contract entry. Spec: §Transport Contracts."""


class TransportDefaults(AdvisoryValidated, StrictModel):
    """Defaults merged into every entry of `transports`. Spec: §Transport Contracts."""

    transport_type: Literal["http", "sqlalchemy", "adbc", "s3", "file", "stdout"] | None = Field(
        default=None,
        description=(
            "Default transport type inherited by every entry of `transports` that "
            "does not declare its own."
        ),
    )
    headers: dict[str, Any] | None = Field(
        default=None,
        description="Default request headers shared across HTTP transport entries.",
    )
    headers_remove: list[str] | None = Field(
        default=None,
        description="Header names to delete from any inherited defaults",
    )
    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Default request timeout in seconds",
    )
    rate_limit: "TransportRateLimit | None" = Field(
        default=None, description="Default rate-limit policy"
    )
    options: dict[str, Any] | None = Field(default=None, description="Default driver/engine options")


# --- Connector Models ---


# file/s3/stdout connectors carry no post-auth workflow, so their
# `connection_contract` must declare neither `post_auth_outputs` nor
# `required_for_activation` (spec-normative; previously unenforced). The schema
# fragment pins both to empty; `_reject_post_auth_contract` is the runtime mirror
# (#977 T2, Q2 = schema + validator).
_FILE_LIKE_CONNECTION_CONTRACT_RULES: dict[str, Any] = {
    "allOf": [
        {
            "properties": {
                "connection_contract": {
                    "properties": {
                        "post_auth_outputs": {"maxProperties": 0},
                        "required_for_activation": {"maxItems": 0},
                    }
                }
            }
        }
    ]
}


def _reject_post_auth_contract(contract: "ConnectionContract", kind: str) -> None:
    if contract.post_auth_outputs:
        raise ValueError(
            f"{kind} connectors must not declare "
            "connection_contract.post_auth_outputs"
        )
    if contract.required_for_activation:
        raise ValueError(
            f"{kind} connectors must not declare "
            "connection_contract.required_for_activation"
        )


class ConnectorBase(AdvisoryValidated, StrictModel):
    """Base connector model — fields shared by every connector kind.

    `connector_id` is the connector's canonical identifier and its registry
    repo name (e.g. `postgres`, `xero`, `pipedrive`).
    """

    schema_url: Annotated[
        str, StringConstraints(pattern=_CONNECTOR_SCHEMA_URL_PATTERN)
    ] | None = Field(
        default=None,
        alias="$schema",
        description=(
            "Connector schema URL (optional in API payloads, required for "
            "standalone files). Accepts the published URL on any environment "
            "host (schemas.analitiq.<tld>) so a document authored against the "
            "canonical analitiq.ai URL validates against the per-environment "
            "schema the engine fetches at runtime."
        ),
    )

    connector_id: str = Field(
        ...,
        pattern=SLUG_PATTERN,
        description=(
            "Connector's canonical identifier and registry repo name "
            "(e.g. `postgres`, `xero`, `pipedrive`). Must be lowercase "
            "alphanumeric with hyphens/underscores, starting with a letter "
            "or a digit."
        ),
    )

    display_name: str | None = Field(
        default=None,
        min_length=DISPLAY_NAME_MIN,
        max_length=DISPLAY_NAME_MAX,
        pattern=NO_EDGE_WHITESPACE_PATTERN,
        description="User-facing connector label (1-120 chars, no leading/trailing whitespace).",
    )
    description: str | None = Field(
        default=None,
        max_length=DESCRIPTION_MAX,
        description="Human-readable connector description (≤2000 chars).",
    )
    documentation_url: str | None = Field(
        default=None,
        max_length=2048,
        pattern=r"^https?://",
        description=(
            "URI string pointing at the upstream provider or spec documentation "
            "for this connector. Connector-only metadata. Must be an absolute "
            "http(s) URL of ≤2048 characters when present."
        ),
        json_schema_extra={"format": "uri"},
    )
    tags: list[TrimmedTag] | None = Field(
        default=None,
        max_length=TAGS_MAX,
        json_schema_extra={"uniqueItems": True},
        description="Grouping/search labels (max 50 unique trimmed strings of 1-64 chars).",
    )

    version: str = Field(
        ...,
        pattern=SEMVER_PATTERN,
        description=(
            "Connector release semantic version. Saved connections record this "
            "version for drift detection."
        ),
    )

    default_transport: str = Field(
        ...,
        min_length=1,
        description="Name of the entry in `transports` used when an operation omits `transport_ref`.",
    )
    transports: dict[str, "Transport"] = Field(
        ...,
        min_length=1,
        description=(
            "Named transport contracts discriminated by `transport_type` "
            "(http | sqlalchemy | adbc | s3 | file | stdout). Each entry inherits "
            "`transport_defaults` and supplies type-specific fields."
        ),
    )
    transport_defaults: TransportDefaults | None = Field(
        default=None,
        description=(
            "Defaults merged into every entry of `transports` (object-valued "
            "fields like `headers` deep-merge; scalars override per-entry)."
        ),
    )
    auth: Auth = Field(
        ...,
        description="Authentication workflow definition.",
    )
    connection_contract: "ConnectionContract" = Field(
        ...,
        description=(
            "Connector-level connection contract: declared inputs, post-auth "
            "outputs, activation requirements, and cross-input validation."
        ),
    )
    resource_discovery: ResourceDiscovery | None = Field(
        default=None,
        description="Resource discovery declarations for dynamic or post-auth resources.",
    )

    @field_validator("display_name")
    @classmethod
    def _validate_display_name_field(cls, v: str | None) -> str | None:
        return validate_display_name(v)

    @field_validator("tags")
    @classmethod
    def _validate_tags_field(cls, v: list[str] | None) -> list[str] | None:
        return validate_tags(v)

    @model_validator(mode="before")
    @classmethod
    def _inherit_transport_type(cls, data: Any) -> Any:
        """Fill in `transport_type` on each `transports.<name>` entry from `transport_defaults.transport_type`.

        Spec: §Transport Contracts. The Pipedrive multi-origin example declares
        `transport_type: http` once on `transport_defaults` and omits it on
        per-entry objects; without this pre-merge the discriminated union
        dispatch would reject those entries.

        Mirrors `_annotate_transport_inheritance` in `scripts/render_schemas.py`:
        a malformed `transport_defaults` or `transports` raises here rather
        than silently passing through to a misleading "missing transport_type"
        error from the discriminator.
        """
        if not isinstance(data, dict):
            return data
        defaults = data.get("transport_defaults")
        if defaults is not None and not isinstance(defaults, dict):
            raise ValueError("transport_defaults must be an object")
        transports = data.get("transports")
        if transports is not None and not isinstance(transports, dict):
            raise ValueError("transports must be an object keyed by transport name")
        default_kind = defaults.get("transport_type") if isinstance(defaults, dict) else None
        if not default_kind or not isinstance(transports, dict):
            return data
        for _name, entry in transports.items():
            if isinstance(entry, dict) and "transport_type" not in entry:
                entry["transport_type"] = default_kind
        return data

    @model_validator(mode="after")
    def _transport_refs_resolvable(self) -> "ConnectorBase":
        """Every `transport_ref` site must point at a declared transport.

        Spec: §Transport Selection — auth ops, post-auth requests, and resource
        discovery may declare `transport_ref`; an unresolved reference would
        otherwise survive Pydantic validation and only fail at runtime.
        """
        transports = set(self.transports.keys())

        def _check(ref: str | None, where: str) -> None:
            if ref is None or ref in transports:
                return
            raise ValueError(
                f"{where} transport_ref={ref!r} is not declared in `transports` "
                f"(declared: {sorted(transports)!r}; spec: §Transport Selection)"
            )

        for op_name in ("authorize", "token_exchange", "refresh", "test"):
            op = getattr(self.auth, op_name, None)
            if op is not None:
                _check(op.transport_ref, f"auth.{op_name}")

        if self.resource_discovery is not None:
            _check(self.resource_discovery.transport_ref, "resource_discovery")

        for name, output in self.connection_contract.post_auth_outputs.items():
            for req_name in ("options_request", "discovery_request"):
                req = getattr(output, req_name)
                if req is not None:
                    _check(
                        req.transport_ref,
                        f"connection_contract.post_auth_outputs.{name}.{req_name}",
                    )
        return self

    @model_validator(mode="after")
    def _connection_contract_internal_refs(self) -> "ConnectorBase":
        contract = self.connection_contract
        # Spec: §Connection Inputs — "No two declarations in one
        # connection_contract may write the same saved storage path. ... applies
        # only to storage='secrets'." Build the path set with explicit duplicate
        # detection so collisions surface instead of silently collapsing.
        secret_paths: list[str] = []
        non_secret_paths: set[str] = set()
        for name, inp in contract.inputs.items():
            path = f"{inp.storage}.{name}"
            if inp.storage == "secrets":
                secret_paths.append(path)
            else:
                non_secret_paths.add(path)
        for name, out in contract.post_auth_outputs.items():
            path = f"{out.storage}.{name}"
            if out.storage == "secrets":
                secret_paths.append(path)
            else:
                non_secret_paths.add(path)

        duplicate_secrets = sorted({p for p in secret_paths if secret_paths.count(p) > 1})
        if duplicate_secrets:
            raise ValueError(
                f"connection_contract declares the same secret storage path more than once: "
                f"{duplicate_secrets!r} (spec: §Connection Inputs — secret-storage uniqueness)"
            )

        all_paths = set(secret_paths) | non_secret_paths
        unresolved = sorted(p for p in contract.required_for_activation if p not in all_paths)
        if unresolved:
            raise ValueError(
                f"connection_contract.required_for_activation paths {unresolved} do not "
                "resolve to any declared input or post_auth_output "
                "(spec: §Save-Time Validation)"
            )

        if contract.validation is not None:
            input_names = set(contract.inputs.keys())
            for idx, rule in enumerate(contract.validation.rules):
                when_field = rule.when.field
                if when_field not in input_names:
                    raise ValueError(
                        f"connection_contract.validation.rules[{idx}].when.field "
                        f"references undeclared input '{when_field}' (spec: §Cross-Input Validation)"
                    )
                for kind in ("require", "forbid"):
                    refs = getattr(rule, kind) or []
                    bad = [r for r in refs if r not in input_names]
                    if bad:
                        raise ValueError(
                            f"connection_contract.validation.rules[{idx}].{kind} "
                            f"references undeclared inputs {bad} (spec: §Cross-Input Validation)"
                        )

        return self


class ApiConnector(ConnectorBase):
    """API-type connector. Provider configuration lives on `transports.<name>` (HttpTransport)."""

    kind: Literal[ConnectorKind.API] = Field(
        description="Connector kind discriminator"
    )


# A database connector's transports are the SQL families only (never http/s3/
# file/stdout). Narrowing the inherited `transports` value type to this union
# publishes a per-kind `oneOf` into the schema, so an external validator rejects
# a `sqlalchemy`-typed transport under, say, a `stdout` connector — which the
# broad `Transport` union on `ConnectorBase` would have accepted.
_DatabaseTransport = Annotated[
    Union[
        Annotated[SqlAlchemyTransport, UnionTag("sqlalchemy")],
        Annotated[AdbcTransport, UnionTag("adbc")],
    ],
    Discriminator("transport_type"),
]


class DatabaseConnector(ConnectorBase):
    """Relational (SQL) database connector. Provider configuration lives on `transports.<name>` (SqlAlchemyTransport or AdbcTransport)."""

    kind: Literal[ConnectorKind.DATABASE] = Field(
        description="Connector kind discriminator"
    )
    transports: dict[str, _DatabaseTransport] = Field(
        ...,
        min_length=1,
        description=(
            "Named database transport contracts (`sqlalchemy` | `adbc`), "
            "discriminated by `transport_type`."
        ),
    )


class NosqlConnector(ConnectorBase):
    """NoSQL database connector — wide-column, key-value, or graph stores
    (e.g. Cassandra, DynamoDB, Redis). Owns the same reusable-definition shape
    as `DatabaseConnector` and selects the `database-endpoint` document schema;
    the transport family is provider-specific and declared on `transports`."""

    kind: Literal[ConnectorKind.NOSQL] = Field(
        description="Connector kind discriminator"
    )


class DocumentConnector(ConnectorBase):
    """Document-store connector (e.g. MongoDB and other document databases).
    Owns the same reusable-definition shape as `DatabaseConnector` and selects
    the `database-endpoint` document schema; the transport family is
    provider-specific and declared on `transports`."""

    kind: Literal[ConnectorKind.DOCUMENT] = Field(
        description="Connector kind discriminator"
    )


class FileConnector(ConnectorBase):
    """Filesystem-backed connector. Provider configuration lives on `transports.<name>` (FileTransport)."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra=_FILE_LIKE_CONNECTION_CONTRACT_RULES
    )

    kind: Literal[ConnectorKind.FILE] = Field(
        description="Connector kind discriminator"
    )
    transports: dict[str, FileTransport] = Field(
        ...,
        min_length=1,
        max_length=1,
        description="The single named `file` transport (spec: §Transport Contracts).",
    )
    auth: NoneAuth = Field(
        ...,
        description="Filesystem connectors carry no auth workflow (`type: 'none'`).",
    )

    @model_validator(mode="after")
    def _validate_no_post_auth_contract(self) -> "FileConnector":
        _reject_post_auth_contract(self.connection_contract, "file")
        return self


class S3Connector(ConnectorBase):
    """S3-backed connector. Provider configuration lives on `transports.<name>` (S3Transport)."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra=_FILE_LIKE_CONNECTION_CONTRACT_RULES
    )

    kind: Literal[ConnectorKind.S3] = Field(
        description="Connector kind discriminator"
    )
    transports: dict[str, S3Transport] = Field(
        ...,
        min_length=1,
        max_length=1,
        description="The single named `s3` transport (spec: §Transport Contracts).",
    )
    auth: CredentialsAuth = Field(
        ...,
        description="S3 connectors authenticate with static AWS credentials (`type: 'credentials'`).",
    )

    @model_validator(mode="after")
    def _validate_no_post_auth_contract(self) -> "S3Connector":
        _reject_post_auth_contract(self.connection_contract, "s3")
        return self


class StdoutConnector(ConnectorBase):
    """Stdout connector — debug/print destination. `StdoutTransport` is a marker."""

    model_config = ConfigDict(
        extra="forbid", json_schema_extra=_FILE_LIKE_CONNECTION_CONTRACT_RULES
    )

    kind: Literal[ConnectorKind.STDOUT] = Field(
        description="Connector kind discriminator"
    )
    transports: dict[str, StdoutTransport] = Field(
        ...,
        min_length=1,
        max_length=1,
        description="The single named `stdout` transport (spec: §Transport Contracts).",
    )
    auth: NoneAuth = Field(
        ...,
        description="Stdout connectors carry no auth workflow (`type: 'none'`).",
    )

    @model_validator(mode="after")
    def _validate_no_post_auth_contract(self) -> "StdoutConnector":
        _reject_post_auth_contract(self.connection_contract, "stdout")
        return self


# --- Discriminated union (single contract entry point) ---


Connector = Annotated[
    Union[
        Annotated[ApiConnector, UnionTag("api")],
        Annotated[DatabaseConnector, UnionTag("database")],
        Annotated[NosqlConnector, UnionTag("nosql")],
        Annotated[DocumentConnector, UnionTag("document")],
        Annotated[FileConnector, UnionTag("file")],
        Annotated[S3Connector, UnionTag("s3")],
        Annotated[StdoutConnector, UnionTag("stdout")],
    ],
    Discriminator("kind"),
]


_CONNECTOR_ADAPTER: TypeAdapter[ConnectorBase] = TypeAdapter(Connector)


def parse_connector(data: dict[str, Any]) -> ConnectorBase:
    """Parse a connector dict into the correct subclass via the discriminated union.

    Args:
        data: Deserialized connector record.

    Returns:
        ApiConnector, DatabaseConnector, NosqlConnector, DocumentConnector,
        FileConnector, S3Connector, or StdoutConnector based on the `kind`
        discriminator.

    Raises:
        pydantic.ValidationError: If the data fails validation, including a
            missing or unknown `kind`.
    """
    return _CONNECTOR_ADAPTER.validate_python(data)
