# Spec: Automation Templates + Triggers

## Overview

**What**: GitHub webhooks that can start fleet work, a template gallery for common automations, per-template caps, and one activity view across scheduler + webhooks + fleet.
**Why**: The scheduler ticks and webhooks ingress, but there's no GitHub route, no webhook→fleet path (only webhook→task→clock→fleet), no templates (every schedule is hand-built), no per-template quotas, and activity lives in three unconnected stores. Automations are powerful but unapproachable and unobservable.
**Scope**: IN — GitHub HMAC route, template gallery + instantiate, per-template caps + burst limits, unified activity view. OUT — non-GitHub providers' webhooks; auto-approving webhook fleet runs by default.

## Requirements

- [ ] WHEN a GitHub `push/pull_request/issues` webhook arrives with valid HMAC THEN it translates to a validated task-style brief (repo + event allowlists, size caps)
- [ ] WHEN a template is instantiated THEN it writes a `pending_approval` schedule (never active directly)
- [ ] WHEN a template runs THEN per-template `max_active` / `max_per_day` caps bind it, plus a global webhook burst limit
- [ ] WHEN the activity view loads THEN scheduler history + ingress history + fleet runs merge into one page
- [ ] WHEN a webhook secret is missing THEN the route refuses to register (no default secrets, same as today)

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/webhook_github.py` — HMAC-SHA256 (`X-Hub-Signature-256`) adapter → validated briefs; registered as `github` route in `default_ingress()`, `requires_approval` configurable per repo
- `ADDED config/automation-templates/*.json` + `core/automation_templates.py` — `list/get/instantiate` (action + params + interval + caps + approval flag); ships `nightly-sweep`, `github-pr-triage`, `fleet-drain`
- `MODIFIED core/serena_scheduler.py` — per-template caps on `start_ready_fleet_task` (columns or sidecar table) + global ingress burst limit
- `ADDED ui/automation_web.py` — `/automation` page merging the three histories; reuse `fleet_web._jsonable` patterns

**Key decisions**:
- Instantiate-to-pending-approval always — templates make starting easy, never make running automatic; the approval gate is the whole safety story
- GitHub first, others later — it's the only provider with fleet-adjacent demand today; the adapter shape should make a second provider trivial
- Caps at two levels (template + global burst) — a noisy repo can't starve the fleet, and a flood can't stampede it
- Merge views, not stores — three sqlite files stay; the view joins at read time (no migration risk)

## Tasks

> Execution: batch as ONE fleet coding run (templates + view are mechanical; webhook adapter is bounded).

### Phase 1: GitHub route + caps
- [ ] HMAC adapter + route registration + allowlists + per-template/global caps
  - accept: signed fixture events translate to exact briefs; bad signature/repo/event refused; caps bind under burst fixture
  - engine: fleet

### Phase 2: Templates + view
- [ ] Template gallery + instantiate-to-pending + unified `/automation` page
  - accept: instantiate yields pending schedule with exact params; page renders merged histories from fixture stores
  - engine: fleet

## Edge Cases / Gotchas

- Webhook→fleet today routes task-store→clock; the direct path must preserve approval semantics — a `requires_approval=false` repo is an explicit operator choice, logged loudly
- Size caps on briefs — GitHub payloads can be huge; truncate + link, never stuff 200KB into a task row
- Template JSON is operator-authored config — validate schema strictly on load; a broken template file must not break the gallery listing
- `fleet-drain` template vs `DEFAULT_MAX_ACTIVE_TASK_RUNS=2` — template caps compose with (never exceed) global fleet caps; document precedence

## Testing

- [ ] HMAC accept/reject + allowlist + size-cap tests
- [ ] Instantiate-to-pending golden test per shipped template
- [ ] Cap-binding test under burst fixture
- [ ] Activity-view merge test on fixture stores
- [ ] Missing-secret refusal test

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
