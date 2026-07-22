#!/usr/bin/env python3
"""Render and publish versioned JSON Schema documents for Analitiq contracts.

Source of truth: Pydantic models in `analitiq.contracts.*`. The version is NEVER picked
by hand. `write` classifies the
structural diff against the committed `latest.json` and advances the version
itself (`--bump` raises it, upward only); CI's `bump-check` re-derives the same
floor and rejects any committed bump below it.

Output trees:
    Rendered into the committed tree, published to schemas.<domain> by the
    infra repo's Terraform (the bucket and CDN are not defined here):
        schemas/<resource>/{X.Y.Z}.json   (immutable per version)
        schemas/<resource>/latest.json     (mutable; mirrors current X.Y.Z)
        schemas/<resource>/index.json       (manifest: latest + versions)

Resources are declared in the `RESOURCES` registry below. Adding a schema is one
entry there + a `paths:` filter line in the CI workflow.

Subcommands:
    write       Auto-compute the next version (classify → advance) and write
                {version}.json + latest.json + index.json for one resource.
    check       Render every registered resource and exit 1 if any checked-in
                {version}.json/latest.json differs from rendered (CI gate).
    classify    Print detected severity vs. checked-in/base latest.json.
    bump-check  Exit 1 if the committed version bump (base→head) is below the
                detected floor or is a rollback (CI gate; replaces labels).
    list        Print registered resource names (one per line) — used by CI.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import inspect
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
# The authored public contract models, published as `analitiq-contract-models`.
# Public schemas render from these and ONLY these.
#
# In the infra repo this renderer also reached into the private `alq.models.*`
# layer to emit ~40 internal-audience schemas. That half stayed behind: this
# copy renders the 13 public resources, and `Resource.__post_init__` asserts a
# registered model tree never leaves `analitiq.contracts`.
CONTRACTS_SRC = REPO_ROOT / "packages" / "contract-models" / "src"
sys.path.insert(0, str(CONTRACTS_SRC))

SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
# `DOMAIN` selects the host stamped into every `$id`. Set it BEFORE the contract
# imports below: `analitiq.contracts.shared.common` reads `os.environ["DOMAIN"]`
# at module load and raises KeyError without it.
#
# A non-default DOMAIN renders different `$id`s than the committed tree, so
# `check` would report every resource stale without saying why. Refuse instead.
_DEFAULT_DOMAIN = "analitiq.ai"
os.environ.setdefault("DOMAIN", _DEFAULT_DOMAIN)
if os.environ["DOMAIN"] != _DEFAULT_DOMAIN:
    raise SystemExit(
        f"DOMAIN={os.environ['DOMAIN']!r} is set in the environment, but the "
        f"committed schemas are rendered for {_DEFAULT_DOMAIN}. Unset it (or run "
        "in a clean shell) — otherwise every resource reports stale.")

from pydantic import BaseModel, TypeAdapter  # noqa: E402
from analitiq.contracts.shared.common import SCHEMA_BASE_URL  # noqa: E402

#: The `$id` host, owned by the contract package.
CANONICAL_BASE = SCHEMA_BASE_URL

from analitiq.contracts.connection import ConnectionInput  # noqa: E402
from analitiq.contracts.credentials_file import CredentialsFile  # noqa: E402
from analitiq.contracts.connector import Connector  # noqa: E402
from analitiq.contracts.endpoints import (  # noqa: E402
    ARROW_TYPE_PATTERN,
    ApiEndpointDoc,
    DatabaseEndpointDoc,
)
from analitiq.contracts.type_map import TypeMapReadDoc, TypeMapWriteDoc  # noqa: E402
from analitiq.contracts.pipelines.config import PipelineInput  # noqa: E402
from analitiq.contracts.pipelines.data_sync import (  # noqa: E402
    PipelineRunAcceptedResponse,
    PipelineRunRequest,
    PipelineRunStatusResponse,
    PipelineTerminateResponse,
)
from analitiq.contracts.stream import StreamInput  # noqa: E402
SCHEMAS_ROOT = REPO_ROOT / "schemas"

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
VERSIONED_FILENAME_RE = re.compile(r"^(\d+\.\d+\.\d+)\.json$")

# Keys whose changes are documentation-only and warrant a PATCH bump.
DOC_KEYS = {"description", "title", "examples", "$comment"}
# Keys stamped by this script that must be ignored when comparing schemas.
STAMP_KEYS = {"$id", "version"}

# JSON Schema 2020-12 keywords that, *when newly introduced* on a node, tighten
# validation. Adding any of these to a property/object that previously didn't
# have them rejects payloads that previously validated, so the change is MAJOR.
# (Mutating an *existing* such keyword's value is already caught by the scalar
# fall-through in `_is_additive`.)
_TIGHTENING_NEW_KEYWORDS = frozenset({
    "dependentRequired", "dependencies",
    "minProperties", "maxProperties",
    "minItems", "maxItems", "minContains", "uniqueItems",
    "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "pattern",
    "propertyNames",
    "not", "if", "then", "else",
    "unevaluatedProperties", "unevaluatedItems",
})


# ---------------------------------------------------------------------------
# Resource registry
# ---------------------------------------------------------------------------


# The published public contract package. Everything a rendered schema is built
# from must live here — enforced by `Resource.__post_init__`.
_PUBLIC_MODEL_PKG = "analitiq.contracts"
# `_model_tree` also surfaces pydantic's own synthetic classes — a
# `TypeAdapter(RootModel[...])` root reports `pydantic.root_model`. Those are
# machinery, not contract models, and carry no vocabulary of ours.
_MODEL_PKG_ALLOWLIST = (_PUBLIC_MODEL_PKG, "pydantic")


def _module_allowed(module: str) -> bool:
    """Exact match or a dotted child — never a bare prefix.

    `startswith(("analitiq.contracts", "pydantic"))` would also admit
    `analitiq.contracts_internal` and `pydantic_extra_types.*`, neither of which
    this project owns.
    """
    return any(module == pkg or module.startswith(pkg + ".")
               for pkg in _MODEL_PKG_ALLOWLIST)


def _model_tree(root: Any) -> set[type[BaseModel]]:
    """Every Pydantic model reachable from a schema's root type.

    Walks fields AND unwraps typing generics (`list[X]`, `X | None`,
    `Annotated[X, ...]`, `RootModel[X]`), because a root is often not itself a
    model — `TypeAdapter(list[ProductPriceItem])` would otherwise report an
    empty tree and silently derive as public.
    """
    seen: set[type[BaseModel]] = set()

    def walk(node: Any) -> None:
        if inspect.isclass(node) and issubclass(node, BaseModel):
            if node in seen:
                return
            seen.add(node)
            for field in node.model_fields.values():
                walk(field.annotation)
            # Computed fields live outside `model_fields` but DO render under
            # mode="serialization" — which is exactly the surface the Data Sync
            # leak came through. Walking only `model_fields` would let a
            # computed_field returning a private model reach `$defs` unseen.
            for computed in node.model_computed_fields.values():
                walk(computed.return_type)
            return
        for arg in typing.get_args(node):
            walk(arg)

    walk(root)
    return seen


@dataclass(frozen=True)
class Resource:
    """A single schema published from a Pydantic root model.

    Attributes:
        name: URL slug under `/schemas/` (also the folder name on disk).
        title: Title stamped into the schema document.
        description: One-paragraph blurb stamped into the schema document.
        adapter: TypeAdapter wrapping the root model (or discriminated union).
            This also DECIDES the audience — see `private` below. There is no
            `visibility` field: the model's home is the fact, and stating it
            twice is how the two drift.
        mode: Pydantic JSON-Schema generation mode. Authored *input* contracts
            use "validation" (the default). *Output* wire contracts (e.g. push
            messages) use "serialization" so computed fields and the serialized
            shape are reflected in the published schema.
        post_process: Optional in-place mutator applied to the rendered body
            before stamping. Use this for surgery JSON-Schema generators don't
            do natively (e.g. forcing a discriminator field into `required[]`).
        source_paths: Repository paths whose changes should trigger re-render
            checks for this resource — surfaced via the `list --paths` command
            so CI workflow filters stay in sync with the registry.
    """

    name: str
    title: str
    description: str
    adapter: TypeAdapter
    mode: Literal["validation", "serialization"] = "validation"
    post_process: Callable[[dict[str, Any]], None] | None = None
    source_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Every registered model must live in `analitiq.contracts`.

        The infra renderer DERIVED audience with the inverse of this check
        (`private` = does any reachable model come from `alq.`), because it
        served both trees. This copy renders only the public tree, so the same
        fact becomes an invariant: a resource whose model tree leaves the public
        contract package is a bug, not a different output path.

        Kept as a structural check rather than a declared flag for the reason
        the original gave — a declared field is a second statement of a fact the
        code already makes, and two statements drift. It drifted exactly this
        way before: the public Data Sync schemas were built from the frontend's
        payload classes and published "AWS Batch job ID" to the CDN. The
        declaration said public; the models said otherwise; nothing reconciled
        them.

        So making a schema public is not a keyword: it is moving its models into
        the public package, which is a reviewable, structural act.
        """
        tree = _model_tree(self.adapter._type)
        if not tree:
            # An empty tree is indistinguishable from a clean one, so the check
            # below would pass without inspecting anything. `_model_tree`'s own
            # docstring names this hazard for `list[X]` roots; make the whole
            # class of it loud instead of patching instances. No registered
            # resource legitimately renders from zero models.
            raise ValueError(
                f"resource {self.name!r} has an empty model tree, so the "
                f"audience check cannot run (root: {self.adapter._type!r}). "
                "A registered resource must render from at least one model."
            )
        leaked = sorted(
            f"{m.__module__}.{m.__qualname__}"
            for m in tree
            if not _module_allowed(m.__module__)
        )
        if leaked:
            raise ValueError(
                f"resource {self.name!r} reaches models outside "
                f"{_PUBLIC_MODEL_PKG!r}: {leaked}. Public schemas render only "
                "from the published contract package; move the model there or "
                "drop the resource."
            )

    def dir(self) -> Path:
        return SCHEMAS_ROOT / self.name

    def base_url(self) -> str:
        return f"{CANONICAL_BASE}/{self.name}"


