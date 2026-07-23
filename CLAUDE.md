# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## What This Repo Is

A **monorepo holding one contract surface**: the Claude Code plugins end users
run to author Analitiq artifacts, and (as they land) the Python packages and
JSON Schemas that define what those artifacts must look like.

These are not independent projects that happen to share a directory. The plugin
prose, the Pydantic contract models, the validator, and the published JSON
Schemas are four expressions of one set of rules. They live together so a rule
changes in one place — every boundary between them would be a surface a human
has to keep in sync by hand. See `.claude/rules/no-drift-surfaces.md`.

This repo is also a **Claude Code plugin marketplace**. `.claude-plugin/marketplace.json`
declares the marketplace `analitiq-claude-code-plugins`; each entry's `source`
is a relative path into `plugins/`.

## Layout

```
.claude-plugin/marketplace.json   # marketplace catalog; one entry per plugin
plugins/
  analitiq-connector-builder/     # authors connectors + API endpoints
  analitiq-pipeline-builder/      # authors pipelines, streams, connections
packages/
  contract-models/                # -> analitiq-contract-models (PyPI); the contract
  validator/                      # -> analitiq-validator (PyPI)
schemas/                          # RENDERED public JSON Schemas -> schemas.analitiq.ai
scripts/
  render_schemas.py               # renders schemas/ from packages/contract-models
tests/
  connector_builder/              # suite per plugin; package suites live in packages/*/tests
  pipeline_builder/
conftest.py                       # puts packages/*/src on sys.path - see "The contract"
requirements-dev.txt              # runtime deps of the packages + pytest
```

**`schemas/` is generated, not authored.** It is rendered from
`packages/contract-models` by `scripts/render_schemas.py`; `render_schemas.py check`
re-renders and fails on any diff, and CI runs it. Never hand-edit a file under
`schemas/`. `canonical-types.json` is versionless and outside the registry but
still generated: `render_schemas.py canonical-types` builds it from the
vendored engine grammar (see "the type vocabulary" below), and `check` covers
it. Two files are exceptions, hand-authored and outside the registry:
`data-sync-api/openapi.json`, which has no version triple, and
`data-sync-run-response/1.0.0.json`, which is versioned but hand-maintained —
the publish treats every pinned `X.Y.Z.json` as immutable, so changing it means
renaming to a new triple, never editing in place. They are served only because
the publish workflow globs `**/*.json`, and `render_schemas.py check` never
inspects them.

