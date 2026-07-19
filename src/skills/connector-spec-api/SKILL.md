---
name: connector-spec-api
description: API connector authoring vocabulary — auth flows, HTTP transports, pagination, replication, post-auth discovery. Loaded by api-connector-creator only. Not invoked directly by users.
disable-model-invocation: true
---

# connector-spec-api

This skill is loaded by `api-connector-creator` when authoring an API
connector. It carries the API-specific vocabulary and examples needed to
populate `transports`, `auth`, `connection_contract`, and
`resource_discovery` for `kind: "api"`, plus the standalone
`type-map-read.json` shipped alongside the connector. API connectors
ship no write map and no package files — those are database-connector
artifacts.

## Required reading (load on demand)

Pick what you need for the auth and pagination styles you're authoring:

- This skill's `spec-auth-flows.md` (for the chosen `auth.type`)
- This skill's `spec-transport.md` (for HTTP transport idioms)
- This skill's `spec-request-binding.md` (how `params` reach a `request` —
  read this before authoring any endpoint request)
- This skill's `spec-pagination.md` (for endpoint pagination)
- This skill's `spec-replication.md` (for incremental sync)
- `connector-spec-db/spec-type-maps.md` for authoring the standalone
  `type-map-read.json` (same rule shape for API and DB; API ships the
  read direction only)
- The closest auth archetype under `examples/<name>/` (`api_key`,
  `oauth2_authorization_code`, `jwt`) — each a `<name>.example.json` connector
  body + sibling `type-map-read.json`. `spec-auth-flows.md` covers every auth
  type; only the diverse archetypes ship a full example dir

## What this skill covers

- HTTP transport idioms: single-origin, multi-origin, templated `base_url`.
- All API auth-type templates: `api_key`, `basic_auth`,
  `oauth2_authorization_code`, `oauth2_client_credentials`, `jwt`,
  `credentials`, `aws_iam`, `none`.
- `auth.authorize` / `auth.token_exchange` / `auth.refresh` / `auth.test`
  operation templates.
- Inline function expressions: `basic_auth`, `base64_encode`, `lookup`, `url_encode` (`jwt_sign` / `pkce_challenge_s256` are planned — not yet registered; do not use).
- `headers_remove` semantics for inheriting transports.
- `post_auth_outputs` with `options_request` / `discovery_request`.
- Pagination styles (offset / cursor / page / link).
- Replication for incremental sync.

## Endpoint `operations` shape (cross-reference)

Endpoint authoring lives in the `endpoint-creator` agent, and the shape of
`operations` — which keys exist, which are required, which combinations are
legal — is owned by the published api-endpoint contract and enforced by the
validator. It is not restated here.

What to read instead:

- `spec-request-binding.md` — how `params` reach a `request` (the part most
  likely to fail validation).
- `spec-pagination.md` / `spec-replication.md` — choosing and wiring those
  blocks.
- `connector-builder/references/advisory-rules.md` — the `api-endpoint`
  cross-field rules, by id.

## What this skill does NOT cover

- DSN URL templates, bindings, or encoding enums (that's `connector-spec-db`).
- `tls` block (that's `connector-spec-db`).
- Database `resource_discovery` (DB-specific shape).
- Type-map file shape and authoring rules (see
  `connector-spec-db/spec-type-maps.md` — the standalone `type-map-read.json`
  has the same shape for API and DB).
