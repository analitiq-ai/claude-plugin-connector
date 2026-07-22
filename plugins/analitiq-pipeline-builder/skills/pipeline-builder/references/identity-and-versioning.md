# Identity and versioning

Pipelines, streams, and connections each carry an RFC-4122 UUID **identity
field** (`pipeline_id`, `stream_id`, `connection_id`) that the plugin authors
directly. Connectors and database endpoints use **slug identifiers**
(`connector_id`, `endpoint_id`). Directory names on disk stay human-readable
slugs and are independent of the UUID identity stored inside the documents.

## Identifier shapes

<!-- BEGIN GENERATED: shared-vocabulary -->
| Concern | Published constant | Pattern |
|---|---|---|
| Slug (ids + directory names) | `analitiq.contracts.shared.common.SLUG_PATTERN` | `^[a-z0-9][a-z0-9_-]*$` |
| UUID (`*_id` identity fields) | `analitiq.contracts.shared.types.UUID_PATTERN` | `^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$` |
| Cron expression | `analitiq.contracts.shared.common.CRON_PATTERN` | `^cron\(.+\)$` |
| No edge whitespace (`display_name`, tags) | `analitiq.contracts.shared.common.NO_EDGE_WHITESPACE_PATTERN` | `^\S(?:[\s\S]*\S)?$` |

| Bound | Value |
|---|---|
| `display_name` length | `1..120` |
| `description` max length | `2000` |
| `tags` max count | `50` |
| tag length | `1..64` |
<!-- END GENERATED: shared-vocabulary -->

**Directory-slug convention.** The contract's `SLUG_PATTERN` governs document
fields only; it does not constrain on-disk names. This plugin reuses the same
shape for every directory and file slug (see §"Directory layout vs. document
identity") **by convention**, so a directory name is always a legal identifier.
The convention is pinned by reference: if the published pattern changes, the
directory-slug rule follows it. Prose elsewhere cites this convention instead
of restating the pattern.

All three identity fields are **optional** in the contract — omit one and the
service assigns it on ingest. The plugin authors them anyway so that sibling
documents written in the same run can cross-reference each other; the
orchestrator keeps the minted UUIDs in memory for exactly that reason.

`connector_id` and `endpoint_id` are **immutable**. Renaming one is not an
edit — it creates a different entity. In edit mode, never rewrite an existing
identifier in place; author a new artifact and let the user retire the old one.

Do not read meaning out of a UUID. It is an opaque handle: no embedded version,
no embedded tenant, nothing to parse. In particular the pipeline's
server-managed integer `version` is a separate field and must never be encoded
into `pipeline_id`.

## Cross-document references — contract vs. plugin policy

Keep these two apart; the prose used to conflate them.

**What the contract says.** Every cross-document reference field is just a
non-empty string. The schema constrains nothing further, and engines resolve the
reference at runtime. The published field descriptions go further and say that
`pipeline.connections.source`, `pipeline.connections.destinations[]` and
`pipeline.streams[]` are *typically versioned* ids of the form `<uuid>_v<n>`
(the published examples show `…_v1`), while `stream.pipeline_id` is *typically
the base UUID*. Both forms validate.

**What this plugin does.** The plugin authors **bare UUIDs** in every reference —
it does not append a `_v<n>` suffix anywhere. That is a deliberate plugin
convention, not a contract requirement: the plugin never calls the registry, so
it has no version to pin to, and a bare id lets the engine resolve the current
version. Do not "fix" an authored bare reference into a versioned one, and do not
strip a `_v<n>` suffix from a reference the user supplied.

| Reference field | The plugin sets it to |
|---|---|
| `pipeline.connections.source` | the source `connection.connection_id` UUID |
| `pipeline.connections.destinations[]` | each destination `connection.connection_id` UUID |
| `pipeline.streams[]` | each child `stream.stream_id` UUID |
| `stream.pipeline_id` | the parent `pipeline.pipeline_id` UUID |
| `stream.source.endpoint_ref.connection_id` | the source `connection.connection_id` UUID |
| `stream.destinations[].endpoint_ref.connection_id` | each destination `connection.connection_id` UUID |

The bundle referential checks (run with `--bundle-root`) compare these values
against the identities inside the sibling documents, so a mismatch is caught
locally.

Version-pinned reference strings that only exist in a specific deployment are
outside the public contract — never author one.

## Metadata fields

`display_name` is a user-facing **label, not an identity key**: it is
case-preserving, may change freely without changing identity, and nothing should
key off it. `description` is a plain summary and may be an empty string. `tags`
are opaque, case-preserving grouping labels the contract assigns no meaning to.

Top-level artifact metadata uses `display_name` — never `name` or `title`, which
no authored model declares.

These rules govern **artifact metadata only**. They say nothing about
provider-owned names: a database column, an operation parameter, or a
connection-contract input key keeps whatever spelling the provider uses.

## Directory layout vs. document identity

Directories use human-readable slugs:

```
pipelines/<pipeline-slug>/pipeline.json
pipelines/<pipeline-slug>/streams/<stream-slug>.json
connections/<connection-slug>/connection.json
connections/<connection-slug>/definition/endpoints/<endpoint-slug>.json
```

The slug is **only** for file organization. Cross-document refs inside the JSON
use the identities above, never the directory slugs. The bundle checks find
stream files by walking `pipelines/<slug>/streams/` and then compare the values
inside the documents.

## "Lifecycle" means three unrelated things

Never conflate them:

1. **Connector template phases** — `pre_auth` / `auth` / `post_auth` / `active`,
   describing when a connector's contract fields resolve. Connector-owned; this
   plugin only reads them.
2. **Authored artifact `status`** — the lifecycle of a pipeline or stream
   document. See `pipeline-spec`.
3. **Per-run operation lifecycle** — the state of one execution. Runtime-owned
   and absent from every authored document.

## Server-managed `version` field

Pipelines and streams have a server-managed integer `version` field. **The plugin
does not author it.** The registry sets it on insert and increments it on certain
updates per the published lifecycle contract.

This differs from connectors, which use semver and a drift classifier to bump the
field. Pipelines and streams use a counter, and the registry owns it.
