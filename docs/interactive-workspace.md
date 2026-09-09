# Interactive Workspace Delivery Contract

Status: implementation in progress. Not a delivered replacement.

Busy structured sessions now queue sibling bridge messages FIFO. The caller gets
an immediate queued acknowledgement, avoiding two siblings synchronously waiting
on one another. The same receipt retrieves the eventual reply; each delivery
starts only when the owner is ready, without steering/interruption or a second
process. Queue waiting releases the command lock so permissions remain answerable.
The pane header shows the queued count. Shutdown/unavailable owners settle queued
requests as not submitted; crash-unconfirmed receipts still prevent automatic
replay. Queue editing/cancellation and crash recovery remain incomplete.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_bridge.py tests/test_workspace_host.py -q --basetemp=/tmp/serena-workspace-bridge-queue-final
# exit 0: 27 passed in 7.39s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_bridge.py::test_busy_bridge_queue_is_fifo_and_acknowledges_without_mutual_wait tests/test_workspace_pane.py::test_bridge_queue_count_tracks_native_host_events -q --basetemp=/tmp/serena-workspace-bridge-queue-ui
# exit 0: 3 passed in 1.87s
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --bridge
# exit 0: now asserts queued acknowledgement during a real running turn and a
# distinct subsequent turn ID, then exact output and receipt reuse; process cleanup
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py tests/test_workspace_bridge.py tests/test_workspace_pane.py scripts/verify-workspace-codex-roundtrip.py
# exit 0: All checks passed!
```

Initial queue suite exited 1 with four teardown timeouts (27 passed): pending
deliveries didn't observe host shutdown. Added shutdown checks before dispatch
and while collecting results; final run above is clean. Both providers have
fixture queue coverage; native busy-queue proof currently covers Codex only.

The existing /api/codex-bridge and /api/claude-bridge routes now prefer an already
attached structured owner. They submit to the same session, wait for that exact
turn's native completion, and collect only its output. Unknown owners still use
the unchanged terminal path; mismatched or unavailable structured owners
never fall back. No bridge call attaches or focuses a structured pane.
Stable request_id receipts prevent re-submission after observation timeout or
host restart, including unresolved receipts. Responses expose the ID for polling
retries. Legacy callers without an ID receive a new one per HTTP request; they
must reuse the returned ID to obtain retry deduplication. Queued-message controls,
Fleet/work-bridge reservations and process-crash recovery remain separate gaps.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_bridge.py tests/test_workspace_host.py tests/test_workspace_journal.py -q --basetemp=/tmp/serena-workspace-bridge-verification
# exit 0: 22 passed in 7.82s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_bridge.py -q --basetemp=/tmp/serena-workspace-bridge-routes-verification
# exit 0: 8 passed in 3.47s; real existing Flask endpoints, terminal fallback forbidden
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_bridge.py tests/test_workspace_journal.py -q --basetemp=/tmp/serena-workspace-bridge-receipts-verification
# exit 0: 13 passed in 1.62s; restart receipt protection
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --bridge
# exit 0: real Codex response through host bridge, same persisted ID, receipt replay
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference --bridge
# exit 0: real Claude response through host bridge, same persisted ID
# Both proofs reap owned processes and remove isolated auth/history.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py ui/workspace_bridge.py tests/test_workspace_bridge.py scripts/verify-workspace-codex-roundtrip.py scripts/verify-workspace-claude-roundtrip.py
# exit 0: All checks passed!
```

