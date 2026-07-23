"""Drift-check CI for schema-owned enums the plugin restates as decision logic.

A handful of enums can't simply be deleted from the plugin: they ARE the
mapping logic (`enum-mappers.md` maps researched provider facts onto schema
enum values; `ProviderFacts` classifies into them; `CLAUDE.md` documents the
closed sets). Per the drift policy, anything that must stay duplicated is pinned
to the contract here. If the contract's enum changes, the matching test fails
and names the divergence, so the prose + mappers are updated in the same change
instead of silently drifting.

This guard reads the enums straight from the **pinned contract models**
(`analitiq-contract-models`) — the same models the `connector-schema-validator`
agent validates against, and the same ones the published JSON Schemas are
generated from. Each document's JSON Schema is generated locally (pydantic
`json_schema()`) and its enum sets compared. That makes the guard **offline,
CDN-free, and self-consistent with the validator**: it pins the plugin's prose
to the exact contract the plugin enforces at authoring time, not a
separately-hosted copy that can drift, 403, or 404.

The suite exercises the in-repo source (whose version
`tests/connector_builder/_pins.py` mirrors); the `connector-schema-validator`
agent self-installs the published runtime pin (`VALIDATOR_PIN`), which trails
that version during a release window. When the package isn't importable the whole
module is skipped (offline-dev convenience) — except in CI, which sets
`DRIFT_REQUIRE_CONTRACT_MODELS=1` so a missing or broken package is a hard
failure there, never a green all-skipped gate. Run `-rs` to print skip reasons.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

# Read the SAME contract models the validator validates against. The shared guard
# skips an offline dev run but hard-fails in CI (DRIFT_REQUIRE_CONTRACT_MODELS=1),
# so this merge gate can never pass by skipping. (A renamed submodule errors at
# the imports below, which the parent-package guard does not cover — that
# asymmetry is intentional: both surface as red in CI.)
from _pins import REPO_ROOT, assert_pinned_versions, require_contract_models  # noqa: E402

require_contract_models("analitiq.contracts")

from pydantic import TypeAdapter  # noqa: E402  (imports gated by the guard above)
from analitiq.contracts.connector import Connector  # noqa: E402
from analitiq.contracts.endpoints import ApiEndpointDoc  # noqa: E402
from analitiq.contracts.shared.common import SLUG_PATTERN  # noqa: E402

PLUGIN_ROOT = REPO_ROOT / "plugins" / "analitiq-connector-builder"

# --- plugin-side expected sets ---------------------------------------------
# These mirror the schema-owned enums restated across CLAUDE.md and
# enum-mappers.md. (io-contracts.md restates the pagination set verbatim and an
# auth `family` set that is intentionally a SUBSET — its API `auth_model.family`
# omits `db`, which never applies to an API.) When a test below fails, update
# BOTH the prose and the matching expected set here in the same change.

EXPECTED_AUTH_TYPES = {
    "api_key",
    "basic_auth",
    "oauth2_authorization_code",
    "oauth2_client_credentials",
    "jwt",
    "db",
    "credentials",
    "aws_iam",
    "none",
}
EXPECTED_ADBC_DRIVERS = {"postgresql", "snowflake", "bigquery"}
# SqlAlchemyTransport.driver is deliberately an OPEN `dialect+driver` pattern —
# no driver allow-list (sync and async DBAPIs are both authorable; dispatch is
# engine-side). The openness itself is what the prose restates as decision
# logic (spec-driver-selection.md §Constraints, the Redshift routing, the
# db-connector-creator checklist), so pin the pattern: a re-tightening — e.g.
# a revert to the old async-only alternation — must fail here and move the
# prose in the same change.
EXPECTED_SQLALCHEMY_DRIVER_PATTERN = r"^[a-z][a-z0-9_]*\+[a-z][a-z0-9_]*$"
EXPECTED_DSN_ENCODINGS = {
    "raw",
    "host",
    "url_userinfo",
    "url_path_segment",
    "url_query_key",
    "url_query_value",
}
EXPECTED_PAGINATION_STYLES = {"offset", "page", "cursor", "link", "keyset"}
# WriteOperation.idempotency `in` targets (api-endpoint ≥ 9.1.0,
# infrastructure#890) — restated in io-contracts.md EndpointFacts,
# endpoint-creator.md, connector-provider-researcher.md, and
# connector-spec-api/SKILL.md.
EXPECTED_IDEMPOTENCY_TARGETS = {"header", "body"}
# Bare-marker arrow_type vocabulary enforced by the contract's authored-shape
# rules (Object→properties, List→items, Json→neither). Owned by the
# api-endpoint contract's `_ARROW_AUTHORED_SHAPE` (the same alternation the
# `arrow_type` pattern embeds); the contract model leaves the sibling-key
# contract open, so the validator enforces it — keep this set in lockstep with
# the prose.
EXPECTED_BARE_MARKER_ARROW_TYPES = {"Object", "List", "Json"}
# The kind + transport discriminators — the outputs of KindMapper /
# TransportTypeMapper in enum-mappers.md, restated in CLAUDE.md. Each lives as a
# `properties.<field>.const` across the per-variant `$defs`. The contract admits
# `nosql` / `document` alongside the storage stubs; the plugin recognizes them
# in its vocabulary but authors none (KindMapper still routes document DBs →
# `database`), the same posture it holds for `file` / `s3` / `stdout`.
EXPECTED_KINDS = {"api", "database", "nosql", "document", "file", "s3", "stdout"}
EXPECTED_TRANSPORT_TYPES = {"http", "sqlalchemy", "adbc", "s3", "file", "stdout"}
# Validator ids a connector/endpoint/type-map finding may carry — restated in
# io-contracts.md's `Diagnostics` enum and the connector-schema-validator agent's
# id table. Owned by `analitiq.validator.VALIDATOR_IDS`, minus the `bundle-*` ids,
# which only apply to pipeline bundles this plugin never validates.
EXPECTED_VALIDATOR_IDS = {
    "contract-model",
    "document",
    "type-map-coverage",
    "type-map-rule",
    "type-map-write-coverage",
    "endpoint-filename",
    "endpoint-id-unique",
    "endpoint-id-locator",
    "embedded-json-schema",
}
# Resolution scopes a `ref` / `${...}` placeholder may lead with — restated as the
# scope table in references/value-expressions.md.
EXPECTED_RESOLUTION_SCOPES = {
    "connector",
    "connection",
    "secrets",
    "auth",
    "stream",
    "state",
    "runtime",
    "request",
    "response",
}
# Where the slug pattern — the `connector_id` / `endpoint_id` charset, owned by
# `analitiq.contracts.shared.common.SLUG_PATTERN` — may appear hand-typed in the
# plugin's prose, and how many times (issue #58). No other site restates the
# regex: the agent-consumed sites reference one of these, and the rest (README,
# the orchestrator's hard rules, definition-of-done) say "the slug pattern"
# without spelling it:
#   - metadata-and-versioning.md — the canonical `connector_id` statement (the
#     field table); the creator agents point here.
#   - endpoint-identity.md — the canonical `endpoint_id` statement (Invariants).
#   - io-contracts.md — two embedded JSON Schemas agents consume as machine
#     vocabulary (the `resources[].key` description and the
#     `endpoint_files[].endpoint_id` constraint), which need the literal.
# A new copy is a recorded decision: it fails the count test until listed here.
EXPECTED_SLUG_PATTERN_SITES = {
    "skills/connector-builder/references/endpoint-identity.md": 1,
    "skills/connector-builder/references/io-contracts.md": 2,
    "skills/connector-builder/references/metadata-and-versioning.md": 1,
}


# --- helpers ---------------------------------------------------------------


def _schema(model_or_type) -> dict:
    """Generate a contract document's JSON Schema from its pinned model.

    `ApiEndpointDoc` is a `BaseModel` subclass (has `model_json_schema`); the
    connector contract is a discriminated `Union` (not a class), so it goes
    through `TypeAdapter`. Same generation path that produces the published
    schema, so the `$defs` layout the extractors below walk is identical.
    """
    if isinstance(model_or_type, type):
        return model_or_type.model_json_schema()
    return TypeAdapter(model_or_type).json_schema()


def _enum_at(schema: dict, *path: str) -> set[str] | None:
    """Return the `enum` set at `$defs/.../<path>`, or None if the path/enum is absent.

    Tolerates a restructured schema: any missing key, a non-dict node
    mid-traversal, or an `enum` that isn't a list yields None (the caller turns
    that into an explicit "schema was restructured" failure).
    """
    node: object = schema
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    if not isinstance(node, dict):
        return None
    enum = node.get("enum")
    if not isinstance(enum, list):
        return None
    return set(enum)


def _pattern_at(schema: dict, *path: str) -> str | None:
    """Return the `pattern` at `$defs/.../<path>`, looking through `anyOf`.

    Optional fields (`str | None`) carry their constraint inside an `anyOf`
    branch rather than on the property node itself. Same restructure
    tolerance as `_enum_at`: a missing path, a non-dict node, or no string
    `pattern` on the node or any branch yields None.
    """
    node: object = schema
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    if not isinstance(node, dict):
        return None
    branches = node.get("anyOf")
    candidates: list[dict] = [node]
    if isinstance(branches, list):
        candidates += [b for b in branches if isinstance(b, dict)]
    for cand in candidates:
        pattern = cand.get("pattern")
        if isinstance(pattern, str):
            return pattern
    return None


def _const_types(schema: dict, def_suffix: str, const_field: str = "type") -> set[str] | None:
    """Collect the `<const_field>` const across `$defs/*<suffix>` definitions.

    Auth families, pagination styles, connector kinds, and transport types are
    each modelled as one discriminated `$def` per variant (e.g. `ApiKeyAuth`,
    `CursorPagination`, `DatabaseConnector`, `AdbcTransport`), pinning
    `properties.<const_field>.const` — not a single flat enum. The discriminator
    field name varies: `type` for auth/pagination, `kind` for connectors,
    `transport_type` for transports.

    Matching is by `$def`-name suffix (pydantic derives `$def` keys from the
    variant class names), so this couples to generated-schema naming. Returns
    None when the suffix matches nothing, or no matched def carries the const —
    i.e. the per-variant modelling changed — so the caller routes it through the
    "contract was restructured" branch instead of misreporting total enum-drift
    (and it can never silently equal an empty expected set).
    """
    out: set[str] = set()
    for name, node in schema.get("$defs", {}).items():
        if name.endswith(def_suffix) and isinstance(node, dict):
            const_node = (node.get("properties") or {}).get(const_field) or {}
            if "const" in const_node:
                out.add(const_node["const"])
    return out or None


def _bare_marker_arrow_types() -> set[str] | None:
    """The authored-shape container markers, read from the pinned model.

    `analitiq.contracts.endpoints._ARROW_AUTHORED_SHAPE` is the single
    definition of the marker alternation (`"Object|List|Json"`) — the same
    constant the api-endpoint `arrow_type` pattern is built from and the
    authored-shape rules key off. There is no public generated-schema
    pointer for this vocabulary (`arrow_type` doesn't surface in the endpoint
    model schema), so this one check reaches a private symbol. Returns None if
    the symbol is gone (renamed) or is no longer a plain `A|B|C` alternation of
    word tokens (reshaped — e.g. compiled, or wrapped as `^(…)$`) — either way
    the caller surfaces a restructure failure rather than splitting garbage
    tokens off a regex.
    """
    from analitiq.contracts import endpoints

    raw = getattr(endpoints, "_ARROW_AUTHORED_SHAPE", None)
    if not isinstance(raw, str) or not re.fullmatch(r"\w+(?:\|\w+)+", raw):
        return None
    return set(raw.split("|"))


# A slug-flavored charset literal: a character class opening with `a-z0-9`,
# plus any following classes / quantifiers / `$` anchor. Matches the exact
# SLUG_PATTERN and the realistic paraphrases of it (`[a-z0-9_-]+`, a bare
# `[a-z0-9]`) — a respelled class like `[0-9a-z_-]` evades it, the accepted
# limit of a lexical detector — while the `[0-9]+` / `[A-Z]+` classes in
# type-map regex examples are not matched. Line-based: a copy wrapped across
# lines yields a truncated match, which fails the equality test and surfaces
# the site anyway.
_SLUG_LITERAL_RE = re.compile(r"\^?\[a-z0-9[^]]*\](?:\[[^]]*\]|[*+$])*")


def _slug_literal_sites() -> list[tuple[str, int, str]]:
    """Every (relpath, lineno, literal) slug-charset occurrence in the plugin's prose."""
    return [
        (path.relative_to(PLUGIN_ROOT).as_posix(), lineno, match.group(0))
        for path in sorted(PLUGIN_ROOT.rglob("*.md"))
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        for match in _SLUG_LITERAL_RE.finditer(line)
    ]


