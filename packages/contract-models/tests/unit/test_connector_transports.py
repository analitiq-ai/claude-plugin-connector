"""Pin the SqlAlchemyTransport.driver boundary.

The `driver` pattern was once a closed async-driver allow-list
(`…\\+(asyncpg|aiomysql|…)$`); it is now deliberately an OPEN
`dialect+driver` pattern — sync and async DBAPIs are both admissible, and
the contract names no drivers (whether the named driver actually exists as
a SQLAlchemy dialect registration is checked at transport build, not
here). Schema parity (`render_schemas.py check`) only proves the artifact
matches the model; nothing else asserts what the model itself accepts. So
pin the pattern verbatim and the accept/reject boundary through the model:
any future tightening (e.g. a revert to an allow-list) or widening is then
a recorded decision that fails loudly, together with the prose that
restates the open boundary as decision logic (spec-driver-selection.md,
spec-dsn-bindings.md, enum-mappers.md, db-connector-creator.md — pinned on
the plugin side by tests/connector_builder/test_schema_drift.py).
"""
import pytest
from pydantic import ValidationError

from analitiq.contracts.connector import SqlAlchemyTransport

DRIVER_PATTERN_EXPECTED = r"^[a-z][a-z0-9_]*\+[a-z][a-z0-9_]*$"


def _driver_pattern() -> str | None:
    """The `pattern` on the driver property, looking through `anyOf`.

    The field is Optional (`str | None`), so pydantic renders the
    constraint inside an `anyOf` branch rather than on the property node.
    """
    node = SqlAlchemyTransport.model_json_schema()["properties"]["driver"]
    for cand in (node, *node.get("anyOf", [])):
        if isinstance(cand, dict) and isinstance(cand.get("pattern"), str):
            return cand["pattern"]
    return None


def test_driver_pattern_verbatim():
    assert _driver_pattern() == DRIVER_PATTERN_EXPECTED


def test_driver_is_optional():
    assert SqlAlchemyTransport(transport_type="sqlalchemy").driver is None


@pytest.mark.parametrize(
    "value",
    [
        # The canonical examples the plugin prose restates — async…
        "postgresql+asyncpg",
        "mysql+aiomysql",
        # …and sync (Redshift's canonical path, plus common sync DBAPIs).
        "redshift+redshift_connector",
        "postgresql+psycopg2",
        "mssql+pyodbc",
    ],
)
def test_driver_accepts(value):
    transport = SqlAlchemyTransport.model_validate(
        {"transport_type": "sqlalchemy", "driver": value}
    )
    assert transport.driver == value


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("redshift_connector", "bare driver — no dialect segment"),
        ("Postgresql+asyncpg", "uppercase is not accepted"),
        ("postgresql+asyncpg ", "trailing space"),
        ("postgresql+", "empty driver segment"),
        ("+asyncpg", "empty dialect segment"),
        ("postgresql+asyncpg+extra", "a second '+'"),
        ("postgresql+2pg", "driver segment must start with a letter"),
        ("postgresql+_x", "driver segment must start with a letter"),
        ("postgresql+asyncpg\n", "newline anchor bypass"),
    ],
)
def test_driver_rejects(value, why):
    with pytest.raises(ValidationError):
        SqlAlchemyTransport.model_validate(
            {"transport_type": "sqlalchemy", "driver": value}
        )
