# I/O contracts

Pin every I/O between phases and sub-agents as a JSON Schema fragment.

## ProviderFacts (discriminated union by kind)

`ProviderFacts` is the researcher's **coverage of the published contract** —
the facts the live schemas (`connector`, `api-endpoint`,
`type-map-read`/`-write`) require in order to author a connector for the
target system. It is shaped *like* the contract, not maintained as a curated
parallel list (design: `docs/design/contract-derived-research.md`).

Read the schema below as a **floor, not a ceiling**: it pins the facts the
pipeline depends on by name, but the researcher's mission is "ground every
fact the contract asks about." When current docs expose a contract-relevant
fact this fragment does not name, the researcher records it (alongside a
`notes` line) rather than dropping it — the contract, not this fragment, is
the source of truth for *what to know*. Per-resource response **field
schemas** (the field-level facts that decide things like datetime
zone-awareness) are not carried here; they are researched per endpoint in the
fan-out and returned as `EndpointFacts` (below).

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["provider", "kind"],
  "properties": {
    "provider": { "type": "string" },
    "kind": { "type": "string", "enum": ["api", "database"] },
    "notes": { "type": "string" }
  },
  "oneOf": [
    {
      "properties": {
        "kind": { "const": "api" },
        "auth_model": {
          "type": "object",
          "required": ["family"],
          "properties": {
            "family": {
              "type": "string",
              "enum": [
                "api_key", "basic_auth", "oauth2_authorization_code",
                "oauth2_client_credentials", "jwt",
                "credentials", "aws_iam", "none"
              ]
            },
            "scopes": { "type": "array", "items": { "type": "string" } },
            "redirect_required": { "type": "boolean" },
            "refresh_supported": { "type": "boolean" }
          }
        },
        "base_urls": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["name", "url_or_template"],
            "properties": {
              "name": { "type": "string" },
              "url_or_template": { "type": "string" },
              "depends_on": { "type": "array", "items": { "type": "string" } }
            }
          }
        },
        "post_auth_selections": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "key": { "type": "string" },
              "label": { "type": "string" },
              "discovery_endpoint": { "type": "string" }
            }
          }
        },
        "discovery_endpoints": {
          "type": "array",
          "description": "Dynamic POST-AUTH discovery probes only (e.g. a call that resolves a per-tenant api_domain). NOT the list of data resources to author endpoints for — that is `resources`.",
          "items": {
            "type": "object",
            "properties": {
              "purpose": { "type": "string" },
              "method": { "type": "string" },
              "path": { "type": "string" }
            }
          }
        },
        "resources": {
          "type": "array",
          "description": "The data resources the connector should expose — the domain branch's resource list. The orchestrator enumerates these into the endpoint fan-out worklist; each becomes one `endpoint-creator` branch fed by its own `EndpointFacts`. Carries only what the domain pass can know without deep-diving each resource's fields; the per-resource response field schema is researched per endpoint (see `EndpointFacts`).",
          "items": {
            "type": "object",
            "required": ["key"],
            "properties": {
              "key": { "type": "string", "description": "Stable resource slug; becomes the endpoint_id (pattern ^[a-z0-9][a-z0-9_-]*$)." },
              "label": { "type": "string" },
              "method": { "type": "string" },
              "path": { "type": "string" },
              "paginated": { "type": "boolean", "description": "Whether this resource's list operation paginates (style is the connector-level `pagination`)." },
              "writable": { "type": "boolean", "description": "Whether the provider documents a write (insert/upsert) for this resource." },
              "replication_cursor": { "type": "string", "description": "Field usable as an incremental/replication cursor, when the resource supports one; else absent." }
            }
          }
        },
        "native_type_vocabulary": {
          "type": "array",
          "description": "Connector-wide set of native wire-type tokens observed across the provider's resources (e.g. `string`, `integer`, `date-time`, `number`, `boolean`, provider-specific scalar names). Researched at the domain level so the creator can author a COMPLETE `type-map-read` before fan-out; every endpoint field must resolve through that map. A genuinely new native surfaced by an endpoint is a domain-level type-map addition, never an endpoint-local one.",
          "items": { "type": "string" }
        },
        "pagination": {
          "type": "object",
          "properties": {
            "style": { "type": "string", "enum": ["offset", "page", "cursor", "link", "keyset"] },
            "params": { "type": "array", "items": { "type": "string" } }
          }
        },
        "rate_limit": {
          "type": "object",
          "properties": {
            "max_requests": { "type": "integer" },
            "time_window_seconds": { "type": "integer" }
          }
        }
      },
      "required": ["auth_model"]
    },
    {
      "properties": {
        "kind": { "const": "database" },
        "driver": { "type": "string" },
        "transport_family": {
          "type": "string",
          "enum": ["sqlalchemy", "adbc", "flight_sql", "jdbc", "odbc", "mongodb"]
        },
        "adbc_driver_package": {
          "type": "string",
          "description": "First-class ADBC driver wheel when one exists (e.g. 'adbc-driver-postgresql'); absent when the system has no production ADBC driver. Drives step 1 of the driver-selection decision order."
        },
        "flight_sql_endpoint": {
          "type": "boolean",
          "description": "True when the server exposes an Arrow Flight SQL endpoint (step 2 of the decision order — generic adbc-driver-flightsql)."
        },
        "bulk_load_protocol": {
          "type": "string",
          "description": "The system's native bulk-load path when no ADBC driver exists (e.g. 'LOAD DATA LOCAL INFILE', 'COPY FROM stdin BINARY', 'fast_executemany'). Drives step 3 — async SQLAlchemy transport with the bulk path implemented in the connector class."
        },
        "async_sqlalchemy_driver": {
          "type": "string",
          "description": "The async DBAPI for the SQLAlchemy transport (e.g. 'postgresql+asyncpg', 'mysql+aiomysql'). Sync drivers fail at connect — the engine requires the asyncio extension."
        },
        "dsn": {
          "type": "object",
          "properties": {
            "url_template_example": { "type": "string" },
            "logical_fields": { "type": "array", "items": { "type": "string" } }
          }
        },
        "tls": {
          "type": "object",
          "properties": {
            "supported_modes": { "type": "array", "items": { "type": "string" } }
          }
        },
        "native_types": {
          "type": "array",
          "items": { "type": "string" }
        },
        "default_port": { "type": "integer" }
      },
      "required": ["driver", "transport_family"]
    }
  ]
}
```

## EndpointFacts (per-resource field schema — API fan-out only)

One `EndpointFacts` object per data resource: the researcher's
**per-endpoint** pass in the fan-out grounds the researched fields, the
orchestrator injects the connector-level `pagination` (echoed from
`ProviderFacts.pagination`), and `endpoint-creator` consumes it. This
is the category that `ProviderFacts` deliberately does **not** carry: the
field-level truths about one resource's response — including the datetime
zone-awareness that was previously guessed (issue #12). Every field fact is
grounded on the resource's own documentation / a real sample; an
`endpoint-creator` dispatched without `EndpointFacts` refuses (it has no web
access and may not guess field types).

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["resource", "fields"],
  "properties": {
    "resource": { "type": "string", "description": "Resource key; matches the ProviderFacts.resources[].key and becomes the endpoint_id." },
    "method": { "type": "string" },
    "path": { "type": "string" },
    "paginated": { "type": "boolean", "description": "Whether this resource's list operation paginates." },
    "pagination": {
      "type": "object",
      "description": "The connector-wide pagination style + params (echoed from ProviderFacts.pagination into the branch), so endpoint-creator — which sees only EndpointFacts + the connector body — can author the per-endpoint pagination block. Present whenever `paginated` is true.",
      "properties": {
        "style": { "type": "string", "enum": ["offset", "page", "cursor", "link", "keyset"] },
        "params": { "type": "array", "items": { "type": "string" } }
      }
    },
    "replication_cursor": { "type": "string", "description": "Field usable as an incremental cursor, when the resource supports one." },
    "record_path": { "type": "string", "description": "Path to the iterable record collection in the response body (informs response.records, e.g. `response.body.data`)." },
    "writable": { "type": "boolean" },
    "conflict_keys": { "type": "array", "items": { "type": "string" }, "description": "Provider-documented natural key for upsert, when the resource is upsertable." },
    "fields": {
      "type": "array",
      "minItems": 1,
      "description": "One entry per response field the connector exposes. `native_type` must be a token covered by ProviderFacts.native_type_vocabulary; `arrow_type` is the canonical Arrow type the field resolves to.",
      "items": {
        "type": "object",
        "required": ["name", "native_type", "arrow_type"],
        "properties": {
          "name": { "type": "string" },
          "native_type": { "type": "string", "description": "Provider's documented/observed wire-type token (e.g. `string`, `integer`, `date-time`)." },
          "arrow_type": { "type": "string", "description": "Canonical Arrow type (PascalCase). For temporals, chosen from the SAMPLE value's zone-awareness: a zoneless wire value → bare `Timestamp(<unit>)`; a value carrying an offset/Z → `Timestamp(<unit>, UTC)`. Never default date-time to tz-aware." },
          "nullable": { "type": "boolean" },
          "enum": { "type": "array", "items": { "type": "string" }, "description": "Closed value domain, when the field is enumerated in the docs." },
          "format": { "type": "string", "description": "Documented string format (e.g. `email`, `uri`, `uuid`, `date`)." },
          "sample_value": { "type": "string", "description": "A real wire sample. REQUIRED for any temporal field so zone-awareness is decided on evidence, not guessed." },
          "tz_aware": { "type": "boolean", "description": "For date-time fields: true iff the wire value carries a zone/offset. Set from `sample_value`, not assumed." }
        }
      }
    },
    "notes": { "type": "string" }
  }
}
```

