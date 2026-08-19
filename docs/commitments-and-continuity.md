# Commitments and provider-outage continuity

Schema, configuration, and wiring reference for objectives 4 and 8.

- **ws-4**, commitments and proactive intelligence: `core/commitments.py`,
  `core/commitment_sources.py`, `core/briefings.py`
- **ws-8**, provider-outage continuity: `core/provider_health.py`,
  `core/local_model_fallback.py`

Both slices are additive. No existing module was modified, so the shared
`core/scheduler_actions.py` registry, `core/notification_authority.py`, and
`core/brain_provider.py` behave exactly as they did before.

## 1. Commitments

### Storage

SQLite at `~/.local/state/serena/commitments.sqlite3`, mode `0600`, WAL.
Override with `SERENA_COMMITMENTS_DB_PATH`. Schema version 1.

Two tables. `commitments` holds current state; `commitment_corrections` is an
append-only audit of every field Serena ever set or changed, with the actor and
the reason. Nothing is deleted, including abandoned commitments.

### Lifecycle

| State | Meaning |
|---|---|
| `proposed` | Serena inferred it from a source. Not agreed to yet. |
| `accepted` | Real and owed, but its due window has not opened. |
| `active` | Due window is open. This is "what do I owe right now". |
| `completed` | Done. Terminal. |
| `abandoned` | Deliberately dropped, with a required reason. Terminal. |

Legal transitions are a fixed table; anything else raises `CommitmentError`.
`activate_due()` promotes `accepted` to `active` on a timer and deliberately
never promotes `proposed`, so Serena's own guess cannot become an obligation
without Raghav agreeing to it.

### Fields

`title`, `detail`, `owner`, `priority` (`low|normal|high|critical`, the same
vocabulary `core/serena_policy.py` uses for risk), `source`, `source_ref`,
`due_at`, `recurrence`, `lead_seconds`, `snooze_until`, `dismissed_at`,
`subject_entity_id`, `follow_up_of`, `abandoned_reason`, `last_briefed_at`.

`subject_entity_id` is a free-text reference to a ws-3 state-graph entity. It is
intentionally not a foreign key: the graph is a separate authority and may be
empty, and a commitment must not require it to exist.

### Recurrence

`{"frequency": "daily|weekdays|weekly|monthly|yearly", "interval": 1..365}`.
Calendar months and years walk real dates, so a monthly commitment on the 31st
lands on the last day of a shorter month. An explicit `interval: 0` is rejected
rather than silently read as 1.

Completing or dismissing a recurring commitment leaves the finished occurrence
in place as history and creates the next one. A series missed for weeks resumes
at the next future date instead of firing a backlog.

### Snooze, dismiss, complete, follow-up

- **snooze** sets a future `snooze_until` and changes no state. Still owed, just
  quiet.
- **dismiss** drops this occurrence without claiming it was done. Recurring
  commitments roll to the next one.
- **complete** is terminal for that occurrence.
- **follow_up** creates a new `accepted` commitment linked by `follow_up_of`.

### Correction surface

`store.inspect(commitment_id)` returns the commitment plus its full correction
history. `store.correct(...)` accepts `title`, `detail`, `owner`, `priority`,
`due_at`, `recurrence`, `lead_seconds`, and `subject_entity_id`; `state` is not
correctable, because a state change is a transition with its own rules.

A correction is validated as a whole, not field by field. The resulting
`(due_at, recurrence)` pair has to satisfy the same rule `propose` enforces, so
a correction can neither add a recurrence to a commitment with no date nor clear
the date out from under an existing recurrence. Fixing both halves in one call
is allowed, and so is clearing both together.

## 2. Local sources

All free, all local, all read-only. Configure what exists; the rest report
themselves unavailable with a reason instead of failing.

| Adapter | Configuration | Notes |
|---|---|---|
| `MemoryTaskAdapter` | none | Reads existing `memory/store.py` tasks. Does not rewrite them. Priority `high`. |
| `LocalIcsAdapter` | `SERENA_CALENDAR_ICS_DIR` | Parses `.ics` files, dependency-free. |
| `LocalRemindersDbAdapter` | `SERENA_REMINDERS_DB_PATH` | Opens sqlite `mode=ro`; never a second writer. |

