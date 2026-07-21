# Reserved (server-managed) fields per entity

Authored documents must **never** contain any of these fields. The
registry stamps them on insert/update. The published contract models are the
strict *input* variants — they forbid unknown fields, so a leaked server-managed
field fails validation.

## Pipeline

- `version`
- `org_id`
- `created_at`
- `updated_at`

`pipeline_id` is **not** reserved — it is an optional authored UUID
(see `identity-and-versioning.md`).

## Stream

- `version`
- `org_id`
- `created_at`
- `updated_at`
- `schema_hash`
- `mapping.assignments_hash` (server-computed; the model does not declare it at all, so a client-authored value is rejected as an unknown field)
- `source_schema_fingerprint`
- `target_schema_fingerprint`
- `source_schema_id`
- `target_schema_id`
- `source_to_generic`
- `generic_to_destination`
- `type_mapping_assignments_hash`

`stream_id` is **not** reserved — it is an optional authored UUID.
`pipeline_id` is **authored** on streams as the parent pipeline's UUID
cross-reference; it is required.

The legacy mapping fields (`source_to_generic`, `generic_to_destination`,
plus the hash fields above) are server-managed under the new schema.
Authored mapping is `assignments`-only — one entry per destination field.
The registry computes the rest.

## Connection

- `version`
- `org_id`
- `connector_version`
- `auth_state` (the auth lifecycle status block)
- `created_at`
- `updated_at`

`connection_id` is **not** reserved — it is an optional authored UUID.
`connector_id` is **authored** as the connector slug (or UUID) and is
required.

## Database endpoint

- `connector_id`
- `connector_version`
- `connection_id`
- `schema_hash`

`endpoint_id` is **authored** — the derived handle, computed by
`scripts/endpoint_id.py` and never hand-written — and is required; it serves as
the catalog key after the endpoint is materialized. See
`endpoint-spec/spec-database-object.md`.

## Reservation is per-namespace

A name reserved on an artifact is reserved **only there**. It does not reserve
the same name in a provider-owned namespace: a database column may legitimately
be called `version`, an operation parameter `org_id`. Reserve nothing on the
provider's behalf — copy discovered names verbatim.

## Authored shape vs. persisted shape

The published JSON Schemas — and the `*Input` contract models they render from —
describe the **authored** shape (the strict input variant), which already omits
every server-managed field above. So an authored document validates without any
special handling, and authoring a server-managed field fails because the models
forbid unknown fields. The persisted, post-stamped record shape is internal and
not published.
