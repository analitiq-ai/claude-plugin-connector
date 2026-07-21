"""Connection credentials sidecar model — the local secrets file contract.

Source of the PUBLIC `credentials` JSON Schema. A connection's secret material
lives OUT of the connection document, in a local `credentials.json` file the
engine reads at run time; the connection document references each value by name
through `secret_refs.<name>` (the `sidecar:<name>` scheme). This model pins that
file's shape so a plugin author can validate a credentials template against the
published contract.

The one hard constraint the engine enforces is that the file is a flat top-level
object; it then reads each value under the referenced name. Values may be any
JSON type — a connector may declare a non-string secret input, and the engine
string-coerces every value on read — so the contract does not constrain the value
type. Prefer strings: coercion is lossy for structured (object/array) values, so
supply a structured credential JSON-encoded as a string for a lossless round-trip.
"""
from __future__ import annotations

from typing import Any

from pydantic import RootModel


class CredentialsFile(RootModel[dict[str, Any]]):
    """Flat `{ "<name>": <secret value> }` map for one connection.

    Each key is a connection-contract input (or post-auth output) name; each
    value is that input's secret material. A connection document's
    `secret_refs.<name>` entry, via the `sidecar:<name>` scheme, resolves to the
    value stored here under the same `<name>`. Keys are unconstrained; values may
    be any JSON type (the engine string-coerces on read) — prefer strings, and
    JSON-encode a structured credential as a string for a lossless round-trip.
    """

    root: dict[str, Any]