def _diff_msg(label: str, schema_set: set[str] | None, expected: set[str], fix: str) -> str:
    if schema_set is None:
        return (
            f"{label}: enum not found at the expected pointer — the contract was "
            f"restructured. {fix}"
        )
    return (
        f"{label} drift — {fix} "
        f"schema-only={sorted(schema_set - expected)} "
        f"plugin-only={sorted(expected - schema_set)}"
    )


# ---------------------------------------------------------------------------
# Contract-model drift checks (offline) — the enum sets are read from the
# pinned `analitiq-contract-models` package, generated fresh each run.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def connector_schema() -> dict:
    return _schema(Connector)


@pytest.fixture(scope="module")
def api_endpoint_schema() -> dict:
    return _schema(ApiEndpointDoc)


def test_installed_versions_are_pinned() -> None:
    """Guard the guards: every assertion below is only meaningful at the pin."""
    assert_pinned_versions()


def test_auth_types_match_schema(connector_schema: dict) -> None:
    schema_set = _const_types(connector_schema, "Auth")
    assert schema_set == EXPECTED_AUTH_TYPES, _diff_msg(
        "auth.type",
        schema_set,
        EXPECTED_AUTH_TYPES,
        "update CLAUDE.md '## Supported Auth Types' and AuthTypeMapper in "
        "plugins/analitiq-connector-builder/skills/connector-builder/references/enum-mappers.md.",
    )


