#!/usr/bin/env python3
"""Render contract-owned facts from the published package into the prose docs.

The agent-facing prose under this plugin is normative (as is the sibling
connector plugin's) —
the schema contract itself is defined by the published ``analitiq-validator`` +
``analitiq-contract-models`` packages. Anything an agent needs that those
packages already state (enum members, regexes, required-field lists, bounds,
defaults, cross-field rule text) is therefore **generated** into the prose from
the installed package rather than retyped by hand, so a doc cannot drift from
the contract it documents.

Each generated region is delimited in the markdown by a marker pair::

    <!-- BEGIN GENERATED: <block-id> -->
    …emitted content…
    <!-- END GENERATED: <block-id> -->

Everything outside the markers is hand-written judgment (when to use a rule, how
to ask the user, what the plugin refuses to do) — this script never touches it.

Usage::

    python3 plugins/analitiq-pipeline-builder/scripts/gen_contract_docs.py            # rewrite blocks in place
    python3 plugins/analitiq-pipeline-builder/scripts/gen_contract_docs.py --check    # exit 1 if any block is stale

``--check`` is what CI runs: it regenerates into memory and diffs, so a pin bump
that changes the contract fails the build until the docs are regenerated.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

from _analitiq import ensure_deps_or_reexec

# Docs the generator is allowed to rewrite: everything under the plugin root. A block id
# may appear in more than one file; every occurrence is rendered identically.
DOCS_ROOT = Path(__file__).resolve().parent.parent

_BLOCK_RE = re.compile(
    r"(?P<begin><!-- BEGIN GENERATED: (?P<id>[a-z0-9][a-z0-9-]*) -->\n)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END GENERATED: (?P=id) -->)",
    re.DOTALL,
)


class UnknownBlock(KeyError):
    """A doc references a block id no renderer produces — fail loud, never skip."""


# ---------------------------------------------------------------------------
# Renderers (`render_*`) and their helpers. Every `render_*` takes no arguments
# and returns the markdown body for its block, WITHOUT the surrounding markers
# and ending in exactly one newline — `_BLOCK_RE` depends on that trailing newline.
# ---------------------------------------------------------------------------

def _md_escape(text: str) -> str:
    """Make a value safe for a markdown table cell.

    Escapes `|` (which would end the cell) and flattens newlines. Deliberately
    does NOT escape backslashes: the values that reach here are either wrapped in
    a code span by `_code()`, where a backslash is literal, or are published rule
    prose. Escaping would corrupt the very regexes this generator exists to
    reproduce faithfully.

    The plain-cell callers (advisory prose, type summaries) are therefore only as
    safe as the pinned contract's own text. That holds for every rule rc10 ships;
    if a future rule's prose contains markdown metacharacters, escape at that
    call site rather than here.
    """
    return text.replace("|", "\\|").replace("\n", " ")


def _code(value: object) -> str:
    return f"`{_md_escape(str(value))}`"


def _unwrap_alternation(pattern: str) -> str:
    """Strip the `^(?:…)$` wrapper the published alternation patterns use.

    `removeprefix`/`removesuffix` silently no-op on a miss, which would leak the
    anchors into the docs as though they were part of the first alternative. The
    wrapper shape is an assumption about the pinned contract, so assert it.
    """
    if not (pattern.startswith("^(?:") and pattern.endswith(")$")):
        raise RuntimeError(
            f"expected an anchored '^(?:…)$' alternation, got {pattern[:40]!r}…; "
            "the pinned contract changed shape and this renderer needs updating")
    return pattern[len("^(?:"):-len(")$")]


def render_schema_urls() -> str:
    from analitiq.contracts.connection import CONNECTION_SCHEMA_URL
    from analitiq.contracts.endpoints import DATABASE_ENDPOINT_SCHEMA_URL
    from analitiq.contracts.pipelines.config import PIPELINE_SCHEMA_URL
    from analitiq.contracts.stream import STREAM_SCHEMA_URL

    rows = [
        ("Pipeline", "pipelines/<slug>/pipeline.json", PIPELINE_SCHEMA_URL),
        ("Stream", "pipelines/<slug>/streams/<stream-slug>.json", STREAM_SCHEMA_URL),
        ("Connection", "connections/<slug>/connection.json", CONNECTION_SCHEMA_URL),
        ("Database endpoint", "connections/<slug>/definition/endpoints/<endpoint_id>.json",
         DATABASE_ENDPOINT_SCHEMA_URL),
    ]
    out = ["| Entity | Authored file | `$schema` value |", "|---|---|---|"]
    out += [f"| {e} | {_code(f)} | {_code(u)} |" for e, f, u in rows]
    return "\n".join(out) + "\n"


# Concern label -> (module, constant name). Single source for both the
# `shared-vocabulary` block and the hand-typed-pattern gate in
# tests/pipeline_builder/test_prose_vocabulary.py — the pattern-flavored twin of
# `published_vocabularies()`. The gate scans every `*_PATTERN` constant in the
# modules named here, not just these rows.
_SHARED_PATTERN_ROWS = (
    ("Slug (ids; directories by convention)", "analitiq.contracts.shared.common", "SLUG_PATTERN"),
    ("UUID (`*_id` identity fields)", "analitiq.contracts.shared.types", "UUID_PATTERN"),
    ("Cron expression", "analitiq.contracts.shared.common", "CRON_PATTERN"),
    ("No edge whitespace (`display_name`, tags)", "analitiq.contracts.shared.common",
     "NO_EDGE_WHITESPACE_PATTERN"),
)


def shared_patterns() -> dict[str, str]:
    """The patterns the `shared-vocabulary` block emits, keyed by dotted constant name."""
    import importlib

    return {
        f"{module}.{name}": getattr(importlib.import_module(module), name)
        for _label, module, name in _SHARED_PATTERN_ROWS
    }


def render_shared_vocabulary() -> str:
    from analitiq.contracts.shared import common

    patterns = shared_patterns()
    out = ["| Concern | Published constant | Pattern |", "|---|---|---|"]
    out += [f"| {label} | {_code(f'{module}.{name}')} | {_code(patterns[f'{module}.{name}'])} |"
            for label, module, name in _SHARED_PATTERN_ROWS]
    out.append("")
    bounds = [
        ("`display_name` length", f"{common.DISPLAY_NAME_MIN}..{common.DISPLAY_NAME_MAX}"),
        ("`description` max length", str(common.DESCRIPTION_MAX)),
        ("`tags` max count", str(common.TAGS_MAX)),
        ("tag length", f"{common.TAG_MIN_LEN}..{common.TAG_MAX_LEN}"),
    ]
    out += ["| Bound | Value |", "|---|---|"]
    out += [f"| {label} | {_code(value)} |" for label, value in bounds]
    return "\n".join(out) + "\n"


def render_secret_ref_grammar() -> str:
    from analitiq.contracts.connection import SECRET_REF_VALUE_PATTERN

    schemes = _split_top_level_alternatives(_unwrap_alternation(SECRET_REF_VALUE_PATTERN))
    out = [
        "Every `secret_refs` value must carry an explicit scheme — a bare token "
        "(a pasted raw secret) is rejected by the contract.",
        "",
        "Accepted schemes (`analitiq.contracts.connection.SECRET_REF_VALUE_PATTERN`):",
        "",
    ]
    out += [f"- `{s}`" for s in schemes]
    return "\n".join(out) + "\n"


_UNARY_OPERATORS = ("is_null", "is_not_null")

_PROBE_UUID = "11111111-1111-4111-8111-111111111111"


def _accepted_operators_by_scope() -> dict[str, list[str]]:
    """Which filter operators the contract accepts for each endpoint-ref scope.

    The per-scope vocabularies exist in the package only as private constants, so
    this probes the public model instead: validate a minimal stream carrying one
    filter and keep the operators that survive. That cannot drift from the
    contract the way a transcribed list can, and it survives a rename of the
    package's internals.
    """
    from typing import get_args

    from pydantic import ValidationError

    from analitiq.contracts.stream import FilterOperator, StreamInput

    def endpoint_ref(scope: str) -> dict:
        if scope == "connection":
            return {"scope": "connection", "connection_id": _PROBE_UUID,
                    "database_object": {"name": "t", "schema": "public"}}
        return {"scope": "connector", "connection_id": _PROBE_UUID, "endpoint_id": "e"}

    def stream_doc(scope: str, filter_: dict | None) -> dict:
        source: dict = {"endpoint_ref": endpoint_ref(scope)}
        if filter_ is not None:
            source["filters"] = [filter_]
        return {"pipeline_id": _PROBE_UUID, "source": source,
                "destinations": [{"endpoint_ref": endpoint_ref("connection"),
                                  "write": {"mode": "insert"}}]}

    accepted: dict[str, list[str]] = {}
    for scope in ("connection", "connector"):
        # Guard the probe itself: if the baseline (no filters) stops validating,
        # every operator would silently look rejected and the block would render
        # empty. Fail loud instead.
        try:
            StreamInput.model_validate(stream_doc(scope, None))
        except ValidationError as exc:
            raise RuntimeError(
                f"filter-operator probe baseline no longer validates for scope "
                f"{scope!r}; the minimal stream shape in this generator needs "
                f"updating for the pinned contract: {exc}") from exc
        operators = []
        for operator in get_args(FilterOperator):
            probe = {"field": "x", "operator": operator}
            if operator not in _UNARY_OPERATORS:
                probe["value"] = "y"
            try:
                StreamInput.model_validate(stream_doc(scope, probe))
            except ValidationError:
                continue
            operators.append(operator)
        accepted[scope] = operators

    # The baseline guard above only covers the filter-free document. The probe's
    # OTHER moving part is the filter shape itself: rename `Filter.field` upstream
    # and the baseline still validates while every operator is rejected, yielding
    # an empty-but-well-formed table that says "no operator is ever accepted".
    # Assert completeness here, at the boundary, so a local run fails too rather
    # than only the test suite.
    union = set(accepted["connection"]) | set(accepted["connector"])
    published = set(get_args(FilterOperator))
    if union != published:
        raise RuntimeError(
            "filter-operator probe did not reproduce the published vocabulary "
            f"(missing {sorted(published - union)}, unexpected {sorted(union - published)}); "
            "the probe's minimal filter shape needs updating for the pinned contract")
    return accepted


def render_filter_operators() -> str:
    accepted = _accepted_operators_by_scope()
    connection, connector = set(accepted["connection"]), set(accepted["connector"])
    groups = [
        ("Both scopes", connection & connector),
        ('`scope: "connection"` (database) only', connection - connector),
        ('`scope: "connector"` (API) only', connector - connection),
    ]
    out = ["| Availability | Operators |", "|---|---|"]
    for label, members in groups:
        out.append(f"| {label} | {', '.join(f'`{m}`' for m in sorted(members))} |")
    out += [
        "",
        f"`{'`, `'.join(_UNARY_OPERATORS)}` are unary — they must omit `value`; "
        "every other operator requires it.",
    ]
    return "\n".join(out) + "\n"


# Advisory families this plugin is in the lane of — every one is rendered into
# the prose by a renderer below.
IN_SCOPE_ADVISORY_FAMILIES = ("ADV-DBEP-", "ADV-PIPE-", "ADV-RETRY-", "ADV-STRM-")

# Families deliberately NOT rendered: they govern connector / api-endpoint
# documents or server-side run records, none of which this plugin authors.
# Recorded rather than merely omitted so the judgment is reviewable, and so a
# family the contract adds later matches neither list and fails the test that
# pins these two — forcing a decision instead of a silent omission.
OUT_OF_SCOPE_ADVISORY_FAMILIES = (
    "ADV-CONN-", "ADV-CTOR-", "ADV-DSYNC-", "ADV-ENDP-", "ADV-HTTP-", "ADV-TMAP-",
)


def _advisory_block(prefixes: tuple[str, ...]) -> str:
    from analitiq.contracts.shared.advisory import all_rules

    rules = sorted(
        (r for r in all_rules() if r.id.startswith(prefixes)),
        key=lambda r: r.id,
    )
    # Per-prefix, not just overall: a sibling family still matching would
    # otherwise mask one that vanished (dropping ADV-RETRY-* still leaves the
    # four ADV-PIPE-* rules, and the block would render as if nothing were lost).
    for prefix in prefixes:
        if not any(r.id.startswith(prefix) for r in rules):
            raise RuntimeError(f"no advisory rules matched {prefix!r}")
    out = ["| Rule | Constraint |", "|---|---|"]
    out += [f"| {_code(r.id)} | {_md_escape(r.prose)} |" for r in rules]
    return "\n".join(out) + "\n"


# Which families each advisory block emits. Single-sourced: the renderers read
# from here and `advisory_families_rendered()` derives its answer from the same
# map, so the two cannot drift — a second hardcoded tuple would reintroduce
# exactly the dead-constant defect this map exists to remove.
_ADVISORY_BLOCK_FAMILIES: dict[str, tuple[str, ...]] = {
    "advisory-pipeline": ("ADV-PIPE-", "ADV-RETRY-"),
    "advisory-stream": ("ADV-STRM-",),
    "advisory-endpoint": ("ADV-DBEP-",),
}


def advisory_families_rendered() -> tuple[str, ...]:
    """Every family the advisory renderers actually emit, derived not restated."""
    return tuple(sorted({f for fams in _ADVISORY_BLOCK_FAMILIES.values() for f in fams}))


def render_advisory_pipeline() -> str:
    return _advisory_block(_ADVISORY_BLOCK_FAMILIES["advisory-pipeline"])


def render_advisory_stream() -> str:
    return _advisory_block(_ADVISORY_BLOCK_FAMILIES["advisory-stream"])


def render_advisory_endpoint() -> str:
    return _advisory_block(_ADVISORY_BLOCK_FAMILIES["advisory-endpoint"])


def render_validator_ids() -> str:
    from analitiq.validator import VALIDATOR_IDS

    if not VALIDATOR_IDS:
        raise RuntimeError("the published package exposed no validator ids")
    out = [
        "Validator ids the published package can emit:",
        "",
        ", ".join(f"`{v}`" for v in sorted(VALIDATOR_IDS)),
    ]
    return "\n".join(out) + "\n"


def render_endpoint_id_derivation() -> str:
    """The derived database-endpoint handle, shown by calling the published helper.

    Prose used to restate this formula by hand in every file that mentioned an
    `endpoint_id`, and had already drifted — one site wrote `slug(table)` where
    the rest wrote `slug(name)`. A worked example computed by the package settles
    it, and cannot go stale.
    """
    from analitiq.contracts.endpoint_identity import derive_db_endpoint_id

    cases = [
        ("public", "orders", None),
        ("Public", "Orders", "cat"),
    ]
    out = [
        "A database `endpoint_id` is **derived**, not chosen: it is a deterministic "
        "handle over the endpoint's verbatim locator, computed by "
        "`analitiq.contracts.endpoint_identity.derive_db_endpoint_id(catalog, schema, name)`.",
        "",
        "| `catalog` | `schema` | `name` | derived `endpoint_id` |",
        "|---|---|---|---|",
    ]
    for schema, name, catalog in cases:
        derived = derive_db_endpoint_id(catalog, schema, name)
        out.append(f"| {_code(catalog) if catalog else '—'} | {_code(schema)} | "
                   f"{_code(name)} | {_code(derived)} |")
    out += [
        "",
        "Derivation must stay deterministic: a handle that changes for an unchanged "
        "resource mints a new endpoint and breaks every stream pinned to the old one. "
        "Never hand-write one — call the helper (`scripts/endpoint_id.py` wraps it).",
    ]
    return "\n".join(out) + "\n"


# --- Entity field tables, rendered from the published models -------------------

# Block id -> (module path, model class). Each entry emits one field table.
FIELD_TABLE_MODELS = {
    "fields-pipeline": ("analitiq.contracts.pipelines.config", "PipelineInput"),
    "fields-stream": ("analitiq.contracts.stream", "StreamInput"),
    "fields-connection": ("analitiq.contracts.connection", "ConnectionInput"),
    "fields-schedule": ("analitiq.contracts.pipelines.config", "Schedule"),
    "fields-engine": ("analitiq.contracts.pipelines.config", "Engine"),
    "fields-runtime": ("analitiq.contracts.pipelines.config", "Runtime"),
    "fields-batching": ("analitiq.contracts.pipelines.config", "Batching"),
    "fields-logging": ("analitiq.contracts.pipelines.config", "Logging"),
    "fields-error-handling": ("analitiq.contracts.pipelines.config", "ErrorHandling"),
    "fields-pipeline-connections": ("analitiq.contracts.pipelines.config", "PipelineConnections"),
    "fields-database-endpoint": ("analitiq.contracts.endpoints", "DatabaseEndpointDoc"),
    "fields-database-object": ("analitiq.contracts.endpoints", "DatabaseObject"),
    "fields-column": ("analitiq.contracts.endpoints", "Column"),
    "fields-stream-source": ("analitiq.contracts.stream", "StreamSource"),
    "fields-stream-destination": ("analitiq.contracts.stream", "StreamDestination"),
    "fields-stream-write": ("analitiq.contracts.stream", "Write"),
    "fields-stream-execution": ("analitiq.contracts.stream", "Execution"),
    "fields-connector-endpoint-ref": ("analitiq.contracts.stream", "ConnectorEndpointRef"),
    "fields-connection-endpoint-ref": ("analitiq.contracts.stream", "ConnectionEndpointRef"),
    "fields-stream-mapping": ("analitiq.contracts.stream", "StreamMapping"),
    "fields-assignment-target": ("analitiq.contracts.stream", "AssignmentTarget"),
    "fields-assignment-value": ("analitiq.contracts.stream", "AssignmentValue"),
    "fields-validation-rule": ("analitiq.contracts.stream", "ValidationRule"),
}

_CONSTRAINT_KEYS = (
    ("pattern", "pattern"), ("minLength", "minLength"), ("maxLength", "maxLength"),
    ("minimum", "min"), ("maximum", "max"), ("exclusiveMinimum", "exclusiveMin"),
    ("minItems", "minItems"), ("maxItems", "maxItems"), ("uniqueItems", "uniqueItems"),
    ("const", "const"), ("enum", "enum"),
)


def _unwrap_nullable(schema: dict) -> tuple[dict, bool]:
    """Collapse the `anyOf: [X, null]` optional wrapper the models emit."""
    options = schema.get("anyOf")
    if not options:
        return schema, False
    non_null = [o for o in options if o.get("type") != "null"]
    if len(non_null) == len(options):
        return schema, False
    return (non_null[0] if len(non_null) == 1 else {"anyOf": non_null}), True


# Keywords that actually say something about a value's type. A schema carrying
# none of them is an unconstrained "any"; a schema carrying one this function
# does not handle is a gap worth failing on.
_TYPE_BEARING_KEYS = frozenset({
    "type", "$ref", "const", "enum", "anyOf", "oneOf", "allOf", "not",
    "items", "properties", "additionalProperties", "prefixItems",
})


def _type_summary(schema: dict) -> str:
    """One-cell description of a property's type, following $ref by name only."""
    schema, _ = _unwrap_nullable(schema)
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "const" in schema:
        return f"const {schema['const']!r}"
    if "enum" in schema:
        return " | ".join(repr(v) for v in schema["enum"])
    if "anyOf" in schema:
        return " | ".join(_type_summary(o) for o in schema["anyOf"])
    # A discriminated union emits `oneOf` + `discriminator`. Summarising it as a
    # bare type would read to an agent as "free-form" when it is in fact a closed
    # set of variants — `endpoint_ref` is exactly this shape.
    if "oneOf" in schema:
        variants = " | ".join(_type_summary(o) for o in schema["oneOf"])
        discriminator = schema.get("discriminator", {}).get("propertyName")
        return f"{variants} (by `{discriminator}`)" if discriminator else variants
    kind = schema.get("type")
    if kind == "array":
        return f"array of {_type_summary(schema.get('items', {}))}"
    if kind == "object" and "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if isinstance(extra, dict) and extra:
            return f"map of {_type_summary(extra)}"
        return "object"
    if kind:
        return kind
    # No type-bearing keyword at all means the field genuinely accepts any JSON
    # value (`Column.default` is declared that way). Annotation-only keys like
    # `title`/`description`/`default` do not constrain the type, so ignore them.
    # Anything else is a construct this summariser does not understand, and
    # quietly calling it "any" would misdescribe the contract.
    if not (set(schema) & _TYPE_BEARING_KEYS):
        return "any"
    raise RuntimeError(
        f"cannot summarise schema shape {sorted(schema)!r}; the pinned contract "
        "emits a construct this generator does not handle")


