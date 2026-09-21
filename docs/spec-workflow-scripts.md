# Spec: Resumable Leg Scripts (replay-leg)

## Overview

**What**: Every fleet attempt freezes a replayable input bundle (request, argv, prompt, workspace pin, gates), and `fleet replay-leg` re-executes one leg outside the supervisor for debugging and resume.
**Why**: Fleet legs are reproducible in theory (durable state machine, deterministic inputs) but not in practice — prompt assembly reads live snapshots, no frozen bundle exists, and no human can re-run one leg. Debugging a failed leg today means re-running the world.
**Scope**: IN — frozen bundles on attempt start, `replay-leg` CLI, same-input-same-verdict replay with diff record. OUT — byte-identical provider output (nondeterministic by nature); replay that mutates the original attempt.

## Requirements

- [ ] WHEN an attempt begins THEN its input bundle is frozen (worker request, provider argv, prompt sha + text ref, workspace commit/branch, dependency states, test-allowlist version)
- [ ] WHEN `fleet replay-leg --run --leg --attempt` runs THEN it reconstructs the workspace at the pinned base and re-invokes the exact argv
- [ ] WHEN replay validates THEN it uses `completion_gate` without mutating the original attempt (new attempt number)
- [ ] WHEN replay output differs THEN the diff is recorded alongside the replay verdict
- [ ] WHEN the pinned base is unavailable THEN replay refuses with a clear error instead of approximating

## Architecture / Design

**Changes** (blast radius):
- `ADDED fleet_leg_scripts` store — JSON bundles under the run checkout dir (preferred: travels with the worktree) or a table; written on `begin_attempt`
- `ADDED fleet replay-leg` CLI — reconstruct via `FleetIsolationStore` + journal `state()` check, re-invoke `worker_command()` argv, validate via `completion_gate`, record as new attempt
- `MODIFIED fleet/dag.py` — prompt assembly consumes the frozen bundle where feasible (incremental; full byte-determinism not required v1)
- Reuse `execute_saved_integration`'s helper-process pattern and `reset_leg_for_retry` semantics for resume

**Key decisions**:
- Deterministic = same-input-same-verdict, not same-output — provider nondeterminism is physics; the win is reproducible debugging and honest resume
- Bundles under the run checkout, not the DB — big prompts don't belong in sqlite; the DB keeps pointers + hashes
- Replay never mutates — it mints a new attempt, so the audit trail stays append-only
- Refuse on missing base — approximate replay would produce misleading verdicts; loud refusal beats quiet drift

## Tasks

> Execution: batch with track 1 (ONE fleet run with proof-artifacts + review-upgrade).
> Revision 2026-09-17: originally tagged direct for DAG/supervisor risk, but
> fleet has since proven supervisor/store/MCP competence twice (run-reports,
> code-index) with review gates catching real bugs. Shared context across the
> three specs (supervisor/completion/reports) outweighs the split benefit.

### Phase 1: Frozen bundles
- [ ] Bundle schema + freeze-on-`begin_attempt` + pointer in attempt row
  - accept: fixture attempt yields a bundle that round-trips (all fields present, prompt sha matches)
  - engine: direct

### Phase 2: replay-leg
- [ ] CLI reconstruct + re-invoke + gate-validate-as-new-attempt + diff record + missing-base refusal
  - accept: replay of a fixture attempt validates identically; forced output difference records a diff; missing base refuses loudly
  - engine: direct

## Edge Cases / Gotchas

- Workspace reconstruction needs the exact base commit — GC'd/rebased bases refuse; document retention expectations
- Provider CLIs evolve — argv frozen at attempt time may not run on newer CLIs; record CLI versions in the bundle for diagnosis
- Secrets in prompts: bundles inherit existing redaction rules — freeze the redacted prompt, keep a hash of the raw for forensics if policy allows
- Concurrent replay + live run on the same checkout must not collide — replay works in its own worktree, never the live one

## Testing

- [ ] Bundle roundtrip test (fields + prompt sha)
- [ ] Replay-identical-verdict test on fixture attempt
- [ ] Output-diff recording test
- [ ] Missing-base refusal test
- [ ] Live-run isolation test (replay touches nothing live)

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