def _enforce_discriminator_required(schema: dict[str, Any]) -> None:
    """Add the discriminator field to each subclass's `required[]`.

    Pydantic excludes fields with defaults from `required`, but for an external
    JSON Schema validator the discriminated union is meaningless if the
    discriminator isn't required on each branch — without it, a payload missing
    the discriminator can match multiple `oneOf` branches.

    Also applies the same rule to the schema root: when a concrete (non-union)
    model is the published root and its discriminator field has a default, that
    field would otherwise be omitted from the root `required[]`, letting
    consumers submit kind-less payloads against the kind-specific schema.
    """
    # Walk every `$def` entry (covers union members).
    for defn in schema.get("$defs", {}).values():
        if isinstance(defn, dict):
            _add_const_props_to_required(defn)
    # Apply at the root too (covers concrete-root schemas).
    _add_const_props_to_required(schema)


def _add_const_props_to_required(node: dict[str, Any]) -> None:
    props = node.get("properties", {}) or {}
    for field_name, field_schema in props.items():
        if isinstance(field_schema, dict) and "const" in field_schema:
            required = node.setdefault("required", [])
            if field_name not in required:
                required.append(field_name)
                required.sort()


# Conditional `model_validator` rules that don't surface to JSON Schema by
# default. We mirror them as `allOf`/`oneOf` constraints on the relevant `$def`
# so external (JSON-Schema-only) consumers reject the same payloads our Pydantic
# validators reject. Spec: §Post-Auth Outputs, §Resource Discovery,
# §Connection Inputs (secret iff storage='secrets').
_CONDITIONAL_RULES: dict[str, list[dict[str, Any]]] = {
    "PostAuthOutput": [
        {
            "oneOf": [
                {
                    "properties": {
                        "mode": {"const": "user_selection"},
                        "storage": {"const": "connection.selections"},
                    },
                    "required": ["options_request"],
                    "not": {"required": ["discovery_request"]},
                },
                {
                    "properties": {
                        "mode": {"const": "auto_discovery"},
                        "storage": {"enum": ["connection.discovered", "secrets"]},
                    },
                    "required": ["discovery_request"],
                    "not": {
                        "anyOf": [
                            {"required": ["options_request"]},
                            {"required": ["options_path"]},
                            {"required": ["label_path"]},
                        ]
                    },
                },
            ]
        }
    ],
    "ResourceDiscoveryImplementation": [
        {
            "oneOf": [
                {
                    "properties": {"type": {"const": "connector_plugin"}},
                    "required": ["entrypoint"],
                },
                {
                    "properties": {"type": {"const": "builtin"}},
                    "not": {"required": ["entrypoint"]},
                },
            ]
        }
    ],
    "ConnectionContractInput": [
        {
            "oneOf": [
                {
                    "properties": {
                        "storage": {"const": "secrets"},
                        "secret": {"const": True},
                    },
                    "required": ["secret"],
                },
                {
                    "properties": {"storage": {"const": "connection.parameters"}},
                    "anyOf": [
                        {"not": {"required": ["secret"]}},
                        {"properties": {"secret": {"const": False}}},
                    ],
                },
            ]
        },
        # When `enum` is present it must be a non-empty list (spec: §Connection
        # Inputs — `enum` is the authoritative allowed-value list).
        {
            "if": {"required": ["enum"]},
            "then": {"properties": {"enum": {"minItems": 1}}},
        },
    ],
}


def _encode_conditional_rules(schema: dict[str, Any]) -> None:
    """Mirror `model_validator` constraints as JSON-Schema-level `allOf`/`oneOf`.

    Hard-fails when a registered `$def` is missing from the rendered schema —
    a silent skip would let a model-class rename quietly drop the conditional
    constraint from the published artifact, weakening the contract for
    external validators with no CI signal.
    """
    defs = schema.get("$defs", {})
    for def_name, constraints in _CONDITIONAL_RULES.items():
        defn = defs.get(def_name)
        if not isinstance(defn, dict):
            raise RuntimeError(
                f"_encode_conditional_rules: $def {def_name!r} is missing from the "
                "rendered schema. The Pydantic class was renamed/removed but "
                "_CONDITIONAL_RULES was not updated; external consumers would "
                "silently lose this conditional rule."
            )
        all_of = defn.setdefault("allOf", [])
        for c in constraints:
            if c not in all_of:
                all_of.append(c)


# The AUTHORED public contract models — their own source package, published to
# PyPI as `analitiq-contract-models`. That tree IS the published package.
_CONTRACTS_PREFIX = "packages/contract-models/src/analitiq/contracts"

def _connector_post_process(schema: dict[str, Any]) -> None:
    _enforce_discriminator_required(schema)
    _encode_conditional_rules(schema)
    _annotate_transport_inheritance(schema)