def test_adbc_drivers_match_schema(connector_schema: dict) -> None:
    schema_set = _enum_at(
        connector_schema, "$defs", "AdbcTransport", "properties", "driver"
    )
    assert schema_set == EXPECTED_ADBC_DRIVERS, _diff_msg(
        "AdbcTransport.driver",
        schema_set,
        EXPECTED_ADBC_DRIVERS,
        "update the driver-selection guidance (enum-mappers.md, "
        "spec-driver-selection.md).",
    )


def test_sqlalchemy_driver_pattern_matches_schema(connector_schema: dict) -> None:
    pattern = _pattern_at(
        connector_schema, "$defs", "SqlAlchemyTransport", "properties", "driver"
    )
    fix = (
        "update the driver guidance (spec-driver-selection.md, "
        "spec-dsn-bindings.md, enum-mappers.md, db-connector-creator.md, "
        "connector-spec-db/SKILL.md, io-contracts.md) and "
        "EXPECTED_SQLALCHEMY_DRIVER_PATTERN together."
    )
    if pattern is None:
        pytest.fail(
            "SqlAlchemyTransport.driver: pattern not found at the expected "
            f"pointer — the contract was restructured. {fix}"
        )
    assert pattern == EXPECTED_SQLALCHEMY_DRIVER_PATTERN, (
        f"SqlAlchemyTransport.driver pattern drift — {fix} "
        f"schema={pattern!r} expected={EXPECTED_SQLALCHEMY_DRIVER_PATTERN!r}"
    )