Ingestion is idempotent on `(source, source_ref)`. Every field the adapter
actually asserts is compared — `due_at`, `title`, `detail`, `priority`, and
`recurrence` — and any that disagrees is corrected on the existing commitment
with an audit entry; nothing is duplicated. A field the adapter says nothing
about is left alone: a `.ics` file that never mentioned recurrence is not an
instruction to strip a repeat set by hand. Anything already completed or
abandoned is never resurrected. One broken adapter cannot stop the others.
Everything arrives `proposed`.

The ICS parser handles line folding, `VALUE=DATE` all-day events (surfaced at
09:00 local), UTC `Z` times, and quoted parameters containing colons, which is
how Outlook writes a timezone. An `RRULE` it cannot model, such as
`FREQ=HOURLY`, produces no recurrence rather than an invented schedule.

`TZID` is resolved through `zoneinfo`, so a 09:00 New York standup is stored as
that instant rather than 09:00 on this machine. A floating time, or a zone this
machine's database does not carry, falls back to local time; an offset is never
invented from a display string like `(UTC-05:00) Eastern Time`.

## 3. Briefings

`build_morning_briefing`, `build_evening_briefing`, `build_pre_event_briefing`,
and `pending_pre_event_briefings`. Pure local reads and string formatting: no
model call, no network. This is why briefings survive a total provider outage.

Interruption is decided in two layers that are deliberately kept apart:

1. `InterruptionRules` answers "is there anything worth saying" against
   commitment content, and wraps the same `NotificationPolicy` the authority
   uses so quiet hours are defined once.
2. `core/notification_authority.py` decides whether he may actually be
   interrupted, and owns quiet hours, hourly caps, dedupe, and durable retry.

`deliver()` takes a required `authority` argument with no default. That is a
safety property: a test can only ever hand in an authority built with fake
senders, and there is no import path that reaches the real voice bridge or
Telegram bot by accident.

### Scheduling

`register_briefing_actions(scheduler, ...)` adds five actions:
`serena.commitments.ingest`, `serena.commitments.activate`,
`serena.briefing.morning`, `serena.briefing.evening`,
`serena.briefing.pre_event`.

These are registered on top of `core/scheduler_actions.register_all(...)` rather
than added to `REVIEWED_ACTIONS`. That dict is another work unit's file and has
a test asserting its exact membership, so extending it here would have broken a
passing test to save one line. Handlers return a `notify` payload and let the
scheduler's existing path hand it to the one notification authority.

Pass `clock=` to make scheduled briefings deterministic in tests. Pass
`mode_reader=` to let a briefing state which continuity mode produced it.

### When a briefing counts as spoken

`last_briefed_at` is the flag that stops a briefing repeating, so it is only set
on proof the notice was actually `sent`.

`SerenaScheduler._notify` suppresses every exception and discards the
authority's result, so a handler that returns a `notify` payload cannot learn
whether he heard anything. Handlers registered without an `authority` therefore
mark nothing; an unheard briefing stays eligible and the authority's own dedupe
key absorbs the repeat. Pass `authority=` to `register_briefing_actions` and the
handler delivers through `deliver()` instead, sees a real `sent`, and marks only
then. `deliver(..., store=...)` does the same for a direct caller.

## 4. Continuity modes

`assess_continuity()` turns per-provider capacity into one system-wide mode.

| Mode | Condition |
|---|---|
| `full` | At least one cloud subscription has capacity. |
| `degraded` | No cloud capacity, but a local model is loaded and answering. |
| `offline` | No cloud capacity and no local model. |

Unknown or missing capacity is treated as usable, matching `fleet_capacity`'s
own rule that only a positive, unexpired exhaustion signal counts against a
provider.

### What survives

| Capability | full | degraded | offline |
|---|---|---|---|
| `memory_recall` | yes | yes | yes |
| `briefings` | yes | yes | yes |
| `queued_work` | yes | yes | yes |
| `safe_local_actions` | yes | yes | yes |
| `local_reasoning` | yes | yes | no |
| `frontier_reasoning` | yes | no | no |
| `coding_jobs` | yes | no | no |

Everything true in `offline` is sqlite, files, and formatting. Offline does not
mean down; it means no new reasoning.

### Honesty

`describe(state)` returns one line naming the actual mode, the actual model, and
the actual fallback reason. `assert_not_claiming_cloud(state, result)` refuses
both directions of mislabelling: a degraded result claiming a cloud provider,
and a full-mode result claiming to be local.

### Durable resume

