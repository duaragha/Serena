# Spec: User Automations (work finished before he opens the laptop)

## Overview

**What**: Let Raghav define his own scheduled work in plain language — "every weekday at 7, have the overnight numbers and the PR queue ready" — instead of only the nine reviewed actions the scheduler ships with.
**Why**: This is the competitor's most concrete promise ("close your laptop, come back to the boring stuff already handled") and Serena is close: `core/serena_scheduler.py` has approval-gated schedules, `core/scheduler_actions.py` registers nine reviewed actions (`:652-662`), `core/automation_runtime.py` ticks every 30 s, and `core/briefings.py` already builds morning and evening briefs. What is missing is a way for *him* to add one without a code change. `docs/spec-automation-templates.md` designs the template gallery and webhook side; this spec builds it and adds the user-defined piece.
**Scope**: IN — the template gallery, user-defined schedules from a brief, the approval gate, a run history surface, and the "ready before I wake" bundle. OUT — arbitrary code execution from a prompt (a template is a reviewed shape, always), the browser work (specs 9 and 10 own that; this consumes them), Fleet's own scheduling.

## Requirements

- [ ] WHEN he describes a recurring job THEN it is turned into a template instance with an explicit schedule, and starts in `pending_approval` — never running unreviewed
- [ ] WHEN he approves it THEN it runs on the existing 30 s runtime with per-template caps and a global burst limit
- [ ] WHEN a run finishes THEN its output is waiting where he asked (a brief, a text, a page), with a run record he can inspect
- [ ] WHEN a run fails THEN it retries within its cap and then reports the failure once, rather than silently stopping
- [ ] WHEN a template needs a logged-in site THEN it uses a sealed browser profile and fails loudly if the session expired, never half-completing
- [ ] WHEN a job would act consequentially THEN it requires confirmation like anything else, and unattended runs simply refuse to take that step

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/automation_templates.py` + `config/automation-templates/*.json` — the gallery from `spec-automation-templates.md` (`nightly-sweep`, `github-pr-triage`, `fleet-drain`) plus a `morning-prep` template that composes `core/briefings.py` with whatever sources he names. Each template declares its parameters, caps, the capabilities it may use, and what "done" produces.
- `ADDED core/webhook_github.py` — the HMAC-verified GitHub route with repo/event allowlists from that spec, registered through `core/webhook_ingress.py:189` like every other route.
- `ADDED ui/automation_web.py` — one `/automation` page merging the three history stores (schedules, webhook deliveries, runs), following the loopback blueprint pattern of `ui/webhook_web.py`.
- `MODIFIED core/serena_scheduler.py` — instantiate a schedule from a template plus parameters; keep `register_action` as the only way an action exists, so a template can never smuggle in new code paths.
- `MODIFIED core/scheduler_actions.py` — one new reviewed action, `serena.automation.run_template`, which dispatches by template id.
- `ADDED chats automation` CLI verbs — `list | add <template> | pending | approve | history`.

**Key decisions**:
- **Templates, not free-form execution.** A scheduled job that can run anything is a remote code execution feature with a friendly name. Reviewed shapes with declared capabilities keep the authority model intact.
- **Approval first, always.** New schedules start pending; this is already how the scheduler behaves and it is the right default for something that runs while he sleeps.
- **Compose existing actions.** `morning-prep` should be briefings plus obligations plus Fleet status, not a new pipeline. Anything a template needs that does not exist becomes its own reviewed action with its own tests.
- **Unattended means no confirmations.** A job that hits a consequential step at 6am must stop and queue it for him rather than wait on a confirmation nobody will answer — and say so in the run record.
- **Browser steps use sealed profiles** from spec 9; an expired login is an explicit re-enroll signal, never a half-finished run.

## Tasks

> Execution: fleet, one run, after track 3 (browser) so web-shaped templates have profiles to use.

### Phase 1: Gallery + runner
- [ ] `core/automation_templates.py`, template JSON, `serena.automation.run_template`, scheduler instantiation, caps
  - accept: `nightly-sweep` and `morning-prep` run on schedule with caps enforced; a template requesting an undeclared capability is refused
  - engine: fleet

### Phase 2: Approval + history surface
- [ ] `ui/automation_web.py`, CLI verbs, run records, failure reporting
  - accept: a new schedule shows as pending and does not run until approved; the page shows schedules, deliveries and runs together; a forced failure reports exactly once
  - engine: fleet

### Phase 3: GitHub webhook
- [ ] `core/webhook_github.py` with HMAC, allowlists and `github-pr-triage`
  - accept: a signed delivery triages; an unsigned or out-of-allowlist delivery is rejected and recorded
  - engine: fleet

### Phase 4: Morning prep for real
- [ ] Wire `morning-prep` to what he actually wants ready at 7am, delivered to the right surface
  - accept: a week of mornings where the brief is already waiting, with no run he had to trigger
  - engine: direct (needs his input on content)

## Edge Cases / Gotchas

- Overlapping runs: a template that is still running when its next tick arrives must skip, not stack. The 30 s runtime makes this easy to get wrong.
- Time zones and DST: schedules are his local time, and a 7am job must not drift by an hour twice a year.
- A template that texts him at 6am must still obey the notification policy — the automation decides *what*, the interrupt policy decides *when he hears about it*.
- Secrets: templates reference credentials by keyring reference, never inline in template JSON, which is config and not a vault.
- Caps exist to stop a loop: a webhook-triggered template plus a schedule can retrigger each other, so the global burst limit is not optional.
- Fleet runs launched from a template share the tree with interactive work; keep to the existing one-run-at-a-time rule.

## Testing

- [ ] Template validation tests (parameters, declared capabilities, caps)
- [ ] Scheduler instantiation and pending-approval tests
- [ ] Overlap/skip test
- [ ] Webhook signature and allowlist tests (reuse the existing webhook test patterns)
- [ ] Unattended-consequential test: the step is queued, not attempted
- [ ] Run-history surface test (loopback only)

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: browser track (specs 9, 10) for web-shaped templates
**Review**: —
