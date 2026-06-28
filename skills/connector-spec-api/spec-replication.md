# Replication (incremental sync)

Authoring `operations.read.replication` for endpoints that support
incremental sync.

The exact shape — property names, required keys, and the closed enums — is
owned by the published api-endpoint contract, not by this page. Author
against it and let the validator check you:

- `#/$defs/Replication` — the block itself (`supported_methods` +
  `cursor_mappings`, both required).
- `#/$defs/SingleCursorMapping` — a cursor filtered by one provider param.
- `#/$defs/WindowCursorMapping` — a cursor filtered by a start/end param pair.

(all in `https://schemas.analitiq.ai/api-endpoint/latest.json`). This page
covers only the authoring decisions the schema can't express: which
mapping variant fits a provider, and when to skip replication entirely.
The examples below are validated against that schema in CI
(`tests/connector_validator/test_spec_doc_examples.py`), so they stay in
step with the contract instead of drifting from it.

## What a replication block declares

A cursor mapping ties together two halves of incremental sync:

- a **cursor field** — the dotted record path whose value is the
  per-record watermark (`updated_at`, `meta.changed`); and
- the **request param(s)** the runtime sets on the next run to fetch only
  records past that watermark, plus the comparison `operator`.

Pick the variant by how the provider's filter works.

## Single-param cursor (most providers)

The provider takes one open-ended "changed since X" filter. Use a
`SingleCursorMapping`:

```json
{
  "replication": {
    "supported_methods": ["full_refresh", "incremental"],
    "cursor_mappings": [
      {
        "cursor_field": "updated_at",
        "param": "updated_since",
        "operator": "gte",
        "format": "date-time"
      }
    ]
  }
}
```

- `cursor_field` reads the watermark from each record (dotted path).
- `param` is the request param the runtime sets on the next run.
- `operator` relates them (`gte` → "at or after the stored watermark").
- `format` is optional — set it only when the param expects a specific
  encoding of the value (e.g. `epoch_seconds`); omit it when the field is
  already in the param's native form.

## Bounded-window cursor

The provider won't take an open "since" filter — it requires a closed
window with separate start and end params (e.g. `from`/`to`). Use a
`WindowCursorMapping`:

```json
{
  "replication": {
    "supported_methods": ["full_refresh", "incremental"],
    "cursor_mappings": [
      {
        "cursor_field": "created",
        "start_param": "created_after",
        "start_operator": "gte",
        "end_param": "created_before",
        "end_operator": "lt",
        "format": "date"
      }
    ]
  }
}
```

Reach for the window variant only when the provider *requires* both
bounds. If an open "since" filter works, the single-param variant is
simpler and spares the runtime from computing an upper bound.

## Supported methods

`supported_methods` lists what the endpoint can do — `full_refresh`,
`incremental`, or both. List `incremental` only when a cursor mapping
actually backs it; an endpoint with no cursorable field is `full_refresh`
only and should omit `replication` (see below). There is no separate
"default method" key — the block is just these two fields.

## When to omit

Omit `replication` entirely when:

- The resource has no cursorable field (no `updated_at`, no monotonic id).
- The endpoint is a small static lookup (countries, currencies).
- The provider doesn't expose a filter param for the cursor field.

## Common pitfalls

- Don't fabricate a cursor field. If `updated_at` is response-side only
  (no filter param), there's no incremental sync to declare.
- `cursor_field` reads per-record. It's a dotted path into each record
  (`updated_at`, `meta.changed`), not a pointer into the page envelope and
  not a value expression.
- `incremental` in `supported_methods` is only valid with a matching
  cursor mapping — listing it without one is a dead declaration.
- Don't add a `type-map` field to `cursor_mappings`; canonical types are
  resolved through the standalone `type-map-read.json` file shipped
  alongside the connector.