# Enforce canonical arrow_type inside API response.schema / input.schema.
# Pydantic models these as opaque `dict[str, Any]`, so the rendered schema treats
# them as `additionalProperties: true` blobs with no rules on `arrow_type`. Inject
# a recursive `$def` so external validators reject bare parameterized forms
# (`Timestamp`, `Decimal128`, …) at author time, matching the runtime walker on
# ResponseExtraction / WriteInput.
_JSON_SCHEMA_PROPERTY_NODE_DEF: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "description": (
        "JSON Schema Draft 2020-12 node carrying the Analitiq `native_type` / "
        "`arrow_type` annotations on typed field schemas. Recursive: every "
        "JSON Schema keyword whose value is itself a schema (or a map/list of "
        "schemas) is constrained back to this node. Specifically: "
        "`properties`, `patternProperties`, `$defs`, `definitions`, "
        "`dependentSchemas` (maps); `prefixItems`, `allOf`, `anyOf`, `oneOf` "
        "(lists); `items`, `contains`, `additionalProperties`, "
        "`propertyNames`, `unevaluatedItems`, `unevaluatedProperties`, `not`, "
        "`if`, `then`, `else` (single). Issue #424 — canonical `arrow_type` "
        "must carry parameters when the type requires them; `native_type` and "
        "`arrow_type` are paired."
    ),
    "properties": {
        "arrow_type": {
            "type": "string",
            "pattern": ARROW_TYPE_PATTERN,
        },
        "native_type": {"type": "string"},
        "properties": {
            "type": "object",
            "additionalProperties": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        },
        "patternProperties": {
            "type": "object",
            "additionalProperties": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        },
        "$defs": {
            "type": "object",
            "additionalProperties": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        },
        "definitions": {
            "type": "object",
            "additionalProperties": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        },
        "dependentSchemas": {
            "type": "object",
            "additionalProperties": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        },
        "items": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        "contains": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        "propertyNames": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        "unevaluatedItems": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        "unevaluatedProperties": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        "not": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        "if": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        "then": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        "else": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        "additionalProperties": {
            "anyOf": [
                {"type": "boolean"},
                {"$ref": "#/$defs/JsonSchemaPropertyNode"},
            ]
        },
        "prefixItems": {
            "type": "array",
            "items": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        },
        "allOf": {
            "type": "array",
            "items": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        },
        "anyOf": {
            "type": "array",
            "items": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        },
        "oneOf": {
            "type": "array",
            "items": {"$ref": "#/$defs/JsonSchemaPropertyNode"},
        },
    },
    "dependentRequired": {
        "arrow_type": ["native_type"],
        "native_type": ["arrow_type"],
    },
}


_API_ENDPOINT_SCHEMA_HOLDER_DEFS: tuple[str, ...] = ("ResponseExtraction", "WriteInput")


def _api_endpoint_post_process(schema: dict[str, Any]) -> None:
    """Constrain `response.schema` / `input.schema` to carry canonical arrow_type.

    Pydantic emits both fields as opaque object schemas. We swap them for a
    `$ref` to a recursive `JsonSchemaPropertyNode` `$def` that walks the embedded
    JSON Schema and rejects bare-parameterized arrow_type values and unpaired
    native_type/arrow_type leaves — mirroring the Pydantic-side runtime walker
    in `_validate_arrow_type_in_json_schema`.
    """
    _enforce_discriminator_required(schema)

    defs = schema.setdefault("$defs", {})
    defs["JsonSchemaPropertyNode"] = _JSON_SCHEMA_PROPERTY_NODE_DEF

    for def_name in _API_ENDPOINT_SCHEMA_HOLDER_DEFS:
        holder = defs.get(def_name)
        if not isinstance(holder, dict):
            raise RuntimeError(
                f"_api_endpoint_post_process: expected $def {def_name!r} in the "
                "rendered api-endpoint schema. The Pydantic model was renamed "
                "or removed; the JsonSchemaPropertyNode constraint cannot be "
                "wired up. Update _API_ENDPOINT_SCHEMA_HOLDER_DEFS."
            )
        props = holder.get("properties")
        if not isinstance(props, dict) or "schema" not in props:
            raise RuntimeError(
                f"_api_endpoint_post_process: $def {def_name!r} lost its "
                "`schema` property; the api-endpoint JSON-Schema-body shape "
                "changed and the canonical arrow_type constraint cannot be "
                "attached."
            )
        # Preserve the title/description rendered from the Pydantic Field so the
        # public schema documents the slot; replace the body with a $ref to the
        # recursive node so canonical arrow_type validation kicks in.
        original = props["schema"]
        replacement: dict[str, Any] = {
            "$ref": "#/$defs/JsonSchemaPropertyNode",
        }
        for carry_key in ("title", "description"):
            if carry_key in original:
                replacement[carry_key] = original[carry_key]
        props["schema"] = replacement

    _encode_write_mode_conflict_keys_rule(defs)


def _encode_write_mode_conflict_keys_rule(defs: dict[str, Any]) -> None:
    """Publish the per-mode `conflict_keys` rule in the JSON Schema.

    The Pydantic `Operations._conflict_keys_by_mode` validator requires
    `conflict_keys` on the `upsert` write mode and forbids it on every other
    mode, but model validators are not emitted into the rendered schema — so a
    connector author validating an authored document against the published
    `api-endpoint` contract would not see the rule. Encode it structurally:
    `operations.write` renders as `anyOf[{object}, {null}]` with the WriteOperation
    `$ref` under `additionalProperties`; pin per-mode `properties` that keep the
    `$ref` (via `allOf`) and add the `required`/`not-required` constraint.
    """
    operations = defs.get("Operations")
    if not isinstance(operations, dict):
        raise RuntimeError(
            "_encode_write_mode_conflict_keys_rule: expected $def Operations; "
            "the model was renamed/reshaped."
        )
    write_prop = operations.get("properties", {}).get("write")
    branches = write_prop.get("anyOf") if isinstance(write_prop, dict) else None
    obj_branch = next(
        (b for b in branches if isinstance(b, dict) and b.get("type") == "object"),
        None,
    ) if isinstance(branches, list) else None
    if obj_branch is None:
        raise RuntimeError(
            "_encode_write_mode_conflict_keys_rule: operations.write object branch "
            "not found (expected anyOf[{type:object}, {type:null}]); the model "
            "reshaped — update this post-processor."
        )
    write_op_ref = {"$ref": "#/$defs/WriteOperation"}
    # The model rule (`Operations._conflict_keys_by_mode`) is a falsy check:
    # `upsert` requires a truthy `conflict_keys`, every other mode forbids a
    # truthy one (a `null`/absent value is fine on a non-upsert mode). Mirror
    # that exactly — `required`/`not-required` alone would diverge on the
    # field's nullable `default: null`:
    #   - upsert: pin the value to a non-empty array (so `null` is rejected, not
    #     just satisfied by key presence).
    #   - insert: forbid only a *non-null* value (so `null`/absent pass, matching
    #     the model, while a real key is rejected).
    # The insert pin to `type: null` is satisfiable only because the base
    # `WriteOperation.conflict_keys` $ref renders as `anyOf[array, null]`; if that
    # field is ever narrowed to array-only, this branch becomes unsatisfiable.
    obj_branch["properties"] = {
        "insert": {"allOf": [write_op_ref, {"properties": {"conflict_keys": {"type": "null"}}}]},
        "upsert": {"allOf": [write_op_ref, {
            "required": ["conflict_keys"],
            "properties": {"conflict_keys": {"type": "array", "minItems": 1}},
        }]},
    }


_CONNECTOR_DOCUMENT_DEF_NAMES: frozenset[str] = frozenset({
    "ApiConnector",
    "DatabaseConnector",
    "NosqlConnector",
    "DocumentConnector",
    "FileConnector",
    "S3Connector",
    "StdoutConnector",
})


