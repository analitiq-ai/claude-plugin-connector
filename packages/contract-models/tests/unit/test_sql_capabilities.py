"""Pin the SQL write-path capability models and the connector-level write unit.

Issue #87: the engine's SQL write path is "refuse, don't guess"
(analitiq-engine#390, ADR `docs/sql-write-path-v2.md` §5) — it reads SQL-shape
capabilities from the connector definition instead of probing the live database.
`DatabaseConnector` gains an optional `sql_capabilities` block and `ConnectorBase`
gains an optional `write_unit`; the contract models are `extra="forbid"`, so a
connector cannot declare either block until it ships here.

Two facts have to hold and stay held, so both are pinned here:

1. **Model boundary.** A declared `sql_capabilities` block is COMPLETE — all
   five top-level facts required — and the two cross-field rules
   (`stage.dedicated_schema` present iff `stage.schema == "dedicated"`;
   `write_unit` carries at least one of `rows`/`bytes`) are enforced. Omission
   of either block is legal (backwards compatible).

2. **JSON-Schema parity.** The two cross-field rules are mirrored into the
   published JSON Schema via `json_schema_extra` (the same technique as
   `AdbcTransport`'s dsn/db_kwargs `anyOf` and `ConnectionConditionPredicate`'s
   exactly-one-operator `oneOf`), so a JSON-Schema-only consumer (the FE, a
   third-party validator) rejects exactly what the Pydantic model rejects for
   these structural cross-field rules — proven here both on each sub-model's own
   schema and, end-to-end, against the published `connector/latest.json` a real
   consumer fetches. (Primitive-type coercion is deliberately out of scope: like
   every int field in the contract, `rows`/`bytes` inherit Pydantic's lax
   bool→int coercion that a `type: integer` schema does not share — a
   contract-wide characteristic, not a rule this file mirrors.) These tests
   exercise the parity through an external Draft 2020-12 validator, not just the
   Python model.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from analitiq.contracts.connector import (
    SqlCapabilities,
    SqlStageCapabilities,
    WriteUnit,
    parse_connector,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
POSTGRES_EXAMPLE = (
    REPO_ROOT
    / "plugins"
    / "analitiq-connector-builder"
    / "skills"
    / "connector-spec-db"
    / "examples"
    / "postgresql"
    / "postgresql.example.json"
)

# A minimal, fully-declared stage/capabilities/write-unit trio reused as the
# accepted baseline that the negative cases mutate.
VALID_STAGE = {"scope": "temp", "schema": "target", "transactional_ddl": True}
VALID_SQL_CAPS = {
    "catalog": "none",
    "session_targeting": "per_statement",
    "merge_form": "merge",
    "bulk_load": "copy_from",
    "stage": VALID_STAGE,
}


def _external_validator(model) -> Draft202012Validator:
    """A Draft 2020-12 validator over the model's own published JSON Schema.

    `json_schema_extra` rides into `model_json_schema()`, so this is exactly the
    contract an external consumer validates against — no runtime Python model in
    the loop.
    """
    return Draft202012Validator(model.model_json_schema())


# ---------------------------------------------------------------------------
# SqlStageCapabilities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"scope": "temp", "schema": "target", "transactional_ddl": True},
        {"scope": "real", "schema": "target", "transactional_ddl": False},
        {
            "scope": "real",
            "schema": "dedicated",
            "dedicated_schema": "_analitiq_stage",
            "transactional_ddl": True,
        },
    ],
)
def test_stage_accepts(payload):
    stage = SqlStageCapabilities.model_validate(payload)
    # `schema` is the wire key; the Python attribute is `schema_` (it would
    # otherwise shadow BaseModel.schema — the same aliasing the `in_`/`in`
    # predicate operator uses).
    assert stage.schema_ == payload["schema"]
    assert _external_validator(SqlStageCapabilities).is_valid(payload)


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (
            {"scope": "temp", "schema": "dedicated", "transactional_ddl": True},
            "schema='dedicated' requires dedicated_schema",
        ),
        (
            {
                "scope": "temp",
                "schema": "dedicated",
                "dedicated_schema": "",
                "transactional_ddl": True,
            },
            "dedicated_schema must be non-empty",
        ),
        (
            {
                "scope": "temp",
                "schema": "dedicated",
                "dedicated_schema": "   ",
                "transactional_ddl": True,
            },
            "dedicated_schema must not be whitespace-only (SQL identifier)",
        ),
        (
            {
                "scope": "temp",
                "schema": "dedicated",
                "dedicated_schema": " _stage ",
                "transactional_ddl": True,
            },
            "dedicated_schema must not have leading/trailing whitespace",
        ),
        (
            {
                "scope": "temp",
                "schema": "target",
                "dedicated_schema": "_stage",
                "transactional_ddl": True,
            },
            "schema='target' forbids dedicated_schema",
        ),
        (
            {"schema": "target", "transactional_ddl": True},
            "scope is required",
        ),
        (
            {"scope": "temp", "transactional_ddl": True},
            "schema is required",
        ),
        (
            {"scope": "temp", "schema": "target"},
            "transactional_ddl is required",
        ),
        (
            {"scope": "session", "schema": "target", "transactional_ddl": True},
            "scope outside the closed enum",
        ),
        (
            {"scope": "temp", "schema": "other", "transactional_ddl": True},
            "schema outside the closed enum",
        ),
        (
            {
                "scope": "temp",
                "schema": "target",
                "transactional_ddl": True,
                "x-extra": 1,
            },
            "unknown keys are forbidden (closed contract)",
        ),
    ],
)
def test_stage_rejects(payload, why):
    with pytest.raises(ValidationError):
        SqlStageCapabilities.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        # These trip the cross-field rule that the JSON-Schema `oneOf` mirror
        # must also reject — proving model and published schema agree.
        {"scope": "temp", "schema": "dedicated", "transactional_ddl": True},
        {
            "scope": "temp",
            "schema": "target",
            "dedicated_schema": "_stage",
            "transactional_ddl": True,
        },
        {
            "scope": "temp",
            "schema": "dedicated",
            "dedicated_schema": "",
            "transactional_ddl": True,
        },
    ],
)
def test_stage_json_schema_rejects_cross_field_violations(payload):
    assert not _external_validator(SqlStageCapabilities).is_valid(payload)


@pytest.mark.parametrize(
    "dedicated_schema",
    ["   ", " _stage ", "_stage "],
)
def test_stage_whitespace_dedicated_schema_rejected_by_model_and_schema(
    dedicated_schema,
):
    # `dedicated_schema` becomes a SQL identifier, so whitespace-only and
    # edge-whitespace names are rejected (NO_EDGE_WHITESPACE_PATTERN) — and the
    # model and its published schema must agree.
    payload = {
        "scope": "temp",
        "schema": "dedicated",
        "dedicated_schema": dedicated_schema,
        "transactional_ddl": True,
    }
    with pytest.raises(ValidationError):
        SqlStageCapabilities.model_validate(payload)
    assert not _external_validator(SqlStageCapabilities).is_valid(payload)


def test_stage_target_with_explicit_null_dedicated_schema_accepted():
    # The `oneOf` mirror carries a dedicated null-branch so `target` +
    # explicit `dedicated_schema: null` is accepted (not just omission). Pin
    # both the model and the published-schema acceptance so a refactor that
    # drops that branch fails as a parity break, not silently.
    payload = {
        "scope": "temp",
        "schema": "target",
        "dedicated_schema": None,
        "transactional_ddl": True,
    }
    stage = SqlStageCapabilities.model_validate(payload)
    assert stage.dedicated_schema is None
    assert _external_validator(SqlStageCapabilities).is_valid(payload)


def test_stage_schema_uses_wire_alias_not_python_attr():
    # The alias is authoritative on the wire: the Python attribute name must not
    # leak into the accepted contract.
    with pytest.raises(ValidationError):
        SqlStageCapabilities.model_validate(
            {"scope": "temp", "schema_": "target", "transactional_ddl": True}
        )


# ---------------------------------------------------------------------------
# SqlCapabilities
# ---------------------------------------------------------------------------


def test_sql_capabilities_accepts_full_block():
    caps = SqlCapabilities.model_validate(VALID_SQL_CAPS)
    assert caps.merge_form == "merge"
    assert caps.stage.scope == "temp"


@pytest.mark.parametrize(
    "missing",
    ["catalog", "session_targeting", "merge_form", "bulk_load", "stage"],
)
def test_sql_capabilities_rejects_partial_block(missing):
    # "All five facts required inside a declared block — a partial declaration
    # is a config error, not implicit defaults."
    payload = copy.deepcopy(VALID_SQL_CAPS)
    del payload[missing]
    with pytest.raises(ValidationError):
        SqlCapabilities.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("catalog", "readonly"),
        ("session_targeting", "per_session"),
        ("merge_form", "upsert"),
        ("bulk_load", "copy"),
    ],
)
def test_sql_capabilities_rejects_off_vocabulary(field, value):
    payload = copy.deepcopy(VALID_SQL_CAPS)
    payload[field] = value
    with pytest.raises(ValidationError):
        SqlCapabilities.model_validate(payload)


def test_sql_capabilities_rejects_unknown_key():
    payload = copy.deepcopy(VALID_SQL_CAPS)
    payload["x-vendor"] = True
    with pytest.raises(ValidationError):
        SqlCapabilities.model_validate(payload)


# Hand-pinned expected member sets — a deliberate restatement so a future
# NARROWING of any enum (dropping/renaming a member) fails loudly. Neither the
# off-vocabulary reject test nor `render_schemas.py check` catches a narrowing:
# a narrow-then-re-render leaves the committed schema self-consistent and green.
# This is the sanctioned "test's assertion target" copy (no-drift rule #3),
# pinning the accept boundary the way test_connector_transports.py pins the
# driver pattern verbatim.
EXPECTED_SQL_CAP_ENUMS = {
    "catalog": {"none", "read", "full"},
    "session_targeting": {"per_statement", "session_default"},
    "merge_form": {"merge", "insert_on_conflict", "insert_on_duplicate_key", "none"},
    "bulk_load": {
        "none",
        "copy_from",
        "load_data_local_infile",
        "adbc_ingest",
        "load_job",
    },
}


@pytest.mark.parametrize(
    ("field", "expected"), sorted(EXPECTED_SQL_CAP_ENUMS.items())
)
def test_sql_capabilities_enum_membership_is_pinned(field, expected):
    # The rendered enum must equal the pinned set exactly (catches add OR drop)...
    rendered = set(SqlCapabilities.model_json_schema()["properties"][field]["enum"])
    assert rendered == expected, f"{field} enum drifted from the pinned set"
    # ...and every pinned member must actually validate through the model.
    for member in expected:
        payload = copy.deepcopy(VALID_SQL_CAPS)
        payload[field] = member
        assert getattr(SqlCapabilities.model_validate(payload), field) == member


# ---------------------------------------------------------------------------
# WriteUnit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"rows": 200_000},
        {"bytes": 33_554_432},
        {"rows": 200_000, "bytes": 33_554_432},
        {"rows": 1},
        {"bytes": 1},
    ],
)
def test_write_unit_accepts(payload):
    WriteUnit.model_validate(payload)
    assert _external_validator(WriteUnit).is_valid(payload)


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({}, "at least one of rows/bytes is required"),
        ({"rows": None, "bytes": None}, "explicit nulls do not count as a bound"),
        ({"rows": 0}, "rows must be >= 1"),
        ({"bytes": 0}, "bytes must be >= 1"),
        ({"rows": -5}, "rows must be >= 1"),
        ({"rows": 100, "extra": 1}, "unknown keys are forbidden"),
    ],
)
def test_write_unit_rejects(payload, why):
    with pytest.raises(ValidationError):
        WriteUnit.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [{}, {"rows": None, "bytes": None}, {"rows": 0}, {"bytes": 0}],
)
def test_write_unit_json_schema_rejects(payload):
    # The `anyOf` mirror must reject the same empty/under-bound payloads.
    assert not _external_validator(WriteUnit).is_valid(payload)


# ---------------------------------------------------------------------------
# Field wiring on the connector documents
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_example() -> dict:
    return json.loads(POSTGRES_EXAMPLE.read_text())


def test_database_connector_carries_both_blocks(db_example):
    doc = copy.deepcopy(db_example)
    doc["sql_capabilities"] = copy.deepcopy(VALID_SQL_CAPS)
    doc["write_unit"] = {"rows": 200_000, "bytes": 33_554_432}
    connector = parse_connector(doc)
    assert connector.sql_capabilities.bulk_load == "copy_from"
    assert connector.sql_capabilities.stage.schema_ == "target"
    assert connector.write_unit.rows == 200_000


def test_both_blocks_are_optional(db_example):
    # Omission is legal — every connector authored before #87 stays valid.
    connector = parse_connector(copy.deepcopy(db_example))
    assert connector.sql_capabilities is None
    assert connector.write_unit is None


def test_database_connector_rejects_partial_sql_capabilities(db_example):
    doc = copy.deepcopy(db_example)
    caps = copy.deepcopy(VALID_SQL_CAPS)
    del caps["stage"]
    doc["sql_capabilities"] = caps
    with pytest.raises(ValidationError):
        parse_connector(doc)


def test_database_connector_rejects_empty_write_unit(db_example):
    # The at-least-one-bound rule must fire through the whole connector, not
    # only on the isolated WriteUnit model — symmetric to the partial
    # sql_capabilities check above.
    doc = copy.deepcopy(db_example)
    doc["write_unit"] = {}
    with pytest.raises(ValidationError):
        parse_connector(doc)


def test_sql_capabilities_is_database_only(db_example):
    # `sql_capabilities` is a SQL fact; other kinds must not be able to smuggle
    # it in past the closed (`extra="forbid"`) contract. Build a minimal API
    # connector off the DB example's shared blocks and confirm rejection.
    api_doc = {
        "$schema": db_example["$schema"],
        "kind": "api",
        "connector_id": "acme-api",
        "version": "1.0.0",
        "default_transport": "main",
        "transports": {
            "main": {"transport_type": "http", "base_url": "https://api.acme.test"}
        },
        "auth": {"type": "none"},
        "connection_contract": {},
        "sql_capabilities": copy.deepcopy(VALID_SQL_CAPS),
    }
    with pytest.raises(ValidationError):
        parse_connector(api_doc)


def test_write_unit_is_connector_level(db_example):
    # `write_unit` is connector-level, so it is valid on a non-database kind.
    api_doc = {
        "$schema": db_example["$schema"],
        "kind": "api",
        "connector_id": "acme-api",
        "version": "1.0.0",
        "default_transport": "main",
        "transports": {
            "main": {"transport_type": "http", "base_url": "https://api.acme.test"}
        },
        "auth": {"type": "none"},
        "connection_contract": {},
        "write_unit": {"rows": 5_000},
    }
    connector = parse_connector(api_doc)
    assert connector.write_unit.rows == 5_000


# ---------------------------------------------------------------------------
# Published-artifact structural pin
# ---------------------------------------------------------------------------


def test_published_connector_schema_exposes_new_defs():
    """The rendered public schema must carry the new $defs and their mirrors.

    `render_schemas.py check` already pins committed-vs-rendered, but this makes
    the *presence* of the cross-field mirrors an explicit, named assertion so a
    silent regression (e.g. dropping the `json_schema_extra`) fails loudly here
    too, at the exact contract external consumers fetch.
    """
    latest = json.loads(
        (REPO_ROOT / "schemas" / "connector" / "latest.json").read_text()
    )
    defs = latest["$defs"]
    assert {"SqlCapabilities", "SqlStageCapabilities", "WriteUnit"} <= set(defs)
    assert "oneOf" in defs["SqlStageCapabilities"]
    assert "anyOf" in defs["WriteUnit"]
    assert "sql_capabilities" in defs["DatabaseConnector"]["properties"]
    assert "write_unit" in defs["DatabaseConnector"]["properties"]
    # Connector-level: present on every kind that has properties.
    assert "write_unit" in defs["ApiConnector"]["properties"]
    # SQL-only: absent on non-database kinds.
    assert "sql_capabilities" not in defs["ApiConnector"]["properties"]


def test_full_connector_validates_against_published_schema(db_example):
    """End-to-end parity against the artifact a real consumer actually fetches.

    The other `_external_validator` tests exercise isolated sub-model schemas;
    a consumer validates a whole connector.json against `connector/latest.json`.
    This proves the mirrors survive composition into the full connector document
    — a valid doc passes, and each cross-field / field rule is rejected by the
    published schema exactly as the Pydantic model rejects it.
    """
    schema = json.loads(
        (REPO_ROOT / "schemas" / "connector" / "latest.json").read_text()
    )
    validator = Draft202012Validator(schema)

    valid = copy.deepcopy(db_example)
    valid["sql_capabilities"] = copy.deepcopy(VALID_SQL_CAPS)
    valid["write_unit"] = {"rows": 200_000, "bytes": 33_554_432}
    # A dedicated stage naming its schema, to exercise that branch end-to-end.
    valid["sql_capabilities"]["stage"] = {
        "scope": "real",
        "schema": "dedicated",
        "dedicated_schema": "_analitiq_stage",
        "transactional_ddl": True,
    }
    assert validator.is_valid(valid), sorted(
        e.message for e in validator.iter_errors(valid)
    )
    parse_connector(valid)  # the model agrees

    # Each `mutate` is applied to a fresh deepcopy of the valid doc, so a
    # rejection isolates to that one change. Cover every cross-field / field rule
    # end-to-end — not just one failure mode — against the composed artifact.
    def _drop_stage_name(doc):
        doc["sql_capabilities"]["stage"].pop("dedicated_schema")  # dedicated, unnamed

    def _blank_stage_name(doc):
        doc["sql_capabilities"]["stage"]["dedicated_schema"] = "   "  # field pattern

    def _empty_write_unit(doc):
        doc["write_unit"] = {}  # anyOf at-least-one-bound

    for mutate in (_drop_stage_name, _blank_stage_name, _empty_write_unit):
        broken = copy.deepcopy(valid)
        mutate(broken)
        assert not validator.is_valid(broken), broken
        with pytest.raises(ValidationError):
            parse_connector(broken)
