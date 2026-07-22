"""Pin the identifier patterns and the runtime checks compiled from them.

`UUID_PATTERN` and `VERSIONED_ID_PATTERN` were once written out twice — as a
string in `shared.types` and compiled in `_identity` — with a comment asking a
human to keep them in sync. They now derive from one `_UUID_BODY`, which fixes
the drift but concentrates the blast radius: a one-character edit to that body
atomically moves the pattern in six published schemas (connection, pipeline,
stream, connection-read, org-read, user) AND the runtime `_VERSIONED_ID_RE`.
Schema parity would not object — it only checks the artifact matches the model,
and both move together.

So pin the strings verbatim, and pin the runtime behavior the string encodes
(RFC-4122 version nibble 1-5, variant nibble 8/9/a/b) which no schema test
reaches.
"""
import pytest

from analitiq.contracts._identity import validate_versioned_id
from analitiq.contracts.shared.types import UUID_PATTERN, VERSIONED_ID_PATTERN

_UUID_BODY_EXPECTED = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def test_uuid_pattern_verbatim():
    assert UUID_PATTERN == rf"^{_UUID_BODY_EXPECTED}$"


def test_versioned_id_pattern_verbatim():
    assert VERSIONED_ID_PATTERN == rf"^{_UUID_BODY_EXPECTED}_v[1-9][0-9]*$"


@pytest.mark.parametrize(
    "value",
    [
        "b4904c77-0a4a-4a8d-a768-4a8b5f2f2414_v1",
        "b4904c77-0a4a-4a8d-a768-4a8b5f2f2414_v10",
    ],
)
def test_validate_versioned_id_accepts(value):
    assert validate_versioned_id(value) == value


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("b4904c77-0a4a-4a8d-a768-4a8b5f2f2414", "no version suffix"),
        ("b4904c77-0a4a-4a8d-a768-4a8b5f2f2414_v0", "version must be >= 1"),
        ("b4904c77-0a4a-0a8d-a768-4a8b5f2f2414_v1", "version nibble 0 is not RFC-4122"),
        ("b4904c77-0a4a-4a8d-c768-4a8b5f2f2414_v1", "variant nibble c is not RFC-4122"),
        ("B4904C77-0A4A-4A8D-A768-4A8B5F2F2414_v1", "uppercase is not accepted"),
        ("not-a-uuid_v1", "not a uuid"),
        ("b4904c77-0a4a-4a8d-a768-4a8b5f2f2414_v1\nx", "newline anchor bypass"),
    ],
)
def test_validate_versioned_id_rejects(value, why):
    with pytest.raises(ValueError):
        validate_versioned_id(value)
