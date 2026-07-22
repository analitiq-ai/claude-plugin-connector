# `columns` block

A non-empty array of:

<!-- BEGIN GENERATED: fields-column -->
`analitiq.contracts.endpoints.Column` — closed (`additionalProperties: false`); required: `arrow_type`, `name`, `native_type`

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `name` | **yes** | string | — | `minLength=1` |
| `native_type` | **yes** | string | — | `minLength=1` |
| `arrow_type` | **yes** | string | — | `pattern=(long; see `endpoint-spec/spec-columns.md`)` |
| `nullable` | no | boolean \| null | `None` | — |
| `default` | no | any \| null | `None` | — |
| `comment` | no | string \| null | `None` | — |
| `ordinal_position` | no | integer \| null | `None` | `min=1` |
| `properties` | no | map of ColumnFieldSpec \| null | `None` | — |
| `items` | no | ColumnFieldSpec \| null | `None` | — |

Carries 3 declarative cross-field `if`/`then` rule(s) — see the advisory rules for their prose.
<!-- END GENERATED: fields-column -->

## `name`

Verbatim from introspection.

## `native_type`

The provider-native type label, e.g.:

| Dialect | examples |
|---|---|
| PostgreSQL | `uuid`, `text`, `integer`, `numeric(12,2)`, `timestamp with time zone`, `jsonb` |
| MySQL | `BIGINT UNSIGNED`, `VARCHAR(255)`, `DATETIME`, `JSON` |
| Snowflake | `NUMBER(38,0)`, `VARCHAR(16777216)`, `TIMESTAMP_TZ` |
| BigQuery | `STRING`, `INT64`, `STRUCT<…>`, `TIMESTAMP`, `BIGNUMERIC` |
| MongoDB | `BSON.ObjectId`, `BSON.Date`, `BSON.Document` |

Use `"unknown"` as a sentinel when the engine doesn't expose a type.

## `arrow_type`

Fully-qualified Apache Arrow canonical type string. Base names are
PascalCase from `arrow/format/Schema.fbs`.

<!-- BEGIN GENERATED: arrow-types -->
`arrow_type` is validated by one published regex, `analitiq.contracts.endpoints.ARROW_TYPE_PATTERN`. Its top-level alternatives fall into three families.

**Plain names** — write them exactly as shown:

`Null`, `Boolean`, `Int8`, `Int16`, `Int32`, `Int64`, `UInt8`, `UInt16`, `UInt32`, `UInt64`, `Float16`, `Float32`, `Float64`, `Utf8`, `LargeUtf8`, `Binary`, `LargeBinary`, `Date32`, `Date64`, `Object`, `List`, `Json`

**Parameterized** — the parameter is part of the type and is *not* optional; a bare name here is rejected:

- `FixedSizeBinary\([1-9][0-9]*\)`
- `Time32\((?:SECOND|MILLISECOND)\)`
- `Time64\((?:MICROSECOND|NANOSECOND)\)`
- `Timestamp\((?:SECOND|MILLISECOND|MICROSECOND|NANOSECOND)(?:\s*,\s*(?:null|[A-Za-z_][A-Za-z0-9_/\-]*|Etc/GMT[+\-][0-9]{1,2}|[+\-](?:0[0-9]|1[0-4]):[0-5][0-9]))?\)`
- `Duration\((?:SECOND|MILLISECOND|MICROSECOND|NANOSECOND)\)`
- `Interval\((?:YEAR_MONTH|DAY_TIME|MONTH_DAY_NANO)\)`
- `Decimal128\((?:[1-9]|[12][0-9]|3[0-8])\s*,\s*-?[0-9]+\)`
- `Decimal256\((?:[1-9]|[1-6][0-9]|7[0-6])\s*,\s*-?[0-9]+\)`

**Containers** — the inner type is itself an `arrow_type`:

- `List<.+>`
- `LargeList<.+>`
- `FixedSizeList<.+>\[[1-9][0-9]*\]`
- `Struct<.+>`
- `Map<.+,\s*.+>`
- `SparseUnion<.+>`
- `DenseUnion<.+>`
- `Dictionary<.+,\s*.+>`
- `RunEndEncoded<.+,\s*.+>`
<!-- END GENERATED: arrow-types -->

