# Fleet reliability repair: acceptance contract

Status: incomplete. Passing the existing resilience lab is not acceptance of this repair.

### Disk recovery implementation checkpoint

Disk-exhaustion attempt receipts now atomically park the leg in
`waiting_for_resources`. The resident service probes checkout and database free
space every 30 seconds and requeues the same logical leg after both have at least
2 GiB available. Completed attempts and running run owners are preserved;
cancellation prevents wakeup. Failed probes remain parked, and the resource wait
is exposed in the status projection. This does not yet solve a database that is
too full to commit the initial receipt, resource reservations, disk-inode
exhaustion, UI controls, baseline selection, or the other recovery classes.

Verified locally: 8 disk tests (including the real scheduler with a scripted
provider) and 77 combined resource/DAG/supervision/supervisor tests. Not deployed;
no real-model acceptance or production run repair has been performed.

### Explicit baseline implementation checkpoint

New runs recognize `Fleet baseline: <local-ref>` and the exact production task's
`MANDATORY start point: branch ... at commit <40-character SHA>` directive.
The ref resolves to a commit before dispatch and is persisted separately from the
model policy. Arbitrary commit citations do not select a baseline; conflicting
directives or unresolvable explicit refs refuse dispatch rather than using HEAD.

The supervisor provisions a detached, run-owned integration worktree before
Research. Every phase and worker worktree uses that integration root; the original
project checkout stays untouched. Retry reuses the integration root and preserves
its changes, checking repository identity and baseline ancestry. Deletion refuses
to discard committed or uncommitted delivered work in that root. Status retains
`source_cwd`, the effective `cwd`, and the checkout receipt.

Real-Git tests cover all four scheduled phases, dirty source/index preservation,
retry preservation and deletion protection. Three-worker scheduler coverage now
includes real Code/Fix file edits integrated only into the run checkout.
Remaining: production repair of the affected run and live acceptance.
This remains an implementation checkpoint, not a completed reliability claim.

The explicit `own task branch named ...0N` directive now provisions each worker's
two-digit ordinal branch directly (or a literal branch for a single worker).
Fleet records that branch as its owned workspace branch and retains the name
through later refreshes. Existing branches outside this worker's ownership are
never force-moved. Workers are told the branch is already provisioned; normal
local integration does not require a remote, push or PR. Tests run three real-Git
writers through Code/Fix on the requested names without any remote configured.
74 checkout/isolation/process tests and 62 checkout/supervisor tests pass.

Older runs without a checkout receipt now adopt their explicit baseline before
dispatch, provided no worker is running and no write result or integration has
been accepted. Completed Research receipts are preserved. The migration refuses
to relocate accepted work. Clean failed worker workspaces now refresh when their
base tree or ancestry changed; "clean" alone no longer licenses reuse of a stale
checkout. Edited workspaces retain the existing preserved-patch/reapply path.
116 baseline/isolation/supervisor tests pass, including legacy adoption and its
live-worker/accepted-write refusal cases. The production run has not been retried.

### State/UI hardening checkpoint

Resource waits now render as active amber states rather than inheriting a failed
attempt's red badge. The UI shows readiness requirements, next check and retained
baseline checkout paths. The activation gate treats resource waits as active work.
Late or duplicate terminal callbacks cannot overwrite an attempt or its replacement.
Learning uses the source project only for a matching ready checkout receipt, while
fingerprint checks read the actual current integration tree.

The full Fleet pass initially recorded 484 passes and two failures: an absent
worktree `.venv` executable link and a real learning project-scope regression.
Both were corrected; 63 targeted tests and six checkout tests pass afterward,
alongside 69 desktop tests. A fresh full-suite pass is still required before delivery.

The subsequent full suite found a genuine parallel schema-migration race:
502 tests passed, one run failed with `duplicate column name: progress_stage`.
The persisted test database confirmed the error before any worker attempt began.
Supervision, peer, isolation and attempt-column migrations now lock before their
check-then-ALTER sequence. Eight-way concurrent initialization tests cover all four
stores; 103 focused tests pass after repair. Full-suite evidence from the failed
pass is retained at `_artifacts/fleet-recovery-verification-20260910/full-suite.xml`.
Another full pass is still required; this finding must not be dismissed as flakiness.

## Production evidence, 2026-09-10

Transport recovery checkpoint: narrow connection-reset/disconnected-stream/DNS
temporary-failure and 502/503/504 errors now receive two durable same-provider,
same-model retries with 30/60-second backoff. The budget is per leg and survives
restart. This is a diagnostic retry, not proof that connectivity recovered.
Authority, quota, identity, acceptance, integration and cancellation errors are
excluded. Exhaustion has a durable reason and next-action receipt. Honest stops
and exhausted transport retries now park the run in `waiting_for_input`, with
no completed timestamp and no automatic redispatch. Steering and targeted retry
are available; cancellation remains terminal, completed work stays preserved,
and the failed attempt remains truthful failure evidence. New recovery receipts
appear in Autonomy. Other unclassified terminal failure paths remain under audit.
Verification: 102 resource/autonomy/supervisor/UI tests and seven real-Git baseline
tests passed at this checkpoint. Provider responses are scripted, not live models.
Blocked-work verification: 85 completion/recovery/UI/activation tests and 147
retry/store/scheduler/UI tests pass, including persistent blockers, no automatic
resume, explicit steering/targeted resume and cancellation. Not deployed.

