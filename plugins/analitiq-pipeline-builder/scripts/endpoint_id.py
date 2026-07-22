#!/usr/bin/env python3
"""Compute the derived identity of an introspected database object.

`private-endpoint-creator` calls this so the authored `endpoint_id` is exactly
the handle the validator's endpoint-id gate enforces — reusing the published
`analitiq.contracts.endpoint_identity` helpers rather than reimplementing the
slug+hash. Pass the identifiers **verbatim** from introspection (no case-folding
or pre-slugging — the hash is computed over the raw values). Prints one JSON
object:

    {"endpoint_id": "<derived handle>",
     "database_object": {"name": ..., "schema"?: ..., "catalog"?: ..., "object_type": ...}}

The handle's exact composition is the published helper's business, not this
adapter's — restating it here is how the two drift.

Usage:
    python3 scripts/endpoint_id.py --schema public --name orders [--catalog db] [--object-type view]
"""
from __future__ import annotations

import argparse
import json
import sys

from _analitiq import ensure_deps_or_reexec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="Provider-native object name (verbatim).")
    parser.add_argument("--schema", default=None,
                        help="Schema/namespace (verbatim); omit for schemaless stores.")
    parser.add_argument("--catalog", default=None, help="Catalog/database (verbatim); optional.")
    parser.add_argument("--object-type", default="table",
                        help="Descriptive object type (table, view, materialized_view, collection, …).")
    args = parser.parse_args(argv)

    try:
        ensure_deps_or_reexec(__file__)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    from analitiq.contracts.endpoint_identity import build_database_object, derive_db_endpoint_id
    result = {
        "endpoint_id": derive_db_endpoint_id(args.catalog, args.schema, args.name),
        "database_object": build_database_object(
            args.catalog, args.schema, args.name, object_type=args.object_type),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
