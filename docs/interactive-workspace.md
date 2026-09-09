# Interactive Workspace Delivery Contract

Status: implementation in progress. Not a delivered replacement.

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
