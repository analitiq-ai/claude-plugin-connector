# Metadata and versioning

The authored top-level shape of a connector document, and how its `version`
moves between releases. Field shapes are owned by the contract models; the
release table is the plugin's own policy.

## Authored top-level fields

| Field | Required | Notes |
|---|---|---|
| `$schema` | Yes (for standalone files) | `https://schemas.analitiq.ai/connector/latest.json`. Always author this canonical URL. The connector contract accepts the published URL on any environment host (`schemas.analitiq.<tld>`), so a document authored against `.ai` still validates where the engine serves a per-environment schema. The validator matches on this URL offline; it does not fetch it. |
| `kind` | Yes | The connector family — a schema-owned `kind` enum (validator-enforced); see CLAUDE.md for the current set. |
| `connector_id` | Yes | Stable connector slug matching `^[a-z0-9][a-z0-9_-]*$` (lowercase). Names the on-disk `{connector_id}/` directory so the identifier and directory stay in sync. The connector contract **requires** `connector_id` in every authored definition — the "service-assigns-when-omitted" rule is `connection_id`'s on *connection* documents, not `connector_id`'s. |
| `display_name` | No | User-facing label. |
| `description` | No | Human-readable summary. |
| `tags` | No | Search/grouping labels. |
| `documentation_url` | No | Provider docs URL. |
| `version` | Yes | Semantic version string. Start at `1.0.0` for first release. |
| `default_transport` | Yes | Name of an entry in `transports`. |
| `transports` | Yes | Map of named transport contracts. |
| `transport_defaults` | No | Defaults merged into named transports. |
| `auth` | Yes | Auth workflow definition. |
| `connection_contract` | Yes | Connection-contract shape. |
| `resource_discovery` | No | Resource discovery declarations. |

Note: the connector's type maps are **not** top-level fields. They ship
as separate sibling artifacts — `{connector_id}/definition/type-map-read.json`
(native → Arrow, all kinds) and `{connector_id}/definition/type-map-write.json`
(Arrow → native, database only) — validating against
`https://schemas.analitiq.ai/type-map-read/latest.json` and
`https://schemas.analitiq.ai/type-map-write/latest.json` respectively. See
`connector-spec-db/spec-type-maps.md` for authoring.

## Authoring `connector_id`

The plugin authors `connector_id` on every connector document. The same
value names the on-disk directory (`{connector_id}/`), so the contract
path `connectors/{connector_id}/definition/connector.json` and the
plugin's output path align without a rewrite layer.

The connector contract requires `connector_id` to match the slug pattern
in the field table above; this plugin authors that slug directly, so
directory names stay portable and the identifier is stable.

## Registry-stamped fields

The following fields are stamped by the registry on insert/update and
must not appear in authored documents:

- `created_at`
- `updated_at`

The published schema reflects this — the authoring shape does not list
them in `properties` or `required`, so the contract models reject them
(reported under `contract-model`).

Reserving a field name at the **document** level does not reserve it inside a
provider-owned namespace. A provider response legitimately containing a
`created_at` field is fine: `response.schema` describes the provider's data, not
the Analitiq document envelope. Only the document's own top level is reserved.

## Release version (`version`)

Authored top-level `version` is a semver string. It bumps according to
the connector release table:

| Bump | Meaning | Examples |
|---|---|---|
| Patch | No connection drift. | Bug fixes, doc fixes, transport implementation tuning, type-map rule reordered (when the reorder does not change first-match resolution for any existing input). |
| Minor | Additive, non-drifting. | Optional input added, optional discovery output added, optional endpoint added, type-map rule added. |
| Major | Possible connection drift. | Input removed, renamed, type-changed, enum narrowed, storage moved, non-optional input added, auth-shape change, discovery-shape change, type-map rule removed, render side changed for an existing matcher (read map: `canonical` changed for an existing `native`; write map: `native` changed for an existing `canonical`). |

Type-map drift categories apply per file: `type-map-read.json` and
`type-map-write.json` are diffed independently, and a change in either
drives the bump per the table above.

The drift-classifier sub-agent computes this bump from a diff between
the previous release and the new draft.

## First release

If no `previous_release_path` is supplied, set `version: "1.0.0"`.

## Schema URL declaration

Authored connector files declare:

```json
{ "$schema": "https://schemas.analitiq.ai/connector/latest.json" }
```

Always author the canonical `.ai` URL. The validator matches on it offline; it
does not fetch it.

The three document families differ, so don't generalize from one to another:

| Document | `$schema` | Enforced? |
|---|---|---|
| Connector | Author it; matched by pattern, tolerating any environment host (`schemas.analitiq.<tld>`). | Partly. The *pattern* is enforced when present, but the field is optional — a connector omitting `$schema` entirely validates clean. Always writing it is our convention, not a contract rule. |
| API endpoint | Locked to the `.ai` URL by a `const`. | Yes — required, and a different host is rejected. |
| Type maps | None — both maps are bare JSON arrays with no envelope. | N/A; direction comes from the filename. |
