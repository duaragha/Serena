# Workspace Command Parity Audit

Status: incomplete. Catalog presence and generic input forwarding are not proof
that a command's full behavior works. Gemini is deferred.

## Native Codex Account Logout (2026-09-10)

Codex `/logout` and the account dialog now open the same explicit checkbox
confirmation. No native request is sent when the dialog opens or closes. A
confirmed action is serialized against attachment and signs out every live,
idle Codex app-server owner in Serena. This is required because a two-process
native probe proved that already-running app servers cache authentication
independently: removing credentials through one process does not clear the
other process's in-memory account. Any active turn, agent, question, background
task, coding reservation, queue or unconfirmed operation blocks the whole action;
nothing is cancelled.

Each owner must return the empty native `account/logout` response, emit
`account/updated` with null auth and then report no account through
`account/read`. Repeating logout is natively idempotent. Serena clears stale
account-limit UI only after confirmation, retains every conversation and draft,
and keeps the durable command receipt from duplicating a successful request.
A partial/native failure is explicitly retryable because another logout has the
same terminal state and cannot create work.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py -q --tb=short
# exit 0: 194 passed in 1.23s.
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py -q --tb=short
# exit 0: 168 passed in 35.52s.
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_account_status_is_explicit_honest_and_preserves_draft tests/test_workspace_pane.py::test_codex_logout_requires_confirmation_preserves_draft_and_clears_limits tests/test_workspace_pane.py::test_account_connection_check_is_explicit_and_recovers_from_expired_login tests/test_workspace_pane.py::test_browser_login_requires_click_and_closing_does_not_cancel -q --tb=short
# exit 0: 8 passed in 10.92s.
node --test tests/workspace-events.test.mjs
# final exit 0: 17 passed; initial exit 1 found and fixed one test-only bracket.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --logout
# exit 0: two native owners loaded one fake disposable API key, both explicitly
# logged out, repeat logout confirmed, children/profile removed, no inference.
```

Ruff, JavaScript syntax and `git diff --check` passed. Screenshots at 390px and
1600px were inspected without overflow. Source: [official App Server account API](https://learn.chatgpt.com/docs/app-server),
accessed 2026-09-10. This does not claim that signing in one already-running
owner refreshes every other owner; that separate synchronization gap remains.

## Recoverable Native Codex Deletion (2026-09-10)

Codex `/delete`, the current-pane trash control and saved-conversation trash
controls now use one explicit confirmation flow. Serena resolves the exact
unloaded native root and its spawned-agent descendants, closes only idle
workspace owners, rejects live terminals/background reservations/pending work,
and preserves an exact private recovery copy before sending `thread/delete`.
Descendants are deleted deepest-first because native Codex refuses deletion of
a parent while forked history still references it. No terminal, replacement
owner, model turn or duplicate deletion starts automatically.

The browser records the request before delivery. A lost acknowledgement or a
failure after native mutation exposes **Check delete outcome**, which inspects
native state and finishes the same receipt without replaying `thread/delete`.
Catalog, FTS, tags and metadata are removed only after native absence is proven.
Manual/external forks that reference the family but are not represented by the
native spawned-agent graph cause a pre-mutation refusal; Serena does not guess
that they belong to the delete operation.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_archive.py tests/test_workspace_archive_host.py tests/test_workspace_delete.py tests/test_workspace_delete_host.py tests/test_workspace_catalog.py tests/test_workspace_journal.py tests/test_workspace_host.py -q
node --test tests/workspace-*.test.mjs
env SERENA_EVIDENCE_KIND=live SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-native-delete.py --browser-width 390
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_archive.py core/workspace_catalog.py core/workspace_host.py core/workspace_journal.py scripts/verify-workspace-native-delete.py tests/test_workspace_catalog.py tests/test_workspace_delete.py tests/test_workspace_delete_host.py tests/test_workspace_journal.py tests/test_workspace_pane.py
git diff --check
```

