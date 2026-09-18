# Spec: Proactive Policy (when she speaks first)

## Overview

**What**: Enforce in code when Serena may interrupt, feed her recent activity into every turn, and turn on consensual check-ins.
**Why**: The policy exists on paper and not in the product. `docs/architecture.md:119-137` says never interrupt while he is typing, at most 3 phone pings a day and 1 desktop ping an hour, quiet hours 23–08 — but `core/notification_authority.py` implements quiet hours from **22**, a flat **12 per hour**, no daily cap and no typing check. Objective L3-O5 (`:656-665`) is still open. Meanwhile `core/supportive_mode.py` has `checkin_due`/`record_checkin` (`:315-334`) that **only the tests call**. With ambient sensing landing, the risk flips from "she never speaks" to "she is annoying", and the research is blunt about the cost: mid-task suggestions get 31% engagement and 62% dismissals and read as advertising, while suggestions at a natural boundary get 52%.
**Scope**: IN — breakpoint detection, caps and quiet hours aligned with the documented policy, a typing check, dismissal learning, ambient context on turns, wiring check-ins. OUT — the sensor itself (spec-ambient-sensing), notification delivery mechanics (they exist), the call and text channels themselves.

## Requirements

- [ ] WHEN he is typing or classified as focused THEN nothing non-critical is delivered — it waits for a breakpoint
- [ ] WHEN a breakpoint occurs (unlock, return from idle, after a commit or push, an app switch after a long focus block) THEN anything queued is delivered, oldest first
- [ ] WHEN the daily or hourly cap is reached THEN further items are batched into a single digest rather than dropped silently
- [ ] WHEN quiet hours apply THEN only critical items pass, and the rest resume at the end of quiet hours (one policy, matching the documented 23–08, with the code's 22 corrected or the doc changed — not both)
- [ ] WHEN he dismisses or ignores a proactive item THEN that is recorded and the same kind is suppressed harder next time
- [ ] WHEN a brain turn runs THEN a short summary of the last ~20 minutes of activity is available as context, without evicting memory context
- [ ] WHEN check-ins are enabled and one is due THEN it is delivered through the same policy as everything else, and never during quiet hours

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
- [ ] `core/interrupt_policy.py` + caps, typing check, quiet-hours alignment in `notification_authority`
  - accept: unit tests cover typing-suppression, each cap, quiet hours, deferral and resume; existing notification tests stay green
  - engine: fleet

### Phase 2: Batching + learning
- [ ] Digest on over-cap, dismissal feedback with decay
  - accept: over-cap items appear once as a digest at the next breakpoint; a kind dismissed three times is visibly suppressed
  - engine: fleet

### Phase 3: Context + check-ins
- [ ] `core/ambient_context.py` injection with its own budget; `serena.support.checkin` action
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

- [ ] Policy unit tests for every branch (typing, focus, caps, quiet hours, critical bypass)
- [ ] Digest batching test
- [ ] Dismissal-decay test
- [ ] Cross-channel dedupe test
- [ ] Context budget test: ambient summary present, memory context unchanged
- [ ] Check-in scheduling test with quiet hours and breakpoints

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: needs spec-ambient-sensing Phase 3
**Review**: —
