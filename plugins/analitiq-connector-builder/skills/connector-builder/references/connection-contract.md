# Connection contract — outer shape

Both creator agents (api and db) must emit the same outer
`connection_contract` shape. Concrete inputs differ; the structure does
not.

## Top-level fields

| Field | Required | Notes |
|---|---|---|
| `inputs` | Yes | Map of input keys to `ConnectionContractInput` declarations. May be empty. |
| `post_auth_outputs` | No | Map of durable post-auth output keys to `PostAuthOutput` declarations. |
| `required_for_activation` | No | Array of reference paths that must resolve before the connection can become active (e.g. `"connection.discovered.api_domain"`). |
| `validation` | No | Cross-input validation rules. |

## Per-input fields (`ConnectionContractInput`)

Required: `source`, `phase`, `storage`, `type`, `required`.

| Field | Values |
|---|---|
| `source` | `"user"` (entered by end user) or `"platform"` (provisioned by the platform/admin). |
| `phase` | `"pre_auth"` or `"auth"` — when the value must be available. |
| `storage` | Closed enum for inputs: `"connection.parameters"` (non-secret, durable) or `"secrets"` (secret, durable, materialized via secret refs). Inputs **cannot** target `"connection.selections"` / `"connection.discovered"` — those are produced by `post_auth_outputs` (below), never collected as inputs. |
| `type` | JSON Schema type: `string`, `integer`, `boolean`, `number`. |
| `required` | Boolean. |
| `secret` | Boolean (default false). Must be true for any input stored in `secrets`. |
| `enum` | Array of allowed values (closed enum). |
| `default` | Default value (for non-required inputs). |
| `format` / `pattern` | Optional string validation — `format: "uri"` or a regex `pattern`. The published schema names this field `pattern`; the input field set is closed (`additionalProperties: false`). |
| `ui` | Optional UI hint object (label, placeholder, widget). |

## API vs DB inputs

Both kinds emit the same outer shape. Concrete inputs differ:

- **API connectors** — typically declare `api_key`, OAuth `client_id`/`client_secret`, and any tenant/account identifiers the provider requires.
- **DB connectors** — typically declare `host`, `port`, `database`, `username`, `password`, `ssl_mode`, `ssl_ca_certificate`.

## What belongs in the contract at all

**A connector must not own customer-specific values.** A host, tenant id,
account id, database name, profile, credential, or token belongs to a
*connection*, not to the connector — the connector declares the *shape* of the
input, and the connection supplies the value. A connector carrying a real
customer's host is not reusable, which is the whole point of the split. (This
is broader than "no secrets": a tenant slug isn't secret and still doesn't
belong.)

**Place a value by who owns it, not by who first references it.** The question
is never "where is this interpolated from?" but "whose value is this?":

| Value | Where |
|---|---|
| Secret supplied by the end user or platform | input, `storage: "secrets"`, `secret: true` |
| Non-secret value the user supplies before auth | input, `storage: "connection.parameters"` |
| A choice the user makes from a post-auth list | `post_auth_outputs`, `mode: "user_selection"` |
| A value read from a post-auth probe | `post_auth_outputs`, `mode: "auto_discovery"` |
| Operational tunable (API version, timeout, page size, warehouse) | a `default` in the contract, overridable in `connection.parameters` — not a hardcoded literal buried in a transport |

## Post-auth outputs

`post_auth_outputs` are the single source of truth for durable post-auth
context. Required fields per output:

- `mode` — closed enum: `user_selection` (a value the user picks from an `options_request`) or `auto_discovery` (a value read from a `discovery_request`).
- `storage` — closed enum `"connection.selections"` (user choices, `user_selection`), `"connection.discovered"` (auto-discovered values, `auto_discovery`), or `"secrets"` (secret-valued outputs). See the connection contract for the mode↔storage pairing.
- `type` — the value's type.
- `value_path` — the **response-extraction path**: the field read out of the
  `options_request` / `discovery_request` response (e.g. `"id"` for a
  selection option, `"company_domain"` for a discovery field). It is *not* the
  materialized reference path.

The durable reference path is **derived** as `storage` + `"."` + the output
key — e.g. an output keyed `api_domain` with `storage: "connection.discovered"`
materializes at `connection.discovered.api_domain`, which is what refs and
`required_for_activation` target. For `user_selection` outputs, `label_path` /
`options_path` are response-extraction paths too.

Discovery mechanics (`options_request` / `discovery_request`) are
declared in the same output entry where applicable.

**Don't hide a non-secret value in `secrets`.** An output's storage must
reflect what the value *is*: `connection.discovered` for auto-discovered
context, `connection.selections` for user choices, `secrets` only for genuinely
secret values. Routing a tenant domain or account id through `secrets` because
it "feels safer" makes it unreadable to the refs that need it and misreports the
connector's secret surface. (The contract enforces the mode↔storage pairing,
but it cannot tell whether a value is truly secret.)

**Don't rely on output ordering.** `post_auth_outputs` is a map, and nothing in
the contract declares or validates an execution order between entries. Author
each output so it stands on its own: never write one that quietly depends on
another having already run, and don't build a chain of outputs referencing each
other's values.

## Cross-input validation (`validation`)

`validation.rules[]` expresses conditional requirements *between inputs* — "if
the user picked X, then Y is required and Z is meaningless". Each rule is a
`when` predicate plus `require` / `forbid` lists and an operator-authored
`message`. The shape and the predicate operator set are contract-owned
(ADV-CTOR-012, plus ADV-CTOR-008/009 requiring every referenced field to be a
declared input); what the contract can't tell you is what the operators *mean*
and where the boundary sits.

| Operator | Fires when the field… |
|---|---|
| `eq` | equals the given value |
| `in` | is one of the given values |
| `not_in` | is none of the given values |
| `present` | has any value at all (use for "the user filled this in") |
| `regex` | matches the pattern |

A predicate declares exactly one of these — they don't combine inside one
predicate. Write the `message` for the person filling in the form, naming the
field they must fix.

**Scope boundary.** These predicates are for *cross-input* validation only —
relationships among values already on the form. They are not a place to express:

- provider reachability or credential correctness (that's `auth.test`),
- anything requiring a network call (OAuth callbacks, post-auth probes —
  those are `post_auth_outputs`),
- runtime connection health.

If a rule can't be decided from the submitted inputs alone, it doesn't belong
here.

## Drift detection

The `connection_contract` block has no standalone `version`. Drift
detection rides on the connector's top-level `version` semver:

- Additive changes to inputs/outputs/activation/validation → minor bump.
- Breaking changes (input removed, renamed, type-changed, enum narrowed,
  storage moved, non-optional input added) → major bump.

See `metadata-and-versioning.md` and the connector release table.