Codex permission requests now render explicit network/filesystem group selection,
defaulting to no grants and turn scope. Session scope requires selection. The
adapter accepts only exact requested groups, preserving deny entries and rejecting
expanded access; requests remain visible until native resolution. Per-path
selection inside a filesystem group is not implemented. The installed
request_permissions_tool feature is under development and off by default; the
workspace does not enable it. Its isolated live proof enables it only in temporary
CODEX_HOME, receives a real request, denies it through the adapter, and observes
serverRequest/resolved. No permission was granted or network tool executed.
Source: https://learn.chatgpt.com/docs/app-server (accessed 2026-09-09),
installed PermissionsRequestApproval schemas and `codex features list`.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_pane.py -q --basetemp=/tmp/serena-workspace-permission-verification
# exit 0: 34 passed in 14.53s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_pane.py::test_permission_prompt_defaults_to_no_grants_and_exact_selected_scope -q --basetemp=/tmp/serena-workspace-permission-final-verification
# exit 0: 13 passed in 0.72s; type-exact validation and mobile layout
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --permissions
# initial exit 1: tool disabled, no permission request produced
# final exit 0 after isolated feature opt-in: request denied and natively resolved;
# same resumed ID, owned processes reaped, isolated storage removed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py tests/test_workspace_codex.py tests/test_workspace_pane.py scripts/verify-workspace-codex-roundtrip.py
# exit 0: All checks passed!
```

Claude now has an explicit searchable command/skill picker using get_server_info
commands plus the native init command names. Selecting inserts into the draft,
never auto-sends. Native aliases/descriptions are searchable; identity-switching
commands are unavailable and rejected before query. Terminal-only commands
advertised by init are labeled unavailable pending native equivalents.
Zero-model-turn ResultMessage.result output is normalized as commandOutput so
commands such as /context actually display their result, without duplicating
normal model replies. Live /context on an isolated resumed subscription session
confirmed output, readiness and command discovery. This does not prove every
advertised command works or complete the session-switching requirement.
Source: https://code.claude.com/docs/en/agent-sdk/slash-commands
(redirects to SDK skills/commands documentation, accessed 2026-09-09).

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_pane.py -q --basetemp=/tmp/serena-workspace-commands-verification
# exit 0: 46 passed in 15.28s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_command_picker_preserves_draft_and_displays_native_output -q --basetemp=/tmp/serena-workspace-command-icon-verification
# exit 0: 1 passed in 0.76s; final close-icon assertion
node --test tests/workspace-events.test.mjs tests/workspace-connection.test.mjs
# exit 0: 11 passed, 0 failed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude.py
# exit 0: installed command inventory, no inference
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference
# exit 0: real resume/response, native /context commandOutput and command catalog,
# owned processes reaped; isolated storage removed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_events.py core/workspace_host.py tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-claude.py scripts/verify-workspace-claude-roundtrip.py
# exit 0: All checks passed!
```

Earlier browser check exited 1 because it asserted before the animation-frame
render; added an explicit wait. Another run exited 1 when its shared pytest temp
directory disappeared (SQLite/file setup errors); isolated basetemp run above
passed. Mobile screenshot reviewed; missing close icon fixed and guarded.

Codex background tasks now have an explicit native list/refresh/stop panel.
Listing follows provider pagination; stopping rechecks membership in this exact
thread and uses the app-server processId, never an OS PID or turn interruption.
Closing the panel sends no controls. Claude has no equivalent control exposed
yet. The installed protocol's list/terminate APIs remain experimental.
Source: https://learn.chatgpt.com/docs/app-server (accessed 2026-09-09),
plus installed v2 ThreadBackgroundTerminals schemas.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py -q
# exit 0: 43 passed in 17.01s
node --test tests/workspace-connection.test.mjs
# exit 0: 6 passed, 0 failed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference
# exit 0: native empty background-task list on exact resumed thread, real response,
# owned processes reaped and isolated storage removed. Stop tested with fixtures,
# not a real running background command; do not infer that stronger proof.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-codex-roundtrip.py
# exit 0: All checks passed!
```

Playwright verified explicit opening, exact stop ID, no close cancellation,
literal command rendering, and 390px layout; mobile screenshot inspected.

Native completed-turn duration now renders as a compact outcome line, with
failed/interrupted labels and no clock-based guesses for absent values. Codex
last-request token usage and Claude result input/output usage appear in the
footer. No cumulative-token-to-context-percentage conversion is made. The Claude
live resume proof now also asserts native duration and usage publication.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_pane.py -q
# exit 0: 27 passed in 10.02s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_native_usage_and_completed_duration_are_not_invented -q
# exit 0: 1 passed in 0.67s
node --test tests/workspace-events.test.mjs
# exit 0: 5 passed, 0 failed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference
# exit 0: native resume/output plus duration and usage, owned processes reaped
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_events.py scripts/verify-workspace-claude-roundtrip.py tests/test_workspace_pane.py
# exit 0: All checks passed!
```

The conversation initially mounts the most recent 100 items, loading earlier
items in 100-item increments on upward scroll or the keyboard-accessible earlier
control. Incoming items preserve an away-from-tail reader's window and scroll
position. Removed/replaced images release their blob URLs, and late preview
requests cannot create orphan URLs. This bounds initial DOM work, not total
retained provider history: state/journal paging remains a separate performance gap.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q
# exit 0: 18 passed in 9.69s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_history_image_renders_without_base64_text tests/test_workspace_pane.py::test_long_history_mounts_recent_items_and_preserves_reader_position -q
# exit 0: 3 passed in 1.39s after image-lifecycle adjustment
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_history_image_renders_without_base64_text -q
# exit 0: image replacement releases URLs for both providers
node --check ui/static/workspace-pane.mjs
# exit 0
```

Codex context compaction is available through its explicit toolbar control and
the exact `/compact` composer command. It calls thread/compact/start on the owned
session. The acknowledgement leaves it busy until native turn completion; a
contextCompaction item shows progress/completion. No new thread or transcript-only
substitute is involved. The live isolated compaction proof passed.
Reference: https://learn.chatgpt.com/docs/app-server (accessed 2026-09-09).

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py -q
# exit 0: 38 passed in 13.38s
node --test tests/workspace-events.test.mjs tests/workspace-connection.test.mjs
# exit 0: 9 passed, 0 failed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --compact
# exit 0: exact session resume, native compaction completion and ready state;
# owned processes reaped, isolated storage removed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_pane.py scripts/verify-workspace-codex-roundtrip.py
# exit 0: All checks passed!
```

