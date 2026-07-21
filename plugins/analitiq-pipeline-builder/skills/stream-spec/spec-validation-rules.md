# Assignment-level validation

Each `mapping.assignments[]` entry may carry an optional `validate`
block (`analitiq.contracts.stream.Validation`, whose members are
`analitiq.contracts.stream.ValidationRule` and
`analitiq.contracts.stream.StreamValidationErrorHandling`):

```jsonc
{
  "target": {"path": "email", "arrow_type": "Utf8", "nullable": false},
  "value": {"expression": {"op": "get", "path": "email"}},
  "validate": {
    "rules": [
      {"type": "required", "field": "email"},
      {"type": "pattern", "field": "email", "value": "^[^@]+@[^@]+$", "message": "Invalid email format."}
    ],
    "error_handling": {
      "strategy": "dlq",
      "max_retries": 0
    }
  }
}
```

This is **stream record validation** — one of two unrelated validation-rule
families in the platform. The other is connection **input** validation, which
lives on the connector's `connection_contract` and is connection/connector-owned;
it validates configuration a user typed, not records the pipeline moved. Never
carry a rule from one family into the other, and never expect this block to
validate connection inputs.

Validation runs on assignment **output**: the rules see the value the assignment
produced, after any `pipe`/`fn` conversion, and before the destination write. A
rule that names a source field name rather than the mapped output path is
therefore checking nothing.

## `rules[]`

<!-- BEGIN GENERATED: fields-validation-rule -->
`analitiq.contracts.stream.ValidationRule` — closed (`additionalProperties: false`); required: `field`, `type`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `type` | **yes** | 'required' \| 'not_null' \| 'min_length' \| 'max_length' \| 'pattern' \| 'range' \| 'in_list' | — | — |
| `field` | **yes** | string | — | `minLength=1` |
| `value` | no | any | `None` | — |
| `message` | no | string \| null | `None` | — |

Carries 1 declarative cross-field `if`/`then` rule(s) — see the advisory rules for their prose.
<!-- END GENERATED: fields-validation-rule -->

### `rules[].type`

`ADV-STRM-009` settles which members take a `value` and which must omit it. What
neither it nor the table states is what each member *means*:

- `required` — the field must be present.
- `not_null` — the field must be present and non-null.
- `min_length` / `max_length` — string length bound (integer `value`).
- `pattern` — the value matches the regex in `value`.
- `range` — the numeric value falls inside the `{min, max}` in `value`.
- `in_list` — the value is one of the array in `value`.

### `rules[].field`

Must match an `assignments[].target.path` in the same mapping — a `field` that
resolves to no mapped output is a silent typo. Endpoint/field resolution happens
server-side at save time.

## `error_handling`

`StreamValidationErrorHandling` is a mirror of `pipeline.runtime.error_handling`
(`analitiq.contracts.pipelines.config.ErrorHandling`) — the same strategy
vocabulary, the same retry fields, the same retry/delay gating rule. Read the
members and bounds off those models.

Its scope, however, is narrow, and that is the whole point of the block: **it
applies only to failures raised by the validation rules alongside it**, in that
one assignment. It is not a general per-assignment error policy and it does not
see write failures, source failures or conversion failures.

For those validation-rule failures the stream block **wins outright**: the
pipeline's `runtime.error_handling` neither caps it nor replaces it. A stream
that declares `strategy: "skip"` skips its failing records even under a pipeline
configured to `fail`. Conversely, when a stream declares no
`validate.error_handling`, its validation failures fall to the pipeline default.

The two levels do share destinations. `strategy: "dlq"` on the stream and
`strategy: "dlq"` on the pipeline route to the **same** runtime dead-letter
queue, and the two `"skip"` strategies produce the same category of skipped
record. So the choice between them is about *which failures* a policy governs,
never about where the records end up.

Per-assignment validation rules generally use `strategy: dlq` with
`max_retries: 0` — there's no point retrying a validation failure
against the same record.

## When to use

Use sparingly. Validation rules are useful for **defensive** checks
against malformed source data when the destination is strict (e.g.,
a column has a NOT NULL constraint that the source might violate).
For routine type coercion, rely on the registry's type-map machinery
rather than authoring `validate` blocks.
