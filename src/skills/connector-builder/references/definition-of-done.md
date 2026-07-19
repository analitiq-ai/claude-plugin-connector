# Definition of Done

A self-check the creator agents run against their own output **before
returning `CreatorOutput`**. It is a gate, not a substitute for the
`connector-schema-validator`: the validator owns structural and cross-field
conformance (see `src/agents/connector-schema-validator.md` for what it does
and does not check). This checklist deliberately covers **only what the
validator cannot enforce** — classification correctness, completeness against
the provider's documentation, the both-directions principle, driver-choice
discipline, and the non-JSON artifacts (package files, README) the in-plugin
validator never sees. Do NOT restate validator rules here; if an item is
mechanically checkable, it belongs in the validator, not on this list.

Three things authors often assume are validated but are not — a `function`
name, a ref's resolvability, and TLS mode ↔ CA-certificate coherence. Those
belong on this list, not in the validator's column.

The kind-specific lists live at the end of each creator agent
(`api-connector-creator` / `db-connector-creator`); both also apply this
shared core.

## Shared core (both kinds)

- [ ] **Classification is correct.** `kind`, `auth.type`, and each
  `transport_type` match the provider's *actual* documented behavior —
  not merely a schema-valid value. (The validator checks the value is
  in-enum; it cannot check it is the right one.)
- [ ] **`connector_id` is the intended stable slug** and matches the
  on-disk `{connector_id}/` directory the orchestrator will write. (The
  schema checks the `[a-z0-9_-]+` pattern, not that it is the slug the
  user/provider actually means.)
- [ ] **`display_name`, `description`, and `tags` are meaningful**, not
  placeholders.
- [ ] **The read map covers the provider's documented native
  vocabulary**, not just the subset that happened to appear in a sample.
  (Nothing checks read-map completeness for a database connector; for an
  API connector the validator only checks the natives the endpoints
  reference.)
- [ ] **No secret value is embedded as a literal** anywhere (passwords,
  tokens, keys) — every credential is a `ref` / `template` / `function`
  into `secrets.*`. (Nothing can tell a literal default from a leaked
  secret.)
- [ ] **No customer-specific value is baked into the connector** — no real
  host, tenant id, account id, or database name. The connector declares the
  input's shape; the connection supplies the value.
- [ ] **Every `function` name is in the registered catalog.** Nothing
  validates function names, so a typo or a planned-but-unregistered function
  (e.g. `jwt_sign`) ships silently and fails at connect.
- [ ] **Every ref resolves to something a declaration produces.** Scope
  checking is narrow and never proves resolvability (see
  `value-expressions.md`), so a `connection.discovered.*` ref with no post-auth
  output behind it passes validation. Trace each one by hand
  (`lifecycle-phases.md`).
- [ ] **`default_transport` is the right default**, and any
  multi-transport split (auth / discovery / api origins) reflects the
  provider's real topology.
- [ ] **README is present** and describes what the connector connects
  to, its auth, and any setup. (The in-plugin validator ignores README
  entirely.)
- [ ] **Both read and write land as a working unit for this system**
  (the both-directions-first-class *capability* principle) — scope was
  not cut to source-only or destination-only. This means the connector's
  read/write capability, not a write *type-map* file: an API connector
  realizes the write direction through endpoints/operations (and ships
  no write map), a database connector through its two `pyproject.toml`
  entry-point groups.
- [ ] **Version is consistent**: first release → `1.0.0`; otherwise the
  drift verdict the orchestrator computed was applied.