Codex now exposes an explicit Review changes dialog for uncommitted changes,
base branch, commit, or custom instructions. The host only accepts the typed
target; the adapter forces review/start delivery=inline, verifies reviewThreadId,
and rejects busy sessions. Native review entry/result items render as Markdown.
An isolated live custom review completed in the exact resumed thread. This
does not claim review correctness on a real project or full slash-command parity.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py -q
# exit 0: 36 passed in 11.43s
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --review
# exit 0: native inference/resume and inline review on same thread; processes reaped
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_review_dialog_routes_explicit_target_without_submitting_message -q
# exit 0: review target routing and native review-result rendering
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py scripts/verify-workspace-codex-roundtrip.py
# exit 0: All checks passed!
```

Sent-image rendering now supports Claude base64 image blocks as bounded blob
previews, and Codex images uploaded through this workspace as authenticated,
session-bound previews. The host adds preview tokens only for validated paths in
that exact session's upload directory. Arbitrary historical filesystem paths
remain text; they are not exposed through a general file-reading endpoint.
Preview GETs do not attach a runtime and use no-store/nosniff headers. Blob URLs
are released when the pane is disposed. Claude raw image storage in journals and
full history retention/windowing still need work.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_uploads.py tests/test_workspace_host.py tests/test_workspace_pane.py -q
# exit 0: 31 passed in 9.90s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_history_image_renders_without_base64_text -q
# exit 0: 2 passed in 0.95s, browser decoded both provider preview forms
node --test tests/workspace-connection.test.mjs
# exit 0: 5 passed, 0 failed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_uploads.py core/workspace_host.py ui/workspace_web.py tests/test_workspace_uploads.py tests/test_workspace_host.py tests/test_workspace_pane.py
# initial exit 1: test import grouping; corrected exit 0: All checks passed!
```

Claude now has a verified isolated subscription inference/resume round trip as
well. Its model selector is populated from the installed SDK's advertised models;
selection calls set_model on the existing client before submission, rejects
unknown choices, and never substitutes providers. Runtime effort/fast controls
remain unimplemented and hidden. The installed SDK exposes four model choices.

Both round-trip proofs now require matching completed **assistant** message
content, excluding echoed user inputs from the assertion. These still do not
prove all tools, permissions, uploads, user configuration or Windows behavior.

```sh
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference
# exit 0: exact native Claude history/resume, real completed assistant reply,
# both processes reaped, isolated credential/history copy removed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference
# exit 0 after assistant-only assertion: native resume and completed reply
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude.py
# exit 0: four advertised models, set_model accepted on same SDK connection;
# no inference in this control-only command; child exit 0
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_app.py tests/test_workspace_host.py -q
# exit 0: 21 passed in 7.77s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q
# exit 0: 13 passed in 7.02s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_host.py tests/test_workspace_claude.py scripts/verify-workspace-claude.py scripts/verify-workspace-claude-roundtrip.py
# exit 0: All checks passed!
```

## Live Codex Resume Proof

`scripts/verify-workspace-codex-roundtrip.py --allow-inference` now verifies a
real first model turn, process shutdown, exact-ID resume through CodexWorkspace,
persisted first-turn history and real second-turn output. It uses a temporary
CODEX_HOME/project and private copy of existing ChatGPT token authentication,
with metered-auth environment stripped, no copied user configuration, read-only
sandbox and no requested tools. Temporary auth/history are removed on exit.
It does not attach to any existing user session or open a terminal.

This establishes subscription-authenticated inference and native resume for an
isolated test conversation. It does not prove normal project tools, interactive
approvals, images, full user configuration or every CLI feature yet.

```sh
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference
# attempt 1 exit 1: Path.open opener argument rejected before provider launch
# attempt 2 exit 1: first real turn completed; proof inspected cleared process handle
# corrected final exit 0: exact persisted history/resume and real second output;
# both owned processes reaped, temporary auth/history removed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py -q
# exit 0: 8 passed in 0.05s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-roundtrip.py
# exit 0: All checks passed!
```