# A pattern longer than this is a vocabulary, not a constraint, and inlining it
# makes the row unreadable. The Arrow type pattern is ~900 chars and already has
# its own generated block, so the cell points there instead of repeating it.
_MAX_INLINE_PATTERN = 80


def _constraint_summary(schema: dict) -> str:
    schema, _ = _unwrap_nullable(schema)
    parts = []
    for key, label in _CONSTRAINT_KEYS:
        if key in ("const", "enum"):  # already surfaced by the type cell
            continue
        if key in schema:
            value = schema[key]
            # Elide rather than truncate: a half-shown regex is one an agent
            # might copy, which is worse than not showing it at all.
            if key == "pattern" and len(str(value)) > _MAX_INLINE_PATTERN:
                # Name the source rather than a location: this block is embedded
                # in more than one file, and only some of them carry the
                # arrow-types block that spells the vocabulary out.
                parts.append(f"{label}=(long; see `endpoint-spec/spec-columns.md`)")
                continue
            parts.append(f"{label}={value}")
    items = schema.get("items")
    if isinstance(items, dict):
        for key, label in (("pattern", "item pattern"), ("minLength", "item minLength")):
            if key in items:
                parts.append(f"{label}={items[key]}")
    return ", ".join(_code(p) for p in parts) if parts else "—"


