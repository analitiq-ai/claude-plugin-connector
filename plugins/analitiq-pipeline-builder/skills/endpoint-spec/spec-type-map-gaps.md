# Connection-scoped type maps (gap authoring)

A connector ships a *documented base vocabulary* in its
`definition/type-map-read.json` / `type-map-write.json`, not every type a live
deployment can surface — extension types (`citext`, `ltree`, `hstore`,
`vector(N)`, PostGIS geometries), custom domains/enums, parameterized variants.
The engine composes a **connection-scoped** map as primary over the connector
map as fallback, in both directions, and hard-errors on a type neither covers.
Discovery is when the gap is visible and fixable: this file governs authoring
the connection-scoped maps that close it.

## Files

| Direction | File | Validates as entity |
|---|---|---|
| native → Arrow | `connections/<connection-slug>/definition/type-map-read.json` | `type_map_read` |
| Arrow → native DDL | `connections/<connection-slug>/definition/type-map-write.json` | `type_map_write` |

Published schemas: `https://schemas.analitiq.ai/type-map-read/latest.json` /
`.../type-map-write/latest.json`. The rule shape (exact/regex `match`, matcher
vs rendered key per direction, `${name}` captures) is identical to the
connector's own maps — the connector files you resolve against during gap
detection are the live reference for it; do not restate their vocabulary here.

Filenames are load-bearing: the engine loads exactly these two names from the
connection's `definition/`. The pre-split `type-map.json` is dead — the engine
never reads it, and the validators reject it with a migration finding. The
published `type-map-write-coverage` warning does not apply here — it presumes
a connector's full-vocabulary write map, which a gap-only connection map
deliberately is not — so the validator adapter filters it; never "fix" a
coverage warning by adding connection rules.

## Gap detection

Resolution semantics (normalization, first-match-wins, `${name}` substitution)
live in the published packages — never eyeball a regex. Probe with the helper,
maps in precedence order (connection first, when one exists, then connector):

```bash
printf '%s' '["citext", "vector(3)"]' | python3 "${CLAUDE_PLUGIN_ROOT}/scripts/type_map_gaps.py" \
  --direction read \
  --map connections/<slug>/definition/type-map-read.json \
  --map connectors/<connector-slug>/definition/type-map-read.json
```

- **Read probes** — the distinct `native_type` strings introspected across the
  selected tables, before deriving any `arrow_type`.
- **Write probes** — the distinct `arrow_type` strings frozen into the endpoint
  documents, after read-side resolution and judgment are complete.

`resolved` gives the rendered value per covered probe; `gaps` lists the
uncovered ones. Pass only map files that exist.

## Authoring rules

- **Gap-only.** Author a rule only for a probe in `gaps`. A connection rule
  for anything the connector already covers *overrides* the connector for
  every stream on this connection — never shadow.
- **No gaps → no file.** A present-but-empty `[]` is an engine load-time error
  (worse than absent) and the contract rejects it. Write nothing.
- **Extend, never rewrite.** When the connection already ships a map, append
  new rules **after** the existing ones; never remove, reorder, or edit
  existing rules — they are prior authored behavior on this connection.
- **Read rules.** Choose the canonical for an uncovered native with
  `spec-columns.md` judgment. The `arrow_type` frozen into the endpoint
  document and the canonical the new rule renders **must agree** — the rule is
  the durable record of the same judgment. Generalize a parameterized native
  family with one regex rule and `${name}` captures (`vector(3)` observed →
  match the family, not the instance); literals inside a regex must be
  uppercase (read-side probes are normalized to uppercase; the pattern is not).
- **Write rules.** For an uncovered canonical, render the discovered native
  that produced it — the deployment's own spelling is the one type the
  deployment certainly accepts as DDL. When **several distinct** discovered
  natives share one uncovered canonical, do not pick: report the ambiguity
  (see the mode contract in `private-endpoint-creator`) so the orchestrator
  asks the user which native this connection renders.
- **Dialect-override caution.** A connector dialect may render a canonical
  family in code (a `render_column_type` override — e.g. precision-range
  arithmetic no rule can express). No map rule, connector or connection, is
  consulted for such a family, so a connection write rule for it is dead
  weight. If the connector's package files show an override covering the gap
  family, record the gap in `type_maps.notes` instead of authoring a rule.

## What a clean result does not prove

Write coverage is probed against the types *this discovery observed*. A stream
can still hand this destination a canonical no discovered column carried; that
resolves through the connector's write map, and a miss there is a connector
coverage defect to raise upstream, not something to pre-empt with speculative
connection rules.
