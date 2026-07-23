# Changelog

## [0.1.6](https://github.com/analitiq-ai/claude-code-plugins/compare/analitiq-connector-builder-v0.1.5...analitiq-connector-builder-v0.1.6) (2026-07-23)


### Features

* **analitiq-connector-builder:** accept sync SQLAlchemy drivers in the transport contract ([d78b8b5](https://github.com/analitiq-ai/claude-code-plugins/commit/d78b8b55b07b6a4800f61bcf2ffd2947fc405d8e))


### Bug Fixes

* bump the runtime validator pin to 1.0.0rc14 and guard it in CI ([e22bff4](https://github.com/analitiq-ai/claude-code-plugins/commit/e22bff44c9a224f87425fe0ea75deeef1d4a48b8))

## [0.1.5](https://github.com/analitiq-ai/claude-code-plugins/compare/analitiq-connector-builder-v0.1.4...analitiq-connector-builder-v0.1.5) (2026-07-22)


### Features

* **analitiq-connector-builder:** add first-request-only limit to LinkPagination ([#52](https://github.com/analitiq-ai/claude-code-plugins/issues/52)) ([#63](https://github.com/analitiq-ai/claude-code-plugins/issues/63)) ([33499e3](https://github.com/analitiq-ai/claude-code-plugins/commit/33499e33c7e6bc2b6161e96f4bfd8360bc229b8d))
* **analitiq-pipeline-builder:** author connection-scoped type maps for discovered private-endpoint natives ([#62](https://github.com/analitiq-ai/claude-code-plugins/issues/62)) ([2ca9379](https://github.com/analitiq-ai/claude-code-plugins/commit/2ca9379e00da422076aacd7507a8cb17434c10a3))
* restructure as a multi-plugin monorepo owning the contract surface ([#51](https://github.com/analitiq-ai/claude-code-plugins/issues/51)) ([8c75001](https://github.com/analitiq-ai/claude-code-plugins/commit/8c750017a414eee2f2f423fe27a3befd0fb9d128))


### Bug Fixes

* **analitiq-connector-builder:** consolidate slug-pattern copies and pin them to SLUG_PATTERN ([#59](https://github.com/analitiq-ai/claude-code-plugins/issues/59)) ([8a116a3](https://github.com/analitiq-ai/claude-code-plugins/commit/8a116a33a73f3e4acbfc6db987d5344b973249ef))
* bump the runtime validator pin to 1.0.0rc13 ([#68](https://github.com/analitiq-ai/claude-code-plugins/issues/68)) ([eb88e23](https://github.com/analitiq-ai/claude-code-plugins/commit/eb88e23f7224879caf8ee34abc82397ee27dad13))

## [0.1.4](https://github.com/analitiq-ai/claude-plugin-connector/compare/v0.1.3...v0.1.4) (2026-07-02)


### Features

* author WriteOperation.idempotency blocks (api-endpoint 9.1.0) ([#27](https://github.com/analitiq-ai/claude-plugin-connector/issues/27)) ([ffd835b](https://github.com/analitiq-ai/claude-plugin-connector/commit/ffd835b540e09bcddc142ff5c7f0c638dfb47afe))

## [0.1.3](https://github.com/analitiq-ai/claude-plugin-connector/compare/v0.1.2...v0.1.3) (2026-06-30)


### Features

* enforce endpoint filename equals endpoint_id in validation ([#24](https://github.com/analitiq-ai/claude-plugin-connector/issues/24)) ([87061cb](https://github.com/analitiq-ai/claude-plugin-connector/commit/87061cbc373e8dc6e7740289b5b6f5ec279c69ec))

## [0.1.2](https://github.com/analitiq-ai/claude-plugin-connector/compare/v0.1.1...v0.1.2) (2026-06-30)


### Features

* enforce bare-marker arrow_type sibling-key rules in endpoint validation ([#19](https://github.com/analitiq-ai/claude-plugin-connector/issues/19)) ([74527ba](https://github.com/analitiq-ai/claude-plugin-connector/commit/74527ba9e4a28acc5f62a4fd4db15efdd58f9b46))
* package validator as installable analitiq-connector-validator for standalone CI ([#22](https://github.com/analitiq-ai/claude-plugin-connector/issues/22)) ([0e25b10](https://github.com/analitiq-ai/claude-plugin-connector/commit/0e25b10d4f0a5ef4397f3e27a29ea324cc2487a7))


### Bug Fixes

* position-aware response-extraction scopes; value_path as response path ([#18](https://github.com/analitiq-ai/claude-plugin-connector/issues/18)) ([f09395a](https://github.com/analitiq-ai/claude-plugin-connector/commit/f09395af2dc06ee2feafdcb500c983296c2d4d33))

## [0.1.1](https://github.com/analitiq-ai/claude-plugin-connector/compare/v0.1.0...v0.1.1) (2026-06-29)


### Features

* contract-derived research + endpoint fan-out (ProviderFacts from published schemas) ([4dbb381](https://github.com/analitiq-ai/claude-plugin-connector/commit/4dbb381e4470a5c9a516dc8d27e20b2ddbe0bbf6))
* implement contract-derived research + endpoint fan-out, fix drift surfaces ([04a9f6a](https://github.com/analitiq-ai/claude-plugin-connector/commit/04a9f6a7f28f9fc5214c29d40dfd2bf3ddf1d340))
* type-map rule — schemaless/container natives must map to a container canonical ([fe1018b](https://github.com/analitiq-ai/claude-plugin-connector/commit/fe1018be4666640e35f1b9d863ac915d21ba91d7))


### Bug Fixes

* address PR [#14](https://github.com/analitiq-ai/claude-plugin-connector/issues/14) review — validator error-handling, test coverage, prompt wiring ([71f0350](https://github.com/analitiq-ai/claude-plugin-connector/commit/71f0350e5e7d8adc06dc009a97bc50b62851f6c1))
* drop unconditional tz-aware API date-time row in spec-type-maps ([#16](https://github.com/analitiq-ai/claude-plugin-connector/issues/16)) ([00e4bed](https://github.com/analitiq-ai/claude-plugin-connector/commit/00e4bedee8b1c04f7959e6678e6f1761cbcdb420)), closes [#12](https://github.com/analitiq-ai/claude-plugin-connector/issues/12)

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
