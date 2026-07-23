# Changelog

## [0.1.2](https://github.com/analitiq-ai/claude-code-plugins/compare/analitiq-pipeline-builder-v0.1.1...analitiq-pipeline-builder-v0.1.2) (2026-07-23)


### Bug Fixes

* bump the runtime validator pin to 1.0.0rc14 and guard it in CI ([e22bff4](https://github.com/analitiq-ai/claude-code-plugins/commit/e22bff44c9a224f87425fe0ea75deeef1d4a48b8))

## [0.1.1](https://github.com/analitiq-ai/claude-code-plugins/compare/analitiq-pipeline-builder-v0.1.0...analitiq-pipeline-builder-v0.1.1) (2026-07-22)


### Features

* **analitiq-pipeline-builder:** add new-destination-table flow (author-new-table sub-mode) ([#64](https://github.com/analitiq-ai/claude-code-plugins/issues/64)) ([cd449ae](https://github.com/analitiq-ai/claude-code-plugins/commit/cd449aea4f0dcc2b173288c0711e55e3dce6931f))
* **analitiq-pipeline-builder:** author connection-scoped type maps for discovered private-endpoint natives ([#62](https://github.com/analitiq-ai/claude-code-plugins/issues/62)) ([2ca9379](https://github.com/analitiq-ai/claude-code-plugins/commit/2ca9379e00da422076aacd7507a8cb17434c10a3))
* restructure as a multi-plugin monorepo owning the contract surface ([#51](https://github.com/analitiq-ai/claude-code-plugins/issues/51)) ([8c75001](https://github.com/analitiq-ai/claude-code-plugins/commit/8c750017a414eee2f2f423fe27a3befd0fb9d128))


### Bug Fixes

* **analitiq-pipeline-builder:** state the directory-slug convention once, gate hand-typed patterns ([#57](https://github.com/analitiq-ai/claude-code-plugins/issues/57)) ([937e8f7](https://github.com/analitiq-ai/claude-code-plugins/commit/937e8f7b8a0fbc7304deb5fd9f25774f8aceea6f))
* bump the runtime validator pin to 1.0.0rc13 ([#68](https://github.com/analitiq-ai/claude-code-plugins/issues/68)) ([eb88e23](https://github.com/analitiq-ai/claude-code-plugins/commit/eb88e23f7224879caf8ee34abc82397ee27dad13))
* resolve the DeepSource python baseline ([#56](https://github.com/analitiq-ai/claude-code-plugins/issues/56)) ([359e7fb](https://github.com/analitiq-ai/claude-code-plugins/commit/359e7fbb0e6059549f4e03d25472ed93fd52221d))

## [unreleased]

### Changed
- Bumped the consumed contract to `analitiq-validator==1.0.0rc6`
  (`analitiq-contract-models==1.0.0rc6` transitively). Draft pipeline bundles now
  validate with `require_runnable=False`, so a not-yet-runnable draft produces no
  finding (runnability is enforced only once the pipeline is `active`); documented
  the `sidecar:` `secret_refs` scheme. rc5 added the `endpoint-filename` gate for
  stem-addressed database endpoints in `validate_document`; rc6 exports
  `endpoint_filename_findings` publicly, which the bundle path now reuses (below).
- Replaced the bundled `scripts/validate_pipeline.py` with a thin adapter
  (`src/scripts/validate.py`) over the published, offline `analitiq-validator` +
  `analitiq-contract-models` packages; it self-installs the pinned version into a
  managed virtualenv on first use. Added `src/scripts/endpoint_id.py` for the
  derived database-endpoint identity.
- Aligned all authoring to the current published contracts: connection
  `parameters`/`selections`/`secret_refs` (secrets as `env:` pointers, no
  `values` envelope); stream discriminated `endpoint_ref` carrying
  `database_object`, flat `conflict_keys`, and the `get`/`pipe`/`fn` expression
  grammar; database-endpoint derived `endpoint_id`.
- Moved the plugin package (`.claude-plugin/`, `agents/`, `skills/`, `scripts/`)
  under `src/`, separating it from repo-management files.

### Added
- Bundle validation now flags an `endpoint-filename` finding when a connection-scoped
  private endpoint file is not named `<endpoint_id>.json` — the engine locates
  endpoints by filename stem, so a mis-named file (correct id inside, wrong name)
  passes referential checks but fails at runtime. It calls the published
  `endpoint_filename_findings` helper (the bundle validator runs on a filename-less
  dict and can't reach the gate itself). Edit mode also validates a changed
  pipeline/stream's referenced closure (its connections and their private
  endpoints), so a stale or mis-named referenced artifact surfaces at edit time.
- Edit mode in the `pipeline-builder` orchestrator — surgical, in-place changes
  to an existing pipeline / stream / connection / database-endpoint.

## [0.1.0]

### Added
- Initial release of the standalone `analitiq-pipeline-builder` plugin,
  extracted from the `analitiq-ai/ai-plugins-official` monorepo into its
  own repository. Authors pipeline, stream, connection, and
  database-endpoint JSON documents that conform to the published Analitiq
  schema contract at `schemas.analitiq.ai`. Downloads connectors from the
  DIP registry and wires them into complete pipelines; it does not create
  connectors and calls no registration APIs.
- Agent chain: `pipeline-builder` (orchestrator skill) →
  `pipeline-provider-researcher` → `registry-browser` →
  `connection-creator` → `private-endpoint-creator` (DB only) →
  `pipeline-creator` → `stream-creator` (parallel) →
  `pipeline-schema-validator` → `pipeline-drift-classifier`.
- `scripts/validate_pipeline.py` (Layer 1 JSON Schema + Layer 2 semantic
  validators) with the pytest suite under `tests/pipeline_validator/`.
