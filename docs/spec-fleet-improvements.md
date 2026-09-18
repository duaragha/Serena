# Spec: Fleet Delivery Handoff (complete-with-handoff)

## Overview

**What**: Runs that end with root-owed delivery debt complete with a tracked handoff instead of parking in `waiting_for_input` forever; workers are taught to prefer `not_applicable` over `deferred` when nothing is deployable.
**Why**: Two observed strands: (a) a worker that should say "no external surface" defers to root instead, and root never runs, so the run strands with all work done; (b) genuine ship steps (daemon restart, commit, release) have no fleet owner by design. Parking is the worst outcome — finished work held hostage by paperwork.
**Scope**: IN — delivery-prompt tweak, terminal handoff filing into commitments, `completed_with_handoff` terminal marker + event, run-report actions wiring. OUT — auto-deploy agent, auto-commit, auto daemon restart (operator-owned by design); owed/commitments UX changes; already-fixed read-leg delivery exemption (done 2026-09-17, not this spec).

## Requirements

- [ ] WHEN a run reaches terminal evaluation with root-owed delivery debt remaining THEN each debt is filed as a commitment (`source="fleet/delivery"`) and the run completes instead of parking
- [ ] WHEN handoff filing runs twice for the same debt THEN exactly one commitment exists (idempotent via `find_by_source`)
- [ ] WHEN a run completes with handed-off debt THEN its result text lists each handoff item and a `run.delivery.handed_off` event is emitted
- [ ] WHEN a run has no remaining debt THEN terminal behavior is byte-identical to today (no commitments, no marker, no event)
- [ ] WHEN a write leg reads delivery instructions THEN they state: no external surface → `not_applicable` with reason; `deferred` only for work a real owner will actually perform
- [ ] WHEN a run report is generated for a handed-off run THEN its actions section includes the handoff items with commitment ids
- [ ] WHEN operator evidence resolves a mid-run block THEN that path keeps working unchanged (this spec only changes the terminal all-steps-done case)

## Architecture / Design

**Changes** (blast radius):
- `MODIFIED fleet/completion.py` — delivery instruction text: `not_applicable` preference + honest-deferral rule (write legs only; read legs already exempt)
- `MODIFIED fleet/supervisor.py` — terminal path (`_terminal_outcome` area): after all legs settle, compute remaining root-owed debt from the delivery ledger; if any, file handoffs and complete with marker instead of parking
- `MODIFIED fleet/delivery.py` — expose owed-debt query for the terminal path (reuse `reconcile_event` ledger logic; no new debt semantics)
- `MODIFIED fleet/reports.py` — actions section includes handoff items (commitment ids + titles) when present
- `MODIFIED docs/fleet-runtime.md` — short section documenting complete-with-handoff vs parked

**Data model**: no new tables. Commitments via existing `CommitmentStore.propose` / `find_by_source(source="fleet/delivery", source_ref="<run_id>:<unit_id>:<req-hash>")`; run marker as terminal-state suffix or result-text + event (implementer picks the smaller diff that `fleet_list`/`fleet_result` surfaces honestly).

**Handoff item content**: title (requirement short form + unit), detail (run id, unit, full requirement text, deferring leg/attempt, deferral reason quoted, link to inspect), due none, actor `fleet`.

**Key decisions**:
- Complete-with-handoff over parking — best-practice backed: Devin's Session Insights turns remainder into advisory action items, never a parked session; the handoff orchestration pattern treats the human as a first-class handoff target with explicit termination; orchestration anti-pattern catalog says chained agents should "recommend follow-up in the report, operator runs the second pass" rather than wait opaquely
- Commitments store over a new ledger — Serena already tracks owed work there (`chats owed`); a second debt list would drift
- Assumption (no question round; flagged for review): `completed_with_handoff` counts as success for learning-outcome purposes but is visually distinct in list/result; reviewer confirms
- Assumption: one commitment per (unit, requirement); multiple deferrals of the same requirement collapse to the latest reason

## Tasks

> Execution: all direct — high-risk core module, judgment-heavy. No fleet self-surgery.

### Phase 1: Prompt tweak
- [ ] Delivery instruction text: `not_applicable` preference + honest-deferral rule; example updated
  - accept: prompt unit test asserts the guidance renders for write legs and stays absent for read legs
  - engine: direct

### Phase 2: Terminal handoff
- [ ] Owed-debt query + handoff filing + terminal marker + event + result-text listing
  - accept: fixture run with one root-owed debt completes with exactly one commitment filed, marker + event present; re-evaluation files nothing new; zero-debt run byte-identical terminal path
  - engine: direct

### Phase 3: Report wiring
- [ ] Report actions include handoff items with commitment ids
  - accept: golden test on handed-off fixture run asserts actions content
  - engine: direct

### Phase 4: Tests + docs
- [ ] Pytest suite + `docs/fleet-runtime.md` section; full fleet suite green
  - accept: `pytest tests/test_fleet_completion.py tests/test_fleet_delivery.py -q` green (new file if warranted); docs describe handoff vs parked
  - engine: direct

## Edge Cases / Gotchas

- A debt deferred to a REAL owner that later runs must not also file a handoff — only root-owed remainder at terminal files
- Operator evidence arriving between filing and completion must not double-record: re-check owed at file time, and `find_by_source` makes refiles idempotent anyway
- The parked-run dashboard copy ("resolve the blocker, add steering, retry") stays valid for mid-run blocks; only the all-steps-done terminal case changes
- Commitments DB failure at terminal must fail open to parked (today's behavior), never silently complete — the gate's fail-closed rule still holds

## Testing

- [ ] Root-owed debt at terminal → one commitment, marker, event, result listing
- [ ] Idempotent re-evaluation files nothing new
- [ ] Zero-debt terminal path unchanged
- [ ] Mid-run operator evidence path unchanged
- [ ] Report actions include handoff items
- [ ] Full fleet completion + delivery suites green

---

## Progress Log

**Status**: Not started
**Branch**: (planned: `feat/fleet-handoff` — created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