## Diagnostics

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["passed", "findings"],
  "properties": {
    "passed": { "type": "boolean" },
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["validator", "severity", "path", "message"],
        "properties": {
          "validator": {
            "type": "string",
            "enum": [
              "json-schema",
              "reserved-field",
              "expression-resolver",
              "phase-resolvability",
              "transport-ref",
              "dsn-binding",
              "auth-shape",
              "tls-consistency",
              "type-map-coverage",
              "type-map-rule",
              "type-map-write-coverage",
              "endpoint-annotations",
              "endpoint-filename"
            ]
          },
          "severity": { "type": "string", "enum": ["error", "warning"] },
          "path": { "type": "string", "description": "JSON pointer into the document" },
          "message": { "type": "string" },
          "rule_doc": { "type": "string" }
        }
      }
    }
  }
}
```

## DriftVerdict

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["bump", "previous_version", "next_version", "rationale"],
  "properties": {
    "bump": { "type": "string", "enum": ["patch", "minor", "major", "none"] },
    "previous_version": { "type": "string" },
    "next_version": { "type": "string" },
    "rationale": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["change_path", "category"],
        "properties": {
          "change_path": { "type": "string" },
          "category": {
            "type": "string",
            "enum": [
              "input-removed", "input-renamed", "input-type-changed",
              "input-enum-narrowed", "storage-changed",
              "non-optional-input-added", "auth-shape-changed",
              "discovery-shape-changed", "optional-input-added",
              "optional-output-added", "optional-endpoint-added",
              "type-map-rule-added", "type-map-rule-removed",
              "type-map-rule-reordered", "type-map-canonical-changed",
              "bug-fix", "doc-fix", "tuning"
            ]
          },
          "note": { "type": "string" }
        }
      }
    }
  }
}
```

