# Spec: Voice Control (finish the wake word, widen what voice can do)

## Overview

**What**: Get the wake word to a state where it can stay on, and let a spoken request run a real task instead of only the five toy laptop actions.
**Why**: Two half-finished things that together are the competitor's "Hands-Free Mode". The wake listener runs (`voice/desk/wake_listener.py`, `serena-wake-listener.service`) with a two-stage model and a frozen 0.55 threshold, but the last field report says `acceptance_claim: false` — **179 false wakes in 34.8 hours (5.15/h) with zero deliberate attempts** against a bar of 7 days, 20 attempts, ≤5% misses and 0 false wakes. And voice can only trigger the allowlist in `core/laptop_actions.py` (volume, mute, media, `open_app`, `open_url`) or start a 5-minute computer session; "do X for me" is not reachable by voice.
**Scope**: IN — false-wake reduction and a real acceptance run, widening voice to task execution through existing computer-use and device adapters, longer scoped sessions. OUT — the voice pipeline itself (it works), phone calls (spec-business-calls), vision attachment (spec-vision-turns), any new wake model training if tuning is enough.

## Requirements

- [ ] WHEN the listener runs for a full week in his real room THEN false wakes are zero and at least 20 deliberate wakes succeed with ≤5% misses
- [ ] WHEN a false wake does slip through THEN the second-stage verifier rejects it before anything is spoken or executed
- [ ] WHEN he says a request that maps to a known device capability THEN it runs through the same adapter and authority path as any other action, with the same confirmation rules
- [ ] WHEN he says a request that needs the computer THEN a scoped session starts with a duration matched to the task, not a fixed 5 minutes, and ends when the task ends
- [ ] WHEN a spoken request is ambiguous or unrecognised THEN she says so and does nothing, rather than guessing at an action

## Architecture / Design

**Changes** (blast radius):
- `MODIFIED voice/desk/wake_listener.py` — tune the first stage (threshold, refractory window, minimum activation energy) and strengthen the second stage (the faster-whisper tiny phrase check) so a near-miss phrase is rejected. Log every wake with its scores so the acceptance report can distinguish first-stage from second-stage failures.
- `ADDED voice/desk/wake_report.py` (or extend the existing wakeword timer job) — a nightly roll-up of wakes, false wakes per hour, deliberate attempts and misses, written where `wakeword-acceptance-report.json` already lives, so the bar in `voice/call/WAKEWORD.md:121-129` can be judged without hand-counting.
- `ADDED core/voice_intents.py` — mapping a spoken request to either a device capability (`core/device_actions.py` registry: laptop, android, phone, home assistant, mqtt) or a computer-use session, with an explicit "I don't know what you mean" path. Capability effects and tiers are unchanged; voice is just another source.
- `MODIFIED core/computer_service.py` — session duration from the request instead of the fixed default, still bounded by the existing ceiling, and closing on completion rather than on the timer.
- `MODIFIED voice/call/orchestrator.py` — route recognised intents through `voice_intents` before falling through to conversation.

**Key decisions**:
- **Tune before retraining.** 5 false wakes an hour with a frozen threshold is a tuning problem first; retraining is a much bigger job and may not be needed.
- **The second stage is the safety net.** The first stage can be permissive if the verifier is strict, and that trade is better than missing him when he actually calls.
- **Voice reuses the adapter registry — it does not get its own action list.** A second allowlist would drift from the first and quietly bypass authority.
- **Unrecognised means refuse.** A voice assistant that guesses is worse than one that asks, especially when the actions are real.
- **Session length follows the task.** The 5-minute default exists because sessions were open-ended and risky; with authority gating the risk is handled and the arbitrary cap just makes her useless mid-task.

## Tasks

> Execution: mostly direct — acceptance needs his real room and his voice.

### Phase 1: False-wake reduction
- [ ] Threshold/refractory tuning, verifier strengthening, per-wake score logging
  - accept: a 48-hour run with zero false wakes and every deliberate wake caught
  - engine: direct

### Phase 2: Acceptance run
- [ ] Nightly report + the full 7-day run against the documented bar
  - accept: `acceptance_claim: true` with 20+ deliberate attempts, ≤5% misses, 0 false wakes; `WAKEWORD.md` updated (it still claims the model export is pending, which is stale)
  - engine: direct

### Phase 3: Voice to tasks
- [ ] `core/voice_intents.py` + adapter routing + task-scoped sessions + refuse-on-ambiguous
  - accept: three spoken requests of different shapes (a device action, a computer task, an unknown one) behave correctly, with the unknown refused cleanly and the consequential one confirmed
  - engine: fleet

## Edge Cases / Gotchas

- Wake words fire on the television and on her own speech; the refractory window and self-speech suppression matter as much as the threshold.
- The acceptance bar of zero false wakes over a week is strict on purpose — a listener that fires on its own during a work call is one that gets turned off permanently.
- Deliberate attempts have to actually be made: the last report had none, so the run measured only noise and proved nothing about recall.
- Voice is not a typed surface: it can never clear tier 4, and confirmations spoken on a call follow spec-approval-routes.
- A long-running spoken task needs a way to stop by voice that works even while she is speaking — barge-in already exists in the desk duplex path, so reuse it rather than inventing a stop word.

## Testing

- [ ] Wake tuning replay against recorded room audio (false-positive corpus)
- [ ] Verifier unit tests on near-miss phrases ("hey serena" vs "hey sarah", "see ya")
- [ ] Report roll-up test from synthetic wake logs
- [ ] Intent mapping tests: known capability, computer task, ambiguous, unknown
- [ ] Authority test: a consequential spoken action requires confirmation

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: Phases 1–2 need Raghav's room and his voice
**Review**: —
