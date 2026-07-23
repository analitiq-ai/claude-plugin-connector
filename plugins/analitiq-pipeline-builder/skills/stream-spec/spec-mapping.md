# `mapping` block

`mapping` is **optional**. Omit it (or pass `null`) for the default
pass-through mapping: every source field is mapped 1:1 to a destination
field with the same name and the type the registry derives from the
shared canonical-type vocabulary.

When you do author it, the shape is **assignments-only**. The registry
computes `source_to_generic`, `generic_to_destination`, and the mapping hashes.
**The plugin must not author those fields** — they are not on the authored model
at all, so a client-supplied value is rejected outright.

`mapping` is also the *only* stream-owned place where field assignment and type
coercion are declared. If a transformation is expressible here, it belongs here;
if it is not, it belongs to the connector or the destination endpoint, never to a
side channel invented on the stream.

<!-- BEGIN GENERATED: fields-stream-mapping -->
`analitiq.contracts.stream.StreamMapping` — closed (`additionalProperties: false`); required: none

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `assignments` | no | array of Assignment | — | — |
<!-- END GENERATED: fields-stream-mapping -->

Each `assignments[]` entry (`analitiq.contracts.stream.Assignment`) pairs a
`target` with a `value`, plus an optional `validate` block (see
`spec-validation-rules.md`):

```jsonc
{
  "mapping": {
    "assignments": [
      {
        "target": {
          "path": "id",
          "arrow_type": "Utf8",
          "native_type": "uuid",
          "nullable": false
        },
        "value": {
          "expression": {"op": "get", "path": "id"}
        }
      },
      {
        "target": {"path": "tenant_id", "arrow_type": "Utf8", "nullable": false},
        "value": {
          "constant": {"arrow_type": "Utf8", "value": "acme-corp"}
        }
      }
    ]
  }
}
```

## `assignments` order is significant

`assignments[]` is a sequence, not a set. The engine applies assignments in the
order authored, so preserve the order a caller gave you and never re-sort the
array for tidiness — a reordering is a semantic change, and in edit mode it is a
diff the user did not ask for.

## `assignments[].value`

<!-- BEGIN GENERATED: fields-assignment-value -->
`analitiq.contracts.stream.AssignmentValue` — closed (`additionalProperties: false`); required: none

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `expression` | no | GetExpression \| PipeExpression \| null | `None` | — |
| `constant` | no | ConstantValue \| null | `None` | — |
<!-- END GENERATED: fields-assignment-value -->

Both members are individually optional, but exactly one must be present
(`ADV-STRM-008`):

- `expression` — one of:
  - `{"op": "get", "path": "<source field>"}` — read a source field. The default;
    it covers almost every mapping.
  - `{"op": "pipe", "args": [{"op": "get", "path": "<source field>"}, {"op": "fn", "name": "<conversion>"}, …]}` —
    a `get` seed passed through one or more `fn` conversion stages
    (`ADV-STRM-005`). An `fn` node is valid **only** inside `pipe.args`, never
    standalone. Author `pipe` only when a conversion is genuinely required;
    otherwise prefer `get`.
- `constant` — `{"arrow_type": "<fully-qualified Arrow type>", "value": <JSON value>}`.

### When a bare `get` is not enough

The engine's conversion matrix classifies each `(source type, target type)` pair.
A pair classified **`explicit`** — `Int64 → Utf8` is the canonical example — is
writable only when the assignment names the matrix's conversion `fn` in a `pipe`.
A bare `get` across such a pair is **rejected**, not silently coerced. So when
source and target Arrow types differ, check the pair before reaching for `get`:
if it is an explicit conversion, the assignment must be
`{"op": "pipe", "args": [{"op": "get", …}, {"op": "fn", "name": …}]}`. The
conversion function names are closed (`analitiq.contracts.stream.FnExpression`);
the engine's `version`/`args` node fields are deliberately not published, so
never author them.

## `assignments[].target`

<!-- BEGIN GENERATED: fields-assignment-target -->
`analitiq.contracts.stream.AssignmentTarget` — closed (`additionalProperties: false`); required: `arrow_type`, `path`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `path` | **yes** | string | — | `minLength=1` |
| `arrow_type` | **yes** | string | — | `pattern=(long; see `endpoint-spec/spec-columns.md`)` |
| `native_type` | no | string \| null | `None` | — |
| `nullable` | no | boolean | `True` | — |
| `properties` | no | map of ArrowFieldSpec \| null | `None` | — |
| `items` | no | ArrowFieldSpec \| null | `None` | — |

Carries 3 declarative cross-field `if`/`then` rule(s) — see the advisory rules for their prose.
<!-- END GENERATED: fields-assignment-target -->

### `target.path`

Must be unique within `assignments` (`ADV-STRM-002`).

`target.path` addresses the **assignment root only** — the destination field this
assignment writes. Inner structure is declared recursively by `properties` (for
an `Object` target) and `items` (for a `List` target), governed by `ADV-STRM-010`.
Child field specs are **not** separately addressable from `assignments`: you
cannot write a second assignment at `address.city` to reach inside an `address`
Object target. One assignment owns one root and declares everything beneath it.

Cross-document: each `target.path` must exist in the resolved destination
endpoint schema. Endpoint resolution is server-side at save time; the local
validator does **not** check this.

## `arrow_type` vocabulary

`constant.arrow_type` is required too, and every `arrow_type` — target or
constant — must be **fully-qualified**. The vocabulary is owned
by `analitiq.contracts.endpoints.ARROW_TYPE_PATTERN` — the same pattern the
endpoint columns use, generated from the engine-published grammar manifest —
so bare parameterized forms (`Timestamp`, `Decimal128`, `Time64`, `Duration`,
`FixedSizeBinary`, …) are rejected. See
[`endpoint-spec/spec-columns.md`](../endpoint-spec/spec-columns.md) for the
canonical walkthrough: the two shapes (bare / `( )`), the authored-shape
container markers, unit identifiers and timezone forms apply identically here.

Container shape is not free-form either: `ADV-STRM-006`, `ADV-STRM-007` and
`ADV-STRM-010` tie `arrow_type` to whether the field declares `properties`,
`items`, or neither, and tie a constant's JSON kind to its declared type.

Stick to what the destination endpoint's `columns[]` declares — if the
destination column is `Decimal128(12, 2)`, the assignment's `target.arrow_type`
must match exactly, precision and scale included.
