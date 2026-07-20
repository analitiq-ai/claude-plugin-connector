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

This page covers only the authoring decisions the schema can't express —
which strategy fits a provider, and how to wire it.


## Pagination is wired in three places

This is the part authors get wrong. A pagination block **does not create a
request binding on its own**. Every param it names must be:

1. **declared** in `params`, with
2. **`controlled_by: "pagination"`** on that param, and
3. **bound** into the request with `{"from_param": …}`.

Miss any one and validation fails (ADV-ENDP-009 — every declared param needs
exactly one binding; ADV-ENDP-010 — a pagination param reference must exist and
be marked `controlled_by`). A `controlled_by` param must **not** declare
`operators`: pagination owns it, so a stream may not also filter on it
(ADV-ENDP-002).

```json
{
  "params": {
    "page":     { "in": "query", "type": "integer", "required": false, "controlled_by": "pagination" },
    "per_page": { "in": "query", "type": "integer", "required": false, "controlled_by": "pagination" }
  },
  "request": {
    "method": "GET",
    "path": "/v1/items",
    "query": {
      "page":     { "from_param": "page" },
      "per_page": { "from_param": "per_page" }
    }
  },
  "pagination": {
    "type": "page",
    "page":  { "param": "page", "initial": 1, "increment_by": 1 },
    "limit": { "param": "per_page", "default": { "ref": "runtime.batch_size" }, "max": 100 },
    "stop_when": { "empty": { "ref": "response.body.data" } }
  }
}
```

## `limit`: `max` is the provider's cap, `default` is ours

- **`max`** is the largest page the provider permits. Read it from their docs;
  never guess it upward.
- **`default`** is the page size actually requested. Prefer
  `{"ref": "runtime.batch_size"}` so the run's configured batch size flows
  through, rather than hardcoding a number and overriding the operator's
  choice. Hardcode only when the provider's usable page size is genuinely
  fixed.

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

> The predicate **wrappers** are contract-checked. Their **operands** (which
> `ref` resolves to the page's record array or the next-page token) are runtime
> semantics the schema leaves open — match them to the actual response shape of
> the endpoint you're authoring.

## Pagination is not sync scoping

Pagination walks *one* result set. It is not the mechanism for incremental
windows (that's `replication`) or for tenant/account scoping (that's an
ordinary param or a connection value). If you find yourself encoding a date
range or an account id in a pagination block, it belongs elsewhere.

## `offset`

Fixed-size pages addressed by an integer offset. `offset.increment_by` defaults
to the effective limit. Prefer leaving it out: the contract accepts it, but a
non-default step is not reliably honoured, so a provider that counts in
something other than records may not paginate as declared — verify before
depending on it.

```json
{
  "type": "offset",
  "offset": { "param": "offset", "initial": 0 },
  "limit": { "param": "limit", "default": { "ref": "runtime.batch_size" }, "max": 100 },
  "stop_when": { "empty": { "ref": "response.body.data" } }
}
```

## `page`

Pages addressed by a page number. `page.increment_by` defaults to 1; `initial`
is usually 1, but some providers are 0-based — check.

## `cursor`

Server returns an opaque token in each response; the next request passes it
back. **Omit the cursor param on the first request** — there is no token yet;
the runtime sends it only from page two onward.

```json
{
  "type": "cursor",
  "cursor": { "param": "starting_after", "next_cursor": { "ref": "response.body.next_cursor" } },
  "limit": { "param": "limit", "default": { "ref": "runtime.batch_size" }, "max": 100 },
  "stop_when": { "missing": { "ref": "response.body.next_cursor" } }
}
```

## `link`

The next-page URL comes from the response. `link.next_url` resolves to that URL
and **replaces the entire request URL**, so it must resolve to a bare,
**absolute** URL — a relative one cannot be followed. Only the first request is
built from `path` + params.

Prefer a body field that already holds the bare URL:

```json
{
  "type": "link",
  "link": { "next_url": { "ref": "response.body.links.next" } },
  "stop_when": { "missing": { "ref": "response.body.links.next" } }
}
```

A raw `Link:` header is **not** directly usable — its value is
`<https://…>; rel="next"`, angle brackets and rel-parameters included, not a
bare URL. Nothing validates this, so pointing `next_url` at
`response.headers.link` produces a request to a malformed URL at runtime. When
the provider only offers the header, confirm the response exposes a parsed form
before choosing `link`.

## `keyset`

Advance from the last record's ordering key (e.g. `since_id`). `keyset` names
the request `param` and the `order_by_field` — the dotted record path the
runtime reads the next key from. Requires the response records to be ordered by
that field.

**`order_by_field` is not a `cursor_field`.** It is pagination's ordering key
within one result set; replication's `cursor_field` is the incremental
watermark across runs. They are often the same field and still mean different
things — declaring one does not imply the other. Dotted paths preserve the
response's casing, and a literal dot in a field name cannot be addressed.

Omit `initial` entirely for the first page — never write `null`.

```json
{
  "type": "keyset",
  "keyset": { "param": "since_id", "order_by_field": "id" },
  "limit": { "param": "limit", "default": { "ref": "runtime.batch_size" }, "max": 100 },
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
