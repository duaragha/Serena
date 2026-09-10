# Workspace Command Parity Audit

Status: incomplete. Catalog presence and generic input forwarding are not proof
that a command's full behavior works. Gemini is deferred.

## Native Agent Inspection (2026-09-10)

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