Agent messages and plans now render Markdown using vendored markdown-it 15.0.1
(exact renderer dependency/lockfile, local browser bundle and license). Raw HTML
is disabled. Links permit only HTTP(S)/mailto and use noopener/noreferrer; remote
Markdown images render as labels rather than making automatic network requests.
Code blocks include copy controls, tables scroll within their container, and
the mobile screenshot was inspected. Native session attachment previews are a
separate remaining task; this does not claim arbitrary local-link routing.
Reference: https://markdown-it.github.io/markdown-it/ (accessed 2026-09-09).

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py tests/test_workspace_app.py -q
# initial exit 1: 1 failed/13 passed; old textContent suffix assertion expected
# no paragraph newline. Updated assertion trims trailing rendered whitespace.
# final exit 0: 15 passed in 11.27s
node --test tests/workspace-markdown.test.mjs tests/workspace-connection.test.mjs tests/workspace-events.test.mjs
# exit 0: 11 passed, 0 failed
```

Unsent composer text now persists in sessionStorage keyed by provider and full
session ID. Reload never submits it. Only a confirmed send clears matching text;
newer text and failed-send drafts remain. This does not persist file objects or
promise draft recovery after the browser session is destroyed.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q
# initial 11-test run: exit 0, 11 passed in 5.86s
# added race fixture: terminated hung run exit 143; next run exit 1,
# 1 failed/11 passed due to wait_for_function invoking the resolver function
# corrected fixture final run: exit 0, 12 passed in 6.25s
node --check ui/static/workspace-pane.mjs
# exit 0
```

Codex composer steering is now wired through the upload/receipt path. A running
turn changes the send action to "Steer running turn". The browser captures the
displayed turn ID before uploads, the host requires it, and the adapter rejects
a changed turn before dispatch. It never falls back to starting a new turn.
Claude streaming steering and explicit queue management remain separate gaps.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py -q
# exit 0: 27 passed in 6.69s
node --test tests/workspace-connection.test.mjs
# exit 0: 5 passed, 0 failed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_exact_resume_and_real_turn_controls -q
# exit 0: 1 passed in 0.04s after adding stale-turn rejection assertions
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-rpc.py
# exit 0: installed initialization/models/ownership proof only; no model turn sent
```

## Antigravity Protocol Finding (2026-09-09)

Installed `agy --help` (exit 0) exposes stream-json input/output and exact
`--conversation` resume, but no ACP switch. `agy help remote-control` (exit 0)
exposes daemon start/status/stop, not a local structured-client API.

Official evidence, accessed 2026-09-09:

- https://antigravity.google/docs/cli/headless/ explicitly rejects control_request
  and control_response messages (exit 2), non-text content blocks (exit 1), and
  CLI-handled commands such as /model in a continuous stream. This is not a
  full-fidelity custom UI transport. Pre-allowing tools would remove approval
  interaction and is not an acceptable workaround.
- https://antigravity.google/docs/sdk/overview/ documents a local agent SDK with
  Gemini API-key or Vertex authentication. It does not establish compatibility
  with this user's subscription or CLI conversation IDs. Do not silently swap
  the provider/authentication/session store to that SDK.
- https://antigravity.google/docs/remote-control/ describes Google's authenticated
  remote dashboard and OS daemon. No public local embed/control contract was
  established from this page; no daemon was started during investigation.

Decision: Gemini admission reports the concrete missing controls and performs no
launch. Gemini parity remains an unmet delivery gate, not removed from scope.
Next research must establish an authorized subscription-compatible control path
with exact persisted IDs; plain headless streaming alone is disproven as that path.

Verification commands:
```sh
SERENA_EVIDENCE_KIND=live agy --help
# exit 0: stream-json and exact conversation resume flags listed
SERENA_EVIDENCE_KIND=live agy help remote-control
# exit 0: start/status/stop help only; no daemon launched
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_admission.py -q
# exit 0: 8 passed in 0.05s; Gemini refusal occurs before runtime admission
```

Claude clarifying questions now have native radio/checkbox/custom-answer controls.
Answers are keyed by the original question, returned through SDK updated_input,
and validated against the pending request. Plain approval cannot bypass answering.
Ordinary tool approvals now echo their original input for older CLI compatibility.
Reference: https://code.claude.com/docs/en/agent-sdk/user-input (accessed 2026-09-09).
Browser verification includes mobile rendering; the installed control probe is
still initialization-only, not proof of a model-generated question round trip.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_pane.py -q
# exit 0: 16 passed in 4.37s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_claude_questions_send_selected_and_custom_answers -q
# exit 0: 1 passed in 0.65s after mobile checkbox/radio layout adjustment
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude.py
# exit 0: installed SDK/CLI handshake; owned child exit 0; no query/resume
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py tests/test_workspace_claude.py tests/test_workspace_pane.py
# exit 0: All checks passed!
```

