# Spec: Proof Artifacts on Code Runs

## Overview

**What**: Fleet runs attach durable proof — screenshots, full test logs, patch records — queryable per run/leg/attempt and servable over existing artifact links.
**Why**: "Tests passed" is a claim. Exit-code receipts exist but full logs are truncated to 2k tails, screenshots are never persisted, and nothing links evidence to legs. Proof should be inspectable, not asserted.
**Scope**: IN — artifact table + registry adapter, full-log spill on test gates, opt-in UI-verify screenshots, report/operator linkage. OUT — video recording (Raghav call 2026-09-17: shots+logs only; no recorder infra).

## Requirements

- [ ] WHEN a test gate runs THEN its full stdout/stderr is stored as an artifact and the inline record keeps the 2k tail plus artifact pointer
- [ ] WHEN a UI-verify leg runs THEN its screenshot frames persist as PNG artifacts linked to (run, leg, attempt)
- [ ] WHEN an artifact is stored THEN its sha256 + byte count are recorded and reads verify integrity
- [ ] WHEN a run report renders THEN proof artifacts are linked from it
- [ ] WHEN artifacts exceed per-kind size caps THEN the store refuses with a clear error instead of truncating silently

## Architecture / Design

**Changes** (blast radius):
- `ADDED fleet_run_artifacts(run_id, leg_id, attempt_id, kind, artifact_id, sha256, bytes, created_at)` table in `fleet/store.py` (+ migration)
- `MODIFIED fleet/isolation.py` — `run_test_gate(s)` spills full output to artifact files (`fleet_run_artifacts` kind `testlog`)
- `ADDED fleet/artifacts.py` — adapter over `core/artifacts.py:ArtifactRegistry` (per-kind caps above the 512 KiB default, `job_id = run_id`; `ArtifactLink` already carries `fleet_run_id`)
- `MODIFIED fleet/supervisor.py` — post-leg hook in `_execute_leg` (~L2576) persists declared artifacts; opt-in UI-verify leg calls `core/computer_use.observe()` and stores frames (kind `screenshot`)
- `MODIFIED fleet/reports.py` + `ui/operator_web.py` — link artifacts from reports and operator view; serve via existing `/artifacts/<token>`

**Data model**:
```
fleet_run_artifacts {
  id: INTEGER PK, run_id: TEXT, leg_id: TEXT, attempt_id: TEXT,
  kind: TEXT — screenshot|testlog|patch,
  artifact_id: TEXT — ArtifactRegistry id,
  sha256: TEXT, bytes: INTEGER, created_at: REAL
}
```

**Key decisions**:
- Shots+logs, no video (Raghav call) — covers nearly all proof needs at a fraction of the cost; revisit only with a concrete failing case
- Reuse `ArtifactRegistry` (HMAC links, TTL, serving) rather than a new file dump — fleet fields already anticipated in schema
- Full logs spill to files, tails stay inline — keeps event payloads bounded while making the whole log one click away
- UI screenshots opt-in per run (needs a display session), never default-on

## Tasks

> Execution: batch as ONE fleet coding run (mechanical, well-bounded).

### Phase 1: Store + adapter
- [ ] Table + migration + `fleet/artifacts.py` adapter with per-kind caps + integrity verify on read
  - accept: roundtrip test stores/reads/verifies each kind; oversize write refused with clear error
  - engine: fleet

### Phase 2: Capture points
- [ ] Test-gate full-log spill + UI-verify screenshot persistence via post-leg hook
  - accept: fixture run yields testlog artifact with full output and tail pointer; UI leg yields PNG artifacts linked to leg/attempt
  - engine: fleet

### Phase 3: Surfaces
- [ ] Report links + operator listing via existing serving routes
  - accept: report for fixture run contains working artifact links; operator page lists them
  - engine: fleet

## Edge Cases / Gotchas

- Screenshot legs need a display/X11 session — must no-op cleanly (not fail) where none exists
- Artifact bytes could dwarf the DB — store bytes on disk via registry, only pointers + hashes in sqlite
- Existing 2k-tail consumers must keep working — pointers are additive, tails stay where they are
- Retention: artifacts inherit registry TTL; run deletion should cascade pointers (decide: delete bytes too — implementer confirms)

## Testing

- [ ] Roundtrip + integrity + cap-refusal unit tests
- [ ] Fixture run produces linked testlog + screenshot artifacts
- [ ] Report/operator linkage test
- [ ] No-display no-op test for UI legs

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
