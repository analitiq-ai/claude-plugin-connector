# HTTP transport idioms

Authoring patterns for `transports` in API connectors.

## Single-origin

The simplest case: one `base_url`, one transport, one set of common headers.

```json
{
  "default_transport": "api",
  "transports": {
    "api": {
      "transport_type": "http",
      "base_url": "https://api.example.com",
      "headers": {
        "Accept": "application/json",
        "Authorization": { "template": "Bearer ${secrets.api_key}" }
      },
      "timeout_seconds": 30,
      "rate_limit": {
        "max_requests": 1000,
        "time_window_seconds": 60
      }
    }
  }
}
```

## Multi-origin

When a provider exposes auth, discovery, and data on different origins
(e.g. separate `oauth.` / `api.` hosts), define one transport per origin
and factor common headers into `transport_defaults`.

```json
{
  "default_transport": "api",
  "transport_defaults": {
    "transport_type": "http",
    "headers": {
      "Accept": "application/json",
      "Authorization": { "template": "Bearer ${auth.access_token}" }
    }
  },
  "transports": {
    "auth": {
      "base_url": "https://oauth.example.com",
      "headers": {
        "Authorization": {
          "function": "basic_auth",
          "input": {
            "username": { "ref": "connection.parameters.client_id" },
            "password": { "ref": "secrets.client_secret" }
          }
        }
      }
    },
    "discovery": { "base_url": "https://api.example.com" },
    "api": { "base_url": "https://api.example.com/v1" }
  }
}
```

The `auth` transport overrides the inherited Bearer `Authorization` with
Basic auth.

## Templated `base_url`

`base_url` takes either a literal string or a value expression resolving to
one. The expression is resolved once, at connection-materialization time, so a
host that varies per connection is expressed directly on the transport.

A region or subdomain the user supplies before auth:

```json
"base_url": { "template": "https://${connection.parameters.region}.example.com" }
```

The matching `region` input must be declared in `connection_contract.inputs`
with `phase: "pre_auth"` so the template resolves before auth.

A per-tenant host discovered *after* auth:

```json
"base_url": { "template": "https://${connection.discovered.api_domain}.example.com/api/v1" }
```

That value comes from a `post_auth_outputs` entry, so the transport is only
usable once post-auth discovery has run — it cannot serve an `auth`-phase
operation. Declare a separate transport for the discovery request itself (see
`connector-builder/references/lifecycle-phases.md`).

Do not put the host in an operation's `request.path` as an absolute URL:
`endpoint_id` is derived from that path, so `endpoint-id-locator` rejects it.

## Header resolution order

Effective headers per request are built as:

1. Resolved `transport_defaults.headers`.
2. Merge resolved `transports.<ref>.headers`.
3. Remove inherited names listed in operation `headers_remove`.
4. Merge resolved operation `headers`.

Header names match case-insensitively for override and removal.

**Declare a deletion with `headers_remove`, not with `null`.** A block must not
both set and remove the same header name (ADV-HTTP-001). `headers_remove` is
available on endpoint operation requests too, not just connector transports —
that is how one endpoint drops an inherited default (e.g. an auth header a
public sub-resource rejects).

Don't lean on a header resolving to nothing as an implicit delete; express the
intent with `headers_remove` so it survives regardless of how empty values are
treated.
