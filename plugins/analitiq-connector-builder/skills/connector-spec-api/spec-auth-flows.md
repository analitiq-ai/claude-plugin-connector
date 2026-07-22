# API auth flows

Per-auth-type authoring contract. Each section lists the required and
forbidden child fields, points at a worked example under `examples/`,
and notes common pitfalls.

## `api_key`

**Required children:** `type`.
**Forbidden:** `authorize`, `token_exchange`, `refresh` (these are OAuth-only).
**`test` is optional.**

The actual API key value is declared in `connection_contract.inputs` with
`secret: true`. Auth header construction happens in the transport's
`headers` block, e.g. `"Authorization": { "template": "Bearer ${secrets.api_key}" }`.

Example: `examples/api-key/api-key.example.json` (with sibling
`examples/api-key/type-map-read.json`). For a templated / post-auth-discovered
host, see the multi-origin transport pattern in `spec-transport.md`.

## `basic_auth`

**Required children:** `type`.
**Forbidden:** OAuth ops.

`username` and `password` are declared as
`connection_contract.inputs`. The `Authorization` header in the transport
should use the `basic_auth` function expression — never pre-compute base64.

```json
"Authorization": {
  "function": "basic_auth",
  "input": {
    "username": { "ref": "connection.parameters.username" },
    "password": { "ref": "secrets.password" }
  }
}
```

## `oauth2_authorization_code`

**Required children:** `type`, `authorize`, `token_exchange`.
**Optional:** `refresh`, `test`.

`authorize` describes the URL that will be opened in the user's browser
(method usually `GET`); `token_exchange` describes the back-channel
request that swaps the auth code for tokens. Both are
`AuthOperationTemplate` objects with `path` plus optional `method`,
`headers`, `body`, `transport_ref`.

`client_id` typically lives in `connection.parameters` with
`source: "platform"`; `client_secret` lives in `secrets` with
`source: "platform"` and `secret: true`.

**Don't invent *user-facing* form inputs for an OAuth connector.** The redirect
flow collects the user's authorization through the browser, so declare a
`source: "user"` input only when the provider genuinely needs a value *before*
the authorize URL can be built (a region, or a tenant slug in the authorize
host). Asking the user for anything the redirect already yields is noise.

This does not mean an empty `inputs` map: the app's own `client_id` /
`client_secret` are still declared, as `source: "platform"` (above). Nothing
validates that a ref resolves, so dropping them leaves a connector that passes
validation and fails at connect with no credentials.

**Platform-owned vs user-owned OAuth apps differ only in `source`.** Whether
your platform registers one app for everyone (`source: "platform"`) or each
user brings their own (`source: "user"`), the storage paths and every auth
template stay identical — flip `source` and change nothing else.

**Refresh timing is not yours to declare.** Author the `refresh` template and
stop: when to refresh, how expiry is tracked, and retry behaviour are engine
concerns. There is no place to encode a TTL or a refresh policy.

Example: `examples/oauth2-authorization-code/oauth2-authorization-code.example.json`
(multi-origin provider with post-auth discovery; sibling
`examples/oauth2-authorization-code/type-map-read.json`).

## `oauth2_client_credentials`

**Required children:** `type`, `token_exchange`.
**Forbidden:** `authorize` (no redirect flow).
**Optional:** `refresh`, `test`.

Used for machine-to-machine auth. The `token_exchange` request POSTs
client credentials and gets an access token (no browser redirect):

```json
"auth": {
  "type": "oauth2_client_credentials",
  "token_exchange": {
    "transport_ref": "auth",
    "method": "POST",
    "path": "/oauth/token",
    "headers": { "Content-Type": "application/x-www-form-urlencoded" },
    "body": {
      "template": "grant_type=client_credentials&client_id=${connection.parameters.client_id}&client_secret=${secrets.client_secret}"
    }
  }
}
```

## `jwt`

**Required children:** `type`.
**Optional:** `test`.

Declare the signing key, algorithm, and claim inputs in
`connection_contract.inputs` (see `examples/jwt/jwt.example.json`), and set
the `Authorization` header from the minted token:

```json
"Authorization": {
  "template": "Bearer ${auth.access_token}"
}
```

> **Inline signing is not yet available.** The `jwt_sign` function that
> would mint `auth.access_token` from the declared key/claims is **planned,
> not registered** in the engine — connector validation rejects it. Until it
> lands, a `jwt`-auth connector can declare its inputs but cannot mint the
> token inline; do not author a `jwt_sign` call. Flag this capability gap
> before shipping a `jwt` connector that depends on local signing.

## `credentials`

**Required children:** `type`.
**Optional:** `test`.

Use only when the provider's auth doesn't fit any narrower type. Declare
the credential bundle in `connection_contract.inputs` with appropriate
`secret: true` flags.

## `aws_iam`

**Required children:** `type`.
**Optional:** `test`.
**Forbidden:** OAuth ops.

User-supplied AWS account, role, profile, or credential values are
declared in `connection_contract.inputs`. The transport handles signing
via runtime mechanics — connector JSON declares intent only.

## `none`

**Required children:** `type`.
**Forbidden:** `authorize`, `token_exchange`, `refresh`.

For public APIs that require no authentication. Rare.