def _render_field_table(module_path: str, class_name: str) -> str:
    import importlib

    model = getattr(importlib.import_module(module_path), class_name)
    schema = model.model_json_schema()
    required = set(schema.get("required", ()))
    properties = schema.get("properties", {})
    if not properties:
        raise RuntimeError(f"{class_name} exposed no properties")

    out = [
        f"`{module_path}.{class_name}` — "
        f"{'closed (`additionalProperties: false`)' if schema.get('additionalProperties') is False else 'open'}"
        f"; required: "
        + (", ".join(f"`{r}`" for r in sorted(required)) if required else "none"),
        "",
        "| Field | Required | Type | Default | Constraints |",
        "|---|---|---|---|---|",
    ]
    for name, prop in properties.items():
        _, nullable = _unwrap_nullable(prop)
        default = prop.get("default", "—")
        default_cell = "—" if default == "—" else _code(repr(default))
        out.append(
            f"| {_code(name)} | {'**yes**' if name in required else 'no'} "
            f"| {_md_escape(_type_summary(prop) + (' | null' if nullable else ''))} "
            f"| {default_cell} | {_constraint_summary(prop)} |"
        )
    if schema.get("allOf"):
        out += ["", f"Carries {len(schema['allOf'])} declarative cross-field "
                    "`if`/`then` rule(s) — see the advisory rules for their prose."]
    return "\n".join(out) + "\n"


