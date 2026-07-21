# Value expressions

Shared invariant both creator agents must follow. Excerpted from the
authoritative spec at
`docs/schema-contracts/shared/value-expression-parameterization.md`.

## Four expression kinds

A value expression is one of:

| Kind | Shape | Use |
|---|---|---|
| `ref` | `{"ref": "<dotted.path>"}` | Resolve a single value from a runtime scope. |
| `template` | `{"template": "literal-with-${scope.path}-interpolation"}` | Build a string by interpolating one or more refs into a literal. |
| `literal` | `{"literal": <any>}` | A constant value (string, number, boolean, object, array). |
| `function` | `{"function": "<name>", "input": {...}}` | Call a registered function with named inputs. |

Anywhere the schema accepts a value expression, exactly one of the four
shapes is allowed.

## Logical scopes

Every `ref` and every `${...}` interpolation inside a `template` must begin
with one of the contract's resolution scopes. The authoritative set is
`RESOLUTION_SCOPES` in `analitiq.contracts.value_expression` (pinned by
`tests/connector_builder/`); the table below is the authoring guide to the ones a
connector or endpoint actually writes.

| Scope | Phase available | Holds |
|---|---|---|
| `secrets.*` | `auth` and later | User-entered or platform-injected secret values, opaque references. |
| `connection.parameters.*` | `pre_auth` and later | Non-secret user/platform values declared in `connection_contract.inputs` with `storage: "connection.parameters"`. |
| `connection.selections.*` | `post_auth` and later | Durable user choices declared as `post_auth_outputs` with `storage: "connection.selections"`. |
| `connection.discovered.*` | `post_auth` and later | Auto-discovered non-secret context (e.g. `api_domain`) declared as `post_auth_outputs` with `storage: "connection.discovered"`. |
| `auth.*` | `auth` and later | Auth tokens (access_token, refresh_token, expiry). |
| `runtime.*` | varies by ref | Per-run values. Only two families are actually supplied: `runtime.batch_size` (the run's configured page size — use it for a pagination `limit.default`) and the OAuth set (`state`, `redirect_uri`, `code`, `pkce_verifier`, `code_challenge`, `code_challenge_method`) per `lifecycle-phases.md`. The scope accepts any path, so an invented one (`runtime.run_id`, `runtime.current_time`) validates clean and fails at resolution — don't guess names. |
| `response.*` | endpoint response handling | The response being processed — `response.body.*`, `response.headers.*`. This is what pagination `stop_when` / `next_cursor` and `response.metadata` refs target. |
| `request.*` | endpoint request handling | The request being built. |
| `stream.*` | per stream | Stream-owned routing, tenant context, stream-specific auth context. |
| `state.*` | per run | Replication watermarks and other carried-over run state. |
| `connector.*` | any | Connector-level declared values. |

**Scope checking is narrow.** It runs on an endpoint's `request.headers` /
`query` / `body`, and on typed ref fields such as `response.records` and
`response.metadata`. Except where noted below it checks only the **leading
token**, so `connection.discovered.nope` passes and resolves empty.

`response.records` is the one ref whose **full path** is validated: it is
traversed against `response.schema` and must land on an array node
(ADV-ENDP-012), so `response.body.nope` is an error rather than a silent empty.

Everywhere else there is no check at all: a bogus scope in a pagination
`stop_when`, or anywhere in a **connector** document (a transport header, an
auth template), validates clean. Treat every ref as unverified and trace it to
the declaration that produces it yourself.

(`request.path_params` is not scope-checked either — it is separately restricted
to `{from_param: …}`, so a raw ref there is rejected on different grounds.)

> **`stream.*`, `state.*`, and `runtime.*` are barred from endpoint request
> slots.** They may not appear as direct refs in `request.headers` / `query` /
> `body` / `path_params`; route them through a declared param instead. See
> `connector-spec-api/spec-request-binding.md`.

Two paths that *look* like scopes but are not, and so fail at runtime after
passing validation (the leading token `connection` is legal, the rest is not):
`connection.auth_state.*` and `connection.secret_refs.*`.

## Function catalog (registered)

Inline function expressions may only call **registered** functions — the
engine's `DEFAULT_FUNCTIONS` registry. Current catalog:

- `basic_auth` — build a Basic credential/header from `username` + `password` (or client-credentials) inputs.
- `base64_encode` — base64-encode a string/bytes value for provider auth formats.
- `lookup` — map an input value through a connector-declared inline `map`, returning the mapped value.
- `url_encode` — percent-encode a scalar for a URL component. Escapes every reserved character by default (`safe: ""`); pass a `safe` field to widen the unescaped set.

**`lookup` maps must be total.** The inline `map` has to cover every value of
the input's declared `enum`, and add no keys outside it. Nothing validates this
— a value with no entry resolves to nothing and the request goes out missing
that field, so an uncovered enum member fails silently at connect time rather
than loudly at authoring time.

**Never call `url_encode` inside a DSN binding.** The binding's declared
`encoding` already owns percent-encoding; wrapping the value encodes it twice.
`url_encode` is for URL components you build yourself in a `template`.

**Planned — NOT yet registered; do not reference:** `jwt_sign` (sign a JWT from
key/algorithm/claims) and `pkce_challenge_s256` (derive a PKCE S256 challenge
from a runtime verifier). Nothing rejects them at authoring time (see below),
so calling one ships a connector that fails at connect. Until the engine
registers a function, connectors must not call it — this includes the
inline-signing path for `jwt` auth.

**Nothing validates the function name.** An unregistered name (including
`jwt_sign`) passes every check and fails only when the engine tries to resolve
it at connect time. Treat the catalog above as closed and verify by hand; the
validator will not catch a typo or a planned-but-unregistered function. To
extend the catalog, the engine's function registry must be updated first.

## DSN placeholders are not value expressions

Inside `dsn.template`, `{placeholder}` markers are NOT `${...}` value
expressions. They resolve through `dsn.bindings`, where each binding
declares a `value` (a value expression) and an `encoding` (closed enum).

This is intentional: the runtime owns percent-encoding mechanics for DSN
construction and applies declared encodings before substitution.
Connector authors must not pre-encode binding values.
