# TLS declarations

How `sqlalchemy` database transports declare TLS intent without
embedding driver-specific objects. The generic `tls` block is
**SQLAlchemy-only**; for `adbc` transports, TLS lives inside
`db_kwargs` (e.g. `adbc.postgresql.sslmode`, `adbc.postgresql.sslrootcert`)
— see `spec-dsn-bindings.md` and `db-connector-creator.md` step 2.

> **Nothing validates TLS coherence.** The contract's TLS block is
> deliberately vocabulary-agnostic — it enforces no mode set and does not check
> that a verification mode has a CA certificate to verify against. Every rule
> below is author-side discipline; a connector that declares `verify-full` with
> no `ssl_ca_certificate` input validates clean and fails at connect. Apply the
> checklist by hand, for both SQLAlchemy and ADBC shapes (they resolve through
> the same `connection_contract.inputs`).

## Shape

```json
{
  "transports": {
    "database": {
      "tls": {
        "mode": { "ref": "connection.parameters.ssl_mode" },
        "ca_certificate": { "ref": "secrets.ssl_ca_certificate" }
      }
    }
  }
}
```

## Rules

- `tls.mode` is a value expression that resolves to one of the values
  in the connector's declared `ssl_mode` enum (see below — the
  vocabulary is connector-defined). In practice it should `ref` the
  canonical input `connection.parameters.ssl_mode`.
- `tls.ca_certificate` is a value expression that resolves to a
  PEM-encoded CA bundle. It should `ref` the canonical secret
  `secrets.ssl_ca_certificate`.
- If the `ssl_mode` enum allows any certificate-verification mode
  (`verify-ca` / `verify-full`, or MySQL-style `VERIFY_CA` /
  `VERIFY_IDENTITY`), the connection contract **must** declare
  `ssl_ca_certificate` as an input. Nothing checks this — verify it yourself.
- Connector authors must NOT embed driver-specific TLS objects, file
  paths, or executable code in connector JSON. The runtime materializer
  converts the generic declaration into driver-specific arguments.

## SSL mode vocabulary is connector-defined

The `ssl_mode` vocabulary belongs to the connector: declare the
system's native mode names in the `connection_contract.inputs.ssl_mode`
enum, and interpret them in the connector package's dialect via
`build_tls_connect_arg(mode, ca_pem)` (see
`spec-connector-package.md`). Users see the vocabulary their database's
own docs use; no translation table ships anywhere.

Reference vocabularies:

| System family | Enum (from the reference packages) |
|---|---|
| libpq-shaped (postgres, redshift) | `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full` |
| MySQL / MariaDB | `DISABLED`, `PREFERRED`, `REQUIRED`, `VERIFY_CA`, `VERIFY_IDENTITY` |

The dialect maps each declared mode to the driver's connect argument —
pass-through strings for libpq drivers, `False` / `SSLContext` objects
for aiomysql (built with `cdk.transport_factory.ca_ssl_context` when a
CA bundle is supplied). Verification modes (`verify-ca`/`verify-full`,
`VERIFY_CA`/`VERIFY_IDENTITY`) must raise when `tls.ca_certificate`
resolves empty.

## Authoring checklist

1. Always declare `ssl_mode` as a connection input with an explicit
   `enum`.
2. Always declare `ssl_ca_certificate` as a secret input when any
   certificate-verification mode (`verify-ca`/`verify-full`,
   `VERIFY_CA`/`VERIFY_IDENTITY`) is in the enum.
3. Reference both via `ref` inside the transport's `tls` block.
4. Do not duplicate driver-specific SSL options elsewhere in the JSON —
   the dialect's `build_tls_connect_arg` is the single place that
   derives driver connect arguments from `ssl_mode`.
5. Declare the system's native mode vocabulary in the enum and make the
   dialect's `build_tls_connect_arg` handle exactly that vocabulary — the
   dialect owns interpretation, and no validator will tell you the two
   disagree.
