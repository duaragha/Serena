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

Next: connect adapters to the journal and pane through the host, implement Claude/
Antigravity control and the remaining capability matrix, then prove full provider
parity and migrate the real app. The replacement remains disabled and incomplete.
