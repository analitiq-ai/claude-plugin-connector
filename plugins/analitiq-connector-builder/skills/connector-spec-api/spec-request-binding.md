# Request binding (`params` ↔ `request`)

How an endpoint's declared `params` reach its `request`. This is the part of
endpoint authoring most likely to fail validation, because the intuitive form —
dropping a `ref` straight into `request.query` — is rejected for anything
dynamic.

Rule ids below (`ADV-ENDP-*`) are the contract's cross-field rules; the full
list is `connector-builder/references/advisory-rules.md`. The validator enforces
them, so cite them rather than re-deriving them.

## The model: declare the input, then bind it

A dynamic request value is declared **once** as a param (the typed contract —
name, location, type, requiredness, filterability) and **referenced** from the
request slot with a binding expression:

```json
{
  "params": {
    "account_id": { "in": "path", "type": "string", "required": true },
    "updated_since": { "in": "query", "type": "string", "format": "date-time",
                       "required": false, "controlled_by": "replication" }
  },
  "request": {
    "method": "GET",
    "path": "/v1/accounts/{account_id}/invoices",
    "path_params": { "account_id": { "from_param": "account_id" } },
    "query": { "updated_since": { "from_param": "updated_since" } }
  }
}
```

`{"from_param": "<name>"}` is the only way to route a declared param into a
request. A bare `{"ref": "..."}` in a request slot is **not** an alternative
spelling — see the prohibitions below.

## Binding rules

- **`path_params` values must be exactly `{"from_param": <name>}`.** No other
  expression kind is accepted there, and the bound param must declare
  `in: "path"`. The `path_params` keys must equal the `{placeholder}` names in
  `request.path`, and the block is present exactly when the path declares
  placeholders (ADV-ENDP-001).
- **A binding's location must match the site it appears in** (ADV-ENDP-008):
  `request.headers` binds only `in: "header"` params, `request.query` only
  `in: "query"`, `request.body` only `in: "body"`.
- **Every declared param must be referenced by exactly one request binding**
  (ADV-ENDP-009). A declared-but-unbound param is an error, not dead weight —
  if you don't need it, delete it.
- **Every expression dict declares exactly one primary key** — one of `ref` /
  `template` / `literal` / `function` / `from_param` / `from_input`, alongside
  only `x-*` siblings (ADV-ENDP-022).
- **A GET read operation must not declare a body param** (ADV-ENDP-007).

## What must NOT go directly in a request slot

- **No direct `stream.*`, `state.*`, or `runtime.*` ref** in `headers`,
  `query`, or `body` — the contract rejects it and tells you to route the value
  through a declared param. These are the per-run values (filters, cursors,
  batch sizing), and routing them through a param is what gives them a declared
  type, requiredness, and operator set. Without that, nothing downstream knows
  whether a stream may filter on the value or what it may filter with.

  The check catches `{"ref": …}` specifically; smuggling the same value in as
  `{"template": "${runtime.…}"}` slips past it. Don't — the reason to route
  through a param is the declared contract, not the validator.
- **No unscoped ref or `${...}` placeholder.** The leading token of every ref
  and every template placeholder must be one of the contract's resolution
  scopes (see `connector-builder/references/value-expressions.md`).

## What legitimately stays direct

Values that are **fixed for the endpoint** need no param, because there is no
input to type or filter on:

```json
"headers": { "Accept": "application/json" },
"query":   { "api_version": { "literal": "2024-01" } }
```

Connection-scoped values resolved from the connector's connection contract
(`connection.parameters.*`, `secrets.*`, `auth.*`) are also direct refs — they
are not per-run inputs. Only the `stream`/`state`/`runtime` family is barred.

## Params carry the *request-input* type

`params.<name>.type` is a JSON-style request-input type (`string`, `integer`,
`number`, `boolean`, `array`, `object`) describing what is sent **up**. It is
unrelated to `native_type` / `arrow_type`, which describe what comes **back** in
`response.schema`. A timestamp sent as an ISO string is `type: "string"` even
though the response field it filters is `Timestamp(...)`.

Two more param rules worth knowing while authoring:

- **`operators` is the stream-filterability contract.** Declaring
  `operators: ["gte", "lte"]` is what permits a downstream stream to filter on
  that param, restricted to those operators. Omit it and the param is not
  stream-filterable at all.
- **A `controlled_by` param must not declare `operators`** (ADV-ENDP-002) —
  pagination and replication own those params, so a stream may not also filter
  on them.
- **A `query` param of type `array` or `object` must declare `style` and
  `explode`** (ADV-ENDP-003), because the wire serialization is otherwise
  ambiguous.

## The same value in two places is two params

If a provider wants the same value in both a header and a query string, declare
two params with distinct names and bind each at its own site. One param cannot
satisfy two bindings (ADV-ENDP-009 counts bindings per param).

## Write bodies: `from_input`

`{"from_input": ...}` addresses the record being written. It is legal **only**
inside `operations.write.<mode>.request.body` — never in `path_params`,
`headers`, `query`, a read body, or a param `default`.

| Value | Means |
|---|---|
| `record` | the whole destination record |
| `records` | the whole batch array |
| `record.<dotted>` | one field of the record |

- **A write request body must reference `from_input`.** Batching selects the
  arity: a batched write must use `records`, a non-batched write must use
  `record` / `record.<field>` (ADV-ENDP-017).
- **`records.<dotted>` is not supported** — dotted paths through the batch
  array are rejected.
- Referenced fields must exist in that mode's `input.schema`.

Provider envelopes are authored literally around the binding; no wrapper key is
special. This example is a **batched** write (`records`), so it is only legal
alongside a `batching` block — an unbatched write wraps `record` instead:

```json
"body": { "data": { "from_input": "records" } }
```