def _annotate_transport_inheritance(schema: dict[str, Any]) -> None:
    """Document the runtime-only `transport_type` inheritance from defaults.

    The Pydantic models fill `transport_type` on `transports.<name>` entries
    from `transport_defaults.transport_type` (see `ConnectorBase._inherit_transport_type`).
    The discriminated-union JSON Schema cannot express that contextual
    defaulting, so external validators reject entries that omit
    `transport_type`. Authors targeting this published schema must declare
    `transport_type` per entry.

    Scoped to the known connector-document `$defs` so the note never lands on
    an unrelated property that happens to be named `transports`. Hard-fails
    when *any* registered `$def` is missing (rename guard) or when *none* of
    them carry a `transports` property (model-shape guard). Tracking matches
    instead of stamps means the function is idempotent: a second call on an
    already-annotated schema is a no-op rather than a misleading raise.
    """
    note = (
        "Each transport entry must declare its own `transport_type`. The "
        "Pydantic runtime accepts entries that omit it and inherits the value "
        "from `transport_defaults.transport_type`, but this JSON Schema does "
        "not — declare `transport_type` per entry for portable validation."
    )
    defs = schema.get("$defs", {})
    missing = sorted(_CONNECTOR_DOCUMENT_DEF_NAMES - set(defs))
    if missing:
        raise RuntimeError(
            "_annotate_transport_inheritance: registered connector-document "
            f"$defs {missing!r} were not found in the rendered schema. The "
            "model classes were renamed/removed but _CONNECTOR_DOCUMENT_DEF_NAMES "
            "was not updated; the runtime-only transport_type inheritance "
            "note would be silently dropped from the published schema."
        )

    non_dict = sorted(d for d in _CONNECTOR_DOCUMENT_DEF_NAMES if not isinstance(defs[d], dict))
    if non_dict:
        raise RuntimeError(
            f"_annotate_transport_inheritance: registered $defs {non_dict!r} "
            "are not dict schemas (likely a `$ref`-only entry or a list). "
            "The annotation cannot be attached; investigate the renderer "
            "rather than relying on a fallthrough."
        )

    matched: list[str] = []
    missing_props: list[str] = []
    has_props_no_transports: list[str] = []
    for def_name in _CONNECTOR_DOCUMENT_DEF_NAMES:
        defn = defs[def_name]
        if "properties" not in defn:
            missing_props.append(def_name)
            continue
        props = defn["properties"]
        if not isinstance(props, dict):
            raise RuntimeError(
                f"_annotate_transport_inheritance: $def {def_name!r} has a "
                f"non-dict `properties` field (got {type(props).__name__}). "
                "The renderer produced an unexpected shape; investigate "
                "rather than relying on a fallthrough."
            )
        transports = props.get("transports")
        if transports is None:
            has_props_no_transports.append(def_name)
            continue
        if not isinstance(transports, dict):
            raise RuntimeError(
                f"_annotate_transport_inheritance: $def {def_name!r} has a "
                f"non-dict `transports` property (got {type(transports).__name__}). "
                "The annotation cannot attach to a `$ref`-only or scalar "
                "schema entry; investigate rather than relying on a fallthrough."
            )
        matched.append(def_name)
        # Always set the canonical note. `$comment` is owned by this
        # function on the transports property; not skipping when a
        # `$comment` is already present means a pre-seeded value (from
        # a stale rendered file or an external tool) gets normalized
        # to the current canonical text instead of silently passing.
        transports["$comment"] = note
    if not matched:
        raise RuntimeError(
            "_annotate_transport_inheritance: none of the registered "
            f"connector-document $defs ({sorted(_CONNECTOR_DOCUMENT_DEF_NAMES)!r}) "
            "expose a `transports` property (defs without `properties`: "
            f"{sorted(missing_props)!r}; defs with `properties` but no "
            f"`transports` key: {sorted(has_props_no_transports)!r}). The "
            "connector model shape changed in a way that drops the transports "
            "map; the runtime-only transport_type inheritance note has "
            "nothing to attach to."
        )


def _collapse_nullable_anyof(node: Any) -> None:
    """Collapse `anyOf: [X, {"type": "null"}]` to X and drop `default: null`.

    For wire contracts serialized with `exclude_none=True`: an absent field is
    *omitted*, never null, so the published schema must not advertise null —
    Pydantic's `field: T | None = None` idiom renders nullable, but null never
    reaches the wire. Recurses through the whole document.
    """
    if isinstance(node, dict):
        any_of = node.get("anyOf")
        if isinstance(any_of, list) and {"type": "null"} in any_of:
            remaining = [b for b in any_of if b != {"type": "null"}]
            if len(remaining) == 1 and isinstance(remaining[0], dict):
                node.pop("anyOf")
                # Merge the surviving branch; node-level siblings (title,
                # description) win over branch keys.
                for key, value in remaining[0].items():
                    node.setdefault(key, value)
            else:
                node["anyOf"] = remaining
        if "default" in node and node["default"] is None:
            del node["default"]
        for value in node.values():
            _collapse_nullable_anyof(value)
    elif isinstance(node, list):
        for item in node:
            _collapse_nullable_anyof(item)


def _normalize_database_object_namespaces(schema: dict[str, Any]) -> None:
    """Mirror `DatabaseObject._reject_explicit_null_namespaces` into the schema.

    `catalog` and `schema` are modeled `str | None`, so Pydantic renders a
    nullable `anyOf`, but the runtime validator REJECTS an explicit null for them
    (they are omit-when-absent, never null). Drop the null branch (and any
    `default: null`) so a client validating against the published schema cannot
    pass `{schema: null}` only to be 400'd by `validate_stream_input`. `name` is
    required, and `object_type` is legitimately nullable (the validator does not
    reject its null), so both are left untouched.

    Runs for every resource (self-guarding: a no-op when the schema does not
    embed `DatabaseObject`), and is idempotent — schemas whose own
    post-processor already collapsed nullables (e.g. the read contracts) simply
    have nothing left to drop. Keeps the published `DatabaseObject` perfectly
    aligned with the model wherever it appears.
    """
    defn = schema.get("$defs", {}).get("DatabaseObject")
    if not isinstance(defn, dict):
        return
    props = defn.get("properties")
    if not isinstance(props, dict):
        return
    for name in ("catalog", "schema"):
        prop = props.get(name)
        if not isinstance(prop, dict):
            continue
        any_of = prop.get("anyOf")
        if not isinstance(any_of, list) or {"type": "null"} not in any_of:
            continue  # already non-null (collapsed) — nothing to drop
        non_null = [b for b in any_of if b != {"type": "null"}]
        if len(non_null) == 1 and isinstance(non_null[0], dict):
            merged = dict(non_null[0])
            for carry in ("title", "description"):
                if carry in prop:
                    merged.setdefault(carry, prop[carry])
            props[name] = merged  # replaces the prop → drops anyOf + default:null
        else:
            prop["anyOf"] = non_null
            prop.pop("default", None)






















def _data_sync_response_post_process(schema: dict[str, Any]) -> None:
    # `success: Literal[True]` carries a `const` — force it into `required[]`
    # so external validators reject a body that omits it. The envelope dumps
    # with `exclude_none=True` (absent `message`/`data` omitted, never null),
    # so the nullable anyOf branches collapse to their non-null shape.
    _enforce_discriminator_required(schema)
    _collapse_nullable_anyof(schema)


def _data_sync_terminate_post_process(schema: dict[str, Any]) -> None:
    # The shared response post-process, plus the documented terminate invariant
    # the type system can't express: an idempotent no-op omits `data` and is
    # "distinguished by `message`", so when `data` is absent `message` MUST be
    # present. pipeline-invoker always sends a message, so this mirrors the
    # runtime; encoding it as a JSON-Schema `if/then` (the same model_validator
    # -> schema mirroring as `_CONDITIONAL_RULES`) lets external validators
    # enforce the promise the contract documents.
    _data_sync_response_post_process(schema)
    schema["if"] = {"not": {"required": ["data"]}}
    schema["then"] = {"required": ["message"]}






