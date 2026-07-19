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

## `base_url` must be a literal string (current limitation)

`base_url` is typed as a plain string. A value expression — `{"template":
"https://${connection.parameters.region}.example.com"}` or a `ref` — is
**rejected**, so a host that varies per connection cannot be expressed today.
That rules out:

- per-tenant hosts discovered after auth (`connection.discovered.api_domain`),
- region / subdomain hosts taken as user input,
- any provider whose data origin is not knowable at authoring time.

This is a **contract gap, not a design rule**: the runtime resolver already
handles an expression here, and sibling fields on the same transport (`headers`,
the rate limit's `time_window_seconds`) do accept expressions. Until the
contract catches up, a multi-tenant provider cannot be authored — surface that
as a blocker rather than working around it. Do not attempt an absolute URL in
an operation's `request.path`; paths are concatenated onto `base_url`, so that
produces a malformed URL rather than a different host.

## Header resolution order

Effective headers per request are built as:

1. Resolved `transport_defaults.headers`.
2. Merge resolved `transports.<ref>.headers`.
3. Remove inherited names listed in operation `headers_remove`.
4. Merge resolved operation `headers`.

Header names match case-insensitively for override and removal.
