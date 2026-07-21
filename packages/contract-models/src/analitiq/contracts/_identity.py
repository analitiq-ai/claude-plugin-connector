"""Entity-id and UUID identity helpers — pure, stdlib-only.

The single home for the identifier-parsing/validation logic shared between the
runtime's persistence layer (versioned sort-key handling) and the Pydantic
contract models (``analitiq.contracts.shared.types``). Kept dependency-free so the
``analitiq-contract-models`` package can ship it verbatim: the contract models
must validate identifiers offline, without dragging in the storage-coupled
persistence/helper modules that used to own these functions.

Those modules re-export the parsing helpers from here, so existing callers
keep working. The compiled regexes are private to this module; the pattern
strings are re-exported by `analitiq.contracts.shared.types`.
"""
from __future__ import annotations

import re
import uuid

# RFC-4122 strict: version nibble [1-5], variant nibble [89ab]. Declared once
# here and re-exported by `analitiq.contracts.shared.types`, so the JSON-Schema
# constraint a consumer validates against and the runtime check below cannot
# drift apart.
_UUID_BODY = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
UUID_PATTERN = rf"^{_UUID_BODY}$"
VERSIONED_ID_PATTERN = rf"^{_UUID_BODY}_v[1-9][0-9]*$"

# `*_PATTERN` is the string a JSON Schema carries; `*_RE` is a compiled regex.
# `_VERSIONED_ID_RE` compiles the published pattern above, so the runtime check
# and the schema constraint cannot disagree. `_VERSION_RE` has no published
# counterpart: it is a loose parse helper (any `id_vX.Y.Z`), deliberately
# broader than the strict versioned-id grammar.
_VERSION_RE = re.compile(r"^(.+)_v(\d+(?:\.\d+)*)$")
_VERSIONED_ID_RE = re.compile(VERSIONED_ID_PATTERN)


def parse_version_string(version_str: str) -> int:
    """Parse a version string like '1.2.3' or '1' into an integer for storage.

    Converts semantic version to integer:
    - '1' -> 1
    - '1.0' -> 100
    - '1.2' -> 102
    - '1.2.3' -> 10203

    Args:
        version_str: Version string (e.g., '1', '1.2', '1.2.3')

    Returns:
        Integer version number
    """
    parts = version_str.split('.')
    if len(parts) == 1:
        return int(parts[0])
    elif len(parts) == 2:
        return int(parts[0]) * 100 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])
    else:
        raise ValueError(f"Invalid version format: {version_str}")


def parse_entity_id(entity_id_with_version: str) -> tuple[str, int | None]:
    """Parse an entity ID that may include a version suffix.

    Args:
        entity_id_with_version: Entity ID, optionally with version (e.g., 'uuid_v1.2.3')

    Returns:
        Tuple of (entity_id, version) where version is None if not specified
    """
    match = _VERSION_RE.match(entity_id_with_version)
    if match:
        entity_id = match.group(1)
        version_str = match.group(2)
        version = parse_version_string(version_str)
        return entity_id, version
    return entity_id_with_version, None


def validate_versioned_id(value: str) -> str:
    """Validate that ID follows versioned format: {uuid}_v{version}.

    Args:
        value: The ID string to validate

    Returns:
        The validated value

    Raises:
        ValueError: If the ID doesn't match the versioned format
    """
    if not _VERSIONED_ID_RE.match(value):
        raise ValueError(
            f"ID must be versioned in format '{{uuid}}_v{{version}}', got: {value}"
        )
    return value


def _is_valid_uuid(value):
    # Accept any RFC-4122 UUID (v1–v5). The DIP registry webhook mints
    # deterministic v5 ids from the connector slug so retries converge on
    # the same row; a v4-only check would reject those.
    try:
        val = uuid.UUID(str(value))
        return str(val).lower() == str(value).lower()
    except (ValueError, AttributeError, TypeError):
        return False
