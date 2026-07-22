#!/usr/bin/env python3
"""Resolve native / canonical type probes through type-map rule files.

This is the gap-detection half of connection-scoped type-map authoring
(`endpoint-spec/spec-type-map-gaps.md`). It holds no matching logic of its
own — resolution dispatches to the pinned `analitiq-validator`'s resolution
internals (the same first-match-wins, `${name}`-substituting,
read-side-normalizing semantics the engine and the validator use), so a probe
resolves here exactly as it will at runtime. Those helpers are private API,
not a published surface — this repo's tests exercise them against the in-repo
source, which moves in lockstep with the pin, so a pin bump that renames them
fails here first.

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


def _load_rules(path: Path, direction: str) -> list:
    """Read one rule-list file and model-validate it against the pinned
    contract. Validation here is load-bearing, not a courtesy: the resolver
    mirrors runtime semantics, which *skip* a malformed rule — so a broken
    rule would surface as a false "gap", indistinguishable from a genuinely
    uncovered probe, and a false gap makes the authoring agent shadow the very
    rule the map intended. Failing loud keeps a reported gap unambiguous."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(doc, list):
        raise ValueError(f"{path} is not a JSON array of rules")
    from pydantic import ValidationError
    from analitiq.contracts.type_map import TypeMapReadDoc, TypeMapWriteDoc
    model = TypeMapReadDoc if direction == "read" else TypeMapWriteDoc
    try:
        model.model_validate(doc)
    except ValidationError as exc:
        raise ValueError(
            f"{path} is not a valid {direction} type map — fix it (or, for a "
            f"connector map, raise the defect upstream) before probing: {exc}") from exc
    return doc


def resolve(direction: str, probes: list[str], rule_files: list[Path]) -> dict:
    """Resolve every probe through the concatenated rule lists, primary first."""
    # The pinned validator's internal resolution helpers (private API — see the
    # module docstring). `_render_canonical` bundles the read-side native
    # normalization; write matchers compare the canonical as authored
    # (case-preserving).
    from analitiq.validator import _render_canonical
    from analitiq.validator.connectors import _first_match_render

    rules: list = []
    for path in rule_files:
        rules.extend(_load_rules(path, direction))

    probes = list(dict.fromkeys(probes))  # dedupe, order-preserving — one verdict per probe
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

    # Read and write rules share the {match, native, canonical} key set, so a
    # wrong-direction map would not error — it would resolve, plausibly and
    # wrongly. The two load-bearing filenames declare their direction; hold a
    # map named either of them to it.
    load_bearing = {"type-map-read.json": "read", "type-map-write.json": "write"}
    for m in args.maps:
        implied = load_bearing.get(Path(m).name)
        if implied is not None and implied != args.direction:
            return _fail(f"{m} is a {implied}-direction map (by filename) but "
                         f"--direction is {args.direction}")

    try:
        raw = Path(args.probes_file).read_text() if args.probes_file else sys.stdin.read()
        probes = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _fail(f"cannot read probes: {exc}")
    if not isinstance(probes, list) or not all(isinstance(p, str) for p in probes):
        return _fail("probes must be a JSON array of strings")

    try:
        result = resolve(args.direction, probes, [Path(m) for m in args.maps])
    except (OSError, ValueError) as exc:
        # _load_rules wraps every per-file failure (read, parse, model) into a
        # file-naming ValueError; OSError is the escape hatch for anything else.
        return _fail(str(exc))

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
