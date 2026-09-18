# Spec: Fast-Lane Model Routing

## Overview

**What**: Easy turns route to cheap fast models, hard turns stay on the frontier — cutting latency on the turns that don't need brilliance.
**Why**: Subscription cost is flat but latency isn't. Today only casual *voice* small-talk gets the fast lane, and even the `fast` lane's head is Opus-low. Every trivial text turn (titles, recall, yes/no) pays full frontier latency.
**Scope**: IN — trivial/normal/hard complexity signal, cheap-first lane orders on subscription models, outcome logging, lane escalation. OUT — local models (Raghav call 2026-09-17: cheap APIs only); touching fleet/coding preference paths (brain turns only v1).

## Requirements

- [ ] WHEN a trivial turn arrives THEN it routes to the cheap head of the fast lane (haiku-class), not Opus
- [ ] WHEN a turn requests tool calls or follows a user correction THEN the next turn escalates one lane up
- [ ] WHEN any turn routes THEN decision + outcome (latency, lane, model) append to the central log for threshold tuning
- [ ] WHEN the cheap head fails or is unusable THEN routing falls through the ordered lane exactly like today (no new failure modes)
- [ ] WHEN manual override is set THEN it wins over the complexity signal (existing override semantics preserved)

## Architecture / Design

**Changes** (blast radius):
- `MODIFIED core/brain_router.py` — `complexity: trivial/normal/hard` signal in `route_turn` (length + question-shape heuristics v1; tiny local classifier later via reflex profile)
- `MODIFIED config/serena-policy.json` — `fast` lane order `haiku → terra → sonnet → opus`; add `complexity_lanes: {trivial: fast, hard: complex}` (supported by `_select_lane`); `casual` unchanged
- `MODIFIED core/brain_router.py` — escalation rule (tool-call request or correction → bump lane next turn) + outcome emit to observability log
- Tests in `tests/test_brain_router.py`, `tests/test_serena_policy.py`

**Key decisions**:
- Cheap subscription APIs, no local model (Raghav call) — zero new infra; L6 local-model work stays a separate project
- Heuristics first, classifier later — length/shape gets 80% of the split; a learned router needs outcome data this spec starts collecting
- Brain turns only — coding/fleet have separate preference/policy paths; unifying them is out of scope and risky
- Escalation over perfection — a misrouted trivial turn costs one fast turn, then the lane corrects; tune thresholds from logged outcomes

## Tasks

> Execution: direct — router + policy judgment calls, small diff. No fleet.

### Phase 1: Signal + lanes
- [ ] Complexity signal + lane reorder + `complexity_lanes` + override/escalation semantics
  - accept: golden routing table (trivial→haiku, correction→escalated, override wins, fallthrough on unusable)
  - engine: direct

### Phase 2: Outcome logging
- [ ] Decision + outcome emit to central observability log (depends on observability spec's `emit` — stub the call shape if it lands first)
  - accept: routed fixture turns produce exact log rows; no log → routing still works (fail-open emit)
  - engine: direct

## Edge Cases / Gotchas

- "Trivial" misclassified hard things is the failure mode — keep the trivial bar high (short + question-shaped + no code/paths/commands mentioned); log borderline calls for tuning
- Voice casual path already works — don't regress it; the new signal must agree with `_is_casual_conversation` where they overlap (test the overlap)
- Policy JSON is also operator-editable — lane edits must validate (unknown model names fail loudly at load, not mid-turn)
- Outcome logging must never block routing — emit is best-effort with its own try/except

## Testing

- [ ] Routing golden table (trivial/normal/hard × override × unusable × escalation)
- [ ] Voice-casual overlap test (no regression)
- [ ] Policy validation test (bad model name fails at load)
- [ ] Outcome-log shape test + fail-open emit test

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