def test_dsn_encodings_match_schema(connector_schema: dict) -> None:
    schema_set = _enum_at(
        connector_schema, "$defs", "DsnBinding", "properties", "encoding"
    )
    assert schema_set == EXPECTED_DSN_ENCODINGS, _diff_msg(
        "DsnBinding.encoding",
        schema_set,
        EXPECTED_DSN_ENCODINGS,
        "update spec-dsn-bindings.md + CLAUDE.md.",
    )


def test_idempotency_targets_match_schema(api_endpoint_schema: dict) -> None:
    schema_set = _enum_at(
        api_endpoint_schema, "$defs", "Idempotency", "properties", "in"
    )
    assert schema_set == EXPECTED_IDEMPOTENCY_TARGETS, _diff_msg(
        "idempotency.in",
        schema_set,
        EXPECTED_IDEMPOTENCY_TARGETS,
        "update io-contracts.md EndpointFacts.idempotency, endpoint-creator.md, "
        "connector-provider-researcher.md, and connector-spec-api/SKILL.md.",
    )


def test_pagination_styles_match_schema(api_endpoint_schema: dict) -> None:
    schema_set = _const_types(api_endpoint_schema, "Pagination")
    assert schema_set == EXPECTED_PAGINATION_STYLES, _diff_msg(
        "pagination style",
        schema_set,
        EXPECTED_PAGINATION_STYLES,
        "update io-contracts.md ProviderFacts and spec-pagination.md.",
    )


