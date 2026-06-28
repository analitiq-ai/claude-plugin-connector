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
| 4 | **Value-expression scopes** (closed list) | `references/value-expressions.md`, `connection-contract.md`, `lifecycle-phases.md`, `CLAUDE.md`, validator, `spec-transport.md` | Load from schema |
| 5 | **DSN encoding enum** | `CLAUDE.md`, `spec-dsn-bindings.md`, `connector-spec-db/SKILL.md`, `db-connector-creator.md`, validator (+3 examples) | Load from schema |
| 6 | **Canonical Arrow vocab** | `spec-type-maps.md`, `CLAUDE.md`, `db-connector-creator.md`, validator (+ example maps) | **Already single-sourced** in `canonical-types.json` (`analitiq-infra`) — reference it, stop restating |
| 7 | **Pagination styles** (`offset/page/cursor/link/keyset`) | `references/io-contracts.md`, `spec-pagination.md` | Load from schema |
| 8 | **`$schema` URLs hardcoded** | ~12 files (validator 9, `CLAUDE.md` 8, `connector-builder/SKILL.md` 6, `pipeline.md` 5, …) | **Low priority** — `/latest.json` is stable by design; centralize to one constant |

---

## Craft — NOT schema-owned, keep (not drift)

- **`ssl_mode` vocabulary** — connector-defined per provider (`CLAUDE.md`
  states this explicitly), not owned by the published schema.
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