# --- Closed-enum vocabulary ----------------------------------------------------

def published_vocabularies() -> dict[str, dict]:
    """Every closed vocabulary an author picks a value from, read off the package.

    Single source for both the generated `enum-vocabulary` block and the prose
    gate in tests/pipeline_builder/test_prose_vocabulary.py. Restating this list in either place
    would recreate exactly the drift this module exists to prevent.

    Each value is {label, members, published_as}.
    """
    import importlib
    from typing import get_args

    from analitiq.contracts import stream

    vocabularies: dict[str, dict] = {}

    def add(key, label, members, published_as):
        # Empty is not the only bad state. A duplicate key would silently drop the
        # earlier vocabulary from the table AND from every gate iterating this
        # dict; a non-string member (an Optional[Literal] slipping through the
        # union path) would render as "typing.Literal[...]" and could never match
        # prose. Both are wrong-not-empty, so guard them here where every caller
        # inherits the check.
        if not members:
            raise RuntimeError(
                f"{key!r} exposed no members; it is no longer a closed vocabulary")
        if key in vocabularies:
            raise RuntimeError(f"duplicate vocabulary key {key!r}")
        if not all(isinstance(m, str) for m in members):
            raise RuntimeError(
                f"{key!r} yielded non-string members {members!r}; the annotation "
                "is no longer a plain string Literal")
        vocabularies[key] = {"label": label, "members": list(members),
                             "published_as": published_as}

    for key, label, module_path, class_name, field in (
        ("status", "`pipeline.status` / `stream.status`",
         "analitiq.contracts.pipelines.config", "PipelineInput", "status"),
        ("schedule.type", "`pipeline.schedule.type`",
         "analitiq.contracts.pipelines.config", "Schedule", "type"),
        ("log_level", "`pipeline.runtime.logging.log_level`",
         "analitiq.contracts.pipelines.config", "Logging", "log_level"),
        ("error_handling.strategy", "`error_handling.strategy`",
         "analitiq.contracts.pipelines.config", "ErrorHandling", "strategy"),
        ("filter.operator", "`stream…filters[].operator`",
         "analitiq.contracts.stream", "Filter", "operator"),
        ("validation_rule.type", "`stream…validate.rules[].type`",
         "analitiq.contracts.stream", "ValidationRule", "type"),
    ):
        model = getattr(importlib.import_module(module_path), class_name)
        annotation = model.model_fields[field].annotation
        # isinstance(str), not truthiness: Optional[Literal[...]] yields
        # (Literal['a','b'], NoneType), whose Literal member is neither None nor a
        # type — a truthy-but-bogus single "member" that would render as
        # "typing.Literal['a','b']" instead of the vocabulary.
        add(key, label, [a for a in get_args(annotation) if isinstance(a, str)],
            f"`{module_path}.{class_name}.{field}`")

    # Discriminated unions keep their members on each variant's discriminator.
    for key, label, union, union_name, discriminator in (
        ("replication.method", "`stream.source.replication.method`",
         stream.Replication, "Replication", "method"),
        ("database_pagination.type", "`stream.source.database_pagination.type`",
         stream.DatabasePagination, "DatabasePagination", "type"),
        ("endpoint_ref.scope", "`…endpoint_ref.scope`",
         stream.EndpointRef, "EndpointRef", "scope"),
    ):
        variants = get_args(get_args(union)[0])
        add(key, label,
            [get_args(v.model_fields[discriminator].annotation)[0] for v in variants],
            f"discriminated union `analitiq.contracts.stream.{union_name}`")

    # `write.mode` is deliberately an open string: a database destination is closed
    # to this set, but an API destination's mode is whatever key the endpoint
    # declares under operations.write, which no contract enum can enumerate.
    add("write.mode", "`stream.destinations[].write.mode` (database)",
        sorted(stream._DB_WRITE_MODES),  # skipcq: PYL-W0212 — contract-internal vocabulary rendered into prose; the generator is the one sanctioned reader
        "`ADV-STRM-013` (API modes are endpoint-declared, so the field itself is `str`)")

    return vocabularies