def test_bare_marker_arrow_types_match_schema() -> None:
    schema_set = _bare_marker_arrow_types()
    assert schema_set == EXPECTED_BARE_MARKER_ARROW_TYPES, _diff_msg(
        "authored_shape_type",
        schema_set,
        EXPECTED_BARE_MARKER_ARROW_TYPES,
        "update CLAUDE.md and the container-shape guidance in "
        "plugins/analitiq-connector-builder/skills/connector-spec-db/spec-type-maps.md.",
    )


def test_kinds_match_schema(connector_schema: dict) -> None:
    schema_set = _const_types(connector_schema, "Connector", const_field="kind")
    assert schema_set == EXPECTED_KINDS, _diff_msg(
        "connector.kind",
        schema_set,
        EXPECTED_KINDS,
        "update CLAUDE.md ('kind (one of ...)') and KindMapper in "
        "plugins/analitiq-connector-builder/skills/connector-builder/references/enum-mappers.md.",
    )


def test_write_coverage_probe_gaps_are_documented() -> None:
    """`spec-type-maps.md` names the families the write-coverage check misses.

    That warning is the only signal an author gets about write-map gaps, so the
    prose tells them which families it does NOT exercise. If a future validator
    starts probing one of them, the prose becomes a false warning about a check
    that now works — and if it stops probing another, the list is incomplete.
    Assert the documented gaps against the real probe set.
    """
    from analitiq.validator import connectors

    probes = set(getattr(connectors, "_WRITE_VOCABULARY_PROBES", ()))
    assert probes, (
        "_WRITE_VOCABULARY_PROBES not found — the validator was restructured; "
        "recheck the write-coverage guidance in "
        "plugins/analitiq-connector-builder/skills/connector-spec-db/spec-type-maps.md."
    )

    # Families spec-type-maps.md tells authors to verify by hand.
    documented_gaps = {
        "FixedSizeBinary": lambda p: p.startswith("FixedSizeBinary"),
        "Time32": lambda p: p.startswith("Time32"),
        "tz-aware Timestamp": lambda p: p.startswith("Timestamp(") and "UTC" in p,
        "Decimal256": lambda p: p.startswith("Decimal256"),
    }
    now_probed = sorted(
        name for name, matches in documented_gaps.items() if any(matches(p) for p in probes)
    )
    assert not now_probed, (
        f"write-coverage now probes {now_probed}, which "
        "plugins/analitiq-connector-builder/skills/connector-spec-db/spec-type-maps.md still lists as unprobed. "
        "Drop them from that list."
    )

    # The other direction: families the prose implies ARE probed. Without this,
    # the check only catches gaps closing, never new gaps opening — and the
    # prose would quietly become an incomplete list of what to verify by hand.
    expected_probed = {
        "Boolean": lambda p: p == "Boolean",
        "Json": lambda p: p == "Json",
        "Decimal128": lambda p: p.startswith("Decimal128"),
        "bare Timestamp": lambda p: p.startswith("Timestamp(") and "UTC" not in p,
        "Utf8": lambda p: p == "Utf8",
    }
    stopped_probing = sorted(
        name for name, matches in expected_probed.items() if not any(matches(p) for p in probes)
    )
    assert not stopped_probing, (
        f"write-coverage no longer probes {stopped_probing}, so authors get no "
        "warning for those families. Add them to the by-hand list in "
        "plugins/analitiq-connector-builder/skills/connector-spec-db/spec-type-maps.md."
    )