Latest input integration: the host can now map session-bound uploads to Claude
SDK image content blocks and preserve text/document references. It uses the same
command receipt guard as Codex; replay cannot send a second message. The
development host now includes a lazy Claude factory and provider-aware admission.
Production remains behind the disabled structured-workspace flag. Existing PTYs,
external workers, exact process IDs, open transcripts and ambiguous project-local
processes block attachment. Claude's adapter validates native SDK session identity
before creating its process. Native resumed-session inference remains unverified.
The image proof below verifies encoding/storage, not model image understanding.
Claude's outgoing user events currently retain image payloads in the journal;
reference-based image history and bounded rendering remain to implement.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_uploads.py tests/test_workspace_host.py tests/test_workspace_claude.py -q
# exit 0: 20 passed in 2.63s
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-host.py
# exit 0: exact attachment bytes, Claude image encoding, no upload-triggered launch;
# one installed Codex initialization probe, repeated attachment, child exit 0
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_uploads.py core/workspace_host.py tests/test_workspace_uploads.py tests/test_workspace_host.py scripts/verify-workspace-host.py
# exit 0: All checks passed!
```

Claude mounting/admission verification:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_admission.py tests/test_workspace_app.py tests/test_workspace_claude.py tests/test_workspace_host.py -q
# exit 0: 26 passed in 6.63s, including both providers' real browser mount/reload
# with controlled adapters, missing native identity, and existing process rejection
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-mount.py --enabled
# exit 0: actual app registration/auth; no owner loop or provider launched
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_admission.py core/workspace_host.py tests/test_workspace_admission.py tests/test_workspace_app.py tests/test_workspace_claude.py
# exit 0: All checks passed!
```

Raghav approved the conversation panes in `_artifacts/serena-ui-concept`,
including styled messages, tool runs, diffs and composers, with all existing
CLI capabilities. A transcript viewer, cosmetic terminal theme, or reduced
chat client does not satisfy this request. Keep neon black.

## Verified Research (2026-09-09)

- https://learn.chatgpt.com/docs/app-server: official Codex rich-client protocol;
  bidirectional requests, streamed items, approvals, user questions, resume,
  interrupt, steering. Local installed schema generation confirms these methods.
  WebSocket transport is experimental; use local stdio owned by the host first.
- https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode:
  official persistent interactive input, images, tools, permissions and interruption.
- https://code.claude.com/docs/en/agent-sdk/slash-commands:
  discover commands through system/init.slash_commands; terminal-only commands
  such as /theme and /terminal-setup are omitted, not magically supported.
- https://geminicli.com/docs/ide-integration/: official ACP integration path.
  The locally configured Gemini executable may be Antigravity; do not assume
  Gemini CLI's ACP support applies to that executable.

These establish an implementation route, not complete parity. Existing
`core/codex_brain.py` is not a coding-pane backend: it disables shell_tool,
sets brain-specific instructions, and rejects interactive approval requests.
Do not change the brain's safety contract to reuse it for the workspace.

## Required Delivery Gates

Each gate requires provider-specific proof, not a generic mock passing.

- One runtime owner per exact session; attach/reconnect must not duplicate a
  writer. No silent resume-as-new or fork. Existing busy PTYs cannot be migrated
  by starting a concurrent app-server/SDK session with the same ID.
- Persisted real history plus ordered live messages, tool input/output, diffs,
  turn timing, errors, costs/context where provided. Never invent missing state.
- Multiline draft editing, selection, copy/paste, image/file uploads and mentions.
- Approvals, permission changes, questions, planning/review modes and interruption.
- Models/effort, slash commands/skills, MCP, plugins, hooks, subagents and background
  work. Inventory each provider's advertised commands and account for each one.
- CLI-only presentation functions need native equivalents; no raw-terminal toggle
  presented as satisfying the requested full custom interface.
- Linked Claude/Codex/Gemini isolation and routing; Fleet/bridge callers use the
  same owner, not a second process. Usage, pause/wake and attention remain accurate.
- Reconnect, reload, close view, app restart, provider failure, process failure and
  history restore are tested. Closing a view must not cancel work.
- Visual comparison against the mockup with real content; desktop/mobile,
  accessibility, console/network checks; Linux and Windows transport coverage.
- No metered-auth fallback, silent loss of configured instructions/tools, or
  auto-accepting approvals. Subscription compatibility is a separate live gate.

## Delivery Order

