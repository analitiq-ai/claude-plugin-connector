# Changelog

## [unreleased]

### Fixed
- `connector-spec-api/spec-replication.md` had drifted from the published
  api-endpoint contract: it documented `cursor_mappings` keys
  (`name`/`value`/`filter_param`/`filter_operator`) and a
  `supported_methods` value (`"full"`) plus a `default_method` key that the
  schema rejects, and it omitted the `WindowCursorMapping` variant
  entirely. Rewrote the page to match `#/$defs/Replication`,
  `#/$defs/SingleCursorMapping`, and `#/$defs/WindowCursorMapping`, and to
  defer to the schema as the source of truth instead of restating its shape
  as prose (issue #9).
- `connector-spec-api/spec-pagination.md` had drifted the same way (found
  by generalizing the new guard): `stop_when` was documented as a string
  (`"page_empty"`) where the contract requires a predicate object, and the
  `link` (`next_link`/`rel`) and `keyset` (`next_cursor`) shapes did not
  match `#/$defs/LinkPagination` / `#/$defs/KeysetPagination`. Rewrote all
  five strategies to match the contract.

### Added
- `tests/connector_validator/test_spec_doc_examples.py` — validates the
  JSON examples embedded in the API spec docs (`spec-replication.md`,
  `spec-pagination.md`) against the matching `$defs` of the live
  `api-endpoint` schema, so those docs can't silently drift from the
  contract again.
- `test_endpoint_example_passes_against_live_schema` — validates every
  `examples/*/endpoints/*.json` document against the live api-endpoint
  schema (Layer 1). These endpoint examples previously had no automated
  schema check.

## [0.1.0]

### Added
- Initial release of the standalone `analitiq-connector-builder` plugin,
  extracted from the `analitiq-ai/ai-plugins-official` monorepo into its
  own repository. Authors connector and endpoint JSON documents that
  conform to the published Analitiq schema contract at
  `schemas.analitiq.ai` (`kind: api` and `kind: database`; storage kinds
  `file`/`s3`/`stdout` are stubbed pending engine support).
- Agent chain: `connector-builder` (orchestrator skill) →
  `connector-provider-researcher` → `{api,db,storage}-connector-creator`
  → `endpoint-creator` (API, parallel) → `connector-schema-validator`
  (loop) → `connector-drift-classifier`.
- Orchestrator modes: `build` (default), `update` (re-author an existing
  connector from current docs and re-version it), and `validate`
  (read-only validation of an on-disk connector).
- `scripts/validate_connector.py` (Layer 1 JSON Schema + Layer 2 semantic
  validators) with the pytest suite under `tests/connector_validator/`.
