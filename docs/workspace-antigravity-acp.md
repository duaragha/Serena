# Antigravity ACP Integration

Research and native handshake verified 2026-09-09. This supersedes the earlier
assumption that Antigravity has no suitable interactive interface. The installed
`agy` CLI is still unsuitable, but Google distributes a separate ACP server.

## Primary Evidence

### Isolated Real CLI Conversation Copy

2026-09-10: the user approved testing an isolated session copy. The offline
probe copied an inactive CLI SQLite database into a temporary ACP store using
SQLite backup and Google's packaged `session_store` implementation. It decoded
**778 populated steps**, removed **768 thought signatures from the copy**, and
confirmed that every remaining serialized step matched the expected cleanup.
A second cleanup was idempotent. The source hash, size and modification time
were unchanged, no source sidecars appeared, and the temporary store was removed.
No provider process, credentials, authentication or prompt was involved.

This establishes storage compatibility for this one conversation, **not** a
supported migration or authenticated resume. Native `_restore_session` also
resolves authentication, model availability and agent configuration. Automatic
migration and default Gemini admission remain disabled until those are proven.

Commands executed separately, final exit codes all **0**:

```sh
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-gemini-copy.py apps/desktop/build/proof-tools/antigravity-acp/agy_acp_server.par /home/raghav/.gemini/antigravity-cli/conversations/a780b23f-06ed-435b-af40-d9dea8e1f441.db
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_gemini_copy.py -q
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-gemini-copy.py tests/test_workspace_gemini_copy.py
```

Tests: **4 passed** (WAL, SHM, journal and symlink refusal before vendor import).
Initial Ruff checks exited **1** for import ordering and a nested context manager;
these were corrected before the clean final check. The probe rejects databases
with SQLite sidecars; it is an offline diagnostic, not a production migration API.

### Assistant And Tool Images

ACP assistant image chunks now become image-bearing assistant messages instead
of generic protocol dumps. Text before and after remains separate, and original
metadata is preserved. Tool-call image content uses the same bounded loader as
history attachments. Only PNG/JPEG/GIF/WebP blobs are displayed; malformed base64,
undecodable bytes and unsupported media produce a visible unavailable label.
Failed images and replaced items release their object URLs. This adds rendering
support, not authenticated image-generation or Google session compatibility.

2026-09-10 commands, each exit **0**:

```sh
env PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_events.py tests/test_workspace_pane.py -k 'image' -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_events.py -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_acp_events.py tests/test_workspace_acp_events.py tests/test_workspace_pane.py
node --check ui/static/workspace-pane.mjs
```

Image checks: **16 passed, 150 deselected**. Event module: **22 passed** (overlaps
the image run). Actual Chromium rendered controlled 800x400 PNG content at 390px
and 1600px, including user, assistant and tool images. Pixel checks matched the
fixture, no horizontal overflow or page errors occurred, and replacement released
all image URLs. Mobile assistant and desktop tool screenshots were inspected.
SVG, malformed base64 and invalid PNG bytes were rejected. These are browser and
translation checks with fixtures, not a native provider image turn.

### Disconnect Capability Boundary