1. Bidirectional local process transport; test real pipes and reverse requests.
2. Provider adapters and single-owner registry integrated with existing PTYs,
   session IDs and bridges. Establish command/capability matrix and gaps.
3. Shared event journal and session-control routes, subscription authentication,
   permission validation and reconnect/replay semantics.
4. Mockup conversation renderer and complete composer, connected to real owners.
5. Provider-by-provider parity verification and migration. Only enable the new
   default when its required capabilities pass; keep existing sessions intact
   throughout development. Partial rollout is not goal completion.

## Current Slice

`core/workspace_rpc.py` supplies JSONL process transport only. It does not open
threads or run models. Reverse requests stay pending for explicit decisions;
an observation timeout neither restarts nor interrupts a process. This transport
is not yet wired into the app. It needs an event-journal consumer before runtime
use, so queued events do not accumulate without a bound in a resident host.

`core/workspace_codex.py` now implements exact-ID resume, submit, steer,
interrupt, approval/question responses and provider event publication. It keeps
ambiguous submissions unavailable for retry instead of starting duplicate turns.
Session ownership is now enforced with the shared lease described below;
the adapter is still not exposed to app routes.
It is not a complete command surface yet: advanced permission grants, MCP
elicitation, commands/plugins/settings controls and recovery remain to implement.

`ui/static/workspace-events.mjs` is the custom conversation's state model. It
applies history and streamed items using real item IDs, detects replay gaps,
rejects cross-session data, retains actual exit codes/diffs, and preserves
unknown event types for inspection. It is not yet mounted as a visible pane.

Verification to date: 10 Python transport/controller tests and 4 Node event-model
tests pass. The Python transport tests exercise real subprocess pipes; controller
tests use a protocol double. The installed-Codex live proof verifies initialization
and clean process shutdown only, not actual coding/approval/session migration.
`core/workspace_journal.py` adds SQLite-backed per-session event sequencing and
paged replay. Concurrent appends are serialized transactionally. Renderer
disconnection does not consume or delete events. Disk retention/deletion policy
is not yet integrated; history is deliberately not silently truncated.

`ui/static/workspace-pane.mjs` and `.css` now render the actual custom pane
component from structured provider events. Browser tests cover safe message text,
tool expansion, diffs, explicit exit codes, multiline composition, file selection,
send failure retaining drafts, question resolution and non-cancelling disposal.
Desktop/mobile screenshots were inspected. These are protocol-fixture browser
tests, not evidence of live Claude/Codex sessions in the custom interface. The
component still needs richer Markdown, attachment previews, command/settings
controls, pending-request schemas, full history paging and runtime integration.

`core/workspace_lease.py` now provides machine-local OS file locks and atomically
persisted owner/child identities. Both `CodexWorkspace.open` and PTY spawn/register/
migrate acquire the same per-session lease. Child identity includes process birth
time to avoid PID-reuse mistakes. A live orphan or an ambiguous interrupted launch
blocks admission instead of authorizing a duplicate writer. Lease files are not
unlinked; replacing a locked inode would defeat cross-process exclusion.

The lease contract only covers participating runtimes. Older hosts and manually
launched CLI sessions do not hold it. The route admission gate must still inspect
existing runtime registrations, and rollout must not migrate a busy unleased
session by launching another process. Windows locking is implemented but has not
been executed on Windows. Ambiguous-launch recovery needs an explicit, verified
recovery path, not blanket removal of lock metadata.

Verification: 49 scoped lease/controller/PTY/sleep/retry tests pass, including real process
exclusion and pseudo-to-durable migration with an unchanged child PID. Missing
POSIX executables are rejected before the launch marker, leaving the session retryable. The live
installed-Codex probe rejects a second owner, handshakes without a thread/turn,
then reaps the child and reacquires the lease (exit 0).

Exact ownership verification (2026-09-09):

```sh
env SERENA_RUNTIME_LEASE_DIR=/tmp/serena-structured-lease-verification /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_lease.py tests/test_workspace_codex.py tests/test_pty_terminal_runtime.py tests/test_terminal_spawn_retry.py tests/test_terminal_sleep.py -q
# exit 0: 49 passed in 11.31s
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-rpc.py
# exit 0: second owner rejected, initialize/initialized successful, child reaped with exit 0
```

`core/workspace_host.py` now owns adapters on a persistent event loop independent
of HTTP requests. Concurrent attachment reuses one owner, HTTP timeouts do not
cancel operations, and reads never launch anything. Explicit host shutdown waits
for admitted operations and reaps owners; closing a view never invokes it.
The journal also records command IDs before dispatch and confirmed receipts after
dispatch. An incomplete receipt is uncertain and is never automatically reissued.

