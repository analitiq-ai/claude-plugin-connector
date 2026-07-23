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
- If the `ssl_mode` enum allows any certificate-verification mode (a
  mode that verifies the server certificate against a CA, whatever the
  driver names it), the connection contract **must** declare
  `ssl_ca_certificate` as an input. Nothing checks this — verify it yourself.
- Connector authors must NOT embed driver-specific TLS objects, file
  paths, or executable code in connector JSON. The runtime resolves the
  generic declaration and hands it to the connector package's dialect,
  which converts it into driver-specific connect arguments.
- A `tls` block obligates the connector package to ship a dialect
  implementing the TLS hook (see `spec-connector-package.md` §Dialect
  hooks). The engine has no built-in TLS interpretation for any driver —
  the CDK base dialect raises `UnsupportedDialectOperationError` for
  every mode — so a connector that declares `tls` without a dialect TLS
  hook fails loudly at connect, and a package-less (thin) connector
  cannot declare `tls` at all.

## SSL mode vocabulary is connector-defined — researched, never copied

The `ssl_mode` vocabulary belongs to the connector: declare the
system's native mode names in the `connection_contract.inputs.ssl_mode`
enum, and interpret them in the connector package's dialect via the
TLS hook (see `spec-connector-package.md`). Users see the vocabulary
their database's own docs use; no translation table ships anywhere.

The vocabulary is established at author time from the researcher's
grounded facts (`ProviderFacts.tls.supported_modes` — the mode values
the driver's official docs name, verbatim), never copied from another
connector and never assumed from wire-protocol family. Even
wire-compatible systems ship drivers with different TLS surfaces: one
driver may take a many-mode libpq-style string, another only a boolean
toggle plus a narrow set of certificate-verification modes, spread
across several connect parameters. Declare exactly what the driver
documents — no more, no fewer. If the researcher reported `tls` as
null (driver docs ambiguous), that is a gap to surface to the user,
not a license to borrow a vocabulary.

The dialect maps each declared mode to whatever the driver's connect
API takes — a pass-through mode string, a boolean toggle, or an
`SSLContext` built with `cdk.transport_factory.ca_ssl_context` when a
CA bundle is supplied. A driver that takes TLS through a single
connect argument implements `build_tls_connect_arg(mode, ca_pem)`; a
driver that spreads TLS across several connect parameters overrides
`build_tls_connect_args(mode, ca_pem)` and returns the full mapping
(see `spec-connector-package.md` §Dialect hooks).
Certificate-verification modes must raise when `tls.ca_certificate`
resolves empty.

## Authoring checklist

1. Always declare `ssl_mode` as a connection input with an explicit
   `enum`.
2. Always declare `ssl_ca_certificate` as a secret input when any
   certificate-verification mode is in the enum.
3. Reference both via `ref` inside the transport's `tls` block.
4. Do not duplicate driver-specific SSL options elsewhere in the JSON —
   the dialect's TLS hook is the single place that derives driver
   connect arguments from `ssl_mode`.
5. Declare the researched mode vocabulary in the enum and make the
   dialect's TLS hook handle exactly that vocabulary — the dialect owns
   interpretation, and no validator will tell you the two disagree.
