# Spec: Review Phase Upgrade

## Overview

**What**: Verify legs gain teeth — severity-routed findings, a security pass, and a bounded verify↔finalize loop until clean or budget-out.
**Why**: Findings already carry severity (blocker/major/minor) but all severities are treated identically, there is no security scan, and review runs exactly once — Fix output is never re-reviewed, so unresolved blockers ship silently.
**Scope**: IN — severity routing + report histogram, security reviewer pass (prompt + deterministic pre-pass), bounded auto-fix loop with unresolved event. OUT — blocking runs on minor findings; auto-merge/deploy on clean review.

## Requirements

- [ ] WHEN verify findings exist THEN the finalize prompt orders blockers first and the run report carries a severity histogram
- [ ] WHEN a security pass runs THEN its findings carry `category: security` and include deterministic pre-pass output (secret-pattern grep, dependency audit)
- [ ] WHEN blockers/majors remain open after finalize THEN verify re-runs scoped to the affected units, up to N rounds (default 2), then emits `run.review.unresolved` and stops
- [ ] WHEN a clean review lands THEN the loop terminates immediately with no extra rounds
- [ ] WHEN `blocker_gates_run` policy is on THEN any unaddressed blocker fails the run; when off (default) it only records

## Architecture / Design

**Changes** (blast radius):
- `MODIFIED fleet/supervisor.py` — `_findings_block` orders by severity; new loop driver reusing `prepare_phase`/`reset_leg_for_retry` for bounded verify↔finalize rounds; `run.review.unresolved` event
- `MODIFIED fleet/completion.py` — findings schema gains optional `category`; severity histogram helper for reports
- `ADDED fleet/security_pass.py` — deterministic pre-pass (secret-pattern grep via `safe_test_argv` allowlist, dependency audit) emitting machine findings into the verify envelope
- `MODIFIED fleet/reports.py` — severity histogram + unresolved-finding section from parsed findings
- `MODIFIED fleet/policy.py` — `blocker_gates_run` policy flag (default off)

**Key decisions**:
- Reuse `extract_envelope` + `_findings_for_worker` for all parsing — no second findings format
- Default loop budget 2 rounds — enough to catch fix-regressions, bounded against infinite argue-loops
- `blocker_gates_run` defaults off — gating is a policy choice per project; recording is always on
- Security pre-pass is deterministic grep/audit, not a model — machine findings ground the reviewer prompt

## Tasks

> Execution: batch as ONE fleet coding run.

### Phase 1: Severity routing
- [ ] Ordered finalize prompt + histogram helper + report section + `blocker_gates_run` policy
  - accept: fixture findings yield ordered prompt and exact histogram; gated policy fails run with open blocker, passes when addressed
  - engine: fleet

### Phase 2: Security pass
- [ ] `fleet/security_pass.py` pre-pass + `category: security` findings + reviewer checklist prompt
  - accept: fixture repo with planted secret yields a security finding; clean repo yields none
  - engine: fleet

### Phase 3: Auto-fix loop
- [ ] Bounded verify↔finalize rounds + `run.review.unresolved` on exhaustion + immediate stop on clean
  - accept: fixture with fixable blocker converges in 1 round; unfixable blocker exhausts budget and emits event
  - engine: fleet

## Edge Cases / Gotchas

- Solo runs self-review — loop still applies (author re-checks own fix), but note self-review limits in report
- Findings match Fix output via `_findings_for_worker` + finalize `changed_paths`/acceptance — fuzzy matching must be conservative (unmatched = still open)
- Security grep allowlist must stay in `safe_test_argv` — no arbitrary shell from reviewer prompts
- Loop budget counts verify rounds, not attempts — a flaky provider retry must not eat the review budget (check attempt vs round accounting)

## Testing

- [ ] Severity ordering + histogram golden test
- [ ] Gating policy on/off test
- [ ] Security pre-pass planted-secret test
- [ ] Loop convergence + exhaustion tests
- [ ] Full completion + supervisor suites green

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
