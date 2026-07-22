# Lifecycle phases

Which values exist when, so a transport or operation only references values
that can actually resolve at the point it runs.

> **This is entirely author-side.** No validator checks phase resolvability: a
> transport referencing `connection.discovered.api_domain` with no post-auth
> output producing it validates clean and fails at connect. On a connector
> document refs are not checked *at all* — even a nonsense scope passes — so
> there is no backstop here whatsoever. Walk the phases by hand.

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

## Example: a value that arrives after auth

A provider issues an access token, then exposes the account's own settings at a
stable endpoint. Reading those settings needs `auth.access_token`, so that
request cannot run in `pre_auth` — it is declared as a post-auth
`discovery_request`, and the value it produces lands at
`connection.discovered.<key>` for later phases to reference.

The ordering rule is what matters: a transport that references
`connection.discovered.*` is only invokable once post-auth discovery has run,
so it can never be the transport for an `auth`-phase operation. Declare a
separate transport for the discovery request itself, which needs only `auth`
scopes.

A discovered value can be templated straight into the data transport's
`base_url` (see `connector-spec-api/spec-transport.md`), which is the usual
shape for a per-tenant host.

## The failure this prevents

The common error is a transport referencing `connection.discovered.*` with no
post-auth output that produces it — the value is simply absent at connect. The
mirror image is declaring an input's `phase` too late for the transport that
needs it (a `base_url` component declared `phase: "auth"` cannot serve a
pre-auth request).

Neither is caught by validation. Before returning a connector, trace each
transport's refs to the declaration that produces them and confirm the
producing phase is no later than the consuming one.

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
