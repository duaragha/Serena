# Spec: Fleet Run Reports

## Overview

**What**: Auto-generated post-run analysis for every terminal Fleet run — score, issue timeline, knowledge attribution, improved next-prompt, action items.
**Why**: Fleet runs end today and teach us nothing. Each run should make the next one cheaper: surface what went wrong, which knowledge helped vs misled, and what to do differently.
**Scope**: IN — deterministic score + timeline + lesson attribution computed from `fleet.sqlite3`, one bounded LLM pass for narrative/improved-prompt/actions, storage + on-demand regeneration, MCP + CLI + web surfaces, terminal-notification pointer. OUT — full knowledge/memory attribution (workers retrieve neither today; blind spot is documented, not faked), token/cost rollup table, auto KB-maintenance actions (a future consumer of reports), leg-timestamp backfill (derive from events instead).

## Requirements

- [ ] WHEN a run reaches terminal state THEN a report row exists in `fleet_run_reports` within 60s, or a `run.report.failed` event explains why
- [ ] WHEN report generation's LLM pass fails THEN the report still ships with deterministic sections and `generator` records the failure (never blocks terminal notifications)
- [ ] WHEN `fleet_report(run_id)` is called on a terminal run with no report THEN a report is generated on demand (idempotent: existing reports are returned, not regenerated)
- [ ] WHEN `fleet_report(run_id)` is called on a live run THEN it errors instead of generating
- [ ] WHEN a report is written THEN task text and excerpts are redacted exactly like `_finish_run` redacts `result_text`
- [ ] WHEN a run used Fleet lessons THEN the report lists each lesson with a helped/hurt/unclear vote grounded in Fix-phase output
- [ ] WHEN the terminal notification fires THEN it includes the report pointer
- [ ] WHEN a report is generated THEN score, issues, and lesson list are deterministic replays of the same DB state (LLM sections excluded)

## Architecture / Design

**Changes** (blast radius):
- `ADDED fleet/reports.py` — score, timeline scan, lesson assembly, generator prompt + call, `generate_report(run_id)` entry
- `MODIFIED fleet/store.py` — `fleet_run_reports` table DDL + migration, `save_report/get_report/report_exists`
- `MODIFIED fleet/supervisor.py` — call `generate_report` at end of `_terminal_outcome()` (:1604) as bounded best-effort post-step (own try/except, mirrors `learning.finish_failed`)
- `MODIFIED fleet/mcp.py` — new `fleet_report(run_id)` tool
- `MODIFIED cli.py` — `chats fleet report <run-id> [--json]` next to `fleet_group` (:1583+)
- `MODIFIED ui/fleet_web.py` — `GET /fleet_report/:id` reusing `inspect_run` projection style
- `MODIFIED docs/fleet-runtime.md` — short section documenting the report contract

**Data model**:
```
fleet_run_reports {
  run_id: TEXT PK — FK to fleet_runs
  score_json: TEXT — {score 0-100, size_class XS-XL, penalties[{reason, points}]}
  timeline_json: TEXT — [{at, kind, leg_id, attempt_id, summary}]
  knowledge_json: TEXT — {lessons[{lesson_id, attempt_id, outcome, vote}], blind_spot_note}
  narrative_text: TEXT — LLM narrative (may be null)
  next_prompt_text: TEXT — improved prompt for a retry/follow-up (may be null)
  actions_json: TEXT — [{action, reason}] (may be null)
  generator: TEXT — pinned model id, or "none (<reason>)" when LLM pass skipped/failed
  created_at: REAL
}
+ fleet_events row type run.report.ready (payload: run_id, score, size_class)
```

**Score (deterministic v1)**: start 100. −10 per retried leg (cap −30); −5 per `leg.completion_evidence_rejected` (cap −20); −5 per `worker.stalled` (cap −15); −5 if any attempt's `context.budgeted` delivered/source ratio < 0.5; −3 per capacity/resource wait (cap −9); −15 if terminal state is failed/cancelled. Size class from attempt count: ≤2 XS, ≤4 S, ≤8 M, ≤16 L, else XL.

**Timeline issues**: scan `fleet_events` for `attempt.failed`, `worker.stalled`, `leg.completion_evidence_rejected`, `context.budgeted` (below ratio), `run.retried`, capacity waits. Each entry carries timestamp + leg/attempt refs + payload excerpt. Copy the `autonomy_projection` query pattern (`fleet/observability.py`).