RESOURCES: tuple[Resource, ...] = (
    Resource(
        name="connector",
        title="Analitiq Connector",
        description=(
            "Public JSON Schema contract for Analitiq connector documents — "
            "the authored shape used in source control, PR review, and "
            "author-time tooling. `connector_id` is the authored canonical "
            "identifier (slug pattern `^[a-z0-9][a-z0-9_-]*$`). Only "
            "`created_at` and `updated_at` are server-managed and absent in "
            "the authored shape. "
            "Source of truth: analitiq.contracts.connector.Connector (Pydantic)."
        ),
        adapter=TypeAdapter(Connector),
        post_process=_connector_post_process,
        source_paths=(f"{_CONTRACTS_PREFIX}/connector.py",),
    ),
    Resource(
        name="connection",
        title="Analitiq Connection",
        description=(
            "Public JSON Schema contract for Analitiq connection documents — "
            "the authored shape used in source control, PR review, and "
            "author-time tooling. Server-managed fields (connection_id, "
            "version, org_id, connector_id, connector_version, auth_state, "
            "created_at, updated_at) are forbidden in the authored shape and "
            "assigned by the connection service on ingest. The persisted-"
            "record shape is internal and not published. "
            "Source of truth: analitiq.contracts.connection.ConnectionInput (Pydantic)."
        ),
        adapter=TypeAdapter(ConnectionInput),
        post_process=_enforce_discriminator_required,
        source_paths=(f"{_CONTRACTS_PREFIX}/connection.py",),
    ),
    Resource(
        name="credentials",
        title="Analitiq Connection Credentials",
        description=(
            "Public JSON Schema contract for a connection's local secrets file "
            "(`credentials.json`) — a flat map of connection-input (or post-auth "
            "output) name to that input's secret value (any JSON type; the engine "
            "string-coerces on read, so prefer strings). A connection document's "
            "`secret_refs.<name>` entry, via the `sidecar:<name>` scheme, resolves "
            "to the value stored here under the same `<name>`. Kept out of source "
            "control; the "
            "shape is published so a plugin author can validate a credentials "
            "template against it. "
            "Source of truth: analitiq.contracts.credentials_file.CredentialsFile (Pydantic)."
        ),
        adapter=TypeAdapter(CredentialsFile),
        source_paths=(f"{_CONTRACTS_PREFIX}/credentials_file.py",),
    ),
    Resource(
        name="api-endpoint",
        title="Analitiq API Endpoint",
        description=(
            "Public JSON Schema contract for API endpoint documents (owned by "
            "connectors with `kind: 'api'`) — authored shape only. "
            "Endpoint documents have no top-level `kind` field; the owning "
            "connector's `kind` selects this schema. Reserved server-managed "
            "fields (endpoint_id, connector_id, connector_version, "
            "connection_id, schema_hash) are forbidden in the authored shape. "
            "The persisted catalog-row shape is internal and not published. "
            "Source of truth: analitiq.contracts.endpoints.ApiEndpointDoc (Pydantic)."
        ),
        adapter=TypeAdapter(ApiEndpointDoc),
        post_process=_api_endpoint_post_process,
        source_paths=(f"{_CONTRACTS_PREFIX}/endpoints.py",),
    ),
    Resource(
        name="database-endpoint",
        title="Analitiq Database Endpoint",
        description=(
            "Public JSON Schema contract for database endpoint documents "
            "(owned by connectors with `kind` in {'database', 'nosql', "
            "'document'}) — authored shape only. Endpoint documents have no "
            "top-level `kind` field; the owning connector's `kind` selects "
            "this schema. Reserved server-managed fields are forbidden in the "
            "authored shape. "
            "Source of truth: analitiq.contracts.endpoints.DatabaseEndpointDoc (Pydantic)."
        ),
        adapter=TypeAdapter(DatabaseEndpointDoc),
        post_process=_enforce_discriminator_required,
        source_paths=(f"{_CONTRACTS_PREFIX}/endpoints.py",),
    ),
    Resource(
        name="type-map-read",
        title="Analitiq Type Map (read)",
        description=(
            "Public JSON Schema contract for a connector's `type-map-read.json` "
            "— the read direction (native → canonical) of its type-map pair, a "
            "top-level array of `{match, native, canonical}` rules, order "
            "significant (first match wins). The full per-rule contract (ECMA-262 "
            "regex, `${name}` capture correspondence, canonical vocabulary, "
            "schemaless-container handling) lives in the model and is enforced by "
            "the connector validator; this published schema is the structural "
            "projection. Source of truth: analitiq.contracts.type_map.TypeMapReadDoc (Pydantic)."
        ),
        adapter=TypeAdapter(TypeMapReadDoc),
        source_paths=(
            f"{_CONTRACTS_PREFIX}/type_map.py",
        ),
    ),
    Resource(
        name="type-map-write",
        title="Analitiq Type Map (write)",
        description=(
            "Public JSON Schema contract for a database connector's "
            "`type-map-write.json` — the write direction (canonical → native "
            "DDL) of its type-map pair, a top-level array of `{match, native, "
            "canonical}` rules where `canonical` matches and `native` renders. "
            "The full per-rule contract lives in the model and is enforced by the "
            "connector validator; this published schema is the structural "
            "projection. Source of truth: analitiq.contracts.type_map.TypeMapWriteDoc (Pydantic)."
        ),
        adapter=TypeAdapter(TypeMapWriteDoc),
        source_paths=(
            f"{_CONTRACTS_PREFIX}/type_map.py",
        ),
    ),
    Resource(
        name="pipeline",
        title="Analitiq Pipeline",
        description=(
            "Public JSON Schema contract for Analitiq pipeline documents — "
            "the authored shape used in source control, PR review, and "
            "author-time tooling. Server-managed fields (pipeline_id, "
            "version, org_id, created_at, updated_at) are forbidden in the "
            "authored shape and assigned by the pipeline service on ingest. "
            "The persisted-record shape is internal and not published. "
            "Source of truth: analitiq.contracts.pipelines.config.PipelineInput (Pydantic)."
        ),
        adapter=TypeAdapter(PipelineInput),
        source_paths=(f"{_CONTRACTS_PREFIX}/pipelines/config.py",),
    ),
    Resource(
        name="stream",
        title="Analitiq Stream",
        description=(
            "Public JSON Schema contract for Analitiq stream documents — "
            "the authored shape used in source control, PR review, and "
            "author-time tooling. Server-managed fields (stream_id, version, "
            "org_id, created_at, updated_at) are forbidden in the authored "
            "shape and assigned by the stream service on ingest. The "
            "persisted-record shape is internal and not published. "
            "Source of truth: analitiq.contracts.stream.StreamInput (Pydantic)."
        ),
        adapter=TypeAdapter(StreamInput),
        source_paths=(f"{_CONTRACTS_PREFIX}/stream.py",),
    ),
    # ---- Public Data Sync API (rest.<domain>/v1, API-key) ------------------
    # PUBLIC, customer-facing request/response contracts for the API-key Data
    # Sync API. Unlike the internal `pipeline-run-accepted` / `pipeline-run-action`
    # these model the FULL response body so a public consumer can validate
    # an entire HTTP payload directly.
    # terminate data shapes as the private contracts — one source of truth, no
    # drift.
    Resource(
        name="data-sync-run-request",
        title="Analitiq Data Sync API — Pipeline Run Request",
        description=(
            "PUBLIC JSON Schema contract for the request body of the Data Sync "
            "API POST /pipelines/{pipeline_id}/run (rest.<domain>/v1, API-key "
            "auth). The pipeline is taken from the path and the org from the "
            "API key, so the only client-supplied field is the optional "
            "`terminate_existing_sync` flag; an empty body is valid and unknown "
            "keys are rejected. Served at schemas.<domain> for external API-key "
            "consumers."
        ),
        adapter=TypeAdapter(PipelineRunRequest),
        source_paths=(f"{_CONTRACTS_PREFIX}/pipelines/data_sync.py",),
    ),
    Resource(
        name="data-sync-run-accepted",
        title="Analitiq Data Sync API — Pipeline Run Accepted",
        description=(
            "PUBLIC JSON Schema contract for the SUCCESS (202 Accepted) response "
            "body of the Data Sync API POST /pipelines/{pipeline_id}/run — the "
            "canonical `{success, message?, data}` envelope where `data` carries "
            "the run-accepted tracking identifiers (invocation_id, pipeline_id), "
            "always present on a successful accept. The run is dispatched "
            "asynchronously (the Batch job is submitted in the background), so "
            "there is no job_id yet; poll run history by invocation_id for the "
            "final outcome. Error responses use the canonical error envelope and "
            "are documented in the Data Sync OpenAPI rather than here. Absent "
            "optional fields are omitted from the wire, never null. Served at "
            "schemas.<domain> for external API-key consumers."
        ),
        adapter=TypeAdapter(PipelineRunAcceptedResponse),
        mode="serialization",
        post_process=_data_sync_response_post_process,
        source_paths=(
            f"{_CONTRACTS_PREFIX}/pipelines/data_sync.py",
        ),
    ),
    Resource(
        name="data-sync-terminate-response",
        title="Analitiq Data Sync API — Pipeline Terminate Response",
        description=(
            "PUBLIC JSON Schema contract for the SUCCESS response body of the "
            "Data Sync API POST /pipelines/{pipeline_id}/terminate — the "
            "canonical `{success, message?, data?}` envelope. Terminate is "
            "idempotent: `data` (resolved pipeline_id, plus job_id when a "
            "running job was stopped) is present when a pipeline was acted on "
            "and omitted on no-op outcomes, which are distinguished by "
            "`message`. Error responses use the canonical error envelope and "
            "are documented in the Data Sync OpenAPI rather than here. Absent "
            "optional fields are omitted from the wire, never null. Served at "
            "schemas.<domain> for external API-key consumers."
        ),
        adapter=TypeAdapter(PipelineTerminateResponse),
        mode="serialization",
        post_process=_data_sync_terminate_post_process,
        source_paths=(
            f"{_CONTRACTS_PREFIX}/pipelines/data_sync.py",
        ),
    ),
    Resource(
        name="data-sync-run-status",
        title="Analitiq Data Sync API — Pipeline Run Status",
        description=(
            "PUBLIC JSON Schema contract for the SUCCESS (200) response body of "
            "the Data Sync API GET /pipelines/{pipeline_id}/runs/{invocation_id} "
            "(rest.<domain>/v1, API-key auth) — the canonical "
            "`{success, message?, data}` envelope where `data` carries the run's "
            "public status: a coarse `status`, run timestamps, record counts, "
            "and — on failure — a customer-safe error category. A deliberately "
            "small, stable projection of the internal run-log that exposes no "
            "infrastructure detail (batch job ids, raw error strings, ...). A "
            "client that accepted a run (202) polls this by invocation_id for the "
            "outcome. Error responses use the canonical error envelope and are "
            "documented in the Data Sync OpenAPI rather than here. Absent optional "
            "fields are omitted from the wire, never null. Served at "
            "schemas.<domain> for external API-key consumers."
        ),
        adapter=TypeAdapter(PipelineRunStatusResponse),
        mode="serialization",
        post_process=_data_sync_response_post_process,
        source_paths=(f"{_CONTRACTS_PREFIX}/pipelines/data_sync.py",),
    ),
)