Units are the literal Flatbuffers enum identifiers, uppercase, and each type
admits only the units its alternative above lists — `Time32(MICROSECOND)` and
`Time64(SECOND)` are rejected.

### `Timestamp` timezone

Optional second argument. Three valid forms:

- **Omit the slot** — naive timestamp, no implied zone: `Timestamp(MICROSECOND)`.
- **Literal `null`** — explicit naive marker: `Timestamp(MICROSECOND, null)`
  (distinct from omitting; some readers treat it as "zone is unknown
  rather than absent").
- **An actual zone** — IANA name (`UTC`, `Europe/Berlin`), `Etc/GMT±N`,
  or a fixed `±HH:MM` offset: `Timestamp(MICROSECOND, +05:30)`.

### Canonical examples

```
Utf8
Int64
Boolean
Date32
Decimal128(38, 9)
Decimal256(76, 0)
Timestamp(MICROSECOND)
Timestamp(MICROSECOND, UTC)
Timestamp(MILLISECOND, +05:30)
Time32(SECOND)
Time64(NANOSECOND)
Duration(MICROSECOND)
Interval(YEAR_MONTH)
FixedSizeBinary(16)
List<Int64>
LargeList<Utf8>
FixedSizeList<Int64>[8]
Struct<id:Int64, name:Utf8>
Map<Utf8, Int64>
Dictionary<Int32, Utf8>
```

### Mapping guidance

| Provider native | Typical fully-qualified `arrow_type` |
|---|---|
| `uuid`, `text`, `varchar(n)`, `char(n)` | `Utf8` |
| `smallint` / `integer` / `bigint` | `Int16` / `Int32` / `Int64` |
| `BIGINT UNSIGNED` (MySQL) | `UInt64` |
| `real` / `double precision` | `Float32` / `Float64` |
| `boolean` / `BOOL` | `Boolean` |
| `numeric(p,s)` / `DECIMAL(p,s)` | `Decimal128(p, s)` (use `Decimal256` when `p > 38`; max precision is 76) |
| `date` | `Date32` |
| `timestamp` / `DATETIME` (no zone) | `Timestamp(MICROSECOND)` |
| `timestamp with time zone` / `TIMESTAMP_TZ` / BigQuery `TIMESTAMP` | `Timestamp(MICROSECOND, UTC)` |
| BSON `Date` / JavaScript `Date` (ms epoch) | `Timestamp(MILLISECOND, UTC)` |
| `time` | `Time64(MICROSECOND)` |
| `bytea` / `BLOB` | `Binary` |
| arrays | `List<…>` |
| record / composite / STRUCT | `Struct<field:Type, …>` |

For schemaless or opaque container types (e.g. MongoDB `BSON.Document`,
PostgreSQL `jsonb` you do not introspect), prefer `Utf8` (JSON-as-text)
or `Binary` over guessing a `Struct<…>` field list.

### Inner grammar for `Struct<…>` / `Map<…>`

`ARROW_TYPE_PATTERN` enforces only that the angle brackets are present and
non-empty (`Struct<.+>`, `Map<.+, .+>`); the inner grammar shown above
(`field:Type, …` for structs; `key, value` for maps) is the recommended
convention but is **not** regex-enforced. Stay consistent with the
canonical examples block so downstream consumers can parse the inner
shape unambiguously.

## `nullable`

`true` when the database reports the column as nullable, else `false`. Omit when
the dialect doesn't expose this (e.g., schemaless engines).

## `default`

The parsed default expression if reasonable, else `null`. The runtime treats this
as advisory — actual default behavior is dialect-owned.

## `comment`

Provider-attached comment (PostgreSQL `COMMENT ON COLUMN`, MySQL `COMMENT`,
etc.). Forwarded verbatim. `null` when absent.

## `ordinal_position`

Canonicalizes column order for hashing. Omit for schemaless engines (MongoDB).

## Uniqueness

The contract model enforces three advisory rules over this array:

<!-- BEGIN GENERATED: advisory-endpoint -->
| Rule | Constraint |
|---|---|
| `ADV-DBEP-001` | columns[].name must be unique. |
| `ADV-DBEP-002` | columns[].ordinal_position must be unique where present. |
| `ADV-DBEP-003` | primary_keys must reference declared columns[].name. |
<!-- END GENERATED: advisory-endpoint -->
