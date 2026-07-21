# Type maps

How to author the standalone type-map files that ship alongside every
connector. Type maps connect provider-native type labels and Apache
Arrow canonical types, in two directions:

- **Read map** (`type-map-read.json`) — native → Arrow. Required for
  every connector (API and DB). For databases it maps native column
  types (`BIGINT`, `NUMERIC(10,2)`); for API connectors it maps the
  JSON Schema `format`/`type` strings used as endpoint-field natives.
- **Write map** (`type-map-write.json`) — Arrow → native. **Required
  for `kind: database`, forbidden for `kind: api`.** It is the
  connector's declarative DDL vocabulary: every transport (SQLAlchemy
  DDL, ADBC DDL, control-plane create_table) renders column types
  through `dialect.render_column_type`, whose default implementation is
  this map. Connectors must NOT ship Python type-rendering tables.

## On-disk location

Both files are **standalone** siblings of `connector.json`:

```
{connector_id}/definition/type-map-read.json
{connector_id}/definition/type-map-write.json   # database only
```

The read map validates against
`https://schemas.analitiq.ai/type-map-read/latest.json`. The write map
shares the same three-key rule shape but inverts the direction
(`canonical` matches, `native` renders) and validates against its own
published schema, `https://schemas.analitiq.ai/type-map-write/latest.json`;
the validator derives the direction from the filename and runs the full
contract-model + semantic pass on each. Neither map is ever embedded inside
`connector.json` or any endpoint document. Each present file must be
**non-empty** — an empty array is rejected.

The pre-split filename `type-map.json` is dead: the engine never reads
it and the validator rejects it with a migration finding.

## File shape

Each file is a top-level JSON array of rule objects. Order is
significant: **first match wins** during resolution. Each rule object
has exactly three required keys and no others — but which key is the
*matcher* and which is *rendered* depends on the direction:

| Key | Read map (`type-map-read.json`) | Write map (`type-map-write.json`) |
|---|---|---|
| `match` | `"exact"` or `"regex"` — how the matcher is compared. | Same. |
| `native` | **Matcher.** Literal label (`exact`) or ECMA-262 regex (`regex`). | **Rendered.** The native DDL emitted for a matching canonical; may carry `${name}` substitutions on `regex` rules. |
| `canonical` | **Rendered.** Literal Arrow type, or (on `regex` rules) a template with `${name}` placeholders. | **Matcher.** Literal Arrow type (`exact`) or ECMA-262 regex over the canonical string (`regex`). |

Matching uses full-string semantics (Python `re.fullmatch`), so leading
`^` and trailing `$` are harmless but redundant — keep them for
readability when the pattern would otherwise look ambiguous.

## Uppercase rule (read maps)

Read-map rules are matched against **UPPERCASED, whitespace-collapsed**
native strings — the engine normalizes the native label before
matching. Consequences:

- **Author `regex` patterns uppercase** (`^VARCHAR\(\d+\)$`, not
  `^varchar\(\d+\)$`). A lowercase literal in the pattern can never
  match; the validator warns on it.
- `exact` natives are normalized automatically — authored case doesn't
  matter, but uppercase is the convention.
- **Named capture group names stay lowercase** (`(?<precision>…)`) —
  only the matched text is normalized, not the group names.

Write-map matchers run against PascalCase canonical strings verbatim —
no normalization, case is significant.

## `${name}` substitution in regex rules

When a `regex` rule's rendered side carries `${name}` placeholders,
every placeholder must be backed by a matching **named capture group**
in the matcher side. The contract uses ECMA-262 syntax for capture
groups — `(?<name>…)` — translated to Python's `(?P<name>…)` under the
hood at validation time. Authors write the ECMA-262 form.

- Read map: placeholders in `canonical`, captures in `native` —
  `native: "^NUMERIC\\((?<precision>[0-9]+),\\s*(?<scale>[0-9]+)\\)$"`,
  `canonical: "Decimal128(${precision}, ${scale})"`.
- Write map: placeholders in `native`, captures in `canonical` —
  `canonical: "^Decimal(128|256)\\((?<p>\\d+),\\s*(?<s>\\d+)\\)$"`,
  `native: "NUMERIC(${p}, ${s})"`.