All final commands exited 0: 323 Python tests passed in 37.41s; 124 Node tests
passed and one was skipped; the native browser proof passed with one explicit
HTTP delete request, two native family deletes, one read-only reconciliation,
four reaped processes, retained draft/recovery copy and no credentials or
inference; Ruff and diff checks passed. The focused destructive pane rerun also
passed eight tests after removing `/delete` from the stale unsupported-command
fixture. Source: [official App Server reference](https://learn.chatgpt.com/docs/app-server),
accessed 2026-09-10. This is source verification, not final packaged parity.

## Unapplied Restore Native Proof (2026-09-10)

The verifier now covers failure before native restoration, completing the
previously unit-only still-archived reconciliation case. It injects a transport
failure before the native operation, clicks Check in the real browser, verifies
that native inspection still reports the exact archived transcript, and checks
that the original pending claim is finished without an automatic retry. Only a
second explicit confirmation sends a new durable request and performs restoration.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_archive.py tests/test_workspace_archive_host.py -q --tb=short
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py --browser-width 390 --fail-before-restore
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py --browser-width 1600 --fail-before-restore
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-archive-contract.py
```

Each separately executed, exit 0: 43 tests passed in 1.02s; both native browser
proofs passed; Ruff passed. Exactly one `thread/unarchive`, four disposable
processes reaped per proof, no attach/turn calls, no browser errors, unchanged
custom title/draft and no credentials/inference or user data writes. Mobile
`archive-native-unapplied-390.png` inspected: failure wording wraps and the UUID
and explicit action remain visible. Native restore success, lost acknowledgement,
lost final receipt with reload, and confirmed unapplied retry now have source
browser coverage. Packaged and broader provider parity gates are not implied.

Read-only browser discovery was also rechecked: no connected browser was returned,
so the separate authenticated model-workflow gate remains unavailable. No personal
credentials were read, copied or refreshed.

## Restore Recovery After Reload (2026-09-10)

Active and archived catalog pages now carry the exact outstanding restore request
ID from the durable journal. The bounded read does not start the host loop or
resolve/attach a runtime. A pending row opens Check restore outcome instead of
normal navigation or another restore, including when it is the current session.
The target transport can recover that server receipt with empty browser storage;
a conflicting locally saved receipt is refused rather than overwritten. A
confirmed unapplied restoration exposes a new explicit attempt, never auto-retry.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_archive_host.py tests/test_workspace_catalog.py -q --tb=short
node --test tests/workspace-connection.test.mjs
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_archived_picker_restores_only_on_confirmation_and_opens_separately tests/test_workspace_pane.py::test_saved_session_picker_does_not_submit_or_stop_running_work -q --tb=short
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py --browser-width 390 --lose-final-receipt
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py --browser-width 1600 --lose-final-receipt
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_journal.py core/workspace_host.py ui/workspace_web.py tests/test_workspace_archive_host.py tests/test_workspace_catalog.py scripts/verify-workspace-archive-contract.py
node --check ui/static/workspace-pane.mjs
node --check ui/static/workspace-page.mjs
```

All commands separately executed, exit 0: 57 backend tests in 11.87s, 59 transport
tests, six browser regressions in 6.11s, Ruff/syntax checks passed. Both native
browser proofs deliberately fail the final receipt write after catalog registration,
remove the browser's pending receipt and reload. The active row still exposes the
original request; read-only reconciliation completes it. Exactly one unarchive,
four native processes reaped, no attachment/turn calls, no browser errors, draft
preserved and no user data or credentials touched. Mobile restored screenshot
inspected with the full UUID and controls fitting. The desktop proof additionally
prints the explicit rediscovery receipt. This closes the reload-discovery gap
described below for sessions present in the catalog; it does not establish packaged
parity or complete the broader authenticated-provider delivery gates.

## Explicit Restore Outcome Recovery (2026-09-10)

The restore dialog now offers Check restore outcome after an error. This uses
the same saved request ID on an authenticated reconciliation endpoint. The host
requires an existing exact restoration claim and repeats the ownership/work
guards, then inspects native identity, project, transcript location and unloaded
thread state under the shared lease. Inspection sends no unarchive/resume/turn
request. Confirmed active state completes the original success receipt; confirmed
archived state completes an explicitly retryable failure. Only that confirmed
failure clears the browser's pending request for another explicit restore attempt.
Unknown identity, ownership or cleanup leaves the claim blocked, not guessed.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_archive.py tests/test_workspace_archive_host.py -q --tb=short
node --test tests/workspace-connection.test.mjs
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_archived_picker_restores_only_on_confirmation_and_opens_separately tests/test_workspace_pane.py::test_saved_session_picker_does_not_submit_or_stop_running_work -q --tb=short
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py --browser-width 390 --lose-restore-ack
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py --browser-width 1600 --lose-restore-ack
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_archive.py core/workspace_host.py ui/workspace_web.py tests/test_workspace_archive.py tests/test_workspace_archive_host.py scripts/verify-workspace-archive-contract.py
node --check ui/static/workspace-pane.mjs
node --check ui/static/workspace-page.mjs
```

All commands separately executed, exit 0. Final backend tests: 42 passed in
2.00s; 58 transport tests; browser regression tests: 6 passed in 9.29s; lint and
syntax checks passed. Both real browser/native proofs inject a lost acknowledgement
after the actual native restore, recover through the visible Check action and
assert exactly one `thread/unarchive` overall. Four disposable processes reaped
per proof (the fourth is read-only inspection), no coding owner, credentials,
inference, browser errors or user data changes. Receipt replay after restart also
passes. Still-archived recovery is covered by unit tests, not yet native proof.

Remaining recovery gap: discovering a pending restore after catalog registration
succeeded but the final receipt was lost and the view was subsequently reloaded.
That chat may already be absent from the Archived list; a separate pending-restore
entry point is still needed. Packaged checks and broader delivery gates remain.

## Browser-To-Native Restore Proof (2026-09-10)

The archive verifier now optionally starts the real source Flask workspace and
headless Edge. It creates a disposable native Codex transcript, archives it, then
uses the production picker, target transport, authenticated HTTP route, host,
journal and native restore implementation end to end. Only the profile, lease
directory and catalog are isolated; no UI callbacks or native RPC results are
simulated. The browser never attaches a coding owner.

```sh
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py --browser-width 390
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py --browser-width 1600
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_archive_host.py tests/test_workspace_archive.py -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-archive-contract.py
```

All separately executed, exit 0. Each browser proof confirmed explicit restore,
exact native ID and retained custom title, active/archive catalog transition,
preserved draft, separate same-session navigation, no attachment/turn HTTP calls,
no console exceptions or HTTP errors, and receipt replay after host restart.
Each reaped three native processes and removed its disposable profile, without
credentials/inference or user data writes. Tests: 26 passed in 0.98s; Ruff passed.
Inspected native confirmation/restored screenshots at 390px and 1600px under
`apps/desktop/build/workspace-proof/archive-native-{confirm,restored}-{390,1600}.png`;
full UUID and controls fit without horizontal overflow.

This closes the source-browser restore integration gap, not the packaged
Linux/Windows check, uncertain-outcome recovery, archive mutation/descendant
guards, or the broader authenticated Claude/Codex delivery gates.

## Archived Conversation Picker (2026-09-10)

The Codex saved-conversation dialog now has Active/Archived radio filters with
the existing search and paging. Archived rows open an explicit restoration
confirmation instead of navigating into a coding owner. A successful restore
refreshes the archive list and exposes a separate Open action; it never
automatically opens, attaches or sends a message. Claude's existing picker is
unchanged. Drafts remain intact, and errors stay visible in the confirmation.

The target connection persists its restoration request ID before HTTP delivery
and reuses it after a lost response or reload. It validates the returned exact ID
and archive state before clearing the pending receipt. No connection polling or
attachment starts when this temporary transport is constructed.

```sh
node --test tests/workspace-connection.test.mjs
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_archived_picker_restores_only_on_confirmation_and_opens_separately tests/test_workspace_pane.py::test_saved_session_picker_does_not_submit_or_stop_running_work -q --tb=short
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py
node --check ui/static/workspace-pane.mjs
node --check ui/static/workspace-page.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py
```

Each command ran separately and exited 0: 57 transport tests; final browser run
6 passed in 6.65s at 390px/1600px, covering cancel, visible restore error, explicit
retry, exact open destination, separate opening and unchanged draft. Native
host/index restore proof passed with three processes reaped and no credentials
or inference. Node syntax checks and Ruff passed. Screenshots
`apps/desktop/build/workspace-proof/archive-restore-{390,1600}.png` inspected:
full UUID fits, actions and status do not overlap, no horizontal page overflow.

These browser tests use controlled callbacks; the native proof covers the host
separately. Full browser-to-native and packaged restore flow remain unverified.
Uncertain-outcome reconciliation and archiving existing sessions (including
descendant guards) are still pending. Nothing installed or released.

## Durable Archive Restore Control (2026-09-10)

`WorkspaceHost.restore_archive` and the authenticated `POST /api/workspace/<sid>/restore-archive`
route now require explicit confirmation and exact session/request UUIDs. Resolution
uses the host's existing admission checks, not a caller-supplied project. Runtime
owners, job reservations, queued bridge work and unconfirmed work/clear operations
block restoration. The native operation does not attach a coding owner.

The journal atomically claims a restoration before native mutation, allowing only
one outstanding restore per session even with different request IDs. Success is
recorded only after native verification and catalog registration. Repeated requests
and a restarted host return the saved result without launching anything. Lost
native acknowledgements, index failures and receipt-write failures remain explicitly
unconfirmed: neither another request ID nor attachment bypasses that state.
Explicit reconciliation of these uncertain outcomes and the picker controls are
still pending. No release, installed-app change or automatic terminal launch.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_archive_host.py tests/test_workspace_archive.py tests/test_workspace_catalog.py -q --tb=short
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_journal.py -q --tb=short
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py ui/workspace_web.py tests/test_workspace_archive_host.py scripts/verify-workspace-archive-contract.py
```

Commands ran separately, all final exits 0: 62 focused tests in 2.02s; 172
host/journal regressions in 33.88s; Ruff passed. Real unsigned native restoration
now goes through the production host, verifies catalog state, repeats its request
and replays the receipt after host restart. Still exactly three disposable native
processes, all reaped; no coding owner, credentials, inference or user data changes.
The initial live run exited 1 because the new host expected a receipt directly
from `register_fork`, which normally returns no value. Fixed by reusing the existing
`_register_created_fork` adapter; the subsequent live run exited 0.

## Authenticated Archive Catalog Endpoint (2026-09-10)

The saved-session endpoint now accepts exactly one `archived=true` or
`archived=false` query parameter; omission still selects active sessions. Invalid
or repeated values fail before catalog access. Existing loopback, origin and
token checks also protect archived queries. The endpoint is read-only and needs
no runtime owner. The picker and durable restore action remain pending; this does
not expose an unsafe archived-session Resume button.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_workspace_archive.py -q --tb=short
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check ui/workspace_web.py tests/test_workspace_catalog.py scripts/verify-workspace-archive-contract.py
```

All commands ran separately and exited 0: 47 tests passed in 1.05s; Ruff passed.
The native probe now exercises the real authenticated Flask route against its
disposable real index before and after restoration, proving exact-ID active/archive
separation and the retained custom title. Its host is deliberately an inert object,
so catalog reads cannot attach a session. Three native processes reaped, no
credentials or inference, no user data changed. This is endpoint/runtime proof,
not browser or packaged-application verification.

## Exclusive Archive Restoration Primitive (2026-09-10)

`core/workspace_archive.py::restore_codex_archive` now performs native restoration
under the shared exclusive session lease. Explicit confirmation, canonical session
identity, exact project and native archived transcript location are required before
mutation. Restoration verifies the same ID and active transcript location, then
checks that no coding thread was loaded. It never resumes a session, starts a turn
or retries an ambiguous acknowledgement. Transport construction and shutdown
failures release the lock without discarding uncertain live-process ownership.

This is not yet a user-facing restore feature: the host must first provide a
durable mutation receipt, enforce job reservations and reindex the result before
the archive picker can expose it. No installed app or default behavior changed.

Separately executed verification commands:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_archive.py tests/test_workspace_catalog.py -q --tb=short
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_archive.py tests/test_workspace_archive.py scripts/verify-workspace-archive-contract.py
```

Each exited 0. Tests: 39 passed in 0.95s, including foreign identity/project,
existing ownership, missing confirmation, transport creation failure, lost reply,
wrong restored identity, unexpected loaded writer and uncertain cleanup. Native
proof now calls the production restoration function, not a raw unarchive RPC:
exact history restored, custom title/done/group metadata unchanged, no loaded
coding writer or inference. Three disposable processes reaped; no credentials
used, temporary profile removed and no user sessions/project files changed.
Ruff: all checks passed. The older two-process receipts below describe earlier
versions of the probe, not this run.

## Read-Only Inspection During Recovery (2026-09-10)

`/status` and `/usage` no longer silently stop at the disabled message-send gate
during reconnection or history reconciliation. They open their existing read-only
dialogs; status can display local state before attachment, and unavailable native
usage remains an explicit error. This does not enable normal message submission,
resume a session, answer approvals or change its runtime state. Inspection during
a normal running turn was already supported; this fixes the disconnected states.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_codex_readonly_commands_work_without_enabling_message_submission tests/test_workspace_pane.py::test_native_token_usage_has_explicit_refresh_and_preserves_draft tests/test_workspace_pane.py::test_codex_status_is_read_only_updates_and_does_not_invent_values -q --tb=short
env SERENA_EVIDENCE_KIND=live SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py scripts/verify-workspace-codex-history.py
node --check ui/static/workspace-pane.mjs
```

Each command ran separately, exit 0. Tests: 8 passed in 14.79s, including 390px
and 1600px unavailable/reconciling states, preserved drafts and disabled normal
submission. Native browser proof confirmed `/status` before attachment with zero
owners and no submitted draft, then passed existing history, command output,
exit/clear and same-session restore scenarios on desktop/mobile. Visibility
refresh: 117ms/80ms. Native processes reaped, temporary project untouched, no
credentials/inference. Ruff passed; Node no syntax errors. No new release claim.

## Native Archive Contract Research (2026-09-10)

Catalog foundation now implemented: SQLite `is_archived` is derived from the
native Codex transcript path when a session is registered/reindexed. Migration
backfills existing archived paths, including Windows separators, without changing
done state or custom metadata. Active and archived queries are separate through
`list_sessions(archived=...)` and `list_saved_sessions(..., archived=...)`.
Claude paths do not acquire Codex archive state. Restore/re-registration clears
the derived flag on the exact same row. No duplicate synced archive flag is
written alongside the custom title or linked-group metadata.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_codex_scanner_resident.py tests/test_codex_scanner_skips_copies.py -q --tb=short
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_catalog.py tests/test_workspace_catalog.py scripts/verify-workspace-archive-contract.py
```

All separately executed, exit 0: 38 tests passed in 2.62s; the real native probe
now also reindexes archived/restored sessions and verifies unchanged title, done
and group metadata. Two owned processes reaped, no credentials/inference or user
data writes. Ruff passed for these files. Initial catalog-only run: exit 1,
27 passed/1 failed because the synthetic rollout filename omitted its required
date; corrected fixture then passed 28 tests in 2.88s. Two import-order findings
were fixed in the new test/probe code.

Shared indexer lint remains at its ten pre-existing findings, not new findings:
`ruff check core/indexer.py core/workspace_catalog.py tests/test_workspace_catalog.py scripts/verify-workspace-archive-contract.py`
exited 1 with ten indexer findings. Baseline comparison
`git show HEAD:core/indexer.py | /home/raghav/Documents/Projects/serena/.venv/bin/ruff check --stdin-filename core/indexer.py --output-format concise -`
also exited 1 with the same ten findings. Unrelated cleanup was left alone.
The archive mutation, descendant ownership guards, archive/restore picker,
sidebar counts and cross-platform integration are still pending; this foundation
is not a completed or released archive feature.

Archive is still unimplemented in the rich pane. The disposable native probe
`scripts/verify-workspace-archive-contract.py` now establishes the runtime
contract, rather than assuming archive is a done toggle:

- A real persisted print-only thread produces `thread/archived` for its exact ID
  after `thread/archive` returns an empty success object.
- Its transcript moves from `sessions` to `archived_sessions`; content remains.
- A separate unsigned app-server can read archived turns without resuming.
- `thread/unarchive` restores the same ID and file. `thread/loaded/list` stays
  empty afterward: restoration does not start a writer.

```sh
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-archive-contract.py
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-archive-contract.py
```

Both separately executed, exit 0. Native notification, transcript movement,
preserved history, exact restore and no loaded writer confirmed. Two processes
reaped, disposable profile removed, no credentials/inference or user data writes.
Ruff: all checks passed.

Initial integration constraints, before the catalog foundation above:
`core/codex_scanner.py` excludes archived sessions; `register_fork` in
`core/workspace_catalog.py` can locate their moved transcripts, but
`list_saved_sessions` has no archive filter. `core/indexer.py::toggle_done`
sets completion metadata, not native archive state. Therefore an archive button
must not merely invoke the native method or toggle done. It needs explicit
archive/restore catalog state, retained linked-session identities, confirmed
idle ownership, a durable mutation receipt and view refresh. Native archive
also affects spawned descendants according to the
[official app-server contract](https://learn.chatgpt.com/docs/app-server)
(accessed 2026-09-10), so descendant ownership must be checked before exposing
the mutation. This probe covers a root without descendants, not that final gate.

## Codex Clear Context (2026-09-10)

Follow-up source browser verification now covers the full navigation round-trip:
type `/clear`, confirm, inspect the exact new ID, open its real pending native
session, verify the source output is absent, explicitly disconnect it, then
resume the original conversation and inspect its persisted output. Both 1440px
desktop and 390px mobile passed, and screenshots were visually inspected.
The initial browser proof exited 1 because its selector expected Resume session
instead of the existing Resume original conversation control; the verifier was
corrected, without changing production navigation.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_clear_requires_confirmation_and_recovers_exact_target_without_repeating tests/test_workspace_host.py::test_codex_clear_releases_writer_before_durable_creation -q --tb=short
env SERENA_EVIDENCE_KIND=live SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-history.py
```

All separately executed, exit 0. Tests: 11 passed in 6.62s. Native browser proof
also preserved 50+1 history pagination, native output/exit 0, receipt deduplication,
exact job reservation, file mentions, skill configuration, `/exit` cancellation,
`/quit` cleanup, and non-cancelling page close. Visible refresh: 58ms/61ms.
No browser console/network errors, credentials or inference; owned processes
closed and temporary project unchanged. Ruff: all checks passed. This adds
source HTTP/browser evidence, not packaged Electron or authenticated evidence.

Codex now exposes Clear context and bare `/clear` using the existing confirmation
and exact-target recovery dialog. History and the original draft are retained;
the new chat is opened only on the user's explicit action. Named `/clear title`
arguments remain unsupported, rather than silently sent to the model.

The initial same-process approach was rejected by native verification: two
proof runs exited 1 when reopening the source returned `already has an active
writer`. [Official app-server documentation](https://learn.chatgpt.com/docs/app-server)
(accessed 2026-09-10) confirms unsubscribe retains the thread for a 30-minute
inactivity grace period. It is not a writer-release acknowledgment.

Final implementation closes only the verified-idle Codex owner, confirms cleanup,
then uses the existing durable native-creation path with a deterministic creation
request ID derived from source and clear request. The new native identity is
checkpointed by creation and clear receipts; repeated requests cannot create
another thread. Active turns, questions, agents, background terminals, queued
bridge work and coding-job reservations block clear. The original session can
resume independently after clear, without waiting for the native grace period.
Claude retains its existing same-runtime SDK handoff.

Exact final commands, each run separately:

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_codex_clear_releases_writer_before_durable_creation tests/test_workspace_host.py::test_clear_checkpoint_exact_owner_routing_and_no_replay tests/test_workspace_host.py::test_clear_requires_confirmation_idle_owner_and_no_queued_bridge tests/test_workspace_pane.py::test_clear_requires_confirmation_and_recovers_exact_target_without_repeating tests/test_workspace_journal.py -q --tb=short
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-clear.py
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-codex-clear.py
node --check ui/static/workspace-pane.mjs
```

All exit 0. Tests: 25 passed in 8.07s. Coverage includes release-before-create,
background/agent/cleanup refusal, lost creation/checkpoint/receipt, deduplication,
Claude regression and Codex `/clear` dialog at 390/1600px. Two intermediate runs
exited 1 (24 passed, 1 failed) because the receipt-failure test inspected the
incomplete clear record rather than the separately committed creation checkpoint;
the assertion now checks the correct durable record.

Native proof passed twice after replacing the unsubscribe design: old writer
closed, new exact session, deduplicated receipt, real print-only output confined
to the target, old history independently resumed, four processes reaped, no
credentials/inference, temporary profile removed and project unchanged. Ruff
passed; Node no syntax errors. Source feature only; packaged browser clear flow,
authenticated work and release remain part of the full delivery gates.

## Explicit Session Exit Commands (2026-09-10)

Codex `/exit` and `/quit` now route to the existing Disconnect session dialog,
not a model prompt. Unlike the CLI's immediate exit, Serena retains explicit
confirmation and refuses a busy/queued/reserved session. Cancellation preserves
the owner and composer. Failure stays visible and retryable; history is retained.
Arguments, attached files, skills and selected apps are not silently discarded.
This closes the missing command route, not archive/delete/logout functionality.

Evidence: [official commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
accessed 2026-09-10, defines exit/quit as CLI exits. Browser discovery still
returned no connected browser; this turn did not attempt another expired login.

Exact commands, each run separately from this checkout:

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_codex_exit_requires_confirmation_and_preserves_failed_draft tests/test_workspace_host.py::test_explicit_disconnect_preserves_history_and_never_stops_other_owner -q --tb=short
env SERENA_EVIDENCE_KIND=live SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-codex-history.py
node --check ui/static/workspace-pane.mjs
```

All exit 0. Tests: 16 passed in 8.83s (mobile/desktop, both aliases, failed retry,
cancel, arguments, busy state; Claude/Codex backend history/other-owner/cleanup
guards). Native proof: 51 print-only persisted turns, 50+1 history, real command
output/exit 0, receipt deduplication; desktop and mobile `/exit` cancellation kept
the native owner, confirmed `/quit` reaped it, and retry resumed the exact stored
session with output intact. Visibility refresh 40ms/59ms. Project unchanged,
all owned children closed, no credentials or inference. Ruff passed; Node no
syntax errors. This is source verification, not an installed/released build.

## Native Session Usage Estimates (2026-09-10)

The usage dialog now separates Account from This session. The latter calls
`account/usage/read` with the attached owner's exact `threadId`; callers cannot
choose another thread. Returned foreign IDs are rejected. Native credit/USD
estimates and per-model token breakdowns retain missing values and exact int64
precision. No local pricing calculation, inference turn, or automatic polling
is introduced. Model groups paginate locally in batches of 20.

The radio group uses a local sequence, not secure-context-only randomUUID.
The initial browser run exposed this HTTP compatibility defect (14 passed,
4 failed, exit 1); after repair the four browser cases passed in 5.83s, exit 0.
Desktop 1600px and mobile 390px screenshots were inspected without overflow.

Final verification, from the isolated workspace checkout:

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_session_usage_is_exact_estimated_and_preserves_int64 tests/test_workspace_codex.py::test_account_token_usage_is_bounded_exact_and_preserves_missing_data tests/test_workspace_host.py::test_session_usage_scope_is_readonly_and_cannot_choose_another_session tests/test_workspace_host.py::test_account_status_requires_explicit_owner_and_rejects_mutations tests/test_workspace_pane.py::test_session_usage_estimates_keep_scope_precision_and_missing_values tests/test_workspace_pane.py::test_native_token_usage_has_explicit_refresh_and_preserves_draft -q --tb=short
```

Exit 0: 18 passed in 6.25s. Coverage includes exact routing, reservation-safe
reads, receipt deduplication, precision, missing data, foreign IDs, error clearing,
explicit refresh and preserved drafts.

```sh
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --usage
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py scripts/verify-workspace-account.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py
node --check ui/static/workspace-pane.mjs
node --check ui/static/workspace-page.mjs
```

Each command ran separately, exit 0. Native account and session usage both
refused the unsigned disposable profile; same owner, no browser or inference,
child reaped and temporary profile removed. Ruff: all checks passed; Node: no
syntax errors. Positive signed-in service data remains unverified pending a
fresh dedicated test login. This slice is not installed or released and does
not establish full Claude/Codex parity.

## Native Account Token Activity (2026-09-10)

Codex `/usage` and the Account token usage action now open an account-wide native
activity snapshot through `account/usage/read` with no thread parameter. Lifetime
tokens, peak daily tokens, longest turn, current/longest streak and daily buckets
come from that response. This is not the 5h/7d rate-limit percentage, a local
transcript token sum, or a per-thread billing estimate.

The adapter whitelists fields, preserves missing metrics as null, rejects invalid
dates/duplicate days/negative counters and oversized daily lists, and serializes
int64 counts as decimal strings so JavaScript cannot round them. Native daily
dates retain their literal date; no client timezone conversion changes the day.
Daily rows load locally in groups of 31 from the explicit snapshot. Null daily
data and an empty daily list have distinct unavailable/empty states. Errors clear
prior data, Refresh is explicit, and closing the dialog leaves the draft intact.
Readonly usage checks are allowed on the exact attached owner during work or a
job reservation; they never submit a turn or request token-refresh explicitly.

Official evidence: https://learn.chatgpt.com/docs/app-server, Token usage section,
accessed 2026-09-10. Checked against installed 0.153.4 schemas
`GetAccountTokenUsageResponse.json` and `NullableGetAccountTokenUsageParams.json`.
The installed schema also supports thread-specific estimates; this account view
does not claim to implement those. A signed-in service response remains a live
verification gap; positive UI/data contracts use controlled responses.

Verification:

- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_account_token_usage_is_bounded_exact_and_preserves_missing_data tests/test_workspace_codex.py::test_account_token_usage_rejects_invalid_daily_activity tests/test_workspace_host.py::test_account_status_requires_explicit_owner_and_rejects_mutations tests/test_workspace_pane.py::test_native_token_usage_has_explicit_refresh_and_preserves_draft tests/test_workspace_live_agent_proof.py -q --tb=short`: exit 0, 40 passed in 6.82s. Initial run before explicit null/empty daily UI assertions: exit 0, 40 passed in 8.91s. Screenshots inspected at 390px and 1600px.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --usage`: exit 0. Real installed Codex rejects unsigned token activity, retains the same owner, sends no model turn, and reaps its child/removes its temporary profile. No user credentials copied or changed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py scripts/verify-workspace-account.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py`: exit 0, all checks passed.
- `node --check ui/static/workspace-pane.mjs`: exit 0, no output.
- `node --check ui/static/workspace-page.mjs`: exit 0, no output.

Development packages still represent runtime `3725a32`; this feature is not yet
packaged, installed, released or default-enabled.

## Failed Preference Recovery (2026-09-10)

Failed Codex personality/speed restoration now exposes an exact failure identity
only after the failed owner is fully closed and safely retryable. The connection
passes that recovery metadata only for its own session. The pane offers an
explicit confirmation dialog to clear just that saved override; it does not
connect, submit, alter Plan mode, erase history, or discard drafts.

The host rejects stale failure identities, unconfirmed/extra payloads, coding-job
reservations, pending work, and owners without confirmed cleanup. A journal writer
transaction compares the expected preference and its exact journal revision and records a setting-specific
reset marker. Historical events remain intact; other sessions/settings and any
newer explicit preference still apply. Command receipts deduplicate delivery.
The user separately clicks Retry connection to resume the same native session.

This is not a general corrupted-journal repair or an automatic bypass of failed
Plan-mode restoration. Those failures remain closed. No installed runtime was
restarted, and no authentication or model inference was needed for this slice.

Verification:

- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_failed_saved_setting_recovery_is_exact_confirmed_and_does_not_launch tests/test_workspace_host.py::test_saved_speed_restores_before_admission_and_refuses_failed_restore tests/test_workspace_journal.py tests/test_workspace_pane.py::test_saved_setting_recovery_requires_confirmation_without_reconnect -q --tb=short`: exit 0, 17 passed in 6.61s. Recovery screenshots inspected at 390px and 1600px.
- `node --test tests/workspace-connection.test.mjs`: exit 0, 55 passed in 194.609087ms; exact-session attach-error metadata and existing receipt/transport regression coverage.
- `env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_saved_preference_recovery_flows_through_real_page_and_host -q --tb=short`: final exit 0, 2 passed in 5.54s. Full page, HTTP, host and journal at 1440px/390px, controlled provider only. Initial exit 1, 2 failed in 3.10s because the test did not pass the configured browser executable; fixed the new test to honor it.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-setting-recovery.py`: exit 0, real native invalid-speed restoration fails closed, confirmed reset launches nothing, explicit retry resumes exact history and Plan mode; three processes reaped and temporary profile removed. No authentication/inference.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-speed.py`: exit 0, native priority restoration and explicit default clear still survive replacement; four processes reaped, no authentication/inference.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py tests/test_workspace_host.py tests/test_workspace_journal.py tests/test_workspace_pane.py tests/test_workspace_app.py scripts/verify-workspace-setting-recovery.py`: exit 0, all checks passed.
- `node --check ui/static/workspace-page.mjs`: exit 0, no output.
- `node --check ui/static/workspace-pane.mjs`: exit 0, no output.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_journal.py -q --tb=short`: exit 1, 156 passed/1 failed in 25.89s; the browser composer test lacked the installed Edge executable override. No code failure was observed; rerun below includes the corrected environment.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_failed_saved_setting_recovery_is_exact_confirmed_and_does_not_launch tests/test_workspace_host.py::test_saved_speed_restores_before_admission_and_refuses_failed_restore tests/test_workspace_host.py::test_browser_composer_reaches_host_and_reloads_same_owner tests/test_workspace_journal.py tests/test_workspace_pane.py::test_saved_setting_recovery_requires_confirmation_without_reconnect tests/test_workspace_app.py::test_saved_preference_recovery_flows_through_real_page_and_host -q --tb=short`: exit 0, 20 passed in 14.58s after adding the journal revision guard. Includes rejection of a newer preference with the same value. The native setting-recovery proof was rerun after that guard, exit 0, three processes reaped and Plan mode preserved.

## Account Connection Check (2026-09-10)

The Codex account dialog now has an explicit Check account connection control.
It uses the existing native account/rateLimits/read route, not forced token refresh
or an inference request. Opening the dialog still only reads stored account
metadata. The check reports the returned observation time or the actual failure;
it never treats stored credentials as proof of remote validity or rate-limit
retrieval as proof that model execution succeeds. Successful snapshots also feed
the existing Session status display without requiring that dialog to be open.

Pending login and in-flight account operations disable the check. Account refresh
and login actions clear its displayed result; closing the dialog discards late
responses. The composer is untouched on success, error, and close. No logout or
personal-account mutation was added or performed.

Official evidence: https://learn.chatgpt.com/docs/app-server, accessed 2026-09-10,
Authentication API overview and Rate limits sections. Native account/read with
refreshToken false is a stored-account read; account/rateLimits/read retrieves
account limits. Positive UI rendering uses a controlled response, not a claim
of renewed authentication. The dedicated signed-in proof remains outstanding.

Verification:

- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_account_connection_check_is_explicit_and_recovers_from_expired_login tests/test_workspace_pane.py::test_account_status_is_explicit_honest_and_preserves_draft tests/test_workspace_pane.py::test_browser_login_requires_click_and_closing_does_not_cancel -q --tb=short`: final exit 0, 6 passed in 9.05s. Screenshots inspected at 390px and 1600px. Initial exit 1, 2 failed/4 passed in 69.38s, caught an unconditional call to the status dialog's optional renderer; fixed. Intermediate exit 0, 6 passed in 7.22s; final fixture uses an actual Codex pane.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --browser-login --limits --command-guard`: exit 0. Native unsigned limits refusal, login start/cancel, exact owner, no inference or browser launch, temporary profile removed and child reaped. This checks the underlying native route, not a successful signed-in account.
- `node --check ui/static/workspace-pane.mjs`: exit 0, no output.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py`: exit 0, all checks passed.

## Proof Login Isolation Follow-up (2026-09-10)

All five authenticated Codex proof entrypoints now require an explicit separate
`--auth-home` test profile. Account, MCP, roundtrip, and durable native-work proofs
previously still copied the normal/active login implicitly. They now share the
live-agent proof's guard against default, active, symlinked, and hardlinked login
files. Missing credentials fail before native launch. Unsigned account checks,
MCP inventory, and the isolated native-work child retain their existing paths.
Only verification tooling changed; no user authentication, installed app, or
provider settings were changed. Historical signed proof commands below now need
the additional `--auth-home /path/to/separate-test-profile` argument.

This does not repair expired authentication. Real authenticated model and child
workflows remain unverified until the dedicated test login is renewed. Copying
a disposable test login may still rotate that test profile's refresh token;
never supply a working personal profile.

Verification from the isolated workspace checkout:

- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_live_agent_proof.py -q --tb=short`: exit 0, 23 passed in 1.14s. Earlier account-only checkpoint: 20 passed in 0.64s, exit 0.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/workspace_proof_auth.py scripts/verify-workspace-account.py scripts/verify-workspace-live-agent.py scripts/verify-workspace-mcp.py scripts/verify-workspace-codex-roundtrip.py scripts/verify-workspace-native-work.py tests/test_workspace_live_agent_proof.py`: exit 0, all checks passed.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --browser-login --limits --command-guard`: exit 0. Real native login start/cancel, unsigned limits rejection, unsupported command rejection, same owner, child reaped, temporary profile removed. No browser opened or inference performed.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-mcp.py --inventory-only`: exit 0. Exact-thread native MCP inventory and connection status, idle-thread permission-profile selection, owned process reaped. No inference, tool invocation, or copied authentication.

## Session Speed (2026-09-10)

`/fast` now opens an explicit session-speed control. Native model service tiers
populate the picker; Apply invokes thread/settings/update without submitting a
turn. The current model is rechecked, busy work and queued/reserved jobs reject
changes, and null explicitly selects the default. This is session-scoped, not a
global CLI configuration change; no unsupported tier or pricing is invented.
The existing next-turn speed selector remains an explicit per-turn override.

Native inspection found that settings/update alone does not preserve speed
through process replacement. Confirmed workspace/speed events now persist the
selection in the journal and restore it before work admission. Explicit per-turn
speed overrides update that record too. Restoration validates against the resumed
model's current native catalog and fails closed if unsupported.

The live proof also found an unrelated-setting restoration failure: native
settings updates report a default personality even on models that do not support
personality. These observations are now marked unconfirmed and are not mistaken
for explicit user preferences; old records remain compatible. Explicit confirmed
personality selections still restore, verified on the real runtime.

Source: https://learn.chatgpt.com/docs/app-server accessed 2026-09-10, plus the
installed v2 ThreadSettingsUpdateParams schema: serviceTier overrides subsequent
turns, omission leaves it unchanged, null clears it. The installed native runtime
normalizes an explicit clear to serviceTier `default` in its confirmation event.

Verification:

- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_speed_tier_is_native_exact_and_does_not_start_a_turn tests/test_workspace_codex.py::test_personality_uses_native_capability_and_exact_session tests/test_workspace_host.py::test_saved_speed_restores_before_admission_and_refuses_failed_restore tests/test_workspace_pane.py::test_session_speed_changes_without_sending_and_keeps_failed_selection -q --tb=short`: exit 0, 18 passed in 8.43s.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_session_speed_changes_without_sending_and_keeps_failed_selection tests/test_workspace_journal.py -q --tb=short`: exit 0, 9 passed in 9.80s. Desktop/mobile screenshots inspected; drafts and failed choices preserved.
- `node --test tests/workspace-connection.test.mjs`: exit 0, 54 passed in 498.993ms.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-speed.py`: final exit 0, real `priority` setting and explicit clear retained across host replacements, four native processes reaped, one print-only history turn, no inference/authentication. Earlier exit-1 attempts exposed the unsupported default-personality restore and a proof assumption that cleared native speed was null rather than `default`; fixed both. Initial direct adapter proof also exited 1 and demonstrated the native persistence gap.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-personality.py`: exit 0, native friendly personality restored across replacement, three processes reaped, no inference/authentication.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py core/workspace_journal.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-speed.py`: final exit 0, all checks passed; initial exit 1 for compact one-line statements, expanded without behavioral changes.
- `node --check ui/static/workspace-pane.mjs`: exit 0, no output.

No release or installed-app change. Existing packaged checkpoints predate this
speed change; full parity and authenticated workflow evidence remain open.

## Native Agent Inspection (2026-09-10)

### Dedicated Authentication for Live-Agent Proof

The inference verifier now requires `--auth-home /path/to/separate-test-profile`
in addition to `--allow-inference`. It no longer falls back to CODEX_HOME or
`~/.codex`. Normal/active profile paths, aliases and linked auth files are
rejected before reading credentials or spawning a process. Use a freshly signed
in disposable profile: copying rotating refresh credentials can render that
test profile stale after use, so it must not be a personal working profile.
This repair does not refresh the expired test login or prove model inference.

- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_live_agent_proof.py -q --tb=short`: exit 0, 9 passed in 0.28s, including missing/default/active/linked profile refusal and unchanged explicit test credentials.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-live-agent.py tests/test_workspace_live_agent_proof.py`: initial exit 1 for import order only; after reordering imports, final exit 0, all checks passed.
- Safe runtime proof (exit 0, no credentials read, no process started):

```sh
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python - <<'PY'
import runpy
from pathlib import Path
read_auth = runpy.run_path('scripts/verify-workspace-live-agent.py')['read_test_auth']
try:
    read_auth(Path.home() / '.codex')
except ValueError as error:
    assert 'normal or active' in str(error)
    print('PASS: installed helper rejects normal Codex profile before reading credentials or starting a process')
else:
    raise AssertionError('Normal profile was accepted')
PY
```

### Explicit Idle-Child Continuation

An inspected, loaded idle child can receive a new text turn after checkbox
confirmation. The existing parent connection rechecks native ancestry, latest
turn identity, terminal history, parent readiness, outstanding questions and
active delegated work. Reserved jobs and queued bridge messages reject this
action. No thread/start, thread/resume, new process, model or permission override
is used: only turn/start on that child. An ambiguous response leaves ownership
uncertain and prevents another start, including cancellation while awaiting the
response. Native completion can release child busy state before acknowledgment
without the acknowledgment making it busy again.

The UI retains failures, clears accepted drafts, blocks another continuation of
the same displayed snapshot, and recovers lost-response receipts after reload
through the original action/payload/request ID. This is text-only continuation;
active-turn attachment steering remains separate. Unloaded children are not
silently resumed. Starting while the parent/delegated work is active is rejected.

Official API evidence accessed 2026-09-10:
https://learn.chatgpt.com/docs/app-server documents turn/start on a specified
thread and turn/steer's expectedTurnId guard. Installed TurnStartParams has no
equivalent latest-history compare-and-swap field. The local snapshot recheck is
not an atomic server-side history guard; no such guarantee is claimed.

Verification:

- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_idle_agent_continuation_keeps_exact_owner_and_checks_snapshot tests/test_workspace_host.py::test_agent_reads_use_existing_parent_even_when_job_reserved tests/test_workspace_pane.py::test_idle_agent_continuation_is_explicit_and_preserves_failed_draft tests/test_workspace_pane.py::test_active_agent_message_keeps_failed_draft_and_never_starts_idle_turn -q --tb=short`: exit 0, 19 passed in 9.63s. Ready/busy parent, stale/foreign/unloaded child, missing acknowledgment, fast completion, explicit confirmation, duplicate receipts and 390/1600px UI. Screenshots inspected.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_idle_agent_continuation_keeps_exact_owner_and_checks_snapshot -q --tb=short`: exit 0, 14 passed in 0.59s after aligning ambiguity handling with the existing uncertain-state contract.
- `node --test tests/workspace-connection.test.mjs`: exit 0, 54 passed in 213.402ms, including continuation receipt recovery.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-agents.py`: exit 0, actual native foreign continuation refused with thread/read only, same owner/process, no inference or agents, native shell interruption and cleanup passed. This does not prove successful model-spawned child continuation.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-agents.py`: exit 0, all checks passed.
- `node --check ui/static/workspace-pane.mjs`: exit 0, no output.

The real spawned-child positive test still requires a fresh dedicated ChatGPT
login. Full parity, packaged QA, integration and release remain open.

### Active Child Attachments

The active-child composer accepts picker, dropped and pasted files through the
same bounded parent upload store as parent messages. The host accepts either the
legacy text payload or upload-token inputs, validates tokens in the parent scope,
and passes normalized inputs to the existing exact-child `turn/steer` operation.
It does not start/resume a child or change the parent's active turn. Foreign
tokens and arbitrary file paths are rejected. File-only image messages work.

Selected files survive dialog close/reopen and child switching in the same pane;
they are memory-only until sent, not persisted across application reload. Text
drafts retain their existing storage. Failed sends retain text/files. Uploaded
tokens and command receipts survive connection replacement; an explicit retry
uses the original payload, target turn and request ID without reuploading. A
different message to that child is blocked while its prior receipt is unresolved.
Receipt recovery retains the current draft rather than guessing it is identical
to the old accepted message. Closing the inspector does not send or stop work.

Verification (2026-09-10):

- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_agent_message_steers_exact_turn_without_start_or_resume tests/test_workspace_host.py::test_agent_attachments_validate_parent_scope_and_deduplicate tests/test_workspace_host.py::test_agent_reads_use_existing_parent_even_when_job_reserved tests/test_workspace_pane.py::test_agent_files_reopen_retry_and_do_not_reach_parent tests/test_workspace_pane.py::test_active_agent_message_keeps_failed_draft_and_never_starts_idle_turn -q --tb=short`: initial exit 1 (12 passed, 1 dialog-close locator race); after awaiting dialog removal, exit 0, 13 passed in 12.88s.
- `node --test tests/workspace-connection.test.mjs`: exit 0, 53 passed, 300.246ms; includes existing parent uploads plus exact child receipt recovery with no reupload.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_agent_drop_paste_and_pending_receipt_recovery tests/test_workspace_pane.py::test_agent_files_reopen_retry_and_do_not_reach_parent -q --tb=short`: exit 0, 4 passed in 7.26s. 390/1600 widths, file picker/drop/paste, retained files, original receipt retry, long filenames, visible dynamic icons. Screenshot inspection caught missing icon hydration; fixed and retested.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py`: exit 0, all checks passed.
- `node --check ui/static/workspace-pane.mjs`: exit 0, no output.
- `git diff --check`: exit 0, no output.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-agents.py`: exit 0, native same-owner rejection and shell interruption proof plus cleanup. No inference, no model-spawned child; does not prove model consumption of an attachment.

Still incomplete: actual model-spawned child end-to-end proof requires a fresh
dedicated login, idle-child continuation, full command parity, packaged QA and
release/default enablement. No installed app changed.

### Child History Images and Live-Proof Failure

Agent snapshots now decorate images using the existing parent session upload
store, after native ancestry inspection. Only validated parent-owned paths get
preview tokens; foreign-session paths remain inaccessible. The inspector uses
the existing bounded image loader and zoom dialog, never fetches arbitrary
image URLs, and releases object URLs on refresh and close. This adds previews,
not child file sending or idle-child continuation.

Verification:

- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_agent_history_previews_only_parent_owned_uploads tests/test_workspace_host.py::test_agent_reads_use_existing_parent_even_when_job_reserved tests/test_workspace_pane.py::test_agent_image_preview_zoom_refresh_and_close_release_urls tests/test_workspace_pane.py::test_agent_switcher_inspects_without_launching_and_keeps_parent_draft tests/test_workspace_live_agent_proof.py -q --tb=short`: exit 0, 10 passed in 10.16s. Browser checks at 390/1600 pixels verify decoded green pixels, zoom, refresh/close cleanup, preserved drafts and rejected remote URLs. Both screenshots inspected.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-live-agent.py tests/test_workspace_live_agent_proof.py`: exit 0, all checks passed.
- `node --check ui/static/workspace-pane.mjs`: exit 0, no output.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-agents.py`: exit 0; actual native foreign-thread inspection/stop/steer rejection, same process/session, real shell interruption completion and cleanup. Zero inference/children; not positive child proof.

The opt-in `scripts/verify-workspace-live-agent.py --allow-inference` was attempted
with the dedicated test CODEX_HOME. It exited 1 after timing out: the native
parent failed authentication because its refresh token was already used, before
any child spawned. Local `codex login status` had exited 0 but did not validate
the token remotely. The verifier now detects exact-parent completion immediately
instead of polling for a child after failure; four regression cases cover this.
Do not repeat inference until a fresh dedicated login is available. Positive
model-spawned child steering/interruption remains unproven. No installed app,
user session, default enablement or release was changed.

### Messages to Active Children

The selected agent now has a text composer for its existing active turn. Native
ancestry and the displayed turn are rechecked before `turn/steer`; no turn/start,
resume, model override, shell or replacement session is used. Native confirmation
must name the expected turn. Slash commands are rejected rather than passed to
the model as text. Reserved durable jobs reject steering, while explicit stop
retains its separately permitted cancellation semantics.

Drafts are isolated by parent/child identity and persist when the dialog closes.
Failed or ambiguous delivery keeps the text. Stable connection receipts survive
lost HTTP responses and page replacement; the same intent reuses its original
request ID. Accepted text clears locally even if storage cleanup fails, and that
cleanup failure is labeled as an accepted message with a saved-draft problem,
not as failed delivery. Idle children cannot receive input through this control.

Verification (all final exit codes 0):

- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_agent_message_steers_exact_turn_without_start_or_resume tests/test_workspace_host.py::test_agent_reads_use_existing_parent_even_when_job_reserved -q --tb=short`: 7 passed in 2.12s.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_active_agent_message_keeps_failed_draft_and_never_starts_idle_turn tests/test_workspace_pane.py::test_agent_stop_requires_exact_confirmation_and_waits_for_native_completion -q --tb=short`: 4 passed in 15.01s; desktop/mobile, multiline text, failed-draft reopen, exact target, accepted clearing and idle refusal. Mobile screenshot inspected.
- `node --test tests/workspace-connection.test.mjs`: 52 passed in 335.46ms; explicit child stop/steer receipt identity survives lost response and connection replacement on the unchanged parent route.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-agents.py`: actual native foreign-thread inspection/stop/message rejection, same process/session, native active shell turn interrupted with matching completion; no inference or agent spawn; cleanup passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-agents.py`: all checks passed.
- `node --check ui/static/workspace-pane.mjs` and `git diff --check`: separate commands, both exit 0 with no output.

Still open: actual model-spawned child send/stop end-to-end proof, intentional
idle-child continuation, child uploads/previews, full parity and release. The
positive steering checks here use protocol doubles, not a claim of an actual
model accepting the child message. No installed app was changed.

### Confirmed Child-Turn Interruption

The inspector now provides Stop agent turn only for a snapshot containing one
active turn. A fresh checkbox confirmation is required. The existing parent
owner rechecks native ancestry and the exact active turn, then calls
`turn/interrupt` with that child thread and turn ID. The acknowledgment is shown
as requested, never as completed; the same acknowledged target stays disabled
in that dialog until a different active turn is inspected. Native lifecycle
events retain authority over busy state. Closing the dialog sends nothing.
Host receipts prevent a repeated command ID from issuing another interruption.
Like the parent interruption action, an explicit child stop is permitted during
a reserved job; read-only inspection never stops that job automatically.

Contract rechecked at https://learn.chatgpt.com/docs/app-server on 2026-09-10:
interruption takes `threadId` and `turnId`, acknowledges with an object, and
completes via a native `interrupted` turn. The `.md` URL failed to load; the HTML
documentation and installed JSON schema were read instead.

Verification (all final exit codes 0):

- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_agent_stop_is_confirmed_exact_and_does_not_complete_parent tests/test_workspace_host.py::test_agent_reads_use_existing_parent_even_when_job_reserved -q --tb=short`: 8 passed in 2.16s.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_agent_stop_requires_exact_confirmation_and_waits_for_native_completion tests/test_workspace_pane.py::test_agent_switcher_inspects_without_launching_and_keeps_parent_draft -q --tb=short`: 4 passed in 15.92s. Mobile screenshot inspected.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-agents.py`: exact native fork rejected for inspection and stop; paged history exposed a real active print-only shell turn; real native interrupt produced exactly one matching `turn/completed` with status `interrupted`. Same process and session, zero inference/agents spawned; process reaped and disposable profile removed. The actual interrupted turn belongs to the proof parent, not a model-spawned child; child ancestry plus stop routing remains covered by protocol tests, not an inferred end-to-end proof.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-agents.py`: all checks passed.
- `node --check ui/static/workspace-pane.mjs`: no errors.

Direct child messaging and child media previews remain unfinished. No release.

`/agent`, `/subagents` and Session actions now open an explicit descendant
switcher. Lists and paged full turns use the parent's existing native connection;
inspection never resumes, starts or forks a child. Native ancestry is checked
before history reads, with cycle/foreign-ID/depth rejection. Opening/closing
and switching agents preserve the parent draft and turn. History is labeled a
snapshot; child notifications mark it stale without auto-reading or replacing
the selected transcript. Delegated input is not falsely attributed to Raghav.

Previously, any unexpected foreign-thread notification stopped the owner event
loop. Verified descendant notifications now have their own journal envelope,
never complete or replace the parent turn, and report active agent work to the
busy/sleep/admission guards. Reverse requests retain their original native IDs
and thread data in the owner; the parent UI shows the exact child identity and
still requires an explicit approval/answer. Unrelated threads remain rejected.
Closing the native owner clears descendant caches for the next attachment.

Remaining: this is an inspector, not full child-composer/steering/stop parity.
Child image previews are explicitly unavailable rather than routed through the
wrong parent's media endpoint. Actual model-spawned child end-to-end behavior
has not been proved; positive child event/history coverage uses protocol doubles.
The native proof exercises actual empty descendant discovery and rejection of
a separate persisted fork, with zero inference or delegated agents. The first
proof attempt used an unregistered `thread/start` and exited 1 because the
existing foreign-event guard correctly rejected it; the corrected proof uses
the owner's registered fork path. That failure motivated the ancestry audit.

Commands from this worktree, all final exits 0:

- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py::test_agent_reads_use_existing_parent_even_when_job_reserved -q --tb=short`: 139 passed in 2.90s.
- `env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py -q --tb=short`: 140 passed in 32.10s.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_agent_switcher_inspects_without_launching_and_keeps_parent_draft -q --tb=short`: 2 passed in 7.19s; desktop/mobile, safe text, paging, stale notice, failure preservation and explicit child decline. Mobile screenshot inspected.
- `node --test tests/workspace-events.test.mjs`: 14 passed, 214.30ms; child event isolation and explicit question resolution included.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-agents.py`: native discovery and foreign-fork rejection passed with the same process/session, only `thread/list` and `thread/read` calls during inspection, no inference; child process reaped and disposable profile removed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-agents.py`: all checks passed.
- `node --check ui/static/workspace-pane.mjs`: no errors.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_child_events_are_isolated_and_approvals_stay_explicit -q --tb=short`: final cache-cleanup regression, exit 0; 2 passed in 0.71s.

No release, installed-app mutation or full-parity claim.

## Subagent Integration Audit (2026-09-10, Before Implementation)

Re-read the current renderer and installed 0.153.4 schemas, then checked
https://learn.chatgpt.com/docs/app-server and
https://learn.chatgpt.com/docs/agent-configuration/subagents
(accessed 2026-09-10). This audit did not launch agents.

The current pane preserves `collabAgentToolCall` as inspectable JSON, but has no
dedicated agent switcher. That is an actual interaction gap, not full parity.
The native item supplies sender and receiver thread IDs, last-known agent
states, and optional requested model/effort. Requested model must not be
presented as a independently verified running model. The list API can filter
direct children with `parentThreadId`, or descendants with `ancestorThreadId`,
under the existing experimental capability handshake. These filters must not
be combined; ordinary unfiltered session listing is not an agent inventory.

Next implementation must use the parent's existing native connection for child
inspection and control. Opening a child through the current saved-session
attachment path could create a competing owner while the parent runtime still
owns that child. Do not wire an agent row to generic `openSession` without
resolving that ownership contract. Start with native list/read validation,
paged child history and last-known state, then exact child control through the
same connection. A spawned-child live proof is still required before claiming
full agent interaction, with no inferred model names or generic JSON substitute.

## Codex Goals (2026-09-10)

`/goal` and Session actions inspect the exact persisted native goal. Explicit
confirmation is required to change objective, status or token budget, or clear
the goal. Opening or closing the dialog does not mutate it. Changes are blocked
for reserved jobs and queued bridge work, use durable command receipts, and
compare a fresh native snapshot before mutation. This is not a provider-side CAS:
the native API has no atomic expected-version parameter. The dialog is an
explicitly refreshed snapshot, not a claim of continuously current counters.
Pausing/clearing a goal does not interrupt the currently executing turn.

Contract: https://learn.chatgpt.com/docs/app-server (accessed 2026-09-10),
`thread/goal/get`, `thread/goal/set`, `thread/goal/clear`; verified against the
installed native runtime. Changing the objective resets native usage accounting.

Commands run from this worktree; Python/Ruff binaries are under
`/home/raghav/Documents/Projects/serena/.venv/bin/`:

- `python -m pytest tests/test_workspace_host.py tests/test_workspace_codex.py -k goal -q --tb=short`: exit 0; 7 passed, 256 deselected, 1.28s.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge python -m pytest tests/test_workspace_pane.py -k goal -q --tb=short`: exit 0; 2 passed, 235 deselected, 4.00s. Desktop 1600px and mobile 390px; screenshot inspected, numeric input contrast repaired.
- `env SERENA_EVIDENCE_KIND=live python scripts/verify-workspace-goal.py`: exit 0; real native paused goal survived process replacement with identical session ID, removed budget, rejected stale clear, then cleared. One print-only shell turn; no inference, user credentials or profile changes. Both native processes reaped and disposable profile removed.
- `ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-goal.py`: exit 0; all checks passed. Initial run exited 1 for a test semicolon, corrected before rerun.
- `node --check ui/static/workspace-pane.mjs`: exit 0.

This capability is not a release or completion of all workspace parity gates.

## Codex Personality (2026-09-10)

`/personality` and Session actions open an explicit native selector. The owner
checks `model/list.supportsPersonality` against the current model, rejects busy
or changed sessions, and sends only `threadId` and `personality` through
`thread/settings/update`. Confirmed state is journaled per session and restored
before admitting work after owner replacement. Invalid saved values or rejected
restoration close/refuse the owner rather than silently changing its behavior.
Reserved jobs cannot change personality. No user configuration file is edited.

Official sources, accessed 2026-09-10:
- [Configuration](https://learn.chatgpt.com/docs/config-file/config-basic):
  none/friendly/pragmatic and thread-level overrides.
- [App-server model capabilities](https://learn.chatgpt.com/docs/app-server):
  discover supportsPersonality before presenting selections.

Observed installed 0.153.4 catalog: Astra, Sol, Terra and Luna advertise false;
GPT-5.5 advertises true. The UI reports unsupported state and does not offer fake
choices for those models. The supported native proof selected GPT-5.5 only in a
disposable private profile, with no inference or user-model change.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py -k 'personality or mode' -q --tb=short
# exit 0: 31 passed, 225 deselected in 2.80s.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_journal.py -q --tb=short
# exit 1: 262 passed; one browser test lacked the installed executable path.
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_browser_composer_reaches_host_and_reloads_same_owner -q --tb=short
# exit 0: 1 passed in 4.69s.
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_personality_is_explicit_native_selection_and_keeps_failed_draft -q --tb=short
# exit 0: 4 passed in 6.98s; supported/unsupported, 390px and 1600px.
node --test tests/workspace-connection.test.mjs
# exit 0: 50 passed, 0 failed; 296.312918ms.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-personality.py
# final exit 0: native settings notification, exact same session after process
# replacement, unchanged model/history, three owned processes reaped, no inference.
# Earlier attempts exited 1: empty native chat had no rollout; diagnostic code
# indexed a list as a dict; default model correctly rejected unsupported personality.
# The final fixture persists one print-only turn and selects an advertised
# supporting model in its disposable config. Each attempt cleaned its profile.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py core/workspace_journal.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-personality.py
# final exit 0: All checks passed (initial import ordering finding corrected).
node --check ui/static/workspace-pane.mjs
# exit 0.
```

Inspected mobile screenshot `apps/desktop/build/workspace-proof/personality-390.png`.
Not packaged, released, or default enabled; broader command parity is incomplete.

## New Conversation From A Native Pane (2026-09-10)

Embedded Claude/Codex panes expose New conversation. Codex `/new` and
`/new <title>` use the same app-level creation dialog and durable creation flow.
The parent validates iframe origin, window and source session, then takes the
project/provider from that exact pane rather than the globally selected project
or remembered linked-agent selection. Existing dialogs are not replaced.
Opening or cancelling creates nothing and preserves the current draft/owner,
including while the current turn is running. Creation still requires explicit
confirmation. The standalone page reports `/new` unavailable because it lacks
the app's chat-management surface; this does not claim standalone parity.

The official `/new` behavior was reviewed at
https://learn.chatgpt.com/docs/developer-commands?surface=cli (2026-09-10).
No native clear, fork, or current-session termination is substituted for it.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_codex_new_chat_keeps_running_owner_and_draft tests/test_workspace_pane.py::test_codex_local_commands_use_controls_not_model_prompts -q --tb=short
# exit 0: 12 passed in 7.51s.
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -k app_route_bootstrap -q --tb=short
# exit 0: 2 passed, 13 deselected in 19.39s; both providers and spoof rejection.
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
# exit 0: real Electron /new created/opened the exact native Codex session with
# its selected title, then native input/output passed. Linked-pane cancellation
# preserved drafts and owner counts. No inference or user credentials used;
# disposable owners/profile cleaned up. The old printed New Chat button label
# was subsequently corrected to /new to match the exercised path.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_app.py tests/test_workspace_pane.py
# exit 0: All checks passed.
node --check ui/static/workspace-pane.mjs
# exit 0.
node --check scripts/verify-workspace-electron.cjs
# exit 0.
```

No package rebuild, installation, release or default enablement.

## Confirmed Bulk Background Stop (2026-09-10)

Claude and Codex task dialogs offer an explicitly confirmed stop of the listed
tasks. The reviewed snapshot is traversed by exact process ID through existing
receipted controls; newly appearing tasks are not silently added. A failure
halts the sequence and leaves remaining rows visible. Pending Claude requests
remain visible and cannot be reissued before refresh. Malformed responses never
count as confirmed stops. Refresh clears the old selection; closing the dialog
does not initiate termination. Drafts remain intact.

Codex `/stop` and `/clean` open this dialog, including during an active turn;
they do not submit prompts or stop anything before confirmation. This maps the
[official stop command](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
(accessed 2026-09-10) to native-pane controls. The installed protocol requires
both `threadId` and `processId` for each termination.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -k 'background or bulk_stop or codex_local_commands' -q --tb=short
# exit 0: 17 passed, 212 deselected in 11.59s.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-copy.py
# exit 0: real browser clipboard, inline mention, explicit bulk-stop routing,
# draft preservation and cleanup. Task transport is controlled, not native;
# no provider process or real user task launched/stopped.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py scripts/verify-workspace-copy.py
# exit 0: All checks passed.
node --check ui/static/workspace-pane.mjs
# exit 0.
git diff --check
# exit 0.
```

Mobile rendering inspected at 390px. No packaged rebuild or release in this slice.

## Explicit Codex Rewind Control (2026-09-10)

Follow-up safety audit: native background terminals must also be empty before
rewind; idle turn state alone is insufficient. Failed task discovery refuses
rewind without mutating history. Null or foreign native thread confirmations
leave the owner uncertain instead of claiming success.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py -q --tb=short
# exit 0: 112 passed in 4.03s, including active/unknown background tasks and
# null/foreign native confirmations.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-revert.py
# exit 0: native empty-background query and exact-session rewind; same process,
# retained prefix, no inference, complete disposable cleanup.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py tests/test_workspace_codex.py
# exit 0: All checks passed.
```

Session actions now exposes Rewind conversation for Codex. It requires a chosen
turn and explicit confirmation, verifies the latest turn has not changed, and
uses the existing owner's native `thread/revert` request. It does not undo files,
create a session, or clear the composer draft. Reserved background jobs and queued
messages prevent rewind. Ambiguous native failures make the owner uncertain;
command receipts prevent replaying the same mutation. No slash alias is claimed.

Verification receipts (all exit 0):

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py::test_rewind_is_explicit_deduplicated_and_refuses_background_work tests/test_workspace_pane.py::test_rewind_confirmation_routes_exact_turn_and_preserves_draft -q --tb=short
# 111 passed in 6.18s; desktop/mobile screenshots inspected at 1600 and 390px.
node --test tests/workspace-connection.test.mjs
# 49 passed, 0 failed; 168.472684ms.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-revert.py
# Exact disposable session, same native owner, stale selection rejected,
# one turn retained and one removed; no inference. Child and profile cleaned up.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py scripts/verify-workspace-revert.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py
# All checks passed.
node --check ui/static/workspace-pane.mjs
# No syntax errors.
```

Not released or default enabled. This receipt is not a fresh packaged Windows
proof or a claim that all CLI capabilities are complete.

## Evidence Sources

- Installed Claude CLI 2.1.267 / pinned SDK: `scripts/verify-workspace-claude-commands.py`,
  executed 2026-09-10, exit 0. Disposable unsigned profile, 45 catalog names.
  Catalogs can differ with credentials, plugins, project and native version.
- [Claude commands](https://code.claude.com/docs/en/commands), accessed 2026-09-10:
  official descriptions distinguish skills, workflows and local controls.
- [Codex commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
  accessed 2026-09-10: official reference, not proof of installed capability.
- Source: `core/workspace_claude.py`, `core/workspace_codex.py`,
  `ui/static/workspace-pane.mjs`; focused tests and exact historical runtime
  receipts are in `interactive-workspace.md`.

## Installed Codex App Picker (2026-09-10)

The explicit Apps and connectors button and `/apps` now open an installed-app
picker. Selection is a removable, session-scoped persisted draft attachment.
Submit and steer pass selected IDs separately; the owner rechecks them against
the current native runtime and constructs exact `mention` inputs with
`app://<id>` and the native display name. It does not invent a text-only command,
enable apps, install anything, change permissions, or launch another owner.
Selected app attachments also block background-job admission through view context.
History renders app names as literal text with exact IDs in the tooltip.

[Official App Server documentation](https://learn.chatgpt.com/docs/app-server),
accessed 2026-09-10, documents `app/installed` for effective enabled/callable state,
`app/read` for metadata in batches of at most 100 IDs, and `mention` user input.
Implementation refreshes the installed snapshot for the exact loaded thread,
then reads metadata only for those IDs. Missing metadata makes selection unavailable.
The backend caps the installed inventory at 1,000 and selection at 20 distinct IDs;
it validates returned identities and states and excludes unrelated metadata.

Live discovery changed the implementation: initial full-directory `app/list`
proofs exited 1 on repeated IDs across pages, then on over 1,000 unique entries.
The final implementation does not fetch that directory. A non-refreshing
installed snapshot initially returned zero; explicit refresh returned eight.
The production picker therefore refreshes the runtime snapshot, not just an empty cache.

Commands below ran from the isolated worktree. Python is
`/home/raghav/Documents/Projects/serena/.venv/bin/python`.

- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py::test_account_status_requires_explicit_owner_and_rejects_mutations tests/test_workspace_pane.py::test_app_picker_selects_exact_ids_preserves_failed_draft_and_never_auto_loads -q --tb=short`
  exited 0: 95 passed in 27.46s. Includes malformed metadata, missing metadata,
  100+1 batching, exact submit/steer routing, state revalidation, no auto-load,
  disabled selections, text-safe rendering and retained failed-send drafts.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_app_picker_selects_exact_ids_preserves_failed_draft_and_never_auto_loads -q --tb=short`
  exited 0: 2 passed in 3.81s after adding literal app-mention history rendering.
- `env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short`
  exited 0: 2 passed in 42.34s. Actual mounted Claude/Codex pages; selected app
  alone reports draft=true, removal reports false, no extra owner or submission.
- `node --test tests/workspace-connection.test.mjs` exited 0: 47 passed,
  0 failed, 152.559706ms. Includes exact-session app routes and no auto-launch.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --signed-apps`
  exited 0: signed-in native snapshot contained 8 installed apps; one exact
  selection revalidated through the real native metadata API. Same owner stayed
  ready, child reaped and disposable profile removed. Subscription auth was copied
  into an isolated profile with apps enabled there only. No browser, inference,
  tool execution, or user configuration writes. This proves discovery/selection,
  not a real model's downstream use of a connector; actual turn transport is
  covered by controlled submit/steer tests, not an inference claim.
- `env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_browser.py::test_navigation_projects_and_drafts_do_not_spawn -q --tb=short`
  exited 1 at setup: that separate legacy fixture ignores the executable override
  and its bundled Chromium is missing. No production assertion ran there. The
  mounted rich-page and pane tests above ran with installed Edge successfully.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py tests/test_workspace_app.py scripts/verify-workspace-account.py`
  exited 0: all checks passed. `node --check ui/static/workspace-pane.mjs`,
  `node --check ui/static/workspace-page.mjs`, and `git diff --check` each exited
  0 with no output.

Screenshots inspected at 390 and 1600 pixels:
`apps/desktop/build/workspace-proof/apps-{390,1600}.png` (controlled metadata,
real renderer). Dialogs fit without horizontal overflow; disabled state and
markup-as-text were visible. Remaining app parity: marketplace browsing/install,
configuration management, and a real inference using a selected connector.
The full rich-pane goal remains incomplete and unreleased.

## Codex Native Rename (2026-09-10)

The pane's Rename conversation button and `/rename [name]` open an explicit
confirmation dialog. The session owner calls `thread/name/set` for its exact
thread, then verifies `thread/read` returns the same identity and name. It keeps
the running turn intact and does not submit a prompt, start a thread, or attach
another owner. Invalid titles and unavailable ownership are refused. A failed
confirmation does not claim the requested name was persisted.

After native confirmation, the existing catalog registration path validates
the matching transcript's identity and project, writes the explicit custom
title, and indexes that exact session. This deliberately replaces an older
Serena custom title only for the user-requested Codex rename. Unrelated sessions
and linked siblings are not renamed. Catalog failure is reported separately as
"Native name saved, but Serena title synchronization failed"; a not-yet-written
transcript can still cause this condition. Passive Claude native rename behavior
and its existing-custom-title precedence remain unchanged.

The iframe asks the parent to refresh its sidebar only after the current rename
command succeeds. The parent checks origin, source window and SID, then fetches
current saved titles. It does not replay the requested title over newer cached
metadata, navigate away, or replace the owner. Ordinary composer drafts survive.

Source: [official App Server documentation](https://learn.chatgpt.com/docs/app-server),
accessed 2026-09-10; `thread/name/set`, `thread/name/updated` and persisted
`thread.name`. Installed generated schemas were also inspected.

Verification from the isolated worktree:

- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_catalog.py tests/test_workspace_host.py::test_rename_requires_owner_and_preserves_receipt_without_duplicate_native_write -q --tb=short`
  exited 0: 123 passed in 24.24s. Exact native routing/readback, ready/running
  state preservation, invalid names, malformed/foreign readback, exact metadata
  replacement, sibling preservation and duplicate-receipt protection.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_native_rename_requires_confirmation_and_preserves_failed_draft -q --tb=short`
  exited 0: 2 passed in 6.73s after fixing the initially unstyled input. Desktop
  and mobile confirmation, errors, draft preservation and successful clear.
- `env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short`
  exited 0: 2 passed in 45.07s. Mounted Claude/Codex pages, explicit rename via
  real host route with a controlled provider/catalog, fresh sidebar reload,
  spoofed parent notification rejected, existing owner and draft preserved.
- `node --test tests/workspace-connection.test.mjs` exited 0: 48 passed,
  0 failed, 95.488869ms. Rename is an exact-session control, not prompt input.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-rename.py`
  exited 0: actual native rename and notification, real exact-session transcript
  registration, SQLite catalog/custom-title replacement, and same SID/name after
  process replacement. The old child was reaped before resume; all children and
  temporary profile removed. One print-only shell command materialized the
  disposable session; no inference, subscription auth, or user-profile writes.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py core/workspace_catalog.py scripts/verify-workspace-rename.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_catalog.py tests/test_workspace_pane.py tests/test_workspace_app.py`
  exited 0: all checks passed. `node --check ui/static/workspace-pane.mjs`,
  `node --check ui/static/workspace-page.mjs` and `git diff --check` each exited
  0 with no output.

Viewed `apps/desktop/build/workspace-proof/rename-390.png` and `rename-1600.png`:
black input, compact dialog, visible error and no horizontal overflow. Windows
packaged verification and release remain outstanding for the overall goal.

## Claude Explicit Custom-Title Replacement (2026-09-10)

This closes the custom-title replacement gap mentioned in the historical Claude
rename receipts below. A successful live `/rename <name>` completion now carries
both expected and confirmed native names into registration. The catalog requires
the exact session/project and matching persisted native title before replacing
that session's Serena custom title. Passive indexing has no replacement flag and
retains its existing precedence. Missing or stale native confirmation is refused;
unrelated and linked sibling metadata stays unchanged. Codex rename behavior is
unchanged.

The catalog confirmation flag is preserved through the host's result whitelist.
The frame then requests a fresh parent catalog read, rather than patching its
event's title over the cached custom name. Replayed confirmations fetch the
current saved name again; they do not replay the old requested title. The
existing origin/source/session checks remain, and the conversation and draft
are not replaced. This does not establish a new cross-machine metadata conflict
resolution protocol or a new native rename API; it completes synchronization of
the already-supported explicit Claude command.

Executed separately from the isolated worktree:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_workspace_host.py::test_confirmed_claude_rename_indexes_exact_owner_without_attach tests/test_workspace_claude.py -q --tb=short
```

Exit 0: 86 passed in 2.99s. Exact-session replacement, passive precedence,
unconfirmed/stale rejection, sibling preservation, live completion and failures.

```sh
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch tests/test_workspace_host.py::test_confirmed_claude_rename_indexes_exact_owner_without_attach tests/test_workspace_catalog.py -q --tb=short --show-capture=no
```

Exit 0: 30 passed in 34.48s after preserving the host confirmation flag.
Both mounted provider pages passed. The Claude test exercises confirmed and
stale replayed catalog events, fresh title reads and preserved drafts. An earlier
two-provider browser run exited 1 (1 passed, 1 failed in 15.32s) because the new
test passed Playwright's keyword-only `arg` positionally; corrected before rerun.

```sh
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-commands.py
```

Final exit 0. Actual native Claude local rename persisted for the exact session;
the real parser/catalog initially preserved an existing Serena title under
passive indexing, then replaced it on the confirmed completion. The journal
contained the indexed title and confirmation flag. Nine local commands also
passed, with one native output/turn identity each. All disposable state and the
owned child were cleaned up, with no inference or user credentials. Initial live
exit 1 exposed the host dropping `native_rename`; that production defect was
fixed and regression-tested before the final run.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_catalog.py core/workspace_host.py tests/test_workspace_catalog.py tests/test_workspace_host.py tests/test_workspace_app.py scripts/verify-workspace-claude-commands.py
node --check ui/static/workspace-page.mjs
```

Both commands exited 0; Ruff reported all checks passed, Node no output.
The earlier packaged proofs cover source `748573e`, not this subsequent rename
change. Full parity and release/default enablement remain incomplete.

## Codex Reverted-History Reconciliation (2026-09-10)

The [official CLI command reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
accessed 2026-09-10, says `/copy` is unavailable immediately after a rollback.
Installed 0.153.4 generated schemas expose `thread/reverted` with an exact
`threadId` and `thread/revert` with `beforeTurnId`; the older `thread/rollback`
schema is deprecated. Revert changes durable conversation history, not files.
The fetched public App Server page did not describe the newer endpoint, so its
wire shape was verified against the installed schema and native runtime.

The owned adapter now handles the real revert notification: invalidate the old
history cursor/completion cache, publish invalidation, read full native remaining
turns for the same session, then publish authoritative replacement history.
Errors fail unavailable. Pending old-page reads are rejected; revision-tagged
pages cannot reintroduce discarded turns even if their publication races the
notification. The owner/process and project are not replaced or reset.

The renderer clears discarded history and suppresses latest-output copy until
new completed main-agent text/plan output arrives. Running, empty and subagent
output does not lift suppression. The copy action is disabled while suppressed;
the shortcut reports why without changing clipboard contents or the draft.
Journal replay preserves the flag. This is event handling, not a completed
user-facing rewind picker or authorization to revert real user chats.

Commands ran separately in the isolated worktree:

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_pane.py::test_copy_after_revert_waits_for_new_completed_output tests/test_workspace_pane.py::test_copy_completed_output_ignores_running_turn_and_preserves_draft -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_revert_rejects_inflight_old_history_page -q --tb=short
node --test tests/workspace-events.test.mjs
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-revert.py
```

Exits: 0 (106 passed, 9.00s), 0 (1 passed, 0.44s), 0 (13 passed,
115.320519ms), 0 respectively. Initial adapter run exited 1 with 100 passed and
two new tests observing the initial history rather than waiting for revert;
clearing the fixture's initial event list corrected the test synchronization.

The live proof used two print-only shell turns in a disposable native session,
then reverted before the second turn. The same owner reloaded exactly one
remaining turn, no stale cursor, and the copy-suppression flag. Child reaped and
profile removed; no inference, credentials, user history or project file changes.
Browser copy tests use controlled output/clipboard; no native model answer is
claimed by this print-only proof.

`/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py tests/test_workspace_codex.py tests/test_workspace_pane.py scripts/verify-workspace-revert.py`
exited 0, all checks passed. `node --check ui/static/workspace-pane.mjs` exited 0.
Full command parity, rewind UI, and final release remain unfinished.

After adding the disabled-button state, the final command
`env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_copy_after_revert_waits_for_new_completed_output -q --tb=short`
exited 0: 1 passed in 9.07s. `git diff --check` exited 0 with no output.

## Claude Native Catalog

Every name in the observed 45-entry catalog is included below. Forwarding means
unchanged input through the owned SDK session, not a substitute prompt or CLI
process. Commands can still require subscription access or native configuration.

| Names | Current route | Evidence or remaining gate |
| --- | --- | --- |
| `clear` (`reset`, `new`) | Explicit pane session transition | Existing exact-session transition tests/proofs; CLI naming argument parity remains |
| `color` | Pane-local prompt color | Browser persistence tests; not native remote color synchronization |
| `reload-plugins`, `reload-skills` | Native SDK controls | Existing control tests/proofs; complete flag/argument parity remains |
| `doctor` (`checkup`) | Native skill input | Browser routing plus actual unsigned native acceptance; authenticated repair not run |
| `deep-research`, `design`, `design-sync`, `dataviz`, `update-config`, `verify`, `debug`, `code-review`, `simplify`, `batch`, `fewer-permission-prompts`, `loop`, `claude-api`, `workflow-authoring`, `run`, `run-skill-generator` | Native skill/workflow input | Advertised and forwarded; no blanket end-to-end claim for these workflows |
| `agents`, `auto-mode-setup`, `autocompact`, `compact`, `config`, `context`, `effort`, `fast`, `heapdump`, `init`, `mcp`, `model` | Native input; some also have pane controls | Individual controls have earlier receipts; full argument, output and error matrix remains |
| `__remote-workflow`, `workflow-launch-exec` | Native input | Server-session prerequisites; not exercised or launched by audit |
| `rename`, `security-review`, `usage`, `insights`, `recap`, `goal`, `design-consent`, `design-revoke`, `list-agents`, `team-onboarding` | Native input | Advertised; side effects and integration with pane metadata still need verification |

The terminal-command and skill sets can overlap. A skill entry must not be
silently replaced with a similarly named local command. Diagnostics remains a
separate installation tool, not the doctor skill.

### Verified Local Invocation Forms

On 2026-09-10 the expanded native proof exited 0 for `/effort low`, `/context`,
`/usage`, `/agents`, `/list-agents`, `/model`, `/config --help`,
`/rename command-proof`, and `/autocompact auto`. It verifies zero inference,
one output row through the production event adapter, exact turn identity and
same native process/session. `/agents` correctly reports that the native wizard
was removed, not a fabricated management UI. `/model` here is inspection only.
`/usage` is the unsigned session report, not proof of subscription-limit fetching.
Rename persists a `custom-title` JSONL record containing `customTitle` and the
exact `sessionId`; the native proof now asserts both against the disposable
session file rather than trusting the success message. `parse_metadata` now
retains the latest valid exact-session title and `_upsert_session` uses it for
Claude's catalog title. Synced custom metadata still takes precedence. Native
rename through real persistence, parsing, SQLite indexing and catalog search is
proved. Immediate refresh and intentionally replacing an existing Serena custom
title remain separate work; blindly replaying old transcript titles into synced
metadata would be unsafe.

The live Claude owner now tracks explicit text-only `/rename <name>` by input
turn ID. Only a successful matching completion emits a catalog signal; failed
turns clear it without updating the title. The host indexes that same owner and
project without attachment or a new session, verifies the expected title against
native persistence, and bounds flush retries to five attempts. Indexing errors
are journaled separately without interrupting the owner. The open parent sidebar
now consumes successful indexed catalog notifications from its exact same-origin
iframe, patches only the named chat and current heading, and renders titles as
text. It does not reload/navigate the conversation or touch its draft. Newer
cached custom names retain precedence over replayed native names. Intentionally
replacing an existing Serena custom name from a native rename remains open.

Open-sidebar refresh receipts (2026-09-10):

```sh
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short --show-capture=no
# final exit 0: 2 passed in 26.98s, both provider panes, actual polled catalog event,
# same owner/draft, escaped heading, wrong-source rejection, newer custom name.
# Earlier exits 1: event envelope was not unwrapped, then fixture lacked convTitle.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_workspace_host.py::test_confirmed_claude_rename_indexes_exact_owner_without_attach -q --tb=short
# exit 0: 23 passed in 5.20s.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-commands.py
# exit 0: native rename indexed; catalog notification contains the actual display
# title; owned transport and disposable profile removed, no inference.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_catalog.py core/workspace_host.py tests/test_workspace_app.py
# exit 0: All checks passed!
node --check ui/static/workspace-page.mjs
# exit 0.
```

```sh
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_catalog.py -q --tb=short
# exit 0: 205 passed in 24.24s before extending the host test with pending-flush coverage.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_confirmed_claude_rename_indexes_exact_owner_without_attach -q --tb=short
# exit 0: 2 passed in 1.23s, including bounded pending-flush retries.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-commands.py
# exit 0: real native rename persistence -> host publication -> production catalog
# registration -> exact-session saved title. No inference or extra owner launch.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_host.py core/workspace_catalog.py tests/test_workspace_claude.py tests/test_workspace_host.py scripts/verify-workspace-claude-commands.py --fix
# exit 0: one test-local import ordering issue fixed.
```
All writes and command execution were confined to a disposable profile/project.

```sh
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-commands.py
# exit 0: 45 native commands, 9 local forms, nativeRenamePersistedForExactSession=true;
# zero inference, expected unsigned doctor refusal, child and profile cleaned up.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-claude-commands.py
# exit 0: All checks passed!
```

Persisted-title integration receipts (2026-09-10):

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py -q --tb=short
# exit 0: 21 passed in 0.37s; includes native name, newer rename, foreign SID,
# malformed/empty/control-character/oversized names and explicit custom precedence.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-commands.py
# exit 0: nativeRenameIndexedInCatalog=true. Initial exit 1: temporary project's
# -tmp-serena-* name correctly triggered internal-project hiding; changed fixture
# prefix, not production visibility rules.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_catalog.py scripts/verify-workspace-claude-commands.py
# exit 0: All checks passed!
```

Shared parser/indexer lint remains at 15 pre-existing findings (exit 1),
confirmed against `git show HEAD:core/parser.py` and `git show HEAD:core/indexer.py`
piped separately to Ruff `check --stdin-filename <path> --output-format concise -`
(exit 1 each: 5 and 10 findings). Unrelated cleanup was not included.

## Codex Pane Command Routes

Unhandled slash commands now fail explicitly in the pane and in the Codex owner
instead of becoming model prompts. The owner guard also covers steering and
non-pane callers. Native controls remain separate actions; paths containing
slashes, ordinary prose and `$skill` mentions remain ordinary input. This is an
honest failure boundary, not completion of the still-missing commands below.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_unrouted_commands_never_reach_submit_or_steer tests/test_workspace_codex.py::test_paths_and_normal_text_are_not_slash_commands tests/test_workspace_pane.py::test_unknown_codex_command_stays_in_draft_without_model_call -q --tb=short
# exit 0: 15 passed in 6.38s; unsupported, destructive, hyphenated and custom
# prompt command forms cannot silently become model input.
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_pane.py::test_codex_local_commands_use_controls_not_model_prompts tests/test_workspace_pane.py::test_codex_inline_mention_replaces_command_only_after_file_selection -q --tb=short
# exit 0: 90 passed in 11.83s, full Codex owner and supported-control regressions.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --command-guard
# exit 0: four commands refused on the same real native owner; no turn started,
# inference=false, child reaped and temporary profile removed.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py tests/test_workspace_codex.py tests/test_workspace_pane.py scripts/verify-workspace-account.py
# exit 0: All checks passed!
node --check ui/static/workspace-pane.mjs
# exit 0.
```

| Names | Current route | Remaining gap |
| --- | --- | --- |
| `resume`, `fork`, `review`, `compact` | Existing native session controls | Preserve exact session and confirmation contracts; no claim for every CLI argument |
| `mcp`, `permissions`, `skills` | Native catalogs/control dialogs | Audit full CLI option coverage separately |
| `model`, `reasoning` | Focus native-catalog-backed selectors | Selection applies to next turn; not a claim of persistent TUI configuration |
| `status` | Existing event snapshot plus explicit native account-limit refresh | Native unsigned refusal, signed-in successful retrieval and controlled positive rendering verified; explicit snapshot, not a continuous feed |
| `plan` | Explicit native mode picker and `thread/settings/update` | Native Plan/Default confirmed with unchanged model/effort; workspace-confirmed mode restored across two real process replacements, no inference |
| `copy` | Copy button, slash action and Ctrl+O | Completed response/plan only; drafts retained, native browser clipboard verified. Rollback-specific suppression still needs coverage. |
| `ps` | Existing native background-task dialog | Explicit refresh and task controls; slash routing tested without submitting a prompt |
| `mention` | Existing project file picker, including inline search | Selection replaces the slash command with a quoted file mention; cancellation preserves draft; desktop/mobile verified |
| `hooks` | Native project-scoped `hooks/list` inspector | Read-only enabled/trust/source/handler state and diagnostics; trust/enable mutations remain unimplemented |
| `diff` | Bounded Git working-tree snapshot | Staged/unstaged/untracked regular files, explicit omitted-file notices; native Git and desktop/mobile rendering verified |
| `apps` | Installed connector picker | Exact native IDs/callability, retained drafts and native metadata proof; marketplace management and downstream inference remain |
| `rename` | Explicit native rename and Serena catalog sync | Same-session persistence through process replacement; sidebar refresh and draft preservation verified |

### Codex Documentation Inventory (2026-09-10)

Source: [official developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
accessed 2026-09-10. Documentation inventory is not proof of installed-version
support. These documented names are not yet fully covered by the rows above:

`ide`, `keymap`, `vim`, `setup-default-sandbox`, `sandbox-add-read-dir`, `agent`,
`subagents`, `apps`, `plugins`, `clear`, `archive`, `delete`,
`exit`, `experimental`, `approve`, `memories`, `import`, `feedback`, `init`,
`logout`, `fast`, `goal`, `personality`, `stop`, `app`, `side`,
`btw`, `raw`, `new`, `quit`, `usage`, `debug-config`, `statusline`, `title`, `theme`,
`pets`, `pet`.

Several have existing non-slash controls; each still needs an explicit mapping
and provider-specific verification. Do not equate absent aliases with absent
backend capabilities, or existing buttons with complete CLI argument parity.
Destructive/configuration/account commands require their own explicit authority
and confirmation flows; auditing them does not authorize executing them here.
No full-parity release claim follows from this inventory. Installation, account authentication,
permissions, background tasks and other non-command controls also retain their
provider-specific delivery gates in the main contract.

### Native Hook Inspection and Plugin Constraint

Rechecked [official hooks documentation](https://learn.chatgpt.com/docs/hooks)
on 2026-09-10: trust applies to the current hook definition hash; changed hooks
require renewed review, while managed hooks cannot be disabled. The installed
0.153.4 `ClientRequest.json` exposes `hooks/list`, not a trust/enable mutation.
Do not substitute a blanket bypass or project trust change for exact hook trust.
Hook management remains an explicit parity gap, not covered by the inspector.

### Project Diff Receipts (2026-09-10)

`/diff` uses Git, not a model request. An attached owner determines the directory;
caller-supplied paths are rejected. External diff/text conversion and fsmonitor
helpers are disabled. No Git writes or index refresh are requested. Output is
limited to 2 MiB, 100 untracked paths and a 15-second command deadline. Untracked
symlinks/non-regular files are named as omitted, not followed or hidden. Binary
changes use Git's binary notice rather than a fabricated text patch. Separate
commands are a working-tree snapshot, not an atomic snapshot during concurrent
edits. Windows-specific validation remains part of final packaged verification.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_project_diff_is_explicit_read_only_and_text_safe tests/test_workspace_diff.py -q --tb=short
# exit 0: 5 passed in 5.09s.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_project_diff_requires_existing_owner_and_ignores_caller_paths -q --tb=short
# exit 0: 1 passed in 0.75s.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python -c 'import hashlib,json; from pathlib import Path; from core.workspace_diff import read_project_diff; root=Path.cwd(); index=root/".git"; import subprocess; p=Path(subprocess.check_output(["git","rev-parse","--git-path","index"],text=True).strip()); before=hashlib.sha256(p.read_bytes()).hexdigest(); result=read_project_diff(root); assert hashlib.sha256(p.read_bytes()).hexdigest()==before; print(json.dumps({"ok":True,"indexUnchanged":True,"sectionBytes":{k:len(result[k]) for k in ("staged","unstaged","untracked")},"omittedCount":len(result["omitted"])}))'
# exit 0: index unchanged; staged 0, unstaged 9382, untracked 5082 characters,
# no omitted paths. Field sectionBytes measures decoded characters here.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_diff.py core/workspace_host.py tests/test_workspace_diff.py tests/test_workspace_host.py tests/test_workspace_pane.py
# exit 0: All checks passed!
node --check ui/static/workspace-pane.mjs
# exit 0.
```

Viewed `apps/desktop/build/workspace-proof/diff-390.png` and `diff-1600.png`:
all three sections and omitted-path notice fit; markup remains literal text.

### Hook API Evidence

[Official App Server documentation](https://learn.chatgpt.com/docs/app-server),
accessed 2026-09-10, lists `hooks/list` but explicitly warns against calling
`plugin/list`, `plugin/read`, `plugin/install` and `plugin/uninstall` from production
clients while those endpoints are under development. Full production plugin
management cannot be claimed using those endpoints without resolving that
constraint. No plugin installation, removal or configuration mutation was run.

`/hooks` now opens a read-only native inspector. Reads are bound to the exact
owner's project; malformed/cross-project results fail rather than guessing trust
or enabled status. Hook commands are displayed as text, never executed. Opening
the app does not fetch hooks; opening/refreshing the inspector does. Empty native
inventory is verified; nonempty state and malformed results use controlled tests.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_hook_catalog_is_project_scoped_read_only_and_validated tests/test_workspace_pane.py::test_hook_inspector_reads_only_and_preserves_draft tests/test_workspace_host.py::test_account_status_requires_explicit_owner_and_rejects_mutations -q --tb=short
# exit 0: 9 passed in 10.76s; initial exit 1 was a test assignment returning an
# async function to Playwright evaluate, causing immediate invocation.
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_hook_inspector_reads_only_and_preserves_draft -q --tb=short
# final exit 0: 2 passed in 4.26s, after screenshot-driven row layout repair.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --hooks
# exit 0: nativeEmptyHookCatalogRead=true, same owner, no inference/login/browser,
# child reaped and temporary profile removed.
node --test tests/workspace-connection.test.mjs
# exit 0: 46 passed, 253.300931ms.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-account.py
# exit 0: All checks passed!
node --check ui/static/workspace-pane.mjs
# exit 0.
```

Inspected final `apps/desktop/build/workspace-proof/hooks-390.png` and
`hooks-1600.png`. Both use a real Codex pane with controlled hook data. Status is
readable on one line; the earlier shared task-row grid squeezed it into a narrow
column, now corrected and guarded by a browser height assertion.

Copy verification:

Read-only command routing receipts (2026-09-10):

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_codex_inline_mention_replaces_command_only_after_file_selection tests/test_workspace_pane.py::test_project_file_picker_preserves_draft_and_never_sends tests/test_workspace_pane.py::test_codex_local_commands_use_controls_not_model_prompts tests/test_workspace_pane.py::test_codex_unavailable_or_argument_commands_do_not_submit -q --tb=short
# exit 0: 22 passed in 10.78s.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-copy.py
# exit 0: real browser clipboard plus controlled inline file search/selection
# during a running turn. No provider launched; does not prove native file search.
# Initial exit 1 exposed the send-disabled guard blocking read-only commands;
# ps/mention now bypass only that guard, not control availability checks.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py scripts/verify-workspace-copy.py
# exit 0: All checks passed!
node --check ui/static/workspace-pane.mjs
# exit 0.
```

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_copy_completed_output_ignores_running_turn_and_preserves_draft tests/test_workspace_pane.py::test_codex_local_commands_use_controls_not_model_prompts tests/test_workspace_pane.py::test_codex_picker_lists_local_actions_and_preserves_draft -q --tb=short
# exit 0: 10 passed in 5.85s; includes empty history and clipboard denial.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-copy.py
# exit 0: real Chromium clipboard round-trip, partial response excluded, draft
# retained, no provider process and disposable browser closed.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-copy.py tests/test_workspace_pane.py
# exit 0: All checks passed!
node --check ui/static/workspace-pane.mjs
# exit 0.
```