`ui/workspace_web.py` supplies authenticated loopback-only control/replay routes
through a blueprint factory. It rejects cross-origin requests and unexpected Host
values. The app registers it only with the explicit development flag described
below. The host requires an authoritative resolver; test resolvers are not admission
proof for user sessions. No arbitrary provider RPC proxy is exposed.

`ui/static/workspace-connection.mjs` connects the real pane to those HTTP routes.
Pending send IDs survive view reload through sessionStorage. Replay advances only
after the view accepts each event; disposal stops polling, without stopping work.
Uploads now use the owner-bound transport below rather than sending only text.

The real-browser integration test mounts the actual blueprint and pane, sends
through local HTTP, receives journaled adapter output, reloads, and verifies the
same owner. Its provider is a controlled test fixture, not a live model. A separate
host test runs real subprocess pipes through the Codex adapter and event journal.
The safe installed-Codex host proof initializes a real app-server probe, attaches
twice to the same owner, reads the persisted event, and explicitly reaps it. It
does not resume a real conversation, start a turn, or establish provider parity.

Exact host/connection verification (2026-09-09):

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py tests/test_workspace_host.py tests/test_workspace_journal.py -q
# exit 0: 15 passed in 4.27s
node --test tests/workspace-connection.test.mjs tests/workspace-events.test.mjs
# exit 0: 8 passed, 0 failed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-host.py
# exit 0: one installed Codex initialization probe, repeated attach/replay, child reaped with exit 0
```

`core/workspace_uploads.py` stores bounded uploads privately under the workspace
state directory. A token is scoped to one exact session; renderer-supplied local
paths are rejected. Image payloads are decoded/verified, files are limited to
25 MB, and byte count/hash are checked again before delivery. Codex receives
images as native `localImage` inputs and other documents as explicit attached-file
references for its file tools. That document path is not a claim that every
binary format is natively interpreted by the model.

The composer now accepts file selection, pasted images and dropped files, with
raster previews and removal. Upload IDs are cached by filename/content hash, so
a lost send response reuses the same command receipt and attachment IDs. Preview
URLs are revoked on removal/disposal. Authenticated upload requests do not start
an agent. Submitted files persist for replay/resume; attachment deletion must be
integrated with session deletion before production rollout. No blanket expiry
may delete files still referenced by persisted conversations.

Upload verification (2026-09-09):

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_uploads.py tests/test_workspace_host.py tests/test_workspace_pane.py -q
# exit 0: 17 passed in 4.64s; includes real HTTP/browser upload, image delivery,
# cross-session denial, corrupt/changed file denial, paste/drop and mobile preview
node --test tests/workspace-connection.test.mjs
# exit 0: 4 passed, 0 failed; upload/send retries retain IDs
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-host.py
# exit 0: private byte-exact attachment storage and cross-session rejection,
# installed Codex initialization, single owner/replay, clean child exit 0
```

The mobile upload screenshot was inspected; preview, names, removal and composer
fit the 390x844 viewport. Browser provider responses remain controlled fixtures,
not proof of live image understanding or CLI capability parity.

`core/workspace_admission.py` now resolves the full indexed session ID, original
available project and native transcript, rejects Fleet/background ownership and
existing PTYs, and checks older/manual Codex processes for the exact session,
open transcript or unidentified ownership in the project. Missing metadata or
ambiguous ownership is not treated as permission to duplicate a writer. This
preflight cannot stop a manual future process from bypassing Serena's leases.
Cross-machine cwd fallback is deliberately not guessed by this adapter yet.

`ui/workspace_app.py` mounts the native pane page at `/workspace/<sid>` and the
control endpoints when `SERENA_STRUCTURED_WORKSPACE=1` is explicitly set in the
host environment. The ordinary app remains unchanged without this flag. The
page is local-only, no-store, same-origin framed and uses a restrictive CSP.
Reading the page does not attach a provider; the user explicitly resumes it.
Header provider identity comes from the index, not the selected adapter.

With the development flag, the app's existing code-pane entrypoint mounts this
page inside its linked-pane layout, reuses it on repeat opens, and routes focus
to its native composer. Structured runtimes are not passed to the PTY sleep or
resize APIs, nor given a simulated socket. Unsupported providers/new sessions
report unavailable rather than falling back to a raw terminal. This is an
incomplete development path, not the default or a completed parity migration.
Structured sleep policy, bridge/Fleet controls, new-session creation, full keyboard
integration, and all-provider support still need implementation and verification.

