# Pagination

Authoring patterns for `operations.read.pagination` in API endpoints.

The exact shape — property names, required keys, and the `stop_when`
predicate grammar — is owned by the published api-endpoint contract, not
by this page. Each strategy is a discriminated branch on `type`, and each
has its own definition in
`https://schemas.analitiq.ai/api-endpoint/latest.json`:

- `offset` → `#/$defs/OffsetPagination`
- `page` → `#/$defs/PagePagination`
- `cursor` → `#/$defs/CursorPagination`
- `link` → `#/$defs/LinkPagination`
- `keyset` → `#/$defs/KeysetPagination`

This page covers only the authoring decision the schema can't express —
which strategy fits a provider. The examples below are validated against
those definitions in CI
(`tests/connector_validator/test_spec_doc_examples.py`), so they stay in
step with the contract.

## `stop_when` is a predicate, not a keyword

Every strategy requires a `stop_when` — the condition that ends the page
loop. It is **not** a string like `"page_empty"`; it is a predicate object
from the contract's predicate grammar (`#/$defs/PredEmpty`,
`#/$defs/PredMissing`, `#/$defs/PredExists`, the comparison predicates
`PredEq`/`PredLt`/…, and the combinators `PredAnd`/`PredOr`/`PredNot`).
Each leaf wraps a single key over a value expression:

- `{ "empty": <expr> }` — stop when the expression resolves to nothing
  (an empty record array → no more pages).
- `{ "missing": <expr> }` — stop when the field is absent (no next
  cursor / no next link in the response).

> The predicate **wrappers** below are contract-checked. Their **operands**
> (which `ref` resolves to the page's record array or the next-page token)
> are runtime semantics the schema leaves open — match them to the actual
> response shape of the endpoint you're authoring.

## `offset`

Fixed-size pages addressed by an integer offset. The runtime increments
`offset` by the page size until `stop_when` fires.

```json
{
  "type": "offset",
  "offset": { "param": "offset", "initial": 0, "increment_by": 100 },
  "limit": { "param": "limit", "default": 100, "max": 100 },
  "stop_when": { "empty": { "ref": "response.body.data" } }
}
```

## `page`

Pages addressed by a 1-based page number.

```json
{
  "type": "page",
  "page": { "param": "page", "initial": 1, "increment_by": 1 },
  "limit": { "param": "per_page", "default": 50 },
  "stop_when": { "empty": { "ref": "response.body.data" } }
}
```

## `cursor`

Server returns an opaque token in each response; the next request passes
it back via `cursor.next_cursor` (a value expression). Common with modern
APIs (Stripe, Slack, etc.).

```json
{
  "type": "cursor",
  "cursor": { "param": "starting_after", "next_cursor": { "ref": "response.body.next_cursor" } },
  "limit": { "param": "limit", "default": 100 },
  "stop_when": { "missing": { "ref": "response.body.next_cursor" } }
}
```

## `link`

The next-page URL comes from the response (a `Link: <…>; rel="next"`
header or a body field). `link.next_url` is a value expression resolving
to that URL; the runtime follows it until it's absent.

```json
{
  "type": "link",
  "link": { "next_url": { "ref": "response.headers.link" } },
  "stop_when": { "missing": { "ref": "response.headers.link" } }
}
```

## `keyset`

Advance from the last record's ordering key (e.g. `since_id`). `keyset`
names the request `param` and the `order_by_field` (the dotted record
path the runtime reads the next key from); requires the response records
to be ordered by that field.

```json
{
  "type": "keyset",
  "keyset": { "param": "since_id", "order_by_field": "id" },
  "limit": { "param": "limit", "default": 100 },
  "stop_when": { "empty": { "ref": "response.body.data" } }
}
```

## Pick the right one

- Offset/page work for older REST APIs where total count or deterministic
  ordering is fine.
- Cursor and link are preferred when available — they're robust to
  insertions during a long sync.
- Keyset is the right choice when the server returns ordered records and
  exposes a stable ordering key.
- Some providers offer multiple pagination modes; use the one with the
  most stable semantics (cursor or link beat offset).
