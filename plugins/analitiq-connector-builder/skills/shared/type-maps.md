# Type-map resolution at runtime

How the engine resolves a column type through type maps at the two scopes.
This file documents **shipped engine behavior** — referenced from
`connector-spec-db/spec-type-maps.md` and `spec-resource-discovery.md`, which
own the *authoring* of connector-scoped maps. Connection-scoped maps are
authored by the sibling `analitiq-pipeline-builder` plugin at discovery time,
never by this plugin.

## The two scopes

| Scope | Files | Authored by |
|---|---|---|
| Connector | `{connector_id}/definition/type-map-read.json` / `type-map-write.json` | this plugin (`spec-type-maps.md`) |
| Connection | `connections/<connection-slug>/definition/type-map-read.json` / `type-map-write.json` | `analitiq-pipeline-builder`, for natives the connector maps don't cover on that deployment |

Both scopes share one rule shape and one published schema pair
(`https://schemas.analitiq.ai/type-map-read/latest.json` /
`.../type-map-write/latest.json`).

## Resolution order

For a `scope: "connection"` endpoint, the engine composes the connection map
as **primary** over the connector map as **fallback** — the two rule lists are
concatenated, connection rules first, into one first-match-wins list. This
holds in **both directions**: native → Arrow (read) and Arrow → native DDL
(write). With no connection map present, the connector map resolves alone.

A probe neither map matches is a **hard error** at runtime (an unmapped-type
failure that stops the stream). Deliberate: there is no wildcard fallback at
either scope, so a coverage gap stays visible instead of silently corrupting
types — the fix is a rule in the right map, never a catch-all.

Because composition is first-match over the concatenation, a connection rule
for a native/canonical the connector already covers **overrides** the
connector's rendering for every stream on that connection. Connection maps
are therefore authored gap-only.

## When each direction is consulted

- **Read maps matter at discovery time.** Discovery renders each discovered
  native through the read map to produce the `arrow_type` frozen into the
  endpoint document. At stream run time the frozen `arrow_type` is used
  directly — the read map is not consulted again.
- **The write map is consulted on every run.** Stream configuration renders
  every destination column's frozen `arrow_type` → native DDL through the
  write map each time (`CREATE TABLE IF NOT EXISTS` no-ops database-side for
  an existing table, but the rendering still runs), so a write-side gap fails
  a destination stream even when its table already exists.
- **Dialect overrides bypass the write maps.** Where a connector's dialect
  overrides `render_column_type` for a canonical family (see
  `spec-type-maps.md` §Database coverage), no **write** rule — connector or
  connection — is consulted for that family. Read-side rules are unaffected:
  `render_column_type` exists only on the Arrow → native DDL path.

## File-presence semantics

- **Absent file** — no map at that scope; resolution falls through (a
  connection without maps uses the connector maps alone).
- **Present but empty (`[]`)** — a load-time error, worse than absent. Never
  ship an empty rule array at either scope; the contract rejects it too.
- **The pre-split `type-map.json`** — the engine never reads this filename at
  either scope; a file by that name is silently inert. The validators reject
  it with a migration finding so it cannot linger unnoticed.