def test_validator_ids_match_package() -> None:
    """The finding ids the plugin's prose enumerates must be the ones emitted.

    `bundle-*` ids are excluded: they belong to pipeline-bundle validation, which
    this plugin never invokes, so carrying them in an authoring reference would
    imply findings an author can never see.
    """
    from analitiq.validator import VALIDATOR_IDS

    package_set = {vid for vid in VALIDATOR_IDS if not vid.startswith("bundle-")}
    assert package_set == EXPECTED_VALIDATOR_IDS, _diff_msg(
        "validator ids",
        package_set,
        EXPECTED_VALIDATOR_IDS,
        "update the Diagnostics enum in "
        "plugins/analitiq-connector-builder/skills/connector-builder/references/io-contracts.md, the id table in "
        "plugins/analitiq-connector-builder/agents/connector-schema-validator.md, the Agents bullet in CLAUDE.md, "
        "and the check list in README.md.",
    )


def test_resolution_scopes_match_contract() -> None:
    from analitiq.contracts.value_expression import RESOLUTION_SCOPES

    package_set = set(RESOLUTION_SCOPES)
    assert package_set == EXPECTED_RESOLUTION_SCOPES, _diff_msg(
        "resolution scopes",
        package_set,
        EXPECTED_RESOLUTION_SCOPES,
        "update the scope table in "
        "plugins/analitiq-connector-builder/skills/connector-builder/references/value-expressions.md.",
    )


def test_transport_types_match_schema(connector_schema: dict) -> None:
    schema_set = _const_types(connector_schema, "Transport", const_field="transport_type")
    assert schema_set == EXPECTED_TRANSPORT_TYPES, _diff_msg(
        "transport.transport_type",
        schema_set,
        EXPECTED_TRANSPORT_TYPES,
        "update TransportTypeMapper in "
        "plugins/analitiq-connector-builder/skills/connector-builder/references/enum-mappers.md.",
    )


def test_slug_pattern_restatements_match_contract() -> None:
    """Every hand-typed slug-charset literal must equal the contract's SLUG_PATTERN.

    Catches the contract's pattern moving out from under a prose copy, and a
    loose paraphrase being reintroduced — `[a-z0-9_-]+` accepts a leading `_` /
    `-` the contract rejects, which is how 5 of the pre-#58 copies were wrong.
    """
    wrong = [
        (rel, lineno, literal)
        for rel, lineno, literal in _slug_literal_sites()
        if literal != SLUG_PATTERN
    ]
    assert not wrong, (
        f"slug-pattern literals diverging from the contract's {SLUG_PATTERN!r}:\n"
        + "\n".join(
            f"  plugins/analitiq-connector-builder/{rel}:{lineno}  {literal!r}"
            for rel, lineno, literal in wrong
        )
        + "\nWrite the contract's exact pattern, or reference a canonical site "
        "in EXPECTED_SLUG_PATTERN_SITES instead of restating it."
    )