Native-process checkpoint: a recorded worker terminated by POSIX signal
6/9/11/13/15 now gets up to two durable same-provider process retries. Cancellation
and stalled/fenced generations are excluded by the existing ownership/lifecycle
checks. Repeated process death becomes a resumable blocker. The process test uses
a real disposable Python subprocess speaking native-protocol fixture events: it
writes a file, kills itself with SIGKILL, then resumes with the preserved patch and
completed Research attempt. This is not a live-model test. 108 process/resource/
worker/supervisor/UI tests pass. Other platform exit codes remain unclassified.

New baseline checkout receipts now place Projects-based deliverables under the
synced `_artifacts/fleet-checkouts/<run-id>/` tree. Standalone test repositories
retain colocated private state, and existing receipts are not relocated or erased.
The status/UI checkout path identifies the retained deliverables. 35 baseline,
process and resource tests pass after the storage-path change.

Run `bc257933-5fa8-4c77-ba2d-da4c2e11e80e` requested mandatory commit
`e364331db71c399b948f1a4d87c14dff43c2ec78`, explicitly not main.
All three Research attempts completed. All three Code legs subsequently failed.

- C received synthetic baseline `2736cc419e937c896656259bcec853e2eb7cbbf4`.
  A direct `git merge-base --is-ancestor` check returned 1. Its peer consultation
  answered but did not repair the mismatch; the request escalated and the accepted
  blocked receipt became a terminal failed leg.
- A independently reported the same ancestry mismatch on its own reserved checkout.
- B failed with `[Errno 28] No space left on device`. Later `df` showed 19 GiB
  available, but the run remained failed. Start-time disk preflight is insufficient.
- No worker changes were integrated according to the run's isolation projection.

## Required behavior

Attempt failure is evidence, not automatic abandonment of the user's objective.
Do not rename failure to success, weaken evidence gates, invent authority, discard
patches, restart healthy siblings, or spin indefinitely on an unchanged error.

1. Freeze an explicit repository baseline before Research and use a consistent
   run-owned integration checkout for all phases when it differs from the user's
   checkout. Preserve the user's branch, index, dirty/untracked files and refs.
   Carry the selected baseline through retries, review, integration and delivery.
   Never integrate baseline dependency changes into the unrelated user checkout.
2. Persist recovery decisions atomically with attempt outcomes. Every unfinished
   leg must have a running attempt, scheduled repair, dependency wait, resource
   wait, or actionable authority blocker; none may silently disappear.
3. Resource failures park durably and resume only after a positive readiness
   check. Cover disk capacity, provider capacity, unavailable executables,
   authentication, network and database contention separately. Do not retry
   authentication or destructive repairs as though they were connection resets.
4. Recover interrupted processes, sessions, malformed completion evidence,
   implementation failures and integration conflicts through bounded, classified
   repair. Preserve original evidence and attempts. Exhaustion becomes an explicit
   resumable blocker with diagnosis and a next action, not a success receipt.
5. Reconcile recovery after service restart and after the originating chat closes.
   Fence old process generations before redispatch. Cancel remains terminal.
   Independent healthy work must continue while another lane waits.
6. Surface the actual blocker, recovery action, next check, attempt count and
   preserved work in the UI. An answered peer request is not a resolved blocker.

## Proof required before completion

- Regression using real Git: dirty checkout on unrelated branch, mandatory baseline
  with extra files, three workers, retries and review all see the right ancestry;
  original branch/index/dirty contents are byte-for-byte preserved.
- End-to-end scheduler test: injected ENOSPC parks a leg, healthy sibling finishes,
  simulated positive free-space probe resumes the same logical leg automatically;
  completed attempt identities remain unchanged. Never fill the real disk.
- Restart each recovery state against a real temporary SQLite database; verify
  idempotence, fencing, no duplicate integrations, cancellation and failed probes.
- Fault matrix covers spawn, stream, session, timeout, completion, claims,
  integration, helper, database and notification failures, including repeated and
  mixed failures. Tests exercise actual supervisor transitions, not classifiers alone.
- Dedicated disposable live-model acceptance through official Fleet, with no
  orchestrator steering/retry after dispatch. Inspect actual identities, terminal
  evidence, independent tests and visible recovery receipts. Never inject faults
  into a work Fleet or exhaust real subscription/storage capacity.
- Full Fleet tests, relevant desktop tests, packaging checks, documentation,
  reviewed delivery and deployed-version verification. Do not restart the chat host.

No finite test suite can prove that external systems will never fail. Acceptance
requires demonstrated containment and recovery, and truthful resumable blockers
where progress needs unavailable resources or additional authority.
