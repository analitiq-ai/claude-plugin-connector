# `streams` and `status`

Both fields are declared on `PipelineInput` — see the field table in `SKILL.md`
for their types, defaults and constraints.

## `streams`

An array of **stream UUIDs**. Each entry is the `stream_id` of a stream
defined in a sibling `streams/<stream-slug>.json` file.

```jsonc
{
  "streams": [
    "22222222-2222-4222-8222-222222222222",
    "23232323-2323-4323-8323-232323232323"
  ]
}
```

Rules:

- Uniqueness is checked on the **version-stripped base id**, not merely on the
  literal string — `ADV-PIPE-003` in the cross-field rule table in `SKILL.md`.
- Each referenced stream's `pipeline_id` must equal this pipeline's
  `pipeline_id`. The bundle referential checks enforce this when
  `--bundle-root` is supplied.
- Array order is **display-only**. The runtime treats `streams` as an unordered
  set and the contract defines no inter-stream dependencies, so never encode
  "run A before B" by ordering the array and never tell the user that ordering
  will be honored.
- An empty `streams` array is permitted in `draft` or `inactive`
  status. Only `status: active` requires non-empty `streams` (see the
  status rules below).

## `status`

`status` is the only gate on execution — there is no parallel enabled/disabled
flag. The vocabulary and default are in the `SKILL.md` field table; what each
value means operationally:

| value | semantics |
|---|---|
| `draft` | Editable. Not scheduled. `streams` may be empty. |
| `active` | Scheduled (subject to `schedule.type`). Requires non-empty `streams` AND at least one referenced stream with its own `status: "active"`. |
| `inactive` | Paused. Not scheduled. `streams` may be empty. |

`status: active` requires runnable streams. An `active` pipeline with an empty
`streams` list is rejected at the single-document contract-model level (no
`--bundle-root` needed). The remaining rule — that at least one referenced stream
is itself `active` — needs the bundle: run with `--bundle-root` and the referential
checks error when no referenced stream is `active`. A **draft** pipeline is
legitimately not yet runnable, so runnability is not checked for a draft
(`require_runnable=False`); it is enforced only once the pipeline is `active`.

## Authoring sequence

The orchestrator authors the pipeline shell with `streams: []` in phase
6, then stitches the stream UUIDs back in phase 8 after the parallel
`stream-creator` dispatch returns each new `stream_id`. The shell starts
in `status: draft`. Promotion to `active` happens later (typically when
the user submits the pipeline to the registry).