Placeholders are only legal in **parameter positions** of parameterized
types (`Decimal128(${precision}, ${scale})`, `FixedSizeBinary(${n})` on
the read side; `NUMERIC(${p}, ${s})`, `VARCHAR(${len})` and similar on
the write side). Templated renders are only legal on `regex` rules;
`exact` rules must emit a fully-resolved literal on the rendered side.
(Timestamp precision is **not** a `${}` case — Arrow's unit is a
symbolic enum, not a digit; match on the native's digit count and ladder
it to a unit instead. See "Database coverage → Read map".)

## Schemaless / JSON-shaped natives

A schemaless or structured-container native — `JSON`, `JSONB`, `VARIANT`,
`OBJECT`, `ARRAY`, `MAP`, `STRUCT`, a parameterized container like
`array<object>`, or a SQL array suffix like `integer[]` — **must** map to a
**container canonical** (`Json`, or a typed `List` / `Struct` / `Map`),
**never a scalar** like `Utf8`. The canonical is a *claim about the shape* of
the data: `Utf8` asserts an opaque string and throws the structure away, so it
is wrong for a JSON / array / struct column even when the driver happens to
hand the value over as text on the wire. The `type-map-rule` validator
**enforces** this — a schemaless / container native resolving to a scalar
canonical is an error.

| Native (read) | Canonical |
|---|---|
| `JSON`, `JSONB` (Postgres, MySQL/MariaDB) | `Json` |
| `VARIANT`, `OBJECT`, `ARRAY`, `MAP` (Snowflake) | `Json` |
| `array`, `object` (document stores) | `Json` |
| `integer[]` and other `…[]` array suffixes | `Json` (or a typed `List<…>`) |

`XML` is structured text, not a JSON/array/struct container, so it maps to
`Utf8` — the rule covers JSON / array / struct / map containers only.

On the write side the `Json` canonical renders the system's JSON column
type (`Json` → `JSONB` for postgres, `JSON` for MySQL, `VARIANT` for
Snowflake), so the type round-trips.

The endpoint-only shape markers `Object` and `List` (which require
sibling `properties` / `items` to declare the inner shape) **never**
appear as a type-map `canonical`. The endpoint walker accepts a field
typed `Object` or `List` as a valid narrowing of a `Json` read-map
rule; the validator does not treat that as a mismatch.

## Non-obvious natives (derive, don't guess)

When researching a new system's natives, these are the calls that aren't
mechanical — the same judgment transfers across providers:

- **Semi-structured / container** (`JSON`, `JSONB`, `VARIANT`, `OBJECT`,
  `ARRAY`, `MAP`, `STRUCT`, `…[]`) → a container canonical (`Json`), never a
  scalar (enforced — see "Schemaless / JSON-shaped natives").
- **Opaque scalar types with no Arrow equivalent** (`INTERVAL`, `MONEY`,
  network types `INET`/`CIDR`/`MACADDR`, `UUID`, `ENUM(...)`, `XML`) →
  `Utf8`. They are atomic strings on the wire; don't invent a numeric/Decimal
  canonical.
- **Zoned time-of-day** (`TIME WITH TIME ZONE` / `TIMETZ`) →
  `Time32`/`Time64` (unit per the precision ladder; the zone is dropped —
  a bare time-of-day carries no instant). Contrast
  `TIMESTAMP WITH TIME ZONE` → `Timestamp(<unit>, UTC)`.
- **Bare vs zoned timestamp**: choose the tz-aware canonical only when the
  native (or, for APIs, the sample value) actually carries a zone.

## API coverage (read map)

For API connectors, the validator walks every endpoint file under
`{connector_id}/definition/endpoints/`, collects every `(native_type,
arrow_type)` pair from typed fields, and asserts each one resolves
through `type-map-read.json` (after normalizing the native). Resolution
renders the matched rule's `canonical` (substituting any `${name}`
captures from the regex match) and compares the result to the endpoint
field's `arrow_type`. A pair that does not resolve is a validation
error.

`Object` / `List` endpoint markers are accepted narrowings of `Json` —
an endpoint field with `arrow_type: "Object"` paired with a native that
maps to `Json` is **not** a mismatch.

Common API natives:

| Native | Source | Typical canonical |
|---|---|---|
| `uuid` | `{"type":"string", "format":"uuid"}` | `Utf8` |
| `date-time` | `{"type":"string", "format":"date-time"}` | `Timestamp(MICROSECOND)` **or** `Timestamp(MICROSECOND, UTC)` — per the sample value's zone (see "Bare vs zoned timestamp" above) |
| `date` | `{"type":"string", "format":"date"}` | `Date32` |
| `email` / `uri` | `{"type":"string", "format":"…"}` | `Utf8` |
| `string` | `{"type":"string"}` | `Utf8` |
| `integer` | `{"type":"integer"}` | `Int64` |
| `int32` / `int64` | `{"type":"integer", "format":"…"}` | `Int32` / `Int64` |
| `number` | `{"type":"number"}` | `Float64` |
| `boolean` | `{"type":"boolean"}` | `Boolean` |
| `object` (schemaless) | `{"type":"object"}` with no `properties` | `Json` |
| `array` (schemaless) | `{"type":"array"}` with no `items` | `Json` |

API connectors ship **no write map** — the write direction is a
database-package concept (DDL rendering).

## Database coverage

**Read map:** ship the documented provider native vocabulary.

- For OLTP databases (PostgreSQL, MySQL), include the full documented
  native vocabulary.
- For warehouses and document stores (Snowflake, MongoDB), restrict to
  the researched, documented list — provider docs are authoritative.
- Do NOT ship a wildcard fallback rule. If a native type isn't covered,
  let the runtime hard-error so the gap is visible.
- Use `Utf8` (not `String`) for Arrow's UTF-8 string type — `String` is
  not a member of the published Arrow vocabulary.
- Capture declared precision on parameterized natives — never collapse it
  to a constant. The fixed default belongs only on the unparameterized
  fallback rule.
  - **Decimal:** regex `(precision, scale)` into named captures and route
    by Arrow width — precision ≤ 38 → `Decimal128(${precision},
    ${scale})`, 39–76 → `Decimal256(...)`. A precision-only declaration
    (`NUMERIC(p)`, implicit scale 0) needs its own tier rendering
    `Decimal{128,256}(${precision}, 0)`. Precision > 76 exceeds Arrow, so
    leave it uncovered (visible hard-error, per the no-wildcard rule
    above); the bare/unparameterized native takes the fixed default.
  - **Timestamp/time:** the native carries a fractional-second *digit
    count*, but Arrow's unit is a symbolic enum — so ladder the digit
    count to the smallest unit that holds it exactly: `(0)`→`SECOND`,
    `(1–3)`→`MILLISECOND`, `(4–6)`→`MICROSECOND`, `(7–9)`→`NANOSECOND`,
    with the bare form rendering the system's documented default unit.
    A single flat unit truncates any system finer than it — this is a
    per-system ladder, not a constant. Time-of-day picks the Arrow width
    off the same unit: `Time32(SECOND|MILLISECOND)` for coarse,
    `Time64(MICROSECOND|NANOSECOND)` for fine.

**Write map:** cover the **full canonical vocabulary** — Boolean,
Int8–64, UInt8–64, Float16/32/64, Decimal (regex with `${p}`/`${s}`
captures), Utf8/LargeUtf8, Json, Binary/LargeBinary/FixedSizeBinary,
Date32/64, Time, Timestamp bare + tz variants. The validator probes
every family and warns on gaps. A deliberate gap is legitimate only
when the connector's dialect takes over that family's rendering via a
`render_column_type` override (BigQuery ships no Decimal rule because
NUMERIC/BIGNUMERIC selection needs precision-range arithmetic rules
cannot express) — never as a way to cut scope.

Mind precision survival on the write side: MySQL's write map renders
`DATETIME(6)` / `TIME(6)` so microseconds survive the round trip — a
bare `DATETIME` silently truncates.

## Canonical types

Arrow canonical types are fully-qualified PascalCase strings from the
shared Arrow vocabulary — bare names where the type has no parameters
(`Int32`, `Int64`, `Float64`, `Utf8`, `Boolean`, `Binary`, `Date32`),
parens for parameterized scalars (`Decimal128(p, s)`,
`Decimal256(p, s)`, `Timestamp(MICROSECOND, UTC)`, `Time64(MICROSECOND)`,
`FixedSizeBinary(16)`), and angle brackets for nested types
(`List<Int64>`, `Struct<id:Int64, name:Utf8>`, `Map<Utf8, Int64>`).

The full vocabulary lives in
`docs/schema-contracts/shared/canonical-types.json`.

For parameterized canonicals whose database native carries an implicit
default, encode the default explicitly:

- Snowflake `TIMESTAMP_NTZ` → `Timestamp(NANOSECOND)` (precision 9).
- Snowflake `NUMBER` → `Decimal128(38, 0)`.
- MongoDB `date` → `Timestamp(MILLISECOND, UTC)` (ms epoch UTC).
- MongoDB `decimal` → `Decimal128(34, 0)` (IEEE 754 decimal128).

Do NOT emit a bare parameterized name from an `exact` rule
(`{"match": "exact", "native": "TIMESTAMP_NTZ", "canonical": "Timestamp"}`
is wrong — `Timestamp` requires a unit).

## Worked example: Postgres (read)

Excerpt from the reference read map — uppercase patterns, the
width-tiered `NUMERIC`/`DECIMAL` capture (`Decimal128` ≤ 38, `Decimal256`
above, over a bare fallback; the on-disk file adds precision-only
`(p)`→scale-0 tiers, trimmed here), the timestamp precision ladder (digit
count → Arrow unit, here instantiated to Postgres's 0–6 range), and a
`JSONB` column mapped to the `Json` container canonical (not a scalar):

```json
[
  { "match": "exact", "native": "SMALLINT",                                    "canonical": "Int16" },
  { "match": "exact", "native": "INTEGER",                                     "canonical": "Int32" },
  { "match": "exact", "native": "BIGINT",                                      "canonical": "Int64" },
  { "match": "exact", "native": "TEXT",                                        "canonical": "Utf8" },
  { "match": "exact", "native": "JSONB",                                       "canonical": "Json" },
  { "match": "exact", "native": "DATE",                                        "canonical": "Date32" },

  { "match": "regex", "native": "^(?:NUMERIC|DECIMAL)\\((?<precision>[1-9]|[12]\\d|3[0-8]),\\s*(?<scale>\\d+)\\)$", "canonical": "Decimal128(${precision}, ${scale})" },
  { "match": "regex", "native": "^(?:NUMERIC|DECIMAL)\\((?<precision>39|[4-6]\\d|7[0-6]),\\s*(?<scale>\\d+)\\)$",   "canonical": "Decimal256(${precision}, ${scale})" },
  { "match": "regex", "native": "^(?:NUMERIC|DECIMAL)$",                                                           "canonical": "Decimal128(38, 9)" },

  { "match": "regex", "native": "^TIMESTAMP\\(0\\)( WITHOUT TIME ZONE)?$",        "canonical": "Timestamp(SECOND)" },
  { "match": "regex", "native": "^TIMESTAMP\\([1-3]\\)( WITHOUT TIME ZONE)?$",    "canonical": "Timestamp(MILLISECOND)" },
  { "match": "regex", "native": "^TIMESTAMP(\\([4-6]\\))?( WITHOUT TIME ZONE)?$", "canonical": "Timestamp(MICROSECOND)" }
]
```

## Worked example: Postgres (write)

Excerpt from the reference write map — `canonical` is the matcher (note
the regex over the canonical string with lowercase capture names), and
`native` is the rendered DDL:

```json
[
  { "match": "exact", "canonical": "Boolean",   "native": "BOOLEAN" },
  { "match": "exact", "canonical": "Int64",     "native": "BIGINT" },
  { "match": "regex", "canonical": "^Decimal(128|256)\\((?<p>\\d+),\\s*(?<s>\\d+)\\)$", "native": "NUMERIC(${p}, ${s})" },
  { "match": "exact", "canonical": "Utf8",      "native": "TEXT" },
  { "match": "exact", "canonical": "Json",      "native": "JSONB" },
  { "match": "regex", "canonical": "^FixedSizeBinary\\(\\d+\\)$",          "native": "BYTEA" },
  { "match": "regex", "canonical": "^Time(32|64)\\([A-Z]+\\)$",            "native": "TIME" },
  { "match": "regex", "canonical": "^Timestamp\\([A-Z]+\\)$",              "native": "TIMESTAMP" },
  { "match": "regex", "canonical": "^Timestamp\\([A-Z]+,\\s*UTC\\)$",      "native": "TIMESTAMPTZ" }
]
```

First-match-wins applies per file: more specific rules come **before**
broader fallbacks (the tz Timestamp rule never fires above because the
bare `^Timestamp\([A-Z]+\)$` doesn't match a two-argument canonical —
but a genuinely overlapping family rule must be ordered carefully).

## Out of scope

Connection-scoped type maps are out of scope for this plugin; see
`shared/type-maps.md` for runtime resolution rules.