Rechecked the official [ACP terminal contract](https://agentclientprotocol.com/protocol/v1/terminals)
on 2026-09-10. Its terminal methods manage client-created terminals and require
the client terminal capability; this is not an inventory of provider-owned
background work. The current adapter advertises no client terminal capability
and has no background-task query. Consequently, a ready prompt state alone is
not evidence that closing the native process will preserve all work.

The host now explicitly refuses Gemini's `disconnect_session` before calling an
unsupported adapter method. It reports that background completion cannot be
confirmed, rather than exposing an AttributeError or assuming an empty task
list. A real-pipe integration test completes a prompt, attempts disconnect,
repeats the receipt, and verifies the same process remains alive and history is
unchanged. Ordinary view close still does not shut down the owner. This is an
honest capability boundary, not implemented Gemini safe-disconnect parity.

Executed separately, each exit **0**:

```sh
env PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_gemini.py::test_host_routes_real_pipe_prompt_permission_output_and_receipt tests/test_workspace_host.py::test_explicit_disconnect_preserves_history_and_never_stops_other_owner -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py tests/test_workspace_gemini.py
env SERENA_EVIDENCE_KIND=live PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-antigravity-acp.py apps/desktop/build/proof-tools/antigravity-acp/agy_acp_server.par
```

Tests: **7 passed**; Ruff clean. Native proof revalidated Google server 1.1.1's
handshake, exact CLI-only session rejection without replacement, owner refusal
before launch, and native cleanup exit 0. The disconnect assertion uses the
controlled real-pipe peer, not an authenticated Google session. Authentication,
session-store compatibility and full Gemini admission remain unverified.

### Attachment Recovery After Cleanup

Gemini now implements the host's `can_retry_attachment` contract. A failed
load may be retried by an explicit attach only after cleanup has finished:
the RPC process reference is cleared, its local prompt task has ended and the
lease is released. Cleanup exceptions, cancellation, or a transport returning
without clearing its process retain the lease and refuse replacement. A later
explicit cleanup can recover. Reads/polling never trigger a retry.

2026-09-09 verification:

```sh
env PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_gemini.py -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_gemini.py tests/test_workspace_gemini.py
```

27 passed in 0.84s (exit 0); lint clean (exit 0). Real-pipe cases cover
exception, silent incomplete cleanup and cancellation, exclusive lease retention,
subsequent confirmed shutdown and host retry after failed load.

A separately recorded `SERENA_EVIDENCE_KIND=live` Python command initialized
the actual Google 1.1.1 server under a temporary HOME with a temporary lease.
It interrupted the cleanup callback, verified the original native PID remained
alive and a second lease was refused, then restored production cleanup. The
native process exited 0, retry became available, and the released lease could
be acquired. Command exit 0. No session/load, authentication or prompt was sent;
no user credentials or existing runtime were used. This is native cleanup proof,
not successful authenticated chat proof. CLI indexing/default admission remain
unchanged because their separate session store is still not interchangeable.

### Native Session Mode Control

The Gemini pane now has a Session mode selector backed by the advertised
`category: mode` select option. It displays provider names/descriptions and
requires explicit Apply. The controller validates offered values and sends
`session/set_config_option` with the exact persisted session and native config
ID. Only a matching native acknowledgement confirms the change. Active turns,
unknown values and unconfirmed responses cannot silently change the UI state.
Stable host command receipts prevent repeated delivery. Model and mode changes
share the same validated selector path; mode is not sent as a fabricated prompt.

Rechecked https://agentclientprotocol.com/protocol/v1/session-config-options on
2026-09-09. Inspected Google's downloaded 1.1.1 server.py and config_options.py:
the native server dispatches model/mode IDs through this method and advertises
Default, Auto Edit and YOLO, with descriptions of permission behavior. The UI
does not invent these values or choose a more permissive default.

Exact verification commands (repository root), each exit 0:

```sh
env PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_gemini_native_mode_requires_explicit_apply_and_confirmation tests/test_workspace_acp_session.py tests/test_workspace_gemini.py -q --tb=short
env PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_acp_session.py core/workspace_gemini.py core/workspace_host.py tests/test_workspace_acp_session.py tests/test_workspace_gemini.py tests/test_workspace_pane.py
node --check ui/static/workspace-pane.mjs
```

Results: 40 passed in 3.74s, 58 passed in 7.25s, lint clean, JS syntax clean.
The real subprocess JSON-RPC peer exercises host/owner/controller mode routing,
duplicate receipts, unoffered values and busy-turn refusal. Browser coverage at
390px/1600px checks explicit Apply, confirmed/error states, native descriptions,
unchanged drafts and dialog close. Inspected the 390px screenshot.
An initial browser fixture forgot to reset its event sequence when replacing
the pane, leaving it connecting; the isolated reproduction exited 1. Fixed the
fixture. The initial combined run was interrupted during browser cleanup
(exit 130); the separate final scoped runs above completed successfully.

This proves local protocol routing and rendering, not an authenticated Google
model turn. Default Gemini admission remains closed and the installed app has
not been updated.

### Incremental Assistant Streaming

Assistant text starts with one full item, followed by session-bound
`item/agentMessage/delta` events. The final item and loaded history still contain
the complete text. User history chunks retain their existing behavior.

Verification (2026-09-09), run separately from the isolated worktree:

```sh
env PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_events.py tests/test_workspace_acp_session.py tests/test_workspace_gemini.py -q --tb=short
env PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_acp_stream_deltas_render_exactly_before_and_after_completion tests/test_workspace_pane.py::test_questions_resolve_only_from_provider_and_stream_does_not_collapse_tools -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_acp_events.py tests/test_workspace_acp_events.py tests/test_workspace_acp_session.py tests/test_workspace_pane.py
```

Results: 55 passed (exit 0), 2 browser tests passed (exit 0), lint passed
(exit 0). The real-pipe test confirms queued deltas drain before completion;
Chromium renders all 200 chunks exactly once before and after completion.
A separately recorded `SERENA_EVIDENCE_KIND=live` production-translator probe
(exit 0, no provider launch) measured 80,218 serialized streaming bytes versus
5,178,200 for cumulative full items, a 98.45% reduction for 200 256-byte chunks.
This measures event payload size, not end-to-end model latency or total journal
storage. No installed app or default Gemini admission was changed.

All URLs accessed 2026-09-09:

- https://antigravity.google/docs/ide/extensions/zed/ directs users to install
  Antigravity from the external-agent registry and documents `oauth-personal`
  for individual Free/Pro/Ultra accounts. This establishes a subscription route,
  not proof that existing CLI credentials or session stores are interchangeable.
- https://zed.dev/acp/agent/antigravity-acp identifies the executable as
  `agy_acp_server.par`, separate from `agy`.
- https://raw.githubusercontent.com/agentclientprotocol/registry/main/antigravity-acp/agent.json
  identifies Google LLC, version 1.1.1, proprietary licensing, Linux/macOS/Windows
  distributions and the Linux argument `--uid=`.
- https://antigravity.google/docs/cli/headless/ still documents rejection of
  control request/response events, CLI slash commands and non-text input blocks.
  Do not build the rich pane on that print-mode protocol.
- https://antigravity.google/docs/sdk/overview/ documents API-key or enterprise
  platform authentication for the Python SDK; it is not the selected integration.
- https://antigravity.google/docs/remote-control/ describes a registered OS daemon
  and hosted Google dashboard. No daemon was installed or started.
- https://agentclientprotocol.com/protocol/v1/initialization defines JSON-RPC 2.0
  framing, version negotiation and advertised capabilities. Initialization does
  not require creating a coding session.
- https://agentclientprotocol.com/protocol/v1/session-setup requires checking
  load/resume support and using the exact persisted ID. Load replays history;
  resume does not. Neither implies compatibility with a different native store.

## Native Probe

Downloaded only into ignored development build artifacts:

`https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-agy_acp_server_1.1.1-linux-x86_64.zip`

Observed archive SHA-256:
`38f62d01b32deb0907b3d39a71ec301fd36369f6ffd1cf262d4af385177f79df`

Archive contains `agy_acp_server.par` and `localharness_external`. The digest is
a reproducibility record of the downloaded artifact, not an independently
published signature. No binary is committed or added to application packaging.

The production `WorkspaceAcpRpc` transport successfully negotiated version 1
with `antigravity-acp`, version `agy_acp_server_1.1.1`. Observed capabilities:

- `loadSession: true`; session `list` and `resume` supported.
- Prompt image, audio and embedded context supported.
- MCP HTTP and SSE supported.
- `oauth-personal`, `oauth-business`, `gemini-api-key`, `agent-platform` advertised.

The probe uses a temporary empty HOME, explicit minimal environment and no user
credentials. It sends only `initialize`, then closes and reaps the process.
It does not call authenticate, new, load, resume, prompt or a provider tool.

Exact verification:

- `SERENA_EVIDENCE_KIND=live agy --help`: exit 0; no ACP mode on installed CLI.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp.py -q --tb=short`: exit 0, 7 passed. Real subprocess framing, explicit reverse response, duplicate-response refusal and invalid negotiation; no automatic auth/session request.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_acp.py tests/test_workspace_acp.py scripts/verify-workspace-antigravity-acp.py`: exit 0.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-antigravity-acp.py apps/desktop/build/proof-tools/antigravity-acp/agy_acp_server.par`: exit 0; capability response above, isolated native server exit 0. Repeated after replacing the probe-local transport with production transport.

## Session Store Verification

Additional verification on 2026-09-09 used source files shipped inside the pinned
Google archive, read without modifying or importing the vendor application:

- `google3/cloud/developer_experience/antigravity_extensions/acp_server/paths.py`
  places trajectories under `$GEMINI_HOME/antigravity-acp/conversations` (default
  home is `~/.gemini`). CLI storage is `antigravity-cli/conversations` instead.
  ACP also has separate credential and trust files. Shared global hooks and
  CLI-installed skills do not imply shared sessions or authentication.
- `session_store.py::strip_thought_signatures_from_db` updates stored protobuf
  payloads in place during restoration. Do not symlink existing user databases
  into ACP merely to make an ID resolve: loading can modify those files.
- `server.py::_restore_session` requires a database in the ACP store and raises
  resource-not-found (-32002) otherwise. It does not search the CLI directory.

Extended the native probe with an isolated empty SQLite fixture in the CLI
directory. The actual ACP server returned an empty catalog from `session/list`
and rejected `session/load` for that exact fixture ID with -32002. The CLI
database remained byte-for-byte unchanged and no ACP replacement database was
created. No authenticate/new/prompt call was made, and no user database was read
or copied. This proves store separation, not compatibility of real trajectories.

Exact verification:
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-antigravity-acp.py apps/desktop/build/proof-tools/antigravity-acp/agy_acp_server.par`: exit 0; handshake, empty catalog, exact missing-session rejection, no replacement, native exit 0.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_admission.py tests/test_workspace_acp.py -q --tb=short`: exit 0, 15 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_admission.py scripts/verify-workspace-antigravity-acp.py tests/test_workspace_admission.py`: exit 0.

The unavailable reason now states the unverified exact-session integration
instead of implying Google has no interactive interface. Admission is unchanged.

## Next Integration Work

Owner command interface added 2026-09-09: Gemini owner exposes `submit`,
`interrupt`, `answer`, `active_turn` and `questions` for the existing host
contract. Submission schedules the owned prompt without holding up interruption
and retains its turn ID even if completion precedes acknowledgement. Permission
answers accept only native selected/cancelled envelopes. Explicit shutdown waits
for the owned prompt task after transport teardown. Unsupported per-turn settings
are refused. Host upload mapping and provider admission remain disabled.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_gemini.py tests/test_workspace_acp_session.py -q --tb=short`: exit 0, 12 passed, including nonblocking submission, duplicate refusal, interrupt and strict permission-envelope checks.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_gemini.py tests/test_workspace_gemini.py`: exit 0.

This is controlled interface coverage, not authenticated native turn execution.

Native owner foundation added 2026-09-09 in `core/workspace_gemini.py`. Explicit
open requires a canonical ID, an existing ACP database and project-matching
sidecar, existing personal OAuth settings/token file, and an explicit server
binary. Symlink/hardlink trajectory aliases are refused. It acquires the shared
session lease before launch, binds the actual child PID, validates server identity
and loads only that session. Startup failure closes its transport; explicit close
reaps before releasing ownership. No create/authenticate/migration API is called.

This does not verify the token contents or subscription validity. Settings must
currently be plain JSON; vendor-supported Hjson syntax is not yet handled.
Successful owner tests use a controlled subprocess, not an authenticated Google
session. Manual legacy orphan detection, broader Google auth-environment review,
host adapter methods and production admission remain unfinished. Do not expose
this class as a complete migration path.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_gemini.py tests/test_workspace_acp_session.py -q --tb=short`: exit 0, 11 passed. Actual subprocess lease, competing-owner refusal, cleanup and pre-launch configuration/path refusals.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_gemini.py tests/test_workspace_gemini.py scripts/verify-workspace-antigravity-acp.py --fix`: exit 0; one import-order issue fixed.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-antigravity-acp.py apps/desktop/build/proof-tools/antigravity-acp/agy_acp_server.par`: exit 0; native transport/load refusal plus production owner rejection of CLI-only fixture before any second process launch. No credentials or inference.

Ordered transport consumption added 2026-09-09. The controller can explicitly
start an event reader on the owned RPC queue. Load and prompt responses wait for
earlier queued updates to be consumed before publishing history/completion.
Reader failures disable input and drain remaining queued entries without applying
them. Stopping the reader disables the controller but does not close the native
process; process ownership remains the caller's responsibility.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_session.py tests/test_workspace_acp_events.py tests/test_workspace_acp.py -q --tb=short`: exit 0, 16 passed. New real-subprocess case emits 20 chunks immediately before each load/prompt response; delayed publication still retains every chunk before completion. Reader stop leaves subprocess alive until explicit transport cleanup.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_acp_session.py tests/test_workspace_acp_session.py scripts/verify-workspace-antigravity-acp.py`: final exit 0; test import ordering corrected after initial exit 1.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-antigravity-acp.py apps/desktop/build/proof-tools/antigravity-acp/agy_acp_server.par`: exit 0 with controller reader enabled; exact native rejection, no replacement and clean process exit. No authentication or model inference.

Session controller foundation added 2026-09-09 in `core/workspace_acp_session.py`.
It operates over an already owned transport, loads only the supplied persisted
ID, retains replayed history, serializes prompts, validates advertised content
capabilities and routes permission answers. Cancel also declines permission
requests arriving after cancellation begins. Load failure cannot become create
or automatic retry. Cross-session event failures disable further input.
It never launches/authenticates or acquires a lease itself: native process owner
and host admission wiring remain required before exposing it in the app.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_session.py tests/test_workspace_acp_events.py tests/test_workspace_acp.py -q --tb=short`: exit 0, 15 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_session.py -q --tb=short`: exit 0, 4 passed after final load-routing guard.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_acp_session.py tests/test_workspace_acp_session.py scripts/verify-workspace-antigravity-acp.py`: exit 0.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-antigravity-acp.py apps/desktop/build/proof-tools/antigravity-acp/agy_acp_server.par`: exit 0; now uses the production session controller for native missing-session refusal. No replacement or mutation; process exit 0. Successful load/prompt/cancellation tests still use controlled records, not authenticated native inference.

Event translation foundation added 2026-09-09 in `core/workspace_acp_events.py`:
exact-session validation, explicit turn boundaries, optional native message IDs,
contiguous ID-less text chunks, incremental tool updates and retained unknown
content. Idle command/config/usage metadata does not fabricate a turn. Permission
requests retain native choices, reject unoffered answers and stay pending until
explicit resolution. The shared pane renders ACP tools and native permission
choices without automatic approval or HTML interpretation.

Protocol sources accessed 2026-09-09:
https://agentclientprotocol.com/protocol/v1/prompt-turn and
https://agentclientprotocol.com/protocol/v1/tool-calls. This implementation is not
yet wired to an authenticated ACP owner. History reconstruction, rich content,
configuration controls and cancellation orchestration still need integration.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_events.py tests/test_workspace_pane.py::test_acp_permission_options_are_explicit_and_exact -q --tb=short`: exit 0, 8 passed, including desktop/mobile explicit native option IDs and literal tool output.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_acp_events.py tests/test_workspace_acp_events.py tests/test_workspace_pane.py`: exit 0.
- `node --check ui/static/workspace-pane.mjs`: exit 0.

These are adapter/browser tests using controlled protocol records, not a claim
that a real Gemini model produced these events or executed a permissioned tool.

The JSON-RPC foundation is implemented, not the provider adapter or UI admission.
Gemini remains unavailable in the development rich pane until these checks pass:

1. Establish native session storage and whether an existing `agy` conversation
   can be loaded by this server without conversion, duplication or context loss.
2. Verify supported subscription authentication without copying credentials,
   introducing API billing or silently triggering browser sign-in.
3. Map ACP updates, permission choices, config options, models, commands,
   images and cancellation to the shared owner/journal/pane contracts.
4. Bind exact session leases before load; fail closed if a PTY or other worker
   already owns it. Never fall back from failed load to session/new.
5. Verify configured instructions, skills, plugins, MCP and tools are retained.
6. Exercise native session round trips and Linux/Windows packaging before enabling.

The previous CLI limitation is no longer a reason to declare the overall Gemini
integration impossible. Advertised capability support is also not a substitute
for the still-missing end-to-end proof.
# Session-bound input mapping, 2026-09-09

Submission validation (2026-09-09): content shape and advertised capabilities
are checked before model configuration or prompt admission. Invalid text,
unsupported images and malformed resources leave the owner ready, emit no
synthetic transport-close event and make no native request. A corrected message
can use the same owner immediately. Actual post-admission transport failures
still make the session unavailable and are not automatically retried.

`PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_session.py tests/test_workspace_gemini.py -q --tb=short`:
exit 0, 34 passed. Scoped Ruff: exit 0. Includes invalid-input/no-model-change
cases followed by successful corrected input, plus existing real-pipe ownership
and command/permission tests. No authenticated model execution in this run.

Orphan ownership check (2026-09-09): admission now recognizes Google's
`localharness_external` process and its truncated Linux name alongside `agy`.
A matching open transcript, ambiguous same-project worker or inaccessible
ownership data blocks attachment. A worker in another project without the target
transcript does not block it. This supplements leases; it does not constrain a
manual process launched after admission or solve Windows process containment.

`PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_admission.py tests/test_workspace_gemini.py -q --tb=short`:
exit 0, 28 passed. Ruff: initial import-order finding, fixed; final exit 0.
A `SERENA_EVIDENCE_KIND=live` Python command launched a harmless Python child
with harness argv in an isolated temporary project, observed actual process-table
rejection, terminated/reaped it, and confirmed the check then cleared: exit 0.
No native provider, user session or credentials were used in that proof.

Context usage (2026-09-09): native `usage_update` used/size token counts now
reach the pane as explicitly labeled context usage, not account quota. Valid
zero is retained; invalid/unsafe counts clear stale values. Native cost metadata
is preserved but not turned into a billing claim. Usage arriving during load
is included in the final history snapshot so the renderer does not discard it.
Contract: https://agentclientprotocol.com/protocol/v1/prompt-turn#session-usage-updates
(accessed 2026-09-09).

`PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_events.py tests/test_workspace_acp_session.py tests/test_workspace_pane.py::test_acp_context_usage_is_labeled_and_invalid_updates_clear_it -q --tb=short`:
exit 0, 33 passed. Scoped Ruff and both changed JS module syntax checks: exit 0.
Includes invalid/zero/over-capacity counts, load ordering and mobile/desktop
display. Controlled event coverage; actual authenticated Gemini usage reporting
and installed-app rollout remain unverified.

Honest stopped turns (2026-09-09): `max_tokens` and `max_turn_requests`
now map to interrupted rather than completed; `refusal` maps to failed with
the original stop reason retained. The pane displays the concrete reason even
when no duration was supplied, keeps explicit next input available, and never
auto-continues. Only `end_turn` maps to successful completion. The shared bridge
therefore no longer reports these incomplete results as successful receipts.
Protocol reference, accessed 2026-09-09:
https://agentclientprotocol.com/protocol/v1/prompt-turn#stop-reasons

`PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_events.py tests/test_workspace_pane.py::test_acp_stop_reason_visible_without_duration_or_auto_continuation tests/test_workspace_pane.py::test_native_usage_and_completed_duration_are_not_invented -q --tb=short`:
exit 0, 19 passed. First run: 16 passed, three desktop test locators matched
both panes; narrowed to the tested pane. Ruff and JS syntax checks: exit 0.
Controlled events/browser checks only, not authenticated provider execution.

Plan presentation (2026-09-09): ACP plan notifications replace one stable
per-turn plan item instead of appending opaque blocks. The pane renders a
read-only list with native status icons and priorities; removed entries disappear,
empty plans clear, and no completion is inferred from the turn ending. Content
is literal text, including markup, with responsive wrapping.
Official replacement contract (accessed 2026-09-09):
https://agentclientprotocol.com/protocol/v1/agent-plan

`PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_events.py tests/test_workspace_pane.py::test_acp_plan_updates_in_place_without_inventing_completion -q --tb=short`:
exit 0, 9 passed. Scoped Ruff and JS syntax: exit 0. Desktop/mobile browser
coverage includes update/removal, malformed-plan refusal, literal markup and
overflow checks. Mobile screenshot inspected. Controlled events only; native
authenticated planning remains unverified.

Native command catalog (2026-09-09): session `available_commands_update`
notifications now replace a validated catalog, exposed by the host's commands
action and the Gemini picker. Selecting only prefixes the existing draft;
execution is an explicit ordinary prompt. No authentication/sign-out command
was executed during verification. The installed server advertises the SDK's
`plan` command and its separate `logout` command. No invented CLI catalog.
Official contract: https://agentclientprotocol.com/protocol/v1/slash-commands
(accessed 2026-09-09).

`PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_session.py tests/test_workspace_gemini.py tests/test_workspace_pane.py::test_gemini_command_picker_preserves_draft_until_send -q --tb=short`:
exit 0, 29 passed. Scoped Ruff and JS syntax: exit 0. Tests cover live catalog
replacement/removal, invalid-name rejection, desktop/mobile draft preservation
and production host/owner exact command delivery through a controlled subprocess.
Authenticated command execution, account lifecycle UX and default Gemini admission
remain unfinished; this is not an installed-app update.

Native settings compatibility (2026-09-09): the shipped Google settings parser
uses Hjson. Serena now uses pinned `hjson==3.1.0`, accepting comments, unquoted
keys and trailing commas without rewriting settings. Explicit personal OAuth
and valid object shapes are still required; API-key settings remain refused.
Package reference, accessed 2026-09-09: https://pypi.org/project/hjson/3.1.0/

`PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_gemini.py -q --tb=short`:
exit 0, 17 passed. Scoped Ruff: exit 0.
`SERENA_EVIDENCE_KIND=live PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-antigravity-acp.py apps/desktop/build/proof-tools/antigravity-acp/agy_acp_server.par`:
exit 0, native initialization and missing-session refusal, clean process exit.
The parser was installed only in the ignored branch-local proof dependency
directory; shared environment and user configuration were untouched. The
previous frozen backend predates this parser dependency and needs rebuilding
before claiming packaged Gemini settings support. Authentication remains unverified.

Provider-driven model updates (2026-09-09): load, native configuration updates,
and confirmed selection responses now publish an atomic model catalog/current
selection event. Missing or unsupported model catalogs clear the picker and
current label instead of retaining a stale model. The pane explicitly shows
`Model unavailable` when the native catalog no longer identifies it. No model
change or prompt is sent by receiving these updates.

`/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_session.py tests/test_workspace_gemini.py tests/test_workspace_pane.py::test_model_catalog_update_clears_unavailable_header_without_submission -q --tb=short`:
exit 0, 21 passed. The first run had two browser assertions racing the scheduled
render (19 passed, exit 1); they now wait for the actual DOM state. Scoped Ruff
and JS syntax check: exit 0. Controlled protocol/browser coverage only.

Model controls (2026-09-09): model discovery now uses the loaded ACP select
option with category `model`. Catalog IDs, labels and current selection come
from the provider. Explicit submit options apply `session/set_config_option`
using that exact config ID, and require its response to confirm the requested
model before sending the prompt. An unconfirmed change disables the owner; it
does not guess, resend or change permission mode. Configuration notifications
replace the stored catalog. Unsupported effort/settings remain rejected.

Official contract, accessed 2026-09-09:
https://agentclientprotocol.com/protocol/v1/session-config-options
The shipped Google 1.1.1 server's `set_config_option` implementation also
confirms this path. Grouped/unknown option shapes are not accepted as models.

`/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_session.py tests/test_workspace_gemini.py tests/test_workspace_host.py::test_gemini_explicit_owner_uses_acp_mapping_and_deduplicates_delivery -q --tb=short`:
exit 0, 20 passed. Scoped Ruff: exit 0. Includes real subprocess framing
through host/owner: discover without switching, confirm model selection, then
prompt/permission/output; duplicate requests do not repeat either mutation.
These are controlled-peer checks, not authenticated Google model execution.

Live submitted messages (2026-09-09): inspection of the shipped 1.1.1
`server.py` found `UserMessageChunk` emission in historical step replay, not
the live prompt path. The controller now records the exact submitted content
as a client-origin user item before dispatch, without claiming a provider echo
or acknowledgement. Input is snapshotted before yielding; delivery failure
retains that item and disables further input instead of resending it. Successful
completion includes the submitted item followed by actual provider output.

`/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp_events.py tests/test_workspace_acp_session.py tests/test_workspace_gemini.py -q --tb=short`:
exit 0, 22 passed. Initial run: exit 1, 20 passed and one old positional
assertion needed updating for the added user item. Scoped Ruff: exit 0.
Includes real-pipe integrated host checks and a delivery-failure test; no
authenticated Google prompt was executed.

Integrated owner/host proof (2026-09-09): a real Python subprocess exchanges
ACP frames through production RPC, Gemini owner, shared lease, persistent host
and journal. The test verifies no launch on reads, exact-ID load/prompt,
nonblocking permission delivery, rejection of an unoffered answer, exact valid
reply, streamed result with matching turn ID, duplicate command receipt reuse,
same-process reattachment and clean explicit shutdown (child exit 0).

`/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_gemini.py -q --tb=short`:
exit 0, 10 passed. Scoped Ruff: exit 0. The child is a controlled protocol peer,
not Google's authenticated runtime; this verifies the integrated transport path
without claiming subscription/model execution or enabling default admission.

Tool presentation (2026-09-09): ACP display content is retained separately from
raw output, including through partial updates. The pane prioritizes display
content, renders nested text and before/after diffs, and preserves raw output in
collapsed Native details. Unknown blocks remain inspectable, not executed.
Official schema: https://agentclientprotocol.com/protocol/v1/tool-calls
(accessed 2026-09-09). This is a display change, not proof a file edit occurred.

Command: `.venv/bin/python -m pytest tests/test_workspace_acp_events.py tests/test_workspace_pane.py::test_acp_tool_content_is_readable_and_diff_markup_is_inert tests/test_workspace_pane.py::test_claude_tools_show_readable_native_output_and_requested_edits -q --tb=short`
(using the shared Serena virtualenv): exit 0, 10 passed. Desktop/mobile checks
cover literal malicious markup, new-file diffs, no automatic actions, raw-detail
access and existing Claude presentation. Mobile screenshot inspected. Scoped Ruff
and `node --check ui/static/workspace-pane.mjs`: exit 0.

ACP user-image history now translates to the existing safe image renderer,
preserving the original content and separating surrounding text chunks. Browser
checks cover decoded images, hidden base64 text, blob URL cleanup, and no
horizontal overflow at 390px and 1600px for all three providers. Adapter plus
image browser checks: 11 passed, exit 0; scoped Ruff: exit 0. These exercise
production translation and rendering with controlled events, not an authenticated
Gemini history replay.

The shared host now maps Gemini submissions through session-bound upload tokens:
images become ACP base64 image blocks; documents become file resource links.
Arbitrary renderer paths and another session's tokens remain rejected. Exact
request receipts prevent a repeated submit from delivering twice. The default
Gemini factory and saved CLI session admission remain disabled: this is input
integration, not evidence of authenticated native prompt execution.

Official schema checked 2026-09-09:
https://agentclientprotocol.com/protocol/v1/content

Verification: scoped uploads and host-routing tests: 10 passed, exit 0; Ruff:
exit 0. An isolated runtime command exercised actual upload storage, byte-exact
ACP image mapping and cross-session rejection, exit 0, without launching a
provider. The first scoped test run had one test-cleanup failure (calling
`close` instead of the host's `shutdown`); corrected before the passing run.