def render_enum_vocabulary() -> str:
    """Vocabularies an author picks a value from, with where each is published."""
    out = ["| Field | Members | Published as |", "|---|---|---|"]
    for vocab in published_vocabularies().values():
        members = ", ".join(f"`{m}`" for m in vocab["members"])
        out.append(f"| {vocab['label']} | {members} | {vocab['published_as']} |")
    return "\n".join(out) + "\n"


def _split_top_level_alternatives(pattern: str) -> list[str]:
    """Split a regex alternation on its top-level `|` only, ignoring nesting."""
    parts, depth, current = [], 0, []
    escaped = False
    for char in pattern:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "|" and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def render_arrow_types() -> str:
    """The Arrow type vocabulary, split out of the published column pattern.

    `arrow_type` is one regex covering scalars, parameterized types and the
    authored-shape container markers. Prose used to transcribe it as several
    hand-kept tables; this splits the published pattern instead, so the
    vocabulary cannot drift. The pattern itself is generated from the
    engine-published, vendored grammar manifest
    (`analitiq.contracts.arrow_grammar`) — see issue #81.
    """
    from analitiq.contracts.endpoints import ARROW_TYPE_PATTERN

    alternatives = _split_top_level_alternatives(_unwrap_alternation(ARROW_TYPE_PATTERN))
    plain = [a for a in alternatives if a.replace("_", "").isalnum()]
    parameterized = [a for a in alternatives if a not in plain]
    if not (plain and parameterized):
        raise RuntimeError("could not split ARROW_TYPE_PATTERN into its two families")
    if any("<" in a for a in alternatives):
        raise RuntimeError(
            "ARROW_TYPE_PATTERN grew angle-bracket alternatives; restore the "
            "container section this renderer dropped when the vocabulary was "
            "trimmed to the executable set (issue #81)"
        )

    out = [
        "`arrow_type` is validated by one published regex, "
        "`analitiq.contracts.endpoints.ARROW_TYPE_PATTERN` — generated from the "
        "engine-published grammar manifest, so it accepts exactly what the "
        "engine executes. Its top-level alternatives fall into two families.",
        "",
        "**Plain names** — write them exactly as shown:",
        "",
        ", ".join(f"`{a}`" for a in plain),
        "",
        "**Parameterized** — the parameter is part of the type and is *not* optional; "
        "a bare name here is rejected:",
        "",
    ]
    out += [f"- `{a}`" for a in parameterized]
    out += [
        "",
        "There are **no angle-bracket container forms**: nested data is declared "
        "with the bare authored-shape markers `Object` / `List` (with sibling "
        "`properties` / `items` on the owning column or field spec) or opaque "
        "`Json`. `Decimal128/256` additionally require scale <= precision — a "
        "cross-parameter bound the regex cannot express; the validator enforces "
        "it.",
    ]
    return "\n".join(out) + "\n"


