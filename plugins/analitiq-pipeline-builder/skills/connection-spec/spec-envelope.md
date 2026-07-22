# The connection envelope

A connection document has **four author-time maps**, each keyed by a
connection-contract input or post-auth-output name. There is no single `values`
object — the plugin routes each key into the right map itself, driven entirely by
the connector's contract:

| Map | Holds | Sourced from a contract entry whose… |
|---|---|---|
| `parameters` | non-secret submitted values | input `storage: "connection.parameters"` |
| `secret_refs` | pointers to secret values (never the value) | input/output `storage: "secrets"` |
| `selections` | durable post-auth user choices | output `storage: "connection.selections"` |
| `discovered` | provider-returned non-secret values | output `storage: "connection.discovered"` — **server-managed, never authored** |

Omit any map that would be empty — the required set is the field table in
`SKILL.md`.

## Routing rule (the whole thing)

Read the downloaded connector's `connection_contract`. For each
`inputs.<key>` and `post_auth_outputs.<key>`, route by the **last segment of its
`storage`** literal — this single rule covers every connector and auth type, so
nothing here changes when a new connector ships:

- `connection.parameters` → `parameters.<key>` = the user's value.
- `secrets` → `secret_refs.<key>` = a secret **pointer** (see below); the value
  goes in `.secrets/`, never the document.
- `connection.selections` → `selections.<key>` **only if** the user supplies it
  up front; usually omit (a post-auth selection isn't known at authoring time).
- `connection.discovered` → **never author.** The auto-discovery pipeline writes
  this bucket; the connections API rejects a client-supplied value.

### `selections` vs. `discovered`

The two are easy to conflate and the distinction is *who chose the value*, not
when it arrived. Use `selections` **only** for a durable user choice — the user
was shown options after auth and picked one (a warehouse, a workspace, an
account, a sync target), and that choice must survive re-auth. Use `discovered`
for provider-returned context that is durable and connection-specific but that
the user never entered or picked (a tenant id the provider assigned, an
account's region). A value the user typed at authoring time is a `parameter`,
not a selection.

## Type fidelity

Each contract input declares a JSON `type`. The authored value must use that type
verbatim — `port: 5432` (integer), not `"5432"`. The plugin does not coerce;
read the type from the connector or ask the user. Inputs with `enum: [...]`
accept only a listed value (e.g. `ssl_mode`). Optional inputs (`required: false`)
may be omitted; do not copy a connector's `default` into the document unless the
user is overriding it.

## TLS verification needs its CA material

A database connection authored with `ssl_mode: "verify-ca"` or `"verify-full"`
**must** also supply the contract's CA-material input (`ssl_ca_certificate` in
the connectors that declare one), even where the driver would silently fall back
to the host's trust store. Verifying against whatever CAs happen to be installed
is not the mode the user asked for, and the fallback makes a misconfigured
connection look healthy. If the user selects a verifying mode without supplying
the certificate, ask for it rather than authoring the mode alone. The CA input
routes like any other input — by its declared `storage`, which is normally
`secrets`, so it becomes a pointer in `secret_refs`.

## Secrets — reference, never embed

For every input/output the contract marks `storage: "secrets"`, write a
**pointer** into `secret_refs.<key>`, and record the real value in a gitignored
`.secrets/credentials.json` the user provisions. The plugin never holds, logs, or
writes a secret value into the document.

Author an **`env:` pointer** by default — portable and resolved from the runtime
environment:

```jsonc
{
  "connector_id": "postgresql",
  "parameters": { "host": "db.example.com", "port": 5432, "database": "analytics", "ssl_mode": "verify-full" },
  "secret_refs": { "password": "env:ANALITIQ_POSTGRESQL_PASSWORD" }
}
```

Env-var name: `ANALITIQ_<connection-slug>_<key>`, upper-cased, every
non-alphanumeric replaced with `_`. That composition is a **plugin convention**,
not a contract fact — the contract constrains only the `env:` grammar in the
scheme list below, and any name matching it is legal. Keep the convention unless
the user names their own variable. Emit the sibling template the user fills in:

```jsonc
// .secrets/credentials.json
{ "ANALITIQ_POSTGRESQL_PASSWORD": "<paste-password-here>" }
```

The user (or CI) exports these into the environment where the pipeline runs (or
loads them into their secret store) before submission; the plugin never reads
`.secrets/`.

The file's shape is the published credentials-sidecar contract
(`analitiq.contracts.credentials_file.CredentialsFile`): a **flat** top-level
JSON object — no nesting into sections, no envelope. Keys are unconstrained and
values may be any JSON type, but the engine string-coerces on read, so write
strings, and JSON-encode a structured credential into a string rather than
authoring a nested object.

### `.secrets/credentials.json` is not a `sidecar:` file

The two look alike and resolve completely differently. Do not conflate them:

- The plugin's template is keyed by **env-var name** and paired with `env:`
  pointers. The user resolves it by exporting those variables; nothing reads the
  file itself. This pairing is a plugin convention.
- The `sidecar:<name>` scheme names an entry in a credentials file the engine
  reads directly, keyed by the **connection-contract input name** — the same
  `<name>` that keys `secret_refs`, not an env-var name. Alone among the
  schemes it constrains nothing after its prefix, so a wrong name validates
  cleanly and fails only at resolution time.

So never emit a `sidecar:` pointer against the env-keyed template: the pointer
would look up `password` in a file whose only key is `ANALITIQ_…_PASSWORD`.
Emit `sidecar:` pointers only when the user asks for that store, and then key
the file by the contract input names.

Use `env:` unless the user asks for a specific store; substitute their pointer
verbatim if so. Which resolver runs for a given scheme is **engine-owned** — the
contract declares the scheme and nothing more. Author the pointer; never author,
infer, or promise resolution behavior (lookup order, caching, rotation, failure
mode) on the strength of the scheme name.

<!-- BEGIN GENERATED: secret-ref-grammar -->
Every `secret_refs` value must carry an explicit scheme — a bare token (a pasted raw secret) is rejected by the contract.

Accepted schemes (`analitiq.contracts.connection.SECRET_REF_VALUE_PATTERN`):

- `env:[A-Za-z_][A-Za-z0-9_]*`
- `file:[A-Za-z0-9_.][A-Za-z0-9_./\-]*`
- `s3://[A-Za-z0-9._\-]+/[A-Za-z0-9_./\-]+`
- `arn:aws:secretsmanager:[A-Za-z0-9\-]+:\d+:secret:[A-Za-z0-9/_\-+=.@]+`
- `arn:aws:ssm:[A-Za-z0-9\-]+:\d+:parameter/[A-Za-z0-9_./\-]+`
- `ssm:/[A-Za-z0-9_./\-]+`
- `sidecar:.+`
<!-- END GENERATED: secret-ref-grammar -->
