# Spec: Proactive Policy (when she speaks first)

## Overview

**What**: Enforce in code when Serena may interrupt, feed her recent activity into every turn, and turn on consensual check-ins.
**Why**: The policy exists on paper and not in the product. `docs/architecture.md:119-137` says never interrupt while he is typing, at most 3 phone pings a day and 1 desktop ping an hour, quiet hours 23–08 — but `core/notification_authority.py` implements quiet hours from **22**, a flat **12 per hour**, no daily cap and no typing check. Objective L3-O5 (`:656-665`) is still open. Meanwhile `core/supportive_mode.py` has `checkin_due`/`record_checkin` (`:315-334`) that **only the tests call**. With ambient sensing landing, the risk flips from "she never speaks" to "she is annoying", and the research is blunt about the cost: mid-task suggestions get 31% engagement and 62% dismissals and read as advertising, while suggestions at a natural boundary get 52%.
**Scope**: IN — breakpoint detection, caps and quiet hours aligned with the documented policy, a typing check, dismissal learning, ambient context on turns, wiring check-ins. OUT — the sensor itself (spec-ambient-sensing), notification delivery mechanics (they exist), the call and text channels themselves.

## Requirements

- [x] WHEN he is typing or classified as focused THEN nothing non-critical is delivered — it waits for a breakpoint
- [x] WHEN a breakpoint occurs (unlock, return from idle, after a commit or push, an app switch after a long focus block) THEN anything queued is delivered, oldest first
- [x] WHEN the daily or hourly cap is reached THEN further items are batched into a single digest rather than dropped silently
- [x] WHEN quiet hours apply THEN only critical items pass, and the rest resume at the end of quiet hours (one policy, matching the documented 23–08, with the code's 22 corrected or the doc changed — not both)
- [x] WHEN he dismisses or ignores a proactive item THEN that is recorded and the same kind is suppressed harder next time
- [x] WHEN a brain turn runs THEN a short summary of the last ~20 minutes of activity is available as context, without evicting memory context
- [x] WHEN check-ins are enabled and one is due THEN it is delivered through the same policy as everything else, and never during quiet hours

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/interrupt_policy.py` — breakpoint detection over the ambient stream, and the decision function: given a queued item and the current state, deliver now, hold for the next breakpoint, batch into a digest, or drop. This is the single place that answers "may she speak".
- `MODIFIED core/notification_authority.py` — add a daily cap and per-channel caps (3 phone/day, 1 desktop/hour) alongside the existing hourly limit (`DEFAULT_HOURLY_LIMIT = 12`, `:51`); add the typing/focus check; make quiet hours configurable and align them with the documented policy; keep every existing outcome (`sent | suppressed | deferred | pending_approval | failed`) so history stays readable.
- `ADDED core/ambient_context.py` — the ~20-minute activity summary as a compact string (apps, switches, class, last error seen), with its own token budget, injected by the brain's context builder (`core/brain_daemon.py:499-575`) and **never** at the cost of memory retrieval.
- `MODIFIED core/supportive_mode.py` + `core/scheduler_actions.py` — a reviewed action `serena.support.checkin` that asks `checkin_due()`, routes through the interrupt policy, and calls `record_checkin()` on delivery.
- `ADDED` dismissal feedback: a small table of (kind, outcome) with a decay, read by `interrupt_policy`.

**Key decisions**:
- **One gate, not three.** Every proactive path — notifications, check-ins, stuck nudges — goes through `interrupt_policy`. Anything that bypasses it becomes noise that no cap can fix.
- **Batch, don't drop.** Over-cap items become a digest at the next breakpoint, because dropping means she quietly stops being useful and nobody notices.
- **Breakpoints come from the sensor, not from guesses.** This is the payoff of spec-ambient-sensing and the reason it is built first.
- **Learn from dismissals, per kind.** The measured 30% noise rate of the best product in this space is what makes people turn it off; suppression must be automatic, not something Raghav has to ask for.
- **Critical is a short list.** Anything routinely marked critical to skip the policy defeats the policy; keep it to things with a deadline that he asked for.

## Tasks

> Execution: fleet, one run, after spec-ambient-sensing Phase 3 (needs breakpoints and activity classes).

### Phase 1: Policy engine
- [x] `core/interrupt_policy.py` + caps, typing check, quiet-hours alignment in `notification_authority`
  - accept: unit tests cover typing-suppression, each cap, quiet hours, deferral and resume; existing notification tests stay green
  - engine: fleet

### Phase 2: Batching + learning
- [x] Digest on over-cap, dismissal feedback with decay
  - accept: over-cap items appear once as a digest at the next breakpoint; a kind dismissed three times is visibly suppressed
  - engine: fleet

### Phase 3: Context + check-ins
- [x] `core/ambient_context.py` injection with its own budget; `serena.support.checkin` action
  - accept: a turn shows she knows what he was just doing; memory retrieval is unchanged in size; a due check-in arrives at a breakpoint, never mid-focus
  - engine: fleet

### Phase 4: L3-O5 acceptance
- [ ] Verify the documented interrupt policy end to end and close the objective
  - accept: a week of real operation with the suppression log showing what was held and why, and no complaint from Raghav about noise
  - engine: direct

## Edge Cases / Gotchas

- "Never interrupt while typing" must not mean "never interrupt", since he types most of the day. Typing plus focused blocks delivery; a pause at a boundary releases it.
- A digest that arrives at 2am because that is when the breakpoint happened is worse than no digest: breakpoints are gated by quiet hours too.
- Fleet completion notices are work he is waiting for, not proactive chatter — classify them so the caps do not starve them.
- The stuck classifier firing is an *invitation*, not a licence: offering help at every stuck moment is exactly the "advertisement" failure mode.
- Suppression must be visible. A silent policy that eats messages is indistinguishable from a bug, so the log must say what was held and why.
- Two surfaces (desktop and phone) must not deliver the same item twice; dedupe by key across channels.

## Testing

- [x] Policy unit tests for every branch (typing, focus, caps, quiet hours, critical bypass)
- [x] Digest batching test
- [x] Dismissal-decay test
- [x] Cross-channel dedupe test
- [x] Context budget test: ambient summary present, memory context unchanged
- [x] Check-in scheduling test with quiet hours and breakpoints

---

## Progress Log

**Status**: Phases 1–3 complete (fleet run 7b743f12); Phase 4 pending real-operation soak
**Branch**: serena/fleet/7b743f12-c5cd-42fb-a1fe-f09b1cfcf8d5/agent-a
**Current phase**: Phase 4 (direct, operator-owned)
**Last completed task**: Phase 3 ambient_context injection + serena.support.checkin action
**Files modified**: core/interrupt_policy.py (decide, breakpoints, dismissals, digest scan, presence), core/notification_authority.py (proactive caps, typing/focus holds, quiet 23–08, breakpoint release, digest kind), core/notification_senders.py (quiet default 23, live presence), core/ambient_context.py, core/brain_daemon.py (ambient block injection), core/scheduler_actions.py (serena.proactive.scan, serena.support.checkin), tests/test_interrupt_policy.py, tests/test_notification_proactive.py, tests/test_policy_learning.py, tests/test_ambient_context.py, tests/test_proactive_actions.py, tests/test_scheduler_actions.py (registry pin)
**Blockers**: Phase 4 needs a week of live operation with the suppression log; engine direct. L3-O5 also names heavy-lane exclusion and the Serena-owned mobile path, which this spec did not task — confirm scope before closing the objective.
**Review**: quiet hours aligned code→23 to match docs; supportive_mode.py needed no change (checkin_due/record_checkin API already sufficient)
**Fix (run 7b743f12)**: deliver_due/redeliver re-hold proactive items while typing/focused (they rejoin the breakpoint queue instead of sending at quiet-end); daily cap is one shared 3/day budget across voice/imessage/telegram per the documented "3 phone/day" (desktop keeps its own); suppression is 3 strikes in 30 days → a week of silence (replaces the sub-day half-life decay); dismissal surface is the phone line's `stop` (dismiss_latest_proactive strikes the newest proactive kind; "ignored" stays unwired — no observable signal exists); policy_log returns newest-first; decide() contract corrected (digest lives in scan_and_release). 258 focused tests green.
**Fix-2 (run 7b743f12)**: dismiss_latest_proactive strikes sent rows only, so a held/deferred/suppressed row can no longer absorb a stop meant for a delivered nudge. Focused suites green.