**Generator pass**: pinned model via the existing worker-runner machinery (no adaptive routing — determinism matters more than smarts here). Input is bounded: redacted task + score + issues + lesson list + Fix-phase output excerpts (cap ~8k chars total). Output is strict JSON: `{narrative, next_prompt, actions[], lesson_votes[]}`. Timeout-bounded; any failure → deterministic-only report.

**Key decisions**:
- Full report with LLM narrative (Raghav call 2026-09-16) over deterministic-only — the improved-prompt is the highest-value section
- Best-effort post-terminal step, never on the critical path — a report must not delay or break terminal notifications
- Lessons-only attribution v1 — workers inject no other knowledge/memory (`grep` verified), so anything else would be fabricated; full attribution waits on workers actually retrieving
- No new DB file — reports live in `fleet.sqlite3` next to the data they summarize

## Tasks

> Execution: all fleet-tagged tasks ship as ONE fleet coding run with this spec attached.

### Phase 1: Store
- [ ] `fleet_run_reports` DDL + migration in `fleet/store.py`, `save_report/get_report/report_exists`, write-path redaction
  - accept: roundtrip test saves and reads a report; secrets in task text are redacted on read-back
  - engine: fleet

### Phase 2: Deterministic sections
- [ ] `fleet/reports.py`: `compute_score`, `scan_timeline_issues`, `assemble_lessons` from live schema
  - accept: golden test on a fixture DB asserts exact score JSON + issue list + lesson list
  - engine: fleet

### Phase 3: Generator pass + terminal hook
- [ ] Generator prompt + pinned-model call + strict-JSON parse + failure fallback; wire into `_terminal_outcome()` post-`FleetLearning.finish`
  - accept: mock-provider test yields full report; failure-injection test yields deterministic-only report with `generator` recording the error; terminal notification still fires in both
  - engine: fleet

### Phase 4: Surfaces
- [ ] MCP `fleet_report` + CLI `chats fleet report` + web `GET /fleet_report/:id` + on-demand generation for old terminal runs + terminal-notice pointer
  - accept: end-to-end on a real completed run via all three surfaces; live-run call errors; second call returns cached row (no regen event)
  - engine: fleet

### Phase 5: Tests + docs
- [ ] Pytest suite for the above + short contract section in `docs/fleet-runtime.md`
  - accept: `pytest tests/test_fleet_reports.py` green; docs describe table, score formula, and blind spot
  - engine: fleet

## Edge Cases / Gotchas

- 297k+ `fleet_events` rows live — timeline scan must filter by `run_id` with an index, never full-table scan
- `output_text` capped at 256k per attempt — excerpt before stuffing into the generator prompt or the call blows up
- Legs have no started/completed timestamps — derive phase wall-time from `phase.started/completed` events or unit-phase rows
- Lesson `outcome` today is just terminal run state ("present on a green run") — the helped/hurt vote comes from the generator pass reading Fix output, and `unclear` is a valid vote
- Report generation itself must emit events (`run.report.ready` / `run.report.failed`) so failures are visible, not silent

## Testing

- [ ] Fixture-DB golden test: exact score + issues + lessons
- [ ] Failure injection: dead provider → deterministic-only report, notifications intact
- [ ] Redaction test: secret in task text never lands in report tables
- [ ] Live-run refusal + idempotent on-demand generation
- [ ] End-to-end: real completed run readable via MCP, CLI, and web

---

## Progress Log

**Status**: Implemented (phases 1-5), review findings fixed
**Branch**: Fleet run branch `serena/fleet/83170961-2fb5-4ce2-9dfe-e73410aadc3b/agent-a` (Fleet owns integration; `feat/run-reports` was not used)
**Current phase**: Fix (finalize)
**Last completed task**: Phase 5 — pytest suite + `docs/fleet-runtime.md` contract section
**Files modified**: `fleet/reports.py`, `fleet/store.py`, `fleet/supervisor.py`, `fleet/mcp.py`, `cli.py`, `ui/fleet_web.py`, `docs/fleet-runtime.md`, `tests/test_fleet_reports.py`, `tests/test_fleet_mcp.py`, `tests/test_fleet_supervisor.py`, `tests/test_fleet_web.py`
**Blockers**: none for the feature; no live-provider or installed-release verification was performed
**Review**: three major findings raised and fixed — reports are invalidated when a run reopens and enrichment is fenced by `generation`; provider identity uses `expected_model_matches` instead of exact equality; lesson records are budgeted whole with votes validated against delivered ids and omitted lessons kept `unclear`