**The canonical Arrow type vocabulary is engine-owned.** The set of type
families the platform executes is a capability surface (issue #81): analitiq-core
publishes it as versioned artifacts (`arrow-type-grammar`, `conversion-matrix`
at `schemas.analitiq.ai`), and this repo vendors one pinned grammar version at
`packages/contract-models/src/analitiq/contracts/arrow_type_grammar.json`.
`ARROW_TYPE_PATTERN`, the canonical-types `$defs`, and the container-head set
are all derived from it (`analitiq.contracts.arrow_grammar` states the pin —
version + sha256 — once). Guards: `test_arrow_grammar.py` re-hashes the
vendored file offline; the `engine-grammar-pin-guard` CI job byte-compares it
against the published immutable object and cross-checks the conversion-matrix
family keys. A family is added by shipping it in the engine first, then
bumping the pin here (re-vendor, `render_schemas.py canonical-types`, re-render
the affected resources, re-run the plugin doc generator) — never by hand-editing
the vocabulary.

`.github/workflows/schemas-publish.yml` uploads the tree to the serving bucket
(defined in the infra repo's Terraform) on pushes to main touching `schemas/`.
The publish is additive — pinned `X.Y.Z.json` objects are first-write-wins
(byte-compared on re-runs; divergence fails the publish) and never overwritten,
nothing is ever deleted — and mutable pointers (`latest.json`, `index.json`,
the two versionless hand-authored files) rely on a 5-minute TTL, not CloudFront
invalidation. Auth is OIDC via the `schemas` environment — see "Credentials".

Only the 13 public resources render here. The ~40 internal-audience schemas stay
in the infra repo with the private half of the renderer;
`Resource.__post_init__` fails the build if a registered model tree reaches
outside `analitiq.contracts`.

**`plugins/<name>/` is a distribution artifact.** Its contents are copied
verbatim into every user's plugin cache when they install. Tests, scratch
output, and CI config do not belong inside it — that is why `tests/` sits at
the repo root, namespaced per plugin, rather than under each plugin.

Each plugin carries its own `CLAUDE.md` with its agents, skills, and authoring
rules. Read `plugins/<name>/CLAUDE.md` when working inside that plugin; this
file covers only what spans both.

## The contract, and the runtime pin

This repo is the **source** of `analitiq-contract-models` and
`analitiq-validator` (`packages/*/src`). The version of record is each package's
`pyproject.toml`; the two move in lockstep, enforced by
`packages/validator/tests/test_contract_models_pin.py`.

**Never `pip install` those two packages into a dev environment.** A built wheel
ships a generated `analitiq/contracts/__init__.py`, making it a *regular*
package, while the in-repo tree deliberately has none, making it a *namespace
portion* — and a regular package wins regardless of `sys.path`. An installed copy
therefore silently shadows the source and the suite grades the wrong code. The
repo-root `conftest.py` puts both source trees on the path;
`requirements-dev.txt` carries only their runtime deps.
`test_suite_exercises_in_repo_source_not_an_installed_wheel` guards this.

Separately, the plugins **self-install a published release at runtime** — end
users have no checkout, so this must be a real PyPI version:

The pin is currently **`analitiq-validator==1.0.0rc14`** (which resolves
`analitiq-contract-models==1.0.0rc14`). Three places state it, each pinned by a
test so none can rot silently:

- `VALIDATOR_PIN` in `plugins/analitiq-pipeline-builder/scripts/_analitiq.py`
- the self-install line in
  `plugins/analitiq-connector-builder/agents/connector-schema-validator.md`
  (three occurrences: the comment, the version probe, and the install command)
- this section

`PINNED_VERSION` in `tests/connector_builder/_pins.py` is a different value: it
tracks what this repo *ships* (`packages/contract-models/pyproject.toml`), so it
moves with the pyproject bump, ahead of the pin, and only coincides with the pin
outside a release window. The drift suite states no version — it imports `_pins`.

`test_validator_pin_matches_the_package_this_repo_ships` requires the pin to be
**at or behind** `packages/validator/pyproject.toml`. Not equal: release-please
bumps that file inside the Release PR, and the pin cannot follow in the same
commit because it names a version that must already be on PyPI — which only
happens after that PR merges and publishes. Requiring equality would leave the
Release PR permanently red. The test catches the dangerous direction instead: a
pin *ahead* of what this repo ships points at something users cannot install.
Bump the pin in a follow-up once the release is out.

`scripts/check_validator_pin_contract.py` (CI job `pinned-validator-guard`)
guards the pin from the other side: it installs the pinned release into an
isolated venv and fails if that **published** wheel rejects the canonical
`dialect+driver` values the plugin prose teaches. Marketplace installs track
main HEAD (the plugin sources are unpinned relative paths), so the guard is
strict on pushes to main — a release window (pin ≠ shipped) shows as a red
main until the pin catch-up lands — and on `release-please--*` branches;
ordinary PRs inside a window only warn so a contract-widening PR can still
land. The script's docstring owns the full semantics, including the
live-settings caveat that no branch protection currently requires the check.

Running a plugin helper from a checkout would otherwise trigger the bootstrap
(build a venv, install the published wheel, `os.execv` into it). The root
conftest sets `ANALITIQ_VALIDATOR_FROM_SOURCE=1` to short-circuit that; without
it, the bootstrap would replace the pytest process mid-run.

## Single source of truth (drift policy)

The published schema is the single source of truth. **Never restate what it
defines — reference or load it.** Carry only craft the schema can't express
(judgment, idioms, gotchas, workflow). That splits everything into **contract**
(don't duplicate — field shapes, enums, vocabularies, `$schema` URLs) and
**craft** (keep — *how* to choose, the "why", provider gotchas). Three mechanisms:

- **The live schema is the contract — enforce it, don't restate it.** The
  validator checks each document against the contract models
  (`analitiq-contract-models`, the same models the published JSON Schemas are
  generated from) **offline** — no runtime schema fetch — so authoring and
  validation agree on one contract. Where a plugin must restate a schema-owned
  enum as decision logic (e.g. the DSN-binding `encoding` set), the drift-check
  CI below pins it to the pinned contract models.
- **Fetch-once, pass-down** — an orchestrator hands the live contract schema URLs
  to its researcher (the mission spec) and the creators read the same schemas as
  vocabulary, so authoring and validation agree on one contract.
- **Drift-check CI** for anything that must stay duplicated as decision logic
  (e.g. the `enum-mappers` that map provider facts onto schema enums):
  `tests/connector_builder/test_schema_drift.py` reads the enum sets from the
  pinned `analitiq-contract-models` package and fails the build if a plugin's
  enum targets diverge. The pipeline plugin solves the same problem by
  *generating* contract-owned facts into its prose — see its `CLAUDE.md`.

Enum lists appearing in this file or in skill prose are **illustrative**; the
authoritative definition is always the live schema (or, for canonical Arrow
types, the engine-published grammar manifest vendored and pinned in
`analitiq.contracts.arrow_grammar` — see "The canonical Arrow type vocabulary
is engine-owned" above). Craft the schema never defined (the `ssl_mode`
vocabulary, the driver-selection decision order, datetime naive/tz judgment) is
not drift-exposed and stays.

The behavioural checklist for this policy is `.claude/rules/no-drift-surfaces.md`.

## Conventions

- JSON Schema Draft 2020-12 throughout.
- Test org_id: `d7a11991-2795-49d1-a858-c7e58ee5ecc6`.
- Agents must never author JSON that belongs to another agent's responsibility.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

`testpaths = tests` in `pytest.ini`, so a bare `pytest` runs every plugin's
suite. The connector drift guards additionally honour
`DRIFT_REQUIRE_CONTRACT_MODELS=1`, which turns a missing contract package into a
hard failure instead of an all-skipped green run — CI sets it so the gate can
never pass without actually running.

## Releases

Each publishable artifact carries **its own version and its own tag prefix**
(`analitiq-connector-builder-v*`, `analitiq-pipeline-builder-v*`,
`contract-models-v*`, `validator-v*`), but they are released two different ways.

**The two plugins** are managed by release-please. Their versions are
derived by **release-please in monorepo mode** from Conventional Commit types —
never bump a `plugin.json` version by hand.

- `fix:` → patch, `feat:` → minor, `feat!:` / `BREAKING CHANGE:` → major.
  Non-release types (`chore:`, `docs:`, `ci:`, `refactor:`, `test:`) don't bump.
- **Agent prose is behaviour, not documentation.** A change to an `agents/*.md`,
  `SKILL.md`, or a `references/` file alters what the plugin does, so it takes
  `feat:` or `fix:` — never `docs:`. Committing it as `docs:` ships changed agent
  behaviour under the old version, and nothing catches that afterwards.
  `docs:` is for README / CLAUDE.md / anything an agent never loads.
- Release-please routes a commit to a train by **path**, so an edit under
  `plugins/<name>/` bumps that plugin and nothing else. The bump comes from the
  type; the train comes from the path.
- Both plugins are pre-1.0, so breaking changes bump the minor and features bump
  the patch (`bump-minor-pre-major` / `bump-patch-for-minor-pre-major`).
- release-please maintains a rolling Release PR per artifact; merging it bumps
  the version, regenerates that artifact's `CHANGELOG.md`, and tags it.
- When squash-merging, the PR title is what release-please parses — it must be a
  valid Conventional Commit. A commit lands in a train by the **path** it
  touches, not by its scope; the scope is for the changelog, so use the
  component id (`feat(analitiq-connector-builder): …`).

**The two packages are released by hand**, by pushing a `contract-models-v*` or
`validator-v*` tag. They are deliberately NOT in release-please: it speaks
SemVer, and their versions are PEP 440 pre-releases (`1.0.0rc12`). A dry run
resolved `1.0.0rc12` as plain `1.0.0` and proposed `1.1.0` — silently dropping
the rc suffix and jumping the train to a final release.

Manual tagging also matches their discipline: `packages/validator` pins
contract-models with an exact `==`, `test_contract_models_pin.py` fails on any
skew, so three values must move together (both `[project].version`s and the pin);
and a minor bump is a coordinated engine rollout, not something a commit type
should infer.

Config: `release-please-config.json` + `.release-please-manifest.json`.
Workflow: `.github/workflows/release-please.yml`.

A **human-pushed** tag triggers the publish workflows normally. A tag created by
the release-please action would not — GitHub suppresses triggers from
`GITHUB_TOKEN` to prevent loops — which is why no publish job can be wired to
release-please output, and another reason the packages stay on manual tags.

## Credentials

No long-lived credentials in this repo. Publishing authenticates by OIDC — PyPI
Trusted Publishing for packages, an assumed IAM role for anything touching AWS —
scoped by a GitHub Environment.

### The `pypi` environment

PyPI Trusted Publishers are registered for both `analitiq-contract-models` and
`analitiq-validator` against:

| | |
|---|---|
| Owner | `analitiq-ai` |
| Repository | `claude-code-plugins` (matched **literally** — GitHub rename redirects do not apply) |
| Workflow | `contract-models-release.yml` / `validator-release.yml` |
| Environment | `pypi` |

**Every publish job must therefore declare `environment: pypi`.** PyPI checks the
environment on both sides; a job without it is rejected, and the failure surfaces
at the last step of an otherwise green release run. Both release workflows carry
it.

The environment's deployment rules permit **only** `contract-models-v*` and
`validator-v*`. Publishing is always tag-triggered, so the job's ref is always
the release tag; `main` is deliberately excluded. Adding a branch here would
widen what can reach PyPI for no gain.

The environment's **sole required reviewer is `Analitiq-Bot`**. Every publish
pauses at the `environment: pypi` job until it is approved, and no other account
can approve it — so while anyone with push access can *push* a release tag, only
Analitiq-Bot can let the publish through. That is the "only Analitiq-Bot
publishes" boundary, and it is the human gate that the release-please Release PR
used to provide before the packages moved to hand-pushed tags.

`prevent_self_review` is deliberately **off**: with Analitiq-Bot as the only
reviewer, blocking self-review would deadlock any release Analitiq-Bot itself
pushes. The trade-off is that this is single-account control, not four-eyes — to
require a second approver, add them to the environment and turn self-review
prevention back on.

These are live GitHub environment settings, not repo files, so they are not
covered by any test here; changing the reviewer or the tag rules is a settings
edit in the repo's Environments page.

### The `schemas` environment

`schemas-publish.yml` publishes `schemas/` to the serving bucket through the
`schemas` environment, built to mirror `pypi`'s reviewer gate: **sole required
reviewer Analitiq-Bot** (same single-account trade-off as above),
`prevent_self_review` off, deployment branches restricted to `main` (where
`pypi` restricts to release tags). It holds three environment **variables**,
none of them secrets: `AWS_ROLE_ARN`, `AWS_REGION`, `SCHEMAS_BUCKET`. The role
is specified by infrastructure#1018 (not yet applied there): OIDC trust pinned
to this repo's `schemas` environment, permissions `s3:PutObject` +
`s3:GetObject` on objects (GetObject authorizes the first-write-wins probe)
and `s3:ListBucket` — no delete, matching the workflow's additive publish
semantics. The same live-settings caveat as `pypi` applies: the reviewer,
branch rule, and variables are Environments-page settings, covered by no test
here.

The repo is **public**. Its workflow files are world-readable and that is fine:
the gate is authorization, not secrecy. Two rules follow from it — never use
`pull_request_target` with a checkout of PR code, and never add a static
credential as a repo or environment secret.

## PR Review Process

After creating a PR, follow these steps. Continue invoking the PR review process
until no more errors are raised. If raised errors are not relevant to the PR, ask
if you should create a GitHub issue for the raised error.

1. Use `/pr-review-toolkit` to review the PR after you have implemented all changes.
2. Wait for feedback from the review executor.
3. Determine if the raised issues are legitimate or not.
   a. if the issue is legitimate and relevant to the PR, fix it.
   b. if the issue is outside the scope of the PR, check if there is a related
      issue in the GitHub issue tracker. If not, create a new issue in GitHub and
      move on.
   c. If the issue is not a legitimate problem, summarize your thoughts on the
      point and move on.
4. Once you fixed all issues that need fixing, commit fixes, push to the branch.
5. Use `/pr-review-toolkit` to review again.
6. Continue doing this cycle until the PR is approved by the review executor.
7. Once the PR is approved, run the tests to make sure they all pass.