RENDERERS = {
    "arrow-types": render_arrow_types,
    "schema-urls": render_schema_urls,
    "shared-vocabulary": render_shared_vocabulary,
    "secret-ref-grammar": render_secret_ref_grammar,
    "filter-operators": render_filter_operators,
    "advisory-pipeline": render_advisory_pipeline,
    "advisory-stream": render_advisory_stream,
    "advisory-endpoint": render_advisory_endpoint,
    "validator-ids": render_validator_ids,
    "endpoint-id-derivation": render_endpoint_id_derivation,
    "enum-vocabulary": render_enum_vocabulary,
    **{
        block_id: (lambda m=module, c=cls: _render_field_table(m, c))
        for block_id, (module, cls) in FIELD_TABLE_MODELS.items()
    },
}


# ---------------------------------------------------------------------------
# Block substitution
# ---------------------------------------------------------------------------

def render_text(text: str, source: str) -> str:
    """Return `text` with every generated block re-rendered from the package."""

    def _sub(match: re.Match) -> str:
        block_id = match.group("id")
        try:
            renderer = RENDERERS[block_id]
        except KeyError:
            raise UnknownBlock(
                f"{source}: no renderer for generated block {block_id!r}; "
                f"known blocks: {', '.join(sorted(RENDERERS))}"
            ) from None
        return match.group("begin") + renderer() + match.group("end")

    return _BLOCK_RE.sub(_sub, text)


