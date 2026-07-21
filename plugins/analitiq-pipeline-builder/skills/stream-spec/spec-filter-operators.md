# Filter operator vocabularies

Filters declared in `source.filters[]` draw from a closed, per-scope operator
vocabulary. The contract enforces it: `operator` is a closed Literal
(`analitiq.contracts.stream.FilterOperator`), and the operator must belong to the
**source scope's** vocabulary — a database operator on an API source, or the
reverse, fails local validation with `[ADV-STRM-012]`.

<!-- BEGIN GENERATED: filter-operators -->
| Availability | Operators |
|---|---|
| Both scopes | `eq`, `gt`, `gte`, `in`, `lt`, `lte`, `neq`, `not_in` |
| `scope: "connection"` (database) only | `ilike`, `is_not_null`, `is_null`, `like` |
| `scope: "connector"` (API) only | `contains`, `ends_with`, `starts_with` |

`is_null`, `is_not_null` are unary — they must omit `value`; every other operator requires it.
<!-- END GENERATED: filter-operators -->

## How filters combine

Multiple entries in `source.filters[]` combine with an implicit **`AND`** — every
filter must hold for a record to be read. There is no `or`, no `not`, and no
nesting: complex boolean grouping is deliberately out of the contract's scope.
When a user asks for one, do not attempt to encode it (a disjunction is not
expressible as a list of `in` values in general). Say the contract cannot express
it and offer the alternatives that exist: narrow the filter set, filter
downstream, or ask the connector to expose a suitable parameter.

## Authoring notes

- Inclusivity is carried **by the operator**, never by a separate flag: `gte` and
  `lte` are inclusive, `gt` and `lt` are exclusive. There is no `inclusive`
  field, so a range that must include its boundary value has to be authored with
  the inclusive operator.
- `like` / `ilike` accept SQL wildcard syntax in `value` (`%`, `_`). The engine
  routes these to the dialect's pattern operator.
- All non-unary operators include `value` matching the column's data type — or,
  for `in` / `not_in`, an array of such values.
- `field` references a column (database) or a parameter key (API).

## What the local validator still cannot check

Field-existence is **not** resolved locally — the validator does not read filter
fields against endpoint files, so a typo in `field` passes here and fails
server-side at save time. Read the field name back to the user rather than
guessing it.

Membership in the contract vocabulary is also not a promise of executability. A
database dialect **may reject** an operator it cannot render safely for a given
column or type, even though the operator is in the set above. Local validation
proves the operator is *authorable*, never that the target database will run it.

For an API source, the endpoint document narrows the vocabulary further. A read
parameter is stream-filterable exactly when it declares `operators` **and**
carries no `controlled_by` — the two together are the test:

- `operators` present, no `controlled_by` → filterable; the declared list is the
  subset that parameter accepts.
- `controlled_by: "pagination"` or `controlled_by: "replication"` → runtime-owned,
  never stream-owned. The runtime-side validator rejects a filter that targets
  one, and such a parameter carries no `operators` to draw from anyway.
- no `operators` → not filterable, whatever else the parameter declares.

The plugin can only tell that an operator is in the API set; the registry
validates it against the per-parameter subset on save.
