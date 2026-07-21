# Lifecycle phases

Excerpted from `docs/schema-contracts/shared/lifecycle-phases.md`. Used
by `phase-resolvability` validation and by the creator agents when they
declare which phase produces each value.

## Phases

| Phase | Available scopes | Used by |
|---|---|---|
| `pre_auth` | `connection.parameters.*` | Inputs the user submits before auth (host, port, region, tenant slug, …). Transports for pre-auth discovery may run here. |
| `auth` | `pre_auth` scopes + `secrets.*`, `runtime.oauth.*` | Auth operations (`authorize`, `token_exchange`, `refresh`). |
| `post_auth` | `auth` scopes + `auth.*` | Post-auth discovery requests, `options_request`, `discovery_request`. |
| `active` | `post_auth` scopes + `connection.selections.*`, `connection.discovered.*`, `stream.*` | Endpoint operations. |

A later phase may use any earlier phase's scopes.

## Resolvability rule

For every transport's references, compute the union of scopes used. The
transport must be invokable in a phase where every used scope is
available. If a transport references `connection.discovered.api_domain`,
it cannot be the `default_transport` for an operation that runs in
`auth` or earlier.

## Example: Pipedrive

Pipedrive's `default_transport` (`api`) uses
`connection.discovered.api_domain`, which is populated only after
post-auth discovery. So Pipedrive declares a separate `discovery`
transport for the post-auth `discovery_request` that produces
`api_domain`. Once discovery completes, normal API calls can use the
`api` transport.

## Validator findings

`phase-resolvability` flags the common error of a transport using
`connection.discovered.*` without a documented post-auth output that
produces it. Other phase mismatches require declaring `phase` on each
input correctly.

## Runtime OAuth scope

For `auth.type: "oauth2_authorization_code"` only, the closed
`runtime.oauth.*` set is `code`, `state`, `redirect_uri`, `pkce_verifier`,
`code_challenge`, and `code_challenge_method`. Per-operation availability:

| Field(s) | Available in |
|---|---|
| `state`, `redirect_uri` | `auth.authorize` and `auth.token_exchange` |
| `code_challenge`, `code_challenge_method` | `auth.authorize` only |
| `code`, `pkce_verifier` | `auth.token_exchange` only |

The PKCE **verifier must never appear in the authorize request** — only the
derived `code_challenge` rides the browser-facing authorize — or it leaks
through redirect/provider logs and defeats PKCE. `auth.refresh` must not
reference `runtime.oauth.*` (refresh runs after the in-flight
authorization-code workflow completes). Any `runtime.oauth.*` reference on a
non-`oauth2_authorization_code` connector is an error. These values are
never persisted.
