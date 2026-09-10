# Fleet reliability repair: acceptance contract

Status: incomplete. Passing the existing resilience lab is not acceptance of this repair.

## Production evidence, 2026-09-10

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
