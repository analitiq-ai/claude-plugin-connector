#!/usr/bin/env python3
"""Resolve native / canonical type probes through type-map rule files.

This is the gap-detection half of connection-scoped type-map authoring
(`endpoint-spec/spec-type-map-gaps.md`). It holds no matching logic of its
own — resolution dispatches to the published `analitiq-validator` package
(the same first-match-wins, `${name}`-substituting, read-side-normalizing
semantics the engine and the validator use), so a probe resolves here exactly
as it will at runtime.

Maps are passed in precedence order (connection-scoped first, connector
second) and concatenated into one rule list — mirroring the engine's
`TypeMapper.compose`, where the connection map is primary and the connector
map is the fallback.

Usage::

    printf '%s' '["citext", "vector(3)"]' | python3 type_map_gaps.py \
        --direction read \
        --map connections/pg/definition/type-map-read.json \
        --map connectors/postgresql/definition/type-map-read.json

Probes are a JSON array of strings on stdin (or --probes-file): provider
`native_type` labels for ``--direction read``, Arrow canonical strings for
``--direction write``. Output on stdout::

    {"direction": "read",
     "resolved": {"citext": null, "vector(3)": null},
     "gaps": ["citext", "vector(3)"]}

``resolved`` maps each probe (verbatim) to its rendered value — the Arrow
canonical (read) or the native DDL (write) — or ``null`` when no rule in any
map matches; ``gaps`` lists the null probes. Exit status is ``0`` on a clean
run regardless of gaps (a gap is a result, not an error), ``2`` on a CLI /
input error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _analitiq import ensure_deps_or_reexec


def _fail(message: str) -> "int":
    print(f"type_map_gaps: {message}", file=sys.stderr)
    return 2


def _load_rules(path: Path) -> list:
    """Read one rule-list file. Content validity is the validator's job
    (entities `type_map_read` / `type_map_write`); here only the array shape is
    required so resolution has rules to walk."""
    doc = json.loads(path.read_text())
    if not isinstance(doc, list):
        raise ValueError(f"{path} is not a JSON array of rules")
    return doc


def resolve(direction: str, probes: list[str], rule_files: list[Path]) -> dict:
    """Resolve every probe through the concatenated rule lists, primary first."""
    # Published resolution helpers — the exact semantics every runtime reader
    # uses. `_render_canonical` bundles the read-side native normalization;
    # write matchers compare the canonical as authored (case-preserving).
    from analitiq.validator import _render_canonical
    from analitiq.validator.connectors import _first_match_render

    rules: list = []
    for path in rule_files:
        rules.extend(_load_rules(path))

    if direction == "read":
        resolved = {p: _render_canonical(p, rules) for p in probes}
    else:
        resolved = {p: _first_match_render(p, rules, "canonical", "native") for p in probes}
    return {
        "direction": direction,
        "resolved": resolved,
        "gaps": [p for p in probes if resolved[p] is None],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--direction", required=True, choices=("read", "write"),
                        help="read: probes are native types, maps are type-map-read files; "
                             "write: probes are Arrow canonicals, maps are type-map-write files.")
    parser.add_argument("--map", action="append", required=True, dest="maps", metavar="PATH",
                        help="Rule-list file; repeatable, in precedence order "
                             "(connection-scoped map first, connector map after).")
    parser.add_argument("--probes-file", metavar="PATH",
                        help="JSON array of probe strings; defaults to stdin.")
    args = parser.parse_args(argv)

    try:
        ensure_deps_or_reexec(__file__)
    except RuntimeError as exc:
        return _fail(str(exc))

    try:
        raw = Path(args.probes_file).read_text() if args.probes_file else sys.stdin.read()
        probes = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _fail(f"cannot read probes: {exc}")
    if not isinstance(probes, list) or not all(isinstance(p, str) for p in probes):
        return _fail("probes must be a JSON array of strings")

    try:
        result = resolve(args.direction, probes, [Path(m) for m in args.maps])
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        return _fail(str(exc))

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