## CreatorOutput

Returned by `api-connector-creator` and `db-connector-creator`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["connector", "type_map_read"],
  "properties": {
    "connector": {
      "anyOf": [
        { "type": "object", "description": "Assembled connector body, ready for validation against https://schemas.analitiq.ai/connector/latest.json." },
        { "type": "null", "description": "Returned by stub agents (e.g. storage-connector-creator) that decline to author." }
      ]
    },
    "type_map_read": {
      "anyOf": [
        {
          "type": "array",
          "minItems": 1,
          "description": "On-disk shape of the standalone type-map-read.json (native → Arrow): a top-level, non-empty array of {match, native, canonical} rule objects where `native` is the matcher (regex patterns authored UPPERCASE) and `canonical` is the rendered Arrow type (may carry ${name} substitutions backed by named captures in `native`). Written by the orchestrator to {connector_id}/definition/type-map-read.json and validated against https://schemas.analitiq.ai/type-map-read/latest.json.",
          "items": {
            "type": "object",
            "required": ["match", "native", "canonical"],
            "additionalProperties": false,
            "properties": {
              "match":     { "enum": ["exact", "regex"] },
              "native":    { "type": "string", "minLength": 1 },
              "canonical": { "type": "string", "minLength": 1 }
            }
          }
        },
        { "type": "null", "description": "Returned by stub agents that decline to author." }
      ]
    },
    "type_map_write": {
      "anyOf": [
        {
          "type": "array",
          "minItems": 1,
          "description": "On-disk shape of the standalone type-map-write.json (Arrow → native DDL render rules). REQUIRED for kind=database; MUST be null for kind=api. Same rule shape but the direction inverts: `canonical` is the matcher (regex with ECMA named captures for parameterized types) and `native` is the rendered DDL (may carry ${name} substitutions backed by captures in `canonical`). Must cover the full canonical vocabulary; deliberate gaps are allowed only when the dialect overrides render_column_type for that family. Written to {connector_id}/definition/type-map-write.json and validated against https://schemas.analitiq.ai/type-map-write/latest.json (full Layer 1 + Layer 2; direction derived from the filename).",
          "items": {
            "type": "object",
            "required": ["match", "native", "canonical"],
            "additionalProperties": false,
            "properties": {
              "match":     { "enum": ["exact", "regex"] },
              "native":    { "type": "string", "minLength": 1 },
              "canonical": { "type": "string", "minLength": 1 }
            }
          }
        },
        { "type": "null", "description": "kind=api connectors and stub agents return null — the write direction is a database-package concept." }
      ]
    },
    "package_files": {
      "anyOf": [
        {
          "type": "object",
          "required": ["connector_py", "init_py", "requirements_txt", "pyproject_toml"],
          "additionalProperties": false,
          "description": "Python package files for kind=database connectors (the connector root IS the package). MUST be null for kind=api. Written by the orchestrator to {connector_id}/connector.py, __init__.py, requirements.txt, pyproject.toml. Contents follow the connector-package contract in connector-spec-db/spec-connector-package.md; enforcement (wheel build, entry points) is registry CI's job, not the schema validator's.",
          "properties": {
            "connector_py":     { "type": "string", "minLength": 1, "description": "{Name}Dialect(SqlDialect) + {Name}Connector(GenericSQLConnector); CDK imports only." },
            "init_py":          { "type": "string", "minLength": 1, "description": "Re-exports the connector + dialect classes." },
            "requirements_txt": { "type": "string", "minLength": 1, "description": "THIS connector's driver(s) only — async DBAPI and/or adbc-driver-{driver} wheel." },
            "pyproject_toml":   { "type": "string", "minLength": 1, "description": "name=analitiq-connector-{connector_id}; dynamic dependencies from requirements.txt; package-dir maps the repo root; entry points named {connector_id} under analitiq.source_connectors AND analitiq.destination_connectors." }
          }
        },
        { "type": "null", "description": "kind=api connectors and stub agents return null — API connectors carry only the definition." }
      ]
    },
    "notes": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Human-readable notes the orchestrator should surface (e.g. fields the creator could not populate from ProviderFacts)."
    }
  }
}
```

## EndpointCreatorOutput

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["endpoint_files"],
  "properties": {
    "endpoint_files": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["endpoint_id", "document"],
        "properties": {
          "endpoint_id": {
            "type": "string",
            "pattern": "^[a-z0-9][a-z0-9_-]*$",
            "description": "Stable endpoint identifier; mirrors document.endpoint_id and is used by the orchestrator to derive the on-disk filename."
          },
          "document": {
            "type": "object",
            "description": "One endpoint document body. Must validate against https://schemas.analitiq.ai/api-endpoint/latest.json and carry the same endpoint_id at its top level."
          }
        }
      }
    }
  }
}
```