# CLAUDE.md documents the block mechanism, so it shows the markers inside a
# fenced example rather than carrying a real block. It sits inside the plugin
# root (it used to sit above it, outside this scan), so it has to be excluded
# explicitly or --check would try to "fill in" the illustration.
# Exact paths, not basenames: `rglob` is recursive, so excluding by name would
# also exempt any nested CLAUDE.md (skills/*/CLAUDE.md, agents/CLAUDE.md) that
# legitimately carried a block.
NOT_GENERATED = frozenset({DOCS_ROOT / "CLAUDE.md"})


def _strip_fenced_blocks(text: str) -> str:
    """Drop ``` fenced regions so markers shown as examples don't count."""
    return re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)


def generated_docs() -> list[Path]:
    """Every markdown file under the plugin root carrying a generated block."""
    for excluded in NOT_GENERATED:
        # The exemption is only valid while the file's markers are illustrative
        # (shown inside a fenced example). If someone turns one into a real
        # block, it would stop being generated AND stop being checked, with no
        # diff and no warning - so verify the premise instead of trusting it.
        if excluded.exists() and _BLOCK_RE.search(_strip_fenced_blocks(excluded.read_text())):
            raise SystemExit(
                f"{excluded} carries a generated block outside a fenced example "
                "but is exempt via NOT_GENERATED; remove the exemption or move "
                "the block to a document the generator owns.")
    return sorted(
        p for p in DOCS_ROOT.rglob("*.md")
        if p not in NOT_GENERATED and "<!-- BEGIN GENERATED:" in p.read_text()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="Do not write; exit 1 if any generated block is stale.")
    args = parser.parse_args(argv)

    ensure_deps_or_reexec(__file__)

    docs = generated_docs()
    if not docs:
        print("no documents carry a generated block", file=sys.stderr)
        return 1

    stale: list[str] = []
    for path in docs:
        current = path.read_text()
        rendered = render_text(current, str(path))
        if current == rendered:
            continue
        rel = path.relative_to(DOCS_ROOT.parents[1])
        if args.check:
            stale.append(rel.as_posix())
            sys.stdout.writelines(difflib.unified_diff(
                current.splitlines(keepends=True), rendered.splitlines(keepends=True),
                fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        else:
            path.write_text(rendered)
            print(f"regenerated {rel}")

    if args.check and stale:
        print(f"\n{len(stale)} document(s) out of sync with the published contract: "
              f"{', '.join(stale)}\nRun: python3 plugins/analitiq-pipeline-builder/scripts/gen_contract_docs.py",
              file=sys.stderr)
        return 1
    if args.check:
        print(f"{len(docs)} document(s) in sync with the published contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