`ContinuityStore` at `~/.local/state/serena/continuity.sqlite3`
(`SERENA_CONTINUITY_DB_PATH`) keeps work that never started because nothing was
available to run it. `resume()` refuses to drain while degraded or offline:
replaying frontier work onto a 14b local model would produce answers nobody
asked that model for. When the budget is exhausted the item is abandoned in the
same pass, so `pending()` never reports work that will not run.

This is kept separate from `core/control_plane.py`'s obligation ledger on
purpose. An obligation is something Serena owes Raghav and must redeliver; a
deferred turn is work that never began. Different lifecycles, different
resolution rules.

## 5. Local model profiles

Sized against the real machine: RX 6800 XT, 16 GB VRAM, 32 GB RAM, with 1.5 GB
reserved for the desktop, leaving 14.5 GB usable.

| Role | Model | Quant | ~VRAM | Context |
|---|---|---|---|---|
| `reflex` | `qwen2.5:3b-instruct-q4_K_M` | Q4_K_M | 2.6 GB | 8k |
| `conversation` | `qwen2.5:14b-instruct-q4_K_M` | Q4_K_M | 9.6 GB | 16k |
| `reasoning` | `gpt-oss:20b` | MXFP4 | 13.0 GB | 8k |

**No weights are downloaded by this code.** `weight_instruction(profile)`
returns the `ollama pull ...` command for a human to run. Until then the
endpoint probe reports the model as not loaded and continuity stays `offline`,
which is the correct and honest state.

### Which served model counts as the profile

A served name satisfies a profile only when it is the exact model id, or when it
declares the same parameter count in its tag (`qwen2.5:14b` for the 14b
profile). The family stem alone is not identity: `qwen2.5:3b-instruct-q4_K_M`
and `qwen2.5:14b-instruct-q4_K_M` share `qwen2.5`, and accepting the stem let a
3b answer everything the conversation profile promised — not a crash, just
Serena quietly getting worse while still naming the model she meant to run. A
tag that states no size, such as `qwen2.5:latest`, is unverifiable and refused;
the probe then names the wrong-size model it did find.

### Configuration

- `SERENA_LOCAL_MODEL_URL`, default `http://127.0.0.1:11434/v1`.

The URL **must** be loopback. A non-loopback host raises
`LocalModelUnavailable` and fails closed. A "local" model at someone else's IP
address is a hosted provider with a friendlier name, and private conversation is
the entire reason this lane exists.

Requests carry `keep_alive: 5m` so the GPU is released for gaming or a heavy
build without anyone having to stop a service.

### Adapter shape

`LocalBrain` implements `start`/`turn`/`interrupt`/`close`/`snapshot`, matching
the existing Codex worker contract, so the resident brain can hold it in the
slot it already has. Every result carries `provider: "local"` and the actual
served model id; `assert_local_provenance()` proves that before anything reaches
a surface.

### Routing a turn

`route_brain_turn(capacity, override=...)` returns a `BrainRouting` with the
provider, model, mode, reason, and `should_queue`. It is the whole integration
surface: `core/brain_provider.py`'s `choose_brain_provider` knows only `claude`
and `codex` and raises `BrainProviderUnavailable` when both are out, which is
exactly the moment continuity should take over. Rather than fork that chooser,
this extends the same decision with `local` and with an honest instruction to
queue when nothing can answer.

An explicit `override="local"` consults the local endpoint directly rather than
the assessment, because `assess_continuity` short-circuits to `full` without
ever looking at the GPU; a local model that is not loaded returns a queue
instruction instead of a provider.

## 6. Verification

```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_commitments.py tests/test_briefings.py \
  tests/test_provider_health.py tests/test_local_model_fallback.py
```

No test sends a notification or opens a socket. Notification authorities in
tests are built with explicit fake senders, and every local-model test injects a
deterministic opener.

## 7. Not proven here

- No live local model was run. The endpoint and profiles are verified against a
  deterministic fake server; actual tokens-per-second and real VRAM occupancy on
  the RX 6800 XT remain unmeasured until the weights are pulled by hand.
- No real calendar, reminders database, or notification delivery was exercised.
- The resident brain now adopts the continuity route. Claude and Codex live
  usage-limit failures fall through to the loopback local conversation model;
  when that model is absent, the turn is durably queued and the surface returns
  an explicit offline response instead of crashing.
- No automatic deferred-work replay is enabled yet. A future supervisor pass
  must supply the idempotent handler and call `ContinuityStore.resume()` only in
  full mode, preserving the existing rule that frontier work is never drained
  onto the smaller local model.
