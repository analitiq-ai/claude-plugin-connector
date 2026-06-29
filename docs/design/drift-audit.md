# Drift-surface audit

Point-in-time inventory (2026-06-28) of every place the plugin **restates
content the published schemas already define**, or embeds example documents
that carry schema shape. Companion to the **Drift policy** in
[`contract-derived-research.md`](./contract-derived-research.md) §2:

> The published schema is the single source of truth. Never restate what it
> defines — reference or load it. Carry only craft the schema can't express.

> ⚠️ This is a snapshot and will itself drift as the repo changes. Re-run the
> greps in §"How this was produced" before trusting the counts.

---

## Contract — schema-owned, eliminate the duplication

Ranked by surface size (largest first).

| # | Surface | Where | Fix |
|---|---|---|---|
| 1 | **Example documents** — full schema shape embedded | 30 files under `skills/*/examples/` + inline `json` blocks in 10 spec docs | Move canonical examples to the schema repo (`analitiq-infra`), validated by its CI; plugin **references** them + thin craft notes |
| 2 | **Auth-type enum** (`api_key`…`aws_iam`) | `CLAUDE.md`, `references/enum-mappers.md`, `references/io-contracts.md`, `connector-spec-api/SKILL.md`, `spec-auth-flows.md`, validator (+2 examples) | Load from `connector` schema `auth.type` enum |
| 3 | **Driver enum** (`postgresql/snowflake/bigquery`) | `CLAUDE.md`, `enum-mappers.md`, `io-contracts.md`, `connector-spec-db/SKILL.md`, `spec-driver-selection.md`, `spec-dsn-bindings.md`, `connector-provider-researcher.md`, `db-connector-creator.md` | Load from `AdbcTransport.driver` enum |
| 4 | ~~**Value-expression scopes** (closed list)~~ | `references/value-expressions.md`, `connection-contract.md`, `lifecycle-phases.md`, `CLAUDE.md`, validator, `spec-transport.md` | **Reclassified → craft.** The published schema has **no single scope enum** — only fragmented `storage` enums (`connection.parameters`/`secrets`, `connection.selections`/`connection.discovered`) plus runtime/auth/stream scopes the validator owns semantically. The full scope vocabulary is **plugin-owned** (like `ssl_mode`), so "load from schema" is not achievable or correct. Keep as craft; see below. |
| 5 | **DSN encoding enum** | `CLAUDE.md`, `spec-dsn-bindings.md`, `connector-spec-db/SKILL.md`, `db-connector-creator.md`, validator (+3 examples) | Load from schema |
| 6 | **Canonical Arrow vocab** | `spec-type-maps.md`, `CLAUDE.md`, `db-connector-creator.md`, validator (+ example maps) | **Already single-sourced** in `canonical-types.json` (`analitiq-infra`) — reference it, stop restating |
| 7 | **Pagination styles** (`offset/page/cursor/link/keyset`) | `references/io-contracts.md`, `spec-pagination.md` | Load from schema |
| 8 | **`$schema` URLs hardcoded** | ~12 files (validator 9, `CLAUDE.md` 8, `connector-builder/SKILL.md` 6, `pipeline.md` 5, …) | **Low priority** — `/latest.json` is stable by design; centralize to one constant |

---

## Craft — NOT schema-owned, keep (not drift)

- **`ssl_mode` vocabulary** — connector-defined per provider (`CLAUDE.md`
  states this explicitly), not owned by the published schema.
