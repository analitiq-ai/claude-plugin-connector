# `schedule` block

Omitting `schedule` is equivalent to authoring its defaults.

<!-- BEGIN GENERATED: fields-schedule -->
`analitiq.contracts.pipelines.config.Schedule` — closed (`additionalProperties: false`); required: none

| Field | Required | Type | Default | Constraints |
|---|---|---|---|---|
| `type` | no | 'manual' \| 'interval' \| 'cron' | `'manual'` | — |
| `timezone` | no | string | `'UTC'` | — |
| `interval_minutes` | no | integer \| null | `None` | `min=1` |
| `cron_expression` | no | string \| null | `None` | `pattern=^cron\(.+\)$` |

Carries 3 declarative cross-field `if`/`then` rule(s) — see the advisory rules for their prose.
<!-- END GENERATED: fields-schedule -->

Which fields each `type` admits is a cross-field rule — `ADV-PIPE-002` in the
table in `SKILL.md`. Author only the fields the chosen type calls for; leave the
other type's field out entirely rather than setting it to `null`.

## `type: manual`

```jsonc
{"type": "manual"}
```

Runs only on an explicit user trigger. Nothing schedules it.

## `type: interval`

```jsonc
{"type": "interval", "interval_minutes": 60}
```

Runs on a fixed cadence of `interval_minutes`. That cadence is
**timezone-invariant**: a fixed period is unaffected by the zone the pipeline
names or by a DST transition inside it. The contract bounds only positivity —
the shortest interval a source can actually sustain is engine- and
provider-dependent, so pick one the source can serve, not the smallest the
contract accepts.

## `type: cron`

```jsonc
{"type": "cron", "timezone": "Europe/Berlin", "cron_expression": "cron(0 2 * * ? *)"}
```

Fires on an AWS EventBridge cron expression, interpreted in `timezone`. The
contract's pattern check is deliberately coarse — it verifies the `cron(…)`
wrapper, not the inner spec. Full validity of the inner spec is the scheduler's
responsibility, not the plugin's, so a syntactically accepted expression can
still be rejected downstream.

## `timezone`

A valid IANA tz-database name (e.g., `UTC`, `Europe/Berlin`, `America/New_York`).

It is validated for **every** `schedule.type`, including the types where it has
no scheduling effect: an unknown name is rejected on a `manual` or `interval`
schedule just as it is on a `cron` one. Only `type: cron` interprets it — for
the other two it is accepted and stored as metadata, so never author a non-UTC
value there expecting it to shift when a run happens.

## Status interaction

`schedule` is **declarative**; the pipeline's `status` controls
whether the scheduler actually picks it up:

| `status` | scheduled execution |
|---|---|
| `draft` | disabled |
| `active` | enabled (subject to stream-side runnability) |
| `inactive` | disabled |

See `spec-streams-and-status.md`.
