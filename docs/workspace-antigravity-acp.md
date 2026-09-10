# Antigravity ACP Integration

Research and native handshake verified 2026-09-09. This supersedes the earlier
assumption that Antigravity has no suitable interactive interface. The installed
`agy` CLI is still unsuitable, but Google distributes a separate ACP server.

## Primary Evidence

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