- **Value-expression scopes** (was #4) — the *names* of the scopes
  (`secrets.*`, `connection.parameters.*`, `connection.selections.*`,
  `connection.discovered.*`, `auth.*`, `runtime.*`, `stream.*`) are a
  plugin-owned vocabulary. The schema constrains a few `storage` targets to
  subsets of it but never enumerates the whole list, so the validator's
  `KNOWN_SCOPES` is the source of truth, not a restatement. Keep.
- **Driver-selection decision order**, datetime naive/tz choice, pagination
  choice, auth-flow idioms, provider gotchas, the "why."

These cannot drift from the schema because the schema never defined them.

---

## Special cases — verify, don't blindly remove

- **`scripts/validate_connector.py`** — already fetches + disk-caches the
  schemas for Layer 1 (zero drift). Its Layer-2 restatements of enums
  (encoding, scopes, auth shape) should read from the fetched schema rather
  than hardcode.
- **`references/enum-mappers.md`** — maps researched provider facts → schema
  enums. This is *logic keyed on* the enum (it must change when the enum
  changes anyway), so it is not removable duplication — it is the prime
  candidate for the **drift-check CI** test (load the live schema, fail the
  build if the mapper's target set diverges).

---

## Sequencing

1. **Examples (#1)** dwarf everything — killing them removes ~30 files of
   embedded shape plus the inline blocks. Do this first.
2. **Enum-loading (#2–#7)** — collapse the many prose copies into "load from
   schema"; pair with the drift-check CI for the residue (#enum-mappers).
3. **`$schema` URLs (#8)** — stable by design; defer / centralize last.

---

## Status — addressed by the contract-derived-research change

| # | Surface | Disposition |
|---|---|---|
| 1 | Example documents | **Addressed by reduction (not relocation).** Per best-practice research (3–5 *diverse* few-shot examples; SSOT; golden-file CI validation), the example set was cut from 10 multi-purpose dirs to **5 diverse archetypes** (api_key / oauth2_authorization_code / jwt; sqlalchemy / adbc), all CI-validated against the live schema. Per-provider **type-map data is derived from research** at authoring time (one postgres kitchen-sink + the "non-obvious natives" craft table; the adbc map is a trimmed stub). Distinctive craft of dropped dirs migrated into the specs (oauth2_cc block, dynamic-host, ssl_mode/VARIANT). No cross-repo move needed. |
| 2 | Auth-type enum | **Guarded by CI.** `test_schema_drift.py::test_auth_types_match_schema` pins the set to the live `*Auth` `$defs`; `enum-mappers.md` reframed as schema-derived logic. |
| 3 | Driver enum | **Guarded by CI.** `test_adbc_drivers_match_schema` pins it to `AdbcTransport.driver`. |
| 4 | Value-expression scopes | **Reclassified as craft** (see table + Craft list). No change — not a schema enum. |
| 5 | DSN encoding enum | **Fixed at the source.** The validator now derives the enum from the live connector schema (`known_encodings()`), literal kept only as offline fallback; `test_dsn_encodings_match_schema` pins both. |
| 6 | Canonical Arrow vocab | **Policy.** Already single-sourced in `canonical-types.json`; the drift policy now names it the authority and marks inline lists illustrative. |
| 7 | Pagination styles | **Guarded by CI.** `test_pagination_styles_match_schema` pins the set to the `*Pagination` `$defs`. |
| 8 | `$schema` URLs | **Partial.** The validator's connector URL is centralized to one `CONNECTOR_SCHEMA_URL` constant; the rest stay (stable by design, `/latest.json`). Lowest priority. |

The general rule is now stated once in **CLAUDE.md → "Single source of truth
(drift policy)"** and enforced for the residue (the decision-logic enums that
can't be deleted) by `tests/connector_validator/test_schema_drift.py`. The
fetch-once-pass-down mechanism is wired into the researcher (the contract is
its mission spec) and the creators (they read the same schemas as vocabulary).

## How this was produced

```sh
# examples
find skills agents -path '*/examples/*' -type f
grep -rlc '```json' skills agents

# restated enums / vocab (representative distinctive tokens)
grep -rln -E 'oauth2_authorization_code|oauth2_client_credentials|aws_iam' skills agents CLAUDE.md
grep -rln -E 'url_userinfo|url_path_segment|url_query_value' skills agents CLAUDE.md scripts
grep -rln -E 'connection\.selections|connection\.discovered' skills agents CLAUDE.md scripts
grep -rln -E '\bkeyset\b' skills agents CLAUDE.md
grep -rln -E 'AdbcTransport|adbc_driver_|snowflake.*bigquery' skills agents CLAUDE.md
grep -rln -E 'Timestamp\(MICROSECOND|Decimal128\(|LargeUtf8|FixedSizeBinary' skills agents CLAUDE.md

# $schema URLs
grep -rc 'schemas\.analitiq\.ai' skills agents CLAUDE.md scripts
```
