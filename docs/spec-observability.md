# Spec: Central Observability

## Overview

**What**: One append-only telemetry log + query CLI/API + latency view + crash-loop alerts, replacing today's archaeology across 3+ JSONL sinks and 4+ sqlite stores.
**Why**: "Why was she slow at 3pm" today means grepping JSONL across config dirs. Call metrics, desk metrics, scheduler, fleet, and capacity each log differently, nowhere aggregates, and nothing pages on crash loops. A 24/7 daemon system without observability is flying blind.
**Scope**: IN — central `emit` + adapters, query CLI + API, p50/p95 latency view, crash-loop alerts. OUT — metrics retention policies beyond rotation; tracing/profiling; alerting outside notification-authority.

## Requirements

- [ ] WHEN any instrumented surface records THEN one JSONL row lands in the central log with (timestamp, source, correlation id, event, duration, model/lane, outcome)
- [ ] WHEN `serena obs` queries THEN `--source/--since/--event/--tail` filter correctly over the unified log
- [ ] WHEN the latency view loads THEN p50/p95 group by (surface, lane, model) from stage + end-of-utterance events
- [ ] WHEN consecutive-failure counters trip THEN a deduped alert fires through notification-authority (config thresholds)
- [ ] WHEN the central log write fails THEN the originating surface keeps working (fail-open emit, error counted not raised)

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/observability.py` — `emit(source, event, **fields)` → `~/.local/state/serena/observability.jsonl` (superset schema: `ts, source, run_id/call_id/schedule_id, event, dur_ms, model, lane, outcome`); thin adapters in `CallTelemetry.record`, `DeskMetrics.record`, `AutomationRuntime.run_pass` (`PassReport`), scheduler `_finish`, fleet event writer
- `ADDED serena obs` CLI + `GET /api/obs?source=&event=&since=` — reuse `TurnActivityReader` tail pattern + `_load_jsonl` from desk acceptance
- `ADDED ui/` latency page — p50/p95 by (surface, lane, model) over `latency.stage.*` / `latency.eou_*` + brain `RouteDecision` rows
- `ADDED` crash-loop watcher — consecutive-failure counters (scheduler breaker trips, fleet `run.recovered` bursts, process restarts) → notification with dedupe key; thresholds in config
- `FIXED` desk dual-path bug — `desk_metrics.jsonl` lives in two dirs (`~/.config` vs `~/.local/state`); pick one, migrate, note it

**Key decisions**:
- JSONL superset, not a new DB — append-only, greppable, matches every existing sink; query layer reads tails, no migration
- Adapters, not rewrites — existing per-surface logs stay; central gets a copy. Zero behavior risk to call/desk paths.
- Alerts through notification-authority — dedupe + quiet hours already solved there; no second paging system
- Desk path fix rides along — small, discovered during research, would corrupt the latency view otherwise

## Tasks

> Execution: batch as ONE fleet coding run.

### Phase 1: Emit + adapters
- [ ] `core/observability.py` + adapters on all five surfaces + desk-path fix
  - accept: fixture activity on each surface yields exact-schema rows; emit failure never breaks the surface
  - engine: fleet

### Phase 2: Query + view
- [ ] `serena obs` CLI + `/api/obs` + latency page
  - accept: filter matrix test over fixture log; latency page shows correct p50/p95 on fixture data
  - engine: fleet

### Phase 3: Alerts
- [ ] Crash-loop watcher + deduped notification wiring + config thresholds
  - accept: fixture consecutive-failure burst fires exactly one alert; quiet-period + dedupe respected
  - engine: fleet

## Edge Cases / Gotchas

- Clock skew across writers — `ts` wall + `monotonic_us` both recorded (call telemetry already does this; copy it)
- Log rotation — JSONL grows forever; pick rotation (size or daily) at build time and document; readers must handle rotated segments
- Correlation ids must flow, not be invented per-row — pass run/call/schedule ids through the emit call sites
- The watcher watches the watchers — its own failures alert once then back off (no alert storms about alerting)

## Testing

- [ ] Schema golden test per surface adapter
- [ ] Query filter matrix test
- [ ] Latency aggregation test on fixture data
- [ ] Alert fire/dedupe/backoff test
- [ ] Fail-open emit test (broken log path, surface unharmed)

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
