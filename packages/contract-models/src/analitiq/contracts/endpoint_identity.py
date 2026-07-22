"""Deterministic endpoint-id derivation.

`endpoint_id` is a derived **handle**, never decoded back to a locator. The
verbatim locator is stored separately (database: `database_object`; API:
`operations.*.request.path`) and is the sole target the engine acts on.

Database identity is a **slug + hash**: object names are hostile to the id
charset (case-sensitive quoted identifiers, dots, spaces, unicode), so a
lossless encode is impossible. The slug stays legible; the hash guarantees
uniqueness and determinism::

    slug(schema)__slug(table)[__slug(catalog)]__<hash8>

- ``slug(x)`` lowercases and maps each out-of-charset run to a single ``_``.
- ``<hash8>`` is the first 8 hex characters of a SHA-256 over the **exact
  verbatim** ``(catalog, schema, name)`` tuple, canonically encoded as a JSON
  array so segment boundaries are unambiguous even for identifiers containing
  the delimiter (identifier casing and characters preserved, no normalization).
  It identifies the *object*; it is **not** ``schema_hash`` (which versions a
  captured schema snapshot over ``database_object`` + ``columns`` +
  ``primary_keys``).

The same locator yields the same ``endpoint_id`` on every re-discovery
(idempotent), and the result always satisfies the published ``endpoint_id``
slug pattern (``analitiq.contracts.shared.common.SLUG_PATTERN``).

This is the single source of truth for the derivation: the discovery path
and the connection/endpoint validators import it so they cannot drift.
"""
from __future__ import annotations

import hashlib
import json
import re

# Structural separator between slug parts and the trailing hash. A single "_" is
# valid inside a slugged identifier, so the join token is doubled to read as one
# obvious boundary. Consumers must NOT split on it to recover identity — the
# handle is opaque; read the verbatim locator (`database_object`) instead.
ENDPOINT_SLUG_SEPARATOR = "__"

_NON_SLUG_RUN = re.compile(r"[^a-z0-9]+")
_HASH_HEX_LEN = 8


def slug(value: str) -> str:
    """Lowercase, collapse each out-of-charset run to a single ``_``, trim ``_``.

    Unicode-aware lowercasing via ``str.casefold``. Returns ``""`` when the
    identifier is entirely out of charset (e.g. ``"***"``); callers drop empty
    slugs from the handle — the hash still disambiguates.
    """
    return _NON_SLUG_RUN.sub("_", value.casefold()).strip("_")


def db_hash8(catalog: str | None, schema: str | None, name: str) -> str:
    """First 8 hex of SHA-256 over the verbatim ``[catalog, schema, name]``.

    The payload is a canonical JSON array of the three verbatim fields, NOT a
    dot-joined string: a delimiter-joined encoding is ambiguous when an
    identifier itself contains the delimiter (e.g. ``(schema="x.", name="y")``
    and ``(schema="x", name=".y")`` both dot-join to ``".x..y"`` and would
    collide), which defeats the collision-safety this hash exists to provide.
    JSON quoting makes every boundary unambiguous and keeps ``None`` (absent)
    distinct from ``""``. Identifiers are hashed EXACTLY as discovered — no
    case-folding or normalization — so two objects with the same slug but
    different verbatim identifiers (e.g. ``"Order Items"`` vs ``"order_items"``)
    get distinct hashes and never collide.
    """
    payload = json.dumps([catalog, schema, name], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_HASH_HEX_LEN]


def build_database_object(
    catalog: str | None,
    schema: str | None,
    name: str,
    object_type: str = "table",
) -> dict:
    """Verbatim provider-native object locator dict (`database_object` shape).

    The single builder for the locator carried on `endpoint_ref.database_object`
    AND embedded in the materialized `DatabaseEndpointDoc`. Homing it here means
    the discovery/preview path and the materializer emit a byte-identical
    locator, so their independently-computed `schema_hash` values agree by
    construction (the hash covers `database_object`).

    Absent `catalog`/`schema` are OMITTED (never an explicit null — the
    `DatabaseObject` model rejects null namespaces). Keys use the wire alias
    `schema`. `name` is required.
    """
    obj: dict = {"name": name, "object_type": object_type}
    if schema:
        obj["schema"] = schema
    if catalog:
        obj["catalog"] = catalog
    return obj


def derive_db_endpoint_id(
    catalog: str | None, schema: str | None, name: str
) -> str:
    """Derive ``slug(schema)__slug(table)[__slug(catalog)]__<hash8>``.

    Slug order is schema, table (``name``), catalog — catalog last, only when
    present. Empty slugs are dropped (the hash still disambiguates); when every
    slug is empty the id is the bare 8-hex hash, which still matches the
    published slug pattern (``analitiq.contracts.shared.common.SLUG_PATTERN``).
    """
    if not name:
        raise ValueError("database endpoint name is required to derive endpoint_id")
    parts = [slug(v) for v in (schema, name, catalog) if v is not None]
    parts = [s for s in parts if s]
    parts.append(db_hash8(catalog, schema, name))
    return ENDPOINT_SLUG_SEPARATOR.join(parts)