RESOURCES_BY_NAME: dict[str, Resource] = {r.name: r for r in RESOURCES}


def get_resource(name: str) -> Resource:
    try:
        return RESOURCES_BY_NAME[name]
    except KeyError:
        valid = ", ".join(r.name for r in RESOURCES)
        raise SystemExit(f"unknown resource {name!r}; valid: {valid}") from None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def parse_semver(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.match(version)
    if not match:
        raise ValueError(f"Invalid semver: {version!r} (expected MAJOR.MINOR.PATCH)")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


# Severity ordering shared by the structural classifier and the bump checker.
SEVERITY_RANK: dict[str, int] = {"none": 0, "patch": 1, "minor": 2, "major": 3}


def semver_delta(old: str, new: str) -> str:
    """Classify the version bump from `old` to `new`.

    Returns 'rollback' when new < old, else 'none'/'patch'/'minor'/'major'
    for the highest component that changed. Used to validate that an in-code
    `Resource.version` bump meets the structurally-detected severity floor.
    """
    o, n = parse_semver(old), parse_semver(new)
    if n < o:
        return "rollback"
    if n == o:
        return "none"
    if n[0] != o[0]:
        return "major"
    if n[1] != o[1]:
        return "minor"
    return "patch"


def bump_version(base: str, severity: str) -> str:
    """Advance `base` by `severity` ('none'/'patch'/'minor'/'major').

    'none' returns `base` unchanged. A higher severity zeroes the lower
    components per semver (a minor bump resets patch; a major bump resets
    minor and patch).
    """
    major, minor, patch = parse_semver(base)
    if severity == "none":
        return base
    if severity == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if severity == "minor":
        return f"{major}.{minor + 1}.0"
    if severity == "major":
        return f"{major + 1}.0.0"
    raise ValueError(f"unknown severity {severity!r}")


def render_schema(resource: Resource, version: str, *, identity: str | None = None) -> dict[str, Any]:
    """Render a resource's JSON Schema, stamped with $id/$schema/version.

    By default the `$id` points at the immutable pinned URL for `version`. Pass
    `identity` to override (used when writing `latest.json`, whose canonical URL
    differs from the pinned doc it currently mirrors).
    """
    body = resource.adapter.json_schema(mode=resource.mode, ref_template="#/$defs/{model}")
    if resource.post_process is not None:
        resource.post_process(body)
    # Universal, self-guarding alignment pass: keep the published `DatabaseObject`
    # in lockstep with the model wherever it is embedded (runs after the
    # resource's own post-processor; no-op when `DatabaseObject` is absent).
    _normalize_database_object_namespaces(body)
    stamped: dict[str, Any] = {
        "$schema": SCHEMA_DRAFT,
        "$id": identity or f"{resource.base_url()}/{version}.json",
        "version": version,
    }
    stamped.update(body)
    # Stamp human-friendly title/description AFTER the body merge so they win
    # over Pydantic-generated values (e.g. class-name-derived titles like
    # "ConnectionDocument" — the public schema should advertise the resource
    # name from the registry instead).
    stamped["title"] = resource.title
    stamped["description"] = resource.description
    return stamped


def render_pinned(resource: Resource, version: str) -> dict[str, Any]:
    """Render the immutable doc for `version` (canonical $id = pinned URL)."""
    return render_schema(resource, version)


def render_latest(resource: Resource, version: str) -> dict[str, Any]:
    """Render the mutable `latest.json` mirror for `version` (canonical $id = latest URL)."""
    return render_schema(
        resource, version, identity=f"{resource.base_url()}/latest.json"
    )


# ---------------------------------------------------------------------------
# Diff / classification
# ---------------------------------------------------------------------------


def _strip_doc_and_stamp(obj: Any) -> Any:
    """Strip documentation/identity keys for structural comparison."""
    drop = DOC_KEYS | STAMP_KEYS
    if isinstance(obj, dict):
        return {k: _strip_doc_and_stamp(v) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [_strip_doc_and_stamp(v) for v in obj]
    return obj


def _is_additive(old: Any, new: Any, path: tuple = ()) -> bool:
    """True when `new` only adds keys / list elements compared to `old`.

    Heuristic — returns False (≈ MAJOR) for the changes we explicitly know
    are tightening:

    - Removing a key from a dict node (e.g. dropping a property).
    - Mutating a scalar value (e.g. tightening minLength from 5 to 10).
    - Removing an element from a list other than `required` (e.g. dropping an
      `enum` value, narrowing a `type` union, removing a `oneOf` branch).
    - Adding to a JSON Schema `required` array.
    - Introducing any of `_TIGHTENING_NEW_KEYWORDS` (`pattern`,
      `minProperties`, `dependentRequired`, …) on a node where it didn't
      previously exist.
    - Introducing `additionalProperties: false` where it was previously
      absent or truthy.

    Returns True (≈ MINOR) for the changes we know are additive:

    - Adding a key to a dict node (new optional property, new $defs entry,
      new oneOf branch as a discrete dict key).
    - Removing entries from `required` (loosening).
    - Adding entries to multiset-style lists (extra `enum` values, extra
      union members) — note this is the permissive direction for *input*
      enums and may be wrong for *output* enums; developers must escalate
      via `--bump` when that distinction matters.

    Anything not matched above falls through to False, so the caller errs
    on the side of MAJOR.
    """
    if old == new:
        return True
    if isinstance(old, dict) and isinstance(new, dict):
        for k in set(new) - set(old):
            if k in _TIGHTENING_NEW_KEYWORDS:
                return False
        if "additionalProperties" in new and new["additionalProperties"] is False:
            if old.get("additionalProperties", True) is not False:
                return False
        for k, v in old.items():
            if k not in new:
                return False
            if not _is_additive(v, new[k], path + (k,)):
                return False
        return True
    if isinstance(old, list) and isinstance(new, list):
        if path and path[-1] == "required":
            return set(new).issubset(set(old))
        for item in old:
            if item not in new:
                return False
        return True
    return False


def classify(old: dict | None, new: dict) -> str:
    """Return 'none', 'patch', 'minor', or 'major' for severity vs. previous schema.

    Heuristic — errs on the side of MAJOR for ambiguous changes. Developers can
    always override upward via `--bump`; `bump-check` rejects
    under-bumps.

    A `None` or empty-dict `old` both mean "no usable prior schema" — the
    transition from nothing to a fully-defined contract is a brand-new
    contract, hence MAJOR. Without the empty-dict guard the additive
    heuristic would walk an empty `dict.items()` loop and return MINOR,
    silently under-classifying the publication.
    """
    if not old:
        return "major"
    if old == new:
        return "none"
    stripped_old = _strip_doc_and_stamp(old)
    stripped_new = _strip_doc_and_stamp(new)
    if stripped_old == stripped_new:
        return "patch"
    if _is_additive(stripped_old, stripped_new):
        return "minor"
    return "major"


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def list_published_versions(resource: Resource) -> list[str]:
    """Sorted list of X.Y.Z files published under schemas/<resource>/."""
    if not resource.dir().exists():
        return []
    found: list[str] = []
    for f in resource.dir().glob("*.json"):
        m = VERSIONED_FILENAME_RE.match(f.name)
        if m:
            found.append(m.group(1))
    return sorted(found, key=parse_semver)


def load_latest(resource: Resource) -> dict | None:
    path = resource.dir() / "latest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def build_index(resource: Resource) -> dict[str, Any]:
    """Build the resource-level manifest from on-disk versions."""
    versions = list_published_versions(resource)
    if not versions:
        return {resource.name: {"latest": None, "versions": []}}
    return {resource.name: {"latest": versions[-1], "versions": versions}}


def _load_pinned(resource: Resource, version: str) -> dict:
    return json.loads((resource.dir() / f"{version}.json").read_text())


def _load_previous_arg(previous: str | None, *, cmd: str) -> dict | None:
    """Load and validate a `--previous` base-branch latest.json path.

    Returns the parsed dict, or None when `--previous` was not supplied.
    A supplied-but-broken path (missing, empty, malformed, non-object) is a
    plumbing failure: a typo'd path or empty `git show` output must not
    silently masquerade as a brand-new resource. Exits 2 in that case.
    """
    if not previous:
        return None

    def _fail(msg: str) -> None:
        # Exit 2 (plumbing failure) — distinct from the 0/1 classification codes.
        print(f"{cmd}: {msg}", file=sys.stderr)
        raise SystemExit(2)

    prev_path = Path(previous)
    if not prev_path.exists():
        _fail(f"--previous={previous!r} does not exist.")
    if prev_path.stat().st_size == 0:
        _fail(f"--previous={previous!r} is empty.")
    try:
        parsed = json.loads(prev_path.read_text())
    except json.JSONDecodeError as exc:
        _fail(f"--previous={previous!r} is not valid JSON ({exc}).")
    if not isinstance(parsed, dict):
        _fail(
            f"--previous={previous!r} parsed to "
            f"{type(parsed).__name__}; expected a JSON object."
        )
    return parsed


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_write(args: argparse.Namespace) -> int:
    """Render at an auto-computed version: classify the diff, advance, write.

    The next version is a pure function of the committed `latest.json` version
    and the structural severity of the change — the developer never picks the
    number. `--bump <sev>` raises the bump above the detected floor (the only
    valid override is upward, e.g. an output-enum addition that is structurally
    additive but semantically breaking). A new resource publishes at 1.0.0.
    """
    resource = get_resource(args.resource)
    committed = load_latest(resource)
    base_version = (committed or {}).get("version") or "0.0.0"

    # Probe the new structural shape (rendered at the base version so $id/version
    # don't perturb the structural diff) and classify against the committed doc.
    probe = render_latest(resource, base_version)
    floor = classify(committed, probe)

    severity = floor
    if args.bump:
        if SEVERITY_RANK[args.bump] < SEVERITY_RANK[floor]:
            print(
                f"{resource.name}: --bump {args.bump!r} is below the detected floor "
                f"{floor!r}; overrides may only raise the bump.",
                file=sys.stderr,
            )
            return 2
        severity = args.bump

    version = bump_version(base_version, severity)
    if severity == "none":
        print(f"{resource.name}: no change vs. committed {base_version} — nothing to write.")
        return 0

    pinned = render_pinned(resource, version)
    latest = render_latest(resource, version)

    versioned_path = resource.dir() / f"{version}.json"
    if versioned_path.exists() and not args.force:
        existing = json.loads(versioned_path.read_text())
        if existing != pinned:
            print(
                f"refusing to overwrite immutable {versioned_path} (use --force to confirm)",
                file=sys.stderr,
            )
            return 2

    write_json(versioned_path, pinned)
    write_json(resource.dir() / "latest.json", latest)
    write_json(resource.dir() / "index.json", build_index(resource))
    print(
        f"wrote {resource.name}/{version}.json + latest.json + index.json "
        f"(bump {base_version} → {version}, '{severity}')"
    )
    return 0


def _check_resource(resource: Resource) -> tuple[bool, str]:
    """Return (ok, message). ok=False on drift / missing publication.

    The committed `latest.json` is authoritative for the current version: its
    `version` field must have a matching pinned `{version}.json`, must be the
    highest pinned version, and both files must equal the rendered output at
    that version. Changing a model without re-running `write` fails here,
    naming the fix.
    """
    write_hint = f"`scripts/render_schemas.py write --resource {resource.name}`"
    versions = list_published_versions(resource)
    if not versions:
        return (
            False,
            f"{resource.name}: no checked-in versions under "
            f"{resource.dir().relative_to(REPO_ROOT)}/; run {write_hint}",
        )

    committed_latest = load_latest(resource)
    if committed_latest is None:
        return (False, f"{resource.name}: latest.json is missing; run {write_hint}")

    version = committed_latest.get("version")
    if not version or version not in versions:
        return (
            False,
            f"{resource.name}: latest.json version {version!r} has no matching pinned "
            f"{version}.json; re-run {write_hint}",
        )
    if parse_semver(versions[-1]) > parse_semver(version):
        return (
            False,
            f"{resource.name}: a higher pinned version {versions[-1]}.json exists than "
            f"latest.json points to ({version}); re-run {write_hint}",
        )

    if _load_pinned(resource, version) != render_pinned(resource, version):
        return (
            False,
            f"{resource.name}: {version}.json is stale or hand-edited; re-run {write_hint}",
        )
    if committed_latest != render_latest(resource, version):
        return (
            False,
            f"{resource.name}: latest.json is stale or out of sync with "
            f"{version}.json; re-run {write_hint}",
        )

    # index.json is published to the CDN exactly like the other two, so it needs
    # the same gate. Without this, hand-editing it (or `write` changing the
    # manifest shape) drifts silently while `check` still reports OK.
    index_path = resource.dir() / "index.json"
    committed_index = json.loads(index_path.read_text()) if index_path.exists() else None
    if committed_index is None:
        return (False, f"{resource.name}: index.json is missing; run {write_hint}")
    if committed_index != build_index(resource):
        return (
            False,
            f"{resource.name}: index.json is stale or hand-edited; re-run {write_hint}",
        )

    return (
        True,
        f"{resource.name}: OK — latest.json + {version}.json + index.json match "
        "rendered output",
    )


def cmd_check(args: argparse.Namespace) -> int:
    targets = (
        [get_resource(args.resource)] if args.resource else list(RESOURCES)
    )
    if not targets:
        print("no resources registered", file=sys.stderr)
        return 1
    failed = False
    for resource in targets:
        ok, msg = _check_resource(resource)
        if not ok:
            failed = True
            print(msg, file=sys.stderr)
        else:
            print(msg)
    return 1 if failed else 0


def cmd_classify(args: argparse.Namespace) -> int:
    """Classify severity of the on-disk current latest.json against `--previous`.

    `--previous` is the path to the latest.json from before the current change
    (typically extracted from the PR base branch via `git show`). Without it,
    classification falls back to comparing against the next-highest checked-in
    pinned version of the same resource.

    A `--previous` path that is explicitly supplied but missing, empty,
    malformed JSON, or non-dict JSON is treated as a plumbing failure: an
    error is logged to stderr and the command exits with code 2. This
    prevents a typo'd path or empty `git show` output from silently
    masquerading as a brand-new resource (which would otherwise be classified
    as `major` and pass CI).

    The "no prior schema → major" path is reserved for the case where
    `--previous` was *not* supplied and no prior pinned version exists.
    """
    resource = get_resource(args.resource)
    versions = list_published_versions(resource)
    if not versions:
        print("major")
        return 0
    current = load_latest(resource)
    if current is None:
        print("major")
        return 0

    previous = _load_previous_arg(args.previous, cmd="classify")
    if previous is None and not args.previous and len(versions) >= 2:
        previous = json.loads(
            (resource.dir() / f"{versions[-2]}.json").read_text()
        )

    print(classify(previous, current))
    return 0


def cmd_bump_check(args: argparse.Namespace) -> int:
    """Enforce that the committed version bump meets the structural severity floor.

    Replaces the infra repo's `schema-bump:<resource>:<sev>` PR-label mechanism. The
    head version is read from the checked-in `latest.json` (which `write`
    auto-computed); the base version from the PR base branch's `--previous`
    copy. Requires the base→head delta to be >= the structurally-detected floor
    and never a rollback. A brand-new resource (no `--previous`) passes — its
    publication is its first version.
    """
    resource = get_resource(args.resource)
    current = load_latest(resource)
    if current is None:
        print(
            f"bump-check: {resource.name} has no checked-in latest.json; run "
            f"`render_schemas.py write --resource {resource.name}` first.",
            file=sys.stderr,
        )
        return 2

    head_version = current.get("version")
    if not head_version:
        print(
            f"bump-check: {resource.name} latest.json has no `version` field.",
            file=sys.stderr,
        )
        return 2

    previous = _load_previous_arg(args.previous, cmd="bump-check")
    if previous is None:
        print(f"{resource.name}: new resource — publishing at {head_version}.")
        return 0

    base_version = previous.get("version")
    if not base_version:
        # The base copy predates versioned publishing — a hand-authored schema
        # being adopted into the generator (e.g. type-map-read/write). Treat it
        # as the 0.0.0 baseline rather than skipping the gate: the floor +
        # rollback checks below then still run against the head version, so a
        # corrupt/blanked base `version` can't silently disable them.
        print(
            f"{resource.name}: base copy predates versioned publishing — "
            "treating as 0.0.0 baseline for the floor check."
        )
        base_version = "0.0.0"

    # Validate both versions are well-formed semver before arithmetic, so a
    # corrupt/hand-edited `version` field fails with the function's exit-2
    # plumbing code rather than an opaque parse_semver traceback.
    for label, value in (("head", head_version), ("base", base_version)):
        if not SEMVER_RE.match(value):
            print(
                f"bump-check: {resource.name} {label} version {value!r} is not valid "
                "MAJOR.MINOR.PATCH semver.",
                file=sys.stderr,
            )
            return 2

    floor = classify(previous, current)
    delta = semver_delta(base_version, head_version)
    if delta == "rollback":
        print(
            f"::error::{resource.name}: version rollback — head {head_version} < base {base_version}.",
            file=sys.stderr,
        )
        return 1
    if SEVERITY_RANK[delta] < SEVERITY_RANK[floor]:
        print(
            f"::error::{resource.name}: version bump {base_version} → {head_version} is "
            f"'{delta}', below the detected floor '{floor}'. Re-run "
            f"`render_schemas.py write --resource {resource.name}` "
            f"(it auto-computes the correct bump).",
            file=sys.stderr,
        )
        return 1

    print(f"{resource.name}: OK — bump {base_version} → {head_version} ('{delta}') >= floor '{floor}'.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if args.paths:
        seen: set[str] = set()
        for resource in RESOURCES:
            for p in resource.source_paths:
                seen.add(p)
            seen.add(f"{resource.dir().relative_to(REPO_ROOT).as_posix()}/**")
        seen.add("scripts/render_schemas.py")
        seen.add(".github/workflows/tests.yml")
        for p in sorted(seen):
            print(p)
        return 0
    if args.latest:
        # `<resource>\t<repo-relative latest.json path>` per resource — lets a
        # caller resolve each resource's output path from the registry instead
        # of hard-coding it.
        for resource in RESOURCES:
            rel = (resource.dir() / "latest.json").relative_to(REPO_ROOT).as_posix()
            print(f"{resource.name}\t{rel}")
        return 0
    for resource in RESOURCES:
        print(resource.name)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_write = sub.add_parser(
        "write", help="render and write {version}.json + latest.json + index.json"
    )
    p_write.add_argument(
        "--resource", required=True, help=f"resource name; one of: {', '.join(r.name for r in RESOURCES)}"
    )
    p_write.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        help="raise the auto-detected bump (upward only); for changes that are "
        "structurally additive but semantically breaking (e.g. a new output enum value)",
    )
    p_write.add_argument(
        "--force",
        action="store_true",
        help="allow overwriting an existing immutable {version}.json",
    )
    p_write.set_defaults(func=cmd_write)

    p_check = sub.add_parser(
        "check",
        help="exit 1 if rendered output differs from checked-in latest.json (all resources by default)",
    )
    p_check.add_argument(
        "--resource",
        help="check just one resource; default checks every registered resource",
    )
    p_check.set_defaults(func=cmd_check)

    p_classify = sub.add_parser(
        "classify",
        help="print severity vs. previous publication: none|patch|minor|major",
    )
    p_classify.add_argument("--resource", required=True, help="resource name")
    p_classify.add_argument(
        "--previous",
        help="Path to previous latest.json (e.g. extracted from PR base branch). "
        "Without it, falls back to the prior pinned version of the same resource.",
    )
    p_classify.set_defaults(func=cmd_classify)

    p_bump = sub.add_parser(
        "bump-check",
        help="enforce that the in-code version bump meets the detected severity floor",
    )
    p_bump.add_argument("--resource", required=True, help="resource name")
    p_bump.add_argument(
        "--previous",
        help="Path to the PR base-branch latest.json (e.g. extracted via `git show`). "
        "Omit for a brand-new resource (first publication).",
    )
    p_bump.set_defaults(func=cmd_bump_check)

    p_list = sub.add_parser(
        "list",
        help="print registered resource names (one per line); "
        "with --paths, print the union of CI path filters",
    )
    p_list.add_argument(
        "--paths",
        action="store_true",
        help="print the union of source/output paths used by the CI workflow",
    )
    p_list.add_argument(
        "--latest",
        action="store_true",
        help="print `<resource>\\t<repo-relative latest.json path>` per resource",
    )
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