Admission/mount verification (2026-09-09):

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_admission.py tests/test_workspace_app.py tests/test_workspace_host.py tests/test_terminal_sleep.py tests/test_terminal_spawn_retry.py -q
# exit 0: 46 passed, 1 Python forkpty/thread deprecation warning, 12.30s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py tests/test_workspace_admission.py tests/test_terminal_spawn_retry.py -q
# exit 0 after focus/layout edits: 8 passed in 3.37s
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-mount.py --enabled
# exit 0: actual app routes and bootstrap enabled; no owner loop or provider launch
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-mount.py
# exit 0: routes/default flag disabled; no owner loop or provider launch
```

The mounted-page browser test uses the actual CSP, local HTTP APIs and page
module, then the real `_startStructuredPane` function inside a controlled parent
layout. It verifies compose/replay, repeat opens without a second frame, same
owner on reload, and no owner cancellation on frame removal. Provider output
is a controlled fixture. The standalone page screenshot was visually inspected;
this does not yet establish whole-app visual parity or live model behavior.

Model/effort/speed controls now use Codex's advertised catalog, not alias guesses.
The adapter pages `model/list`, rejects stuck pagination, journals the catalog,
and validates requested model/effort/service-tier combinations before `turn/start`.
Changing models without an explicit effort uses that model's advertised default;
leaving the controls unchanged does not override resumed settings. The header
updates from resumed settings and accepted turn options. Catalog discovery is
available only after explicit attachment; it does not create a second owner.

Official documentation rechecked 2026-09-09:
https://learn.chatgpt.com/docs/app-server (Models / List models). It documents
catalog discovery before rendering selectors, effort options, hidden models and
input modalities. Installed JSON schemas additionally confirm `serviceTiers`
entries with id/name/description and the exact `turn/start` option names.

The native selectors include only advertised effort/speed values. Returning to
the session-model option clears pending effort/speed overrides. Mobile inspection
confirmed they wrap rather than squeezing the model name into a few characters.
Provider selection does not get relabeled as a different agent or model.

Model controls verification (2026-09-09):

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_pane.py tests/test_workspace_host.py tests/test_workspace_app.py -q
# exit 0: 24 passed in 7.89s
node --test tests/workspace-connection.test.mjs tests/workspace-events.test.mjs
# exit 0: 8 passed, 0 failed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_advertised_model_effort_selection_reaches_submit_and_header -q
# exit 0 after mobile/default-reset edits: 1 passed in 0.59s
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-rpc.py
# exit 0: installed provider advertised 6 models on its first page, each with
# effort options; initialization/ownership check and clean child exit 0
```

The live proof reads the installed catalog without resuming a conversation or
running inference. Selection/validation/dispatch tests use controlled protocol
responses and therefore do not prove an actual model turn in the new interface.

`core/workspace_claude.py` now implements a persistent Claude SDK owner, with
exact native session lookup/resume, shared lease acquisition, verified child PID,
streamed input, interruption, explicit tool permissions, ambiguous-send rejection,
and same-task connection/cleanup. It selects the installed `claude` executable,
not an implicitly different bundled CLI. `pyproject.toml` adds an optional
`workspace` extra pinned to the inspected/tested SDK version 0.2.121.

The adapter loads the normal Claude Code prompt and user/project/local settings,
without brain-specific instructions or a tool allowlist. SDK environment merging
requires blocked inherited billing variables to be explicitly emptied; simply
omitting them would restore their parent values. Subscription OAuth stays intact.
Existing configured permission rules remain provider-owned. Permission callbacks
wait for explicit allow/deny, reject stale answers and deny on owner shutdown.

`core/workspace_claude_events.py` maps real SDK message types and native transcript
records into common pane events, retains original SDK records, joins text deltas
and final blocks by message ID, preserves tool input when results arrive, and
rejects foreign session IDs. The browser has native Claude allow/deny controls.

Official streaming-input documentation rechecked 2026-09-09:
https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode . The installed
Python SDK source additionally confirmed resume semantics, settings-source flags,
streamed `query()` session IDs, child ownership, and message dataclass shapes.

Claude verification (2026-09-09):

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_pane.py -q
# exit 0: 12 passed in 4.18s
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude.py
# exit 0: installed SDK control initialization and child identity verified;
# isolated config, no resume/user prompt/tool/inference; child reaped with exit 0
```

Claude remains excluded by app admission and the default host factories pending
native-resume proof, provider-specific file mapping, question/permission schemas,
model/effort and slash-command controls, and the broader parity matrix. The tests
use real SDK dataclasses but controlled clients; the installed SDK proof checks
only control initialization and teardown, not a live coding turn or subscription
billing for inference. No user's Claude process was resumed or interrupted.

Next: connect and verify Claude end to end, implement attachment/session deletion,
Antigravity control and the remaining capability
matrix, then prove full provider parity and migrate the real app. The replacement
remains disabled and incomplete.