def test_slug_pattern_sites_are_pinned() -> None:
    """The set of files carrying the literal is a recorded decision, per file.

    Both directions: a new copy appearing anywhere in the plugin's prose fails
    until it is listed with a reason, and a canonical statement disappearing
    (consolidated away, or reworded past the detector) fails so the references
    pointing at it cannot silently dangle.
    """
    counts = dict(Counter(rel for rel, _lineno, _literal in _slug_literal_sites()))
    assert counts == EXPECTED_SLUG_PATTERN_SITES, (
        "slug-pattern occurrence counts changed (found vs. expected):\n"
        f"  found:    {counts}\n"
        f"  expected: {EXPECTED_SLUG_PATTERN_SITES}\n"
        "A hand-typed copy appeared or a canonical statement vanished — "
        "reference the canonical sites instead of adding copies, or update "
        "EXPECTED_SLUG_PATTERN_SITES if the move is deliberate."
    )


def test_slug_literal_detector_recall() -> None:
    """Pin the detector itself — its recall is what the two gates above stand on.

    A later "simplification" (say, to `re.escape(SLUG_PATTERN)`) would keep both
    gates green on the exact-pattern sites while silently dropping paraphrase
    coverage — the exact blind spot the 5 pre-#58 loose copies lived in.
    """
    assert _SLUG_LITERAL_RE.fullmatch(SLUG_PATTERN), (
        "_SLUG_LITERAL_RE no longer fully matches SLUG_PATTERN itself — the "
        "canonical sites would surface as truncated/divergent literals."
    )
    paraphrases = ["[a-z0-9_-]+", "[a-z0-9]"]
    missed = [p for p in paraphrases if not _SLUG_LITERAL_RE.fullmatch(p)]
    assert not missed, (
        f"_SLUG_LITERAL_RE no longer catches {missed} — a reintroduced loose "
        "copy would pass both slug-pattern gates."
    )
    # Non-slug charsets that legitimately appear in type-map regex examples.
    non_slug = ["[0-9]+", "[A-Z]+", r"^Time(32|64)\([A-Z]+\)$"]
    false_hits = [s for s in non_slug if _SLUG_LITERAL_RE.search(s)]
    assert not false_hits, (
        f"_SLUG_LITERAL_RE now matches non-slug charsets {false_hits} — the "
        "type-map regex examples would start failing the equality gate."
    )


def test_slug_pattern_governs_the_restated_fields(
    connector_schema: dict, api_endpoint_schema: dict
) -> None:
    """SLUG_PATTERN must be the pattern the contract puts on the restated fields.

    The two tests above pin prose to the imported constant. If `connector_id` /
    `endpoint_id` ever stopped using SLUG_PATTERN, they would keep passing while
    pinning prose to a constant that no longer governs the fields it describes —
    so anchor the constant to the generated schemas here.
    """
    connector_id_patterns = {
        name: props["connector_id"].get("pattern")
        for name, node in connector_schema.get("$defs", {}).items()
        if isinstance(node, dict)
        and "connector_id" in (props := node.get("properties") or {})
    }
    assert connector_id_patterns, (
        "no $def carries a connector_id property — the connector contract was "
        "restructured; re-anchor this guard."
    )
    endpoint_id_pattern = (
        (api_endpoint_schema.get("properties") or {}).get("endpoint_id") or {}
    ).get("pattern")
    off = {
        field: pattern
        for field, pattern in {
            **{f"{name}.connector_id": p for name, p in connector_id_patterns.items()},
            "ApiEndpointDoc.endpoint_id": endpoint_id_pattern,
        }.items()
        if pattern != SLUG_PATTERN
    }
    assert not off, (
        f"fields no longer constrained by SLUG_PATTERN ({SLUG_PATTERN!r}): {off}. "
        "The prose pins above now reference the wrong constant — re-anchor them "
        "to whatever governs connector_id/endpoint_id."
    )
