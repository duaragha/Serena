# Interactive Workspace Delivery Contract

Status: implementation in progress. Not a delivered replacement.

Native Claude clear lifecycle research (2026-09-09): current official docs at
https://code.claude.com/docs/en/agent-sdk/slash-commands#reset-context-with-clear
now describe /clear in streaming sessions. Search snippets returned older text
claiming it unavailable; the fetched page and installed SDK/native runtime are
the evidence used here. The existing pane still refuses session-switch commands.
The new isolated native probe establishes:
- /clear is advertised, returns a DIFFERENT session ID, zero model turns/cost.
- The new ID is not immediately discoverable through getSessionInfo and its
  getSessionMessages result is empty before the next prompt.
- A subsequent local /effort command stays on that new ID and persists its
  history. Original history records remain deeply equal to the pre-clear copy;
  the new history contains none of their message UUIDs.
Therefore /clear cannot be implemented by clearing DOM, loosening all identity
checks, or closing the runtime and resuming the new ID immediately. Next
implementation must hold input during an explicit identity-transition command,
checkpoint the native returned ID, acquire/transfer exact-session ownership,
retain the live runtime until native persistence is confirmed, and register the
new conversation without changing the original's history/group links. A lost
transition outcome must remain uncertain, never replay /clear automatically.
This is a pending-session lifecycle, not the existing persisted-fork lifecycle.
Verification:
- `SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude`: initial observation exit 0. Adding an assumption that getSessionInfo must already resolve the cleared ID produced exit 1, disproving immediate close/resume. Final observational probe exit 0 with persistedBeforeNextPrompt:false, messagesAfterClear:0, original message count 1 before/after, next-session count 2. Assertions enforce changed identity, unchanged original records, no original UUIDs in new history and zero inference.
The failed persistence assumption was removed from the probe, not from product
requirements. This does not claim the clear feature implemented or resumable
before first input. Only isolated temporary home/runtime data was created.

Earlier-history attachment previews (2026-09-09): the upload decorator now
handles workspace/historyPage as well as the initial workspace/history event.
Older user images receive preview tokens only after the same session/path/hash
validation. Foreign-session or changed files remain undecorated; original
provider records are unchanged. This repairs preview loss when loading earlier
messages, not cross-session/fork attachment access or retention policy.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_uploads.py::test_all_history_pages_decorate_only_unchanged_session_images -q`: before the fix, exit 1, 1 failed/1 passed; the older-page case lacked previewToken.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_uploads.py tests/test_workspace_app.py -q`: exit 0, 12 passed in 7.94s.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-history-images.py`: initial exit 1 because a string-form Playwright wait violated production CSP. Changed the proof to a function-form wait and the actual image class; no CSP policy changed. Final exit 0: desktop/mobile decoded the 96x64 owned image through real session-bound HTTP, no foreign preview fetched, reopening replayed the older page with one owner and one history request. Provider history was controlled fixture data; no CLI/inference or user files involved.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_uploads.py tests/test_workspace_uploads.py scripts/verify-workspace-history-images.py`: exit 0.
Inspected apps/desktop/build/workspace-proof/history-images-mobile.png; image
visible and content contained. Source-only, not packaged or installed.

Explicit failed-attachment retry (2026-09-09): a closed/unavailable owner can be
replaced only on an explicit attach request and only when its provider reports
finished successful cleanup. Codex requires no transport, lease or event task;
Claude requires completed lifetime cleanup and a finished owner task. Unknown
owners, running sessions and cleanup failures remain pinned. Resolver and shared
lease admission run again for the exact session. Retry itself never closes or
kills the prior owner. Existing command receipts survive replacement, so an
unconfirmed submission is not replayed. The page exposes Retry connection after
attachment/transport failures, including an unavailable event during initial
replay, without automatically attaching. Closing the view remains inert.
This handles safely cleaned failures, not live-orphan takeover or ambiguous
launch recovery; full crash recovery and Windows gates remain open.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_codex.py tests/test_workspace_claude.py tests/test_workspace_app.py -q`: exit 0, 108 passed in 19.68s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_failed_attachment_retry_is_explicit_and_does_not_stop_uncertain_owner -q`: exit 0, 2 passed in 3.19s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_attachment_retry_requires_transport_and_lease_cleanup tests/test_workspace_claude.py::test_attachment_retry_requires_finished_successful_cleanup tests/test_workspace_host.py::test_explicit_reattach_only_replaces_verified_cleaned_owner tests/test_workspace_host.py::test_unconfirmed_receipt_survives_explicit_owner_replacement tests/test_workspace_app.py::test_failed_attachment_retry_is_explicit_and_does_not_stop_uncertain_owner -q`: exit 0, 7 passed in 4.10s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q`: final exit 0, 4 passed in 9.82s.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-mcp.py`: exit 0. Injected resume-lookup failure after real native initialization was cleaned up; browser retry alone resumed the exact persisted session. Native desktop/mobile OAuth/settings and owner-preservation checks also passed. The injected failure is not claimed as a real provider outage.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py`: exit 0, native exact-session controls, source desktop/mobile and cleanup passed; no user auth or inference.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_codex.py core/workspace_claude.py tests/test_workspace_host.py tests/test_workspace_app.py tests/test_workspace_codex.py tests/test_workspace_claude.py scripts/verify-workspace-codex-mcp.py`: exit 0.
Source-only; no installed app or user runtime changed.

POSIX crash containment (2026-09-09): leases now record the runtime's dedicated
process group when it is also a separate OS session, excluding the host's own
group. A dead CLI leader no longer proves its tools exited: surviving non-zombie
group members prevent reacquisition. Permission/inspection ambiguity fails
closed. The check does not terminate processes or launch recovery automatically.
A child that disappears before binding leaves the launching marker unresolved
instead of being recorded as safely dead. Normal shared host groups are not
tracked as owned groups. Native Codex and Claude wrapper/CLI group bindings are
asserted in their runtime proofs.
This closes an orphan-tool admission race, not the complete recovery UI. Older
records without group metadata, deliberately escaped groups, ambiguous launch
markers and Windows containment still need separate recovery work.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_lease.py -q`: initial exit 0, 8 passed in 1.20s; final exit 0, 9 passed in 5.95s with shared-group exclusion and inspection-failure coverage.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_lease.py tests/test_workspace_rpc.py tests/test_workspace_codex.py tests/test_workspace_claude.py -q`: exit 0, 84 passed in 3.95s.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-lease-recovery.py`: exit 0. Real disposable owner crashed via os._exit without releasing the lock; both a surviving runtime and an orphan tool after leader death refused another lease. Exact lease recovered after group exit. No provider/user session involved.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py`: exit 0, repeated with native group-binding assertion also exit 0. Exact ownership, local commands, desktop/mobile flows and cleanup passed without user credentials/inference.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-mcp.py`: exit 0, including actual Codex group-binding assertion, native MCP settings, desktop/mobile and owner preservation.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_lease.py tests/test_workspace_lease.py`: initial exit 1 for test import ordering, corrected.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_lease.py tests/test_workspace_lease.py scripts/verify-workspace-lease-recovery.py scripts/verify-workspace-claude-transport.py scripts/verify-workspace-codex-mcp.py`: final exit 0, all checks passed.
Source-only; no packaged app, deployed service or user runtime changed.

Codex MCP enablement (2026-09-09): the connections dialog now has an explicit
persistent user-setting checkbox backed by native config/value/write. The
server name must exist in current effective configuration; a uniquely identified
base user layer, absolute file path and expected version are required. Native
quoted key paths preserve dots/quotes in names. No arbitrary file/key reaches
the client API. Busy sessions, ambiguous layers and stale versions fail before
an unsafe write. Reload and a fresh effective read follow confirmed writes;
overrides are reported rather than pretending the requested state took effect.
Missing runtime status remains unknown, independently of configured enablement.
Only name/boolean/write-availability metadata is exposed, not config secrets.
Read-only dialog opening and closing a view do not mutate settings or owners.
Native evidence: official https://learn.chatgpt.com/docs/app-server and
https://learn.chatgpt.com/docs/extend/mcp, accessed 2026-09-09; installed
ConfigReadParams, ConfigValueWriteParams and ConfigWriteResponse schemas.
Isolated native probes confirmed config layer versions and quoted-name writes,
both exit 0. This implements base user settings, not a profile/project editor.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py::test_codex_mcp_setting_requires_exact_payload_and_reuses_receipt tests/test_workspace_pane.py::test_codex_mcp_setting_waits_for_effective_confirmation tests/test_workspace_pane.py::test_codex_mcp_inventory_shows_unknown_state_without_unsupported_mutations -q`: exit 0, 38 passed in 3.52s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py::test_codex_mcp_setting_waits_for_effective_confirmation tests/test_workspace_pane.py::test_codex_mcp_login_and_reload_are_explicit_and_wait_for_native_completion tests/test_workspace_pane.py::test_codex_mcp_inventory_shows_unknown_state_without_unsupported_mutations tests/test_workspace_pane.py::test_mcp_connections_explicit_controls_and_failure_state -q`: exit 0, 75 passed in 12.95s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py -q`: final exit 0, 39 passed in 0.35s after adding ambiguous-layer and changed-session rejection coverage.
- `node --test tests/workspace-connection.test.mjs`: exit 0, 20 passed including exact session and stable request identity after response loss.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-mcp.py`: exit 0, repeated after final runtime adjustment also exit 0. Native OAuth/reload and explicit disable/re-enable on desktop/mobile; unrelated config unchanged, same owner retained, no user auth/settings or inference used.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-codex-mcp.py`: exit 0, all checks passed.
Inspected apps/desktop/build/workspace-proof/codex-mcp-settings-mobile.png.
This source-only slice is not packaged, installed or released.

Claude history conversion (2026-09-09): replaced repeated scans of the current
turn's item list with a per-turn ID index. Tool/result updates retain original
order, and the index resets between turns. This removes quadratic lookup work;
it does not fix the SDK's whole-transcript parsing or bound history memory.
Controlled 8,000-pair conversion measured 1.9852s before and 0.2663s after;
a repeat under concurrent verification measured 0.5839s. These are conversion
measurements, not total session-opening latency or a timing guarantee.
Verification from the isolated feature worktree:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py -q`: exit 0, 30 passed in 8.13s. Regression covers reverse-arriving results, ordering, reused IDs across turns and unchanged input records.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-history.py`: exit 0; repeat preserved all 1,000/4,000/8,000 pairs in 0.2301/0.3376/0.5839s respectively.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py`: exit 0; native exact-session input/output, lease rejection, desktop/mobile command output, mentions, plugin/skill reload, forks, view-close ownership and cleanup passed. No user auth, settings or inference used.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_events.py tests/test_workspace_claude.py scripts/verify-workspace-claude-history.py`: exit 0, all checks passed.
The packaged binary was not rebuilt for this conversion-only change.

Packaged and real Electron verification through a171230 (2026-09-09): rebuilt
the Linux sidecar with current inline mentions, Claude permissions, Codex MCP
and skill-setting code. Native Codex and Claude browser flows pass against that
binary. Added an opt-in real Electron main/preload proof against the isolated
frozen backend: sandbox/contextIsolation remain true, nodeIntegration false,
native output and skill catalog render, and closing Electron preserves the
existing backend owner. Real clipboard copy of native output and multiline
paste were exercised on a private X display, without sending a model turn.
This uses the development Electron shell with the frozen backend, not an
installed AppImage or Windows build. Full CLI parity remains incomplete.
The first headless-Ozone attempt exited 1 with an Electron startup SIGSEGV.
An extracted Xvfb package supplies a private display without installing system
packages or touching the user's display/clipboard. The first X11 attempt then
exited 1 because the Python-distributed Playwright package exports no expect;
the verifier now uses locator waits and Node assertions, not relaxed app policy.
Verification commands:
- In apps/desktop: `env SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar`: exit 0, PyInstaller and bundled capability-refusal smoke passed. Optional dependency warnings remain; no Fleet run was launched.
- In apps/desktop: `npm test`: exit 0, 72 passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: exit 0, frozen desktop/mobile local-command, mentions, plugin/skill reload, exact ownership and cleanup passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: final exit 0, repeated with clipboard checks also exit 0. Frozen desktop/mobile native controls/history/forks, real Electron window/preload/clipboard, owner preservation and cleanup passed; no user auth or inference.
- `node --check scripts/verify-workspace-electron.cjs`: exit 0.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-history.py`: initial exit 1 for cleanup style; final exit 0 after using suppress.
- `git diff --check`: exit 0.
Proof tool preparation: in apps/desktop/build/proof-tools, `apt-get download xvfb`
and `dpkg-deb -x xvfb_2%3a21.1.12-1ubuntu1.6_amd64.deb xvfb` both exited 0.
Downloaded from the configured Ubuntu mirror; this is an ignored test artifact,
not a runtime dependency. Inspected screenshot:
apps/desktop/build/workspace-proof/electron-native-workspace.png. No production
flag, installed app, release version, deployment or user service was changed.

Codex skill settings (2026-09-09): the native catalog retains enabled state and
the command dialog exposes an explicit per-skill checkbox. Writes use only a
path rediscovered in this owner's project catalog, a strict boolean, and the
native skills/config/write API. Busy turns are refused before mutation. The
effective native result and reloaded catalog drive the display; toggles remain
at their last confirmed value while saving. Unknown paths cannot be injected.
Command receipt reuse avoids replaying uncertain writes. Drafts are unchanged,
and neither opening the dialog nor closing a view invokes a skill or starts work.
Codex now also has explicit catalog reload in the same dialog.
Source: https://learn.chatgpt.com/docs/app-server (accessed 2026-09-09) documents
skills/list with forceReload and skills/config/write by path. Installed native
SkillsConfigWriteParams/Response schemas establish the boolean effectiveEnabled
response. This does not add skill creation/deletion or Claude/Gemini management.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py::test_codex_skill_setting_requires_exact_payload_and_reuses_receipt -q`: exit 0, 32 passed in 0.52s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_codex_skill_toggle_waits_for_confirmation_and_preserves_draft -q`: initial exit 1, 2 failed because Playwright uncheck retried while the intentionally pending checkbox remained checked/disabled. Changed automation to click once and assert pending/confirmed states separately; final exit 0, 2 passed in 1.53s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_codex_skill_toggle_waits_for_confirmation_and_preserves_draft tests/test_workspace_pane.py::test_plugin_reload_is_explicit_refreshes_commands_and_reports_native_errors tests/test_workspace_pane.py::test_codex_skill_selection_persists_and_sends_exact_path_only_on_submit tests/test_workspace_pane.py::test_command_picker_preserves_draft_and_displays_native_output tests/test_workspace_pane.py::test_command_picker_reload_is_explicit_and_keeps_draft -q`: exit 0, 7 passed in 4.32s.
- `node --test tests/workspace-connection.test.mjs`: exit 0, 19 passed, including exact skill path and lost-response receipt reuse.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py`: exit 0, repeated after spacing correction also exit 0. Native desktop/mobile skill disable/re-enable through real configuration, draft preservation, subsequent native command output and same owner after view close. Isolated home only; no inference or credentials used.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-codex-history.py`: exit 0.
- `git diff --check`: exit 0.
Inspected apps/desktop/build/workspace-proof/codex-native-skills-mobile.png:
bounded dialog and correctly spaced dark checkbox. Not packaged or released.

Codex MCP OAuth and reload (2026-09-09): the connection dialog now invokes native
MCP login and configuration reload on its existing owner. Login is restricted to
an exact configured server advertising OAuth, never a caller-supplied URL or
thread ID. An outstanding/uncertain login cannot be started twice. Authorization
URLs are displayed as explicit links, not opened automatically; only native
completion changes the pending status to success/failure. Completed flows drop
the old URL. Refresh notifications arriving during a load are queued, not lost.
Busy turns reject mutations before dispatch. Stable command receipts preserve
retry identity; closing the view does not close the owner. Enable/disable and
arbitrary MCP configuration editing are still not implemented for Codex.
Source: https://learn.chatgpt.com/docs/app-server (accessed 2026-09-09), plus
installed native generated McpServerOauthLoginParams/Response and completion
schemas under apps/desktop/build/workspace-schema. The protocol supplies the
authorization URL and completion notification; configuration reload queues a
refresh from disk.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py::test_codex_mcp_actions_are_explicit_and_receipted -q`: initial exit 1, 1 failed/30 passed; added the missing host command allowlist entries.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py::test_codex_mcp_actions_are_explicit_and_receipted tests/test_workspace_pane.py::test_codex_mcp_login_and_reload_are_explicit_and_wait_for_native_completion tests/test_workspace_pane.py::test_codex_mcp_inventory_shows_unknown_state_without_unsupported_mutations tests/test_workspace_pane.py::test_mcp_connections_explicit_controls_and_failure_state -q`: exit 1, 3 failed/32 passed; corrected the replacement-pane fixture sequence and a misplaced refreshPending declaration.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py::test_codex_mcp_login_and_reload_are_explicit_and_wait_for_native_completion tests/test_workspace_pane.py::test_codex_mcp_inventory_shows_unknown_state_without_unsupported_mutations tests/test_workspace_pane.py::test_mcp_connections_explicit_controls_and_failure_state -q`: final exit 0, 67 passed in 10.31s.
- `node --test tests/workspace-connection.test.mjs`: exit 0, 18 passed including lost-login-response receipt reuse.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-mcp.py`: exit 0, repeated after visual correction also exit 0. Real native Codex, isolated configuration/home, local OAuth discovery/registration/token endpoints, explicit browser authorization, native success notification and reload; desktop/mobile, page/console/HTTP checks, same owner after view close. No inference or user credentials. Only the disposable loopback provider was authorized.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-codex-mcp.py`: initial exit 1 for loop callback binding in proof; corrected, final exit 0.
- `git diff --check`: exit 0.
Inspected apps/desktop/build/workspace-proof/codex-mcp-mobile.png: neon-black
dialog, explicit pink authorization link, pending state and bounded mobile
layout. Source-only, not packaged/installed/released; full delivery gates remain.

Claude suggested permission updates (2026-09-09): native canUseTool suggestions
now cross the client and owner into the approval UI. Nothing is preselected.
The UI shows the exact rule and labels its destination, including persistent
project/user settings. Applying selections is separate from Allow once/Deny.
The server accepts only unique indices into the pending request's snapshot;
stale requests, invalid indices, rule payload injection and deny-with-updates
are rejected. The TypeScript client returns original native wire objects only,
preserving absent optional fields rather than inventing null values. Closing
the owner retains the existing deny/cancel behavior and cleans suggestions.
Evidence: pinned sdk.d.ts PermissionUpdate/CanUseTool declarations, plus official
https://code.claude.com/docs/en/agent-sdk/typescript (accessed 2026-09-09), which
documents suggestions returned through updatedPermissions and localSettings
persistence. This is native suggested-rule application, not a full permissions
editor or evidence that a model-generated rule was persisted by the real CLI.
Commands:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_client.py tests/test_workspace_claude.py::test_permission_suggestions_are_explicit_pending_and_exact tests/test_workspace_claude.py::test_permissions_wait_for_user_reject_stale_and_cleanup_denies tests/test_workspace_pane.py::test_claude_permission_suggestions_require_explicit_selection -q`: exit 0, 10 passed in 3.73s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_pane.py::test_claude_permission_suggestions_require_explicit_selection -q`: exit 0, 39 passed in 4.57s, after adding Allow once with checked suggestions coverage at both viewport sizes.
- `node --test tests/workspace-claude-channel.test.mjs tests/workspace-claude-sdk.test.mjs`: exit 0, 15 passed, including permission wire roundtrip and cancellation.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_client.py tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_pane.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py`: exit 0, source native local-command/session/browser regression proof passed. This run uses no inference and does not exercise a model-generated permission request.
Not packaged, installed or released. Full CLI parity remains unfinished.

Inline project mentions (2026-09-09): Claude and Codex composers now complete
@path fragments using their existing attached-owner names-only searches.
Debounced results are bound to the draft and caret; stale results are discarded.
Arrow keys, Enter/Tab selection and Escape do not submit or interrupt a turn.
Paths containing spaces are quoted. Disposal cancels pending UI work, not the
native owner. This remains source-only, not installed or released.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q`: exit 0, 67 passed in 44.82s; recovery rerun exit 0, 67 passed in 65.08s. An earlier run failed because crypto.randomUUID was unavailable in the non-secure fixture; element IDs now use a module counter.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py scripts/verify-workspace-codex-history.py scripts/verify-workspace-frozen.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py`: exit 0; desktop/mobile native completion, real command output, exact history/reopen, same owner after closing the page, and isolated cleanup passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py`: exit 0; desktop/mobile native completion, local-command input/output, skills/plugins, exact forks and owner preservation passed without user credentials or inference.
Inspected source-mentions-mobile.png and codex-native-mentions-mobile.png under
apps/desktop/build/workspace-proof: results fit above the composer without
horizontal overflow. These checks do not establish model inference, Windows,
Gemini support or complete CLI parity.

Claude project file picker (2026-09-09): the same composer picker now works for
Claude. Its public SDK has no file-search control in the pinned declaration, so
names-only lookup runs locally against the already attached owner's cwd, without
calling the model. Git projects use tracked/untracked non-ignored names; inherited
GIT_* overrides are stripped. Non-Git projects use a bounded walk excluding
generated directories. Output, time, entry and result limits bound enumeration;
outside-root symlinks are excluded. No file contents are opened by lookup.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_files.py tests/test_workspace_pane.py::test_project_file_picker_preserves_draft_and_never_sends -q`: exit 0, 5 passed in 1.52s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py::test_file_search_requires_attached_owner_and_does_not_submit tests/test_workspace_host.py -q`: initial exit 1, 1 failed/32 passed. The existing which() mock replaced Git as well as Claude; narrowed that fixture to Claude only.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_files.py -q`: exit 0, 31 passed in 0.47s after fixture correction.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_files.py core/workspace_claude.py core/workspace_host.py tests/test_workspace_files.py tests/test_workspace_claude.py scripts/verify-workspace-frozen.py`: exit 0.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py`: exit 0; actual Claude/source-server desktop/mobile picker, same owner, draft insertion, native local-command input/output, plugin/skill reload and fork checks all passed.
- `git diff --check`: exit 0.
Source-only since the preceding packaged build. File content interpretation,
inline @ autocomplete, Gemini parity and full delivery are not claimed here.

Packaged verification through d3ce099 (2026-09-09): rebuilt the Linux sidecar
with the recent history, plugin reload, process-group cleanup and file-picker
changes. Both providers' isolated native browser checks pass against this binary.
The first frozen Codex run exited 1 because the verifier used string evaluation
blocked by production CSP. Replaced it with a Playwright locator assertion;
the application policy was not changed. Claude's packaged proof now explicitly
reloads plugins and waits for skill reload completion before screenshots, avoiding
a stale-list assertion introduced when plugin reload also refreshes commands.
Commands and observed results:
- In apps/desktop: `env SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar`: exit 0, PyInstaller build and bundled capability-refusal smoke passed. Optional dependency warnings remain; no Fleet run was started.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: initial exit 1 (verifier CSP issue), final exit 0. Native desktop/mobile file search, output, history, exact forks and cleanup passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: exit 0, repeated after strengthening reload assertion also exit 0. Native plugin/skill controls, exact session input/output, fork, Electron Node worker and packaged desktop/mobile browser checks passed.
- In apps/desktop: `npm test`: exit 0, 72 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-history.py scripts/verify-workspace-frozen.py`: exit 0.
- `git diff --check`: exit 0.
Screenshot inspected: apps/desktop/build/workspace-proof/frozen-skills-mobile.png
shows the completed catalog, not Reloading. These remain Linux isolated local-
command proofs, not model inference, Windows verification, full CLI parity or
an installed Electron-window acceptance run. Nothing was installed or released.

Codex file picker (2026-09-09): the composer now has an explicit project-file
search dialog using the existing native app-server's fuzzyFileSearch. The host
supplies only the owned session cwd as roots; callers cannot choose another root.
Returned paths are validated against that root, with traversal, absolute-path
and outside-project symlink protections. Search reads names, not file contents,
and starts no turn. Selecting a result inserts an @path into the draft at its
selection, quoting whitespace paths, without sending it. Arrow keys/Enter and
mobile layout are covered. This adds Codex path selection, not Claude/Gemini
parity or evidence that a model has read the referenced file.
Official sources accessed 2026-09-09:
https://learn.chatgpt.com/docs/prompting documents explicit CLI @path mentions;
https://learn.chatgpt.com/docs/app-server documents the rich-client transport.
Installed native fuzzyFileSearch response was additionally probed directly in
an isolated home and returned the requested fixture under the supplied root.
Verification (exit 0):
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_pane.py::test_project_file_picker_preserves_draft_and_never_sends -q`: 30 passed in 1.97s, including 390px/1600px picker keyboard/draft checks and root rejection.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_pane.py scripts/verify-workspace-codex-history.py`: all checks passed.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py`: native desktop/mobile file search and draft insertion before existing shell/history/reopen/cleanup checks; all passed. Mobile screenshot inspected at apps/desktop/build/workspace-proof/codex-native-mobile.png.
Not rebuilt into the packaged sidecar or released.

POSIX owner shutdown (2026-09-09): a marked isolated subprocess proof reproduced
an orphaned worker: parent exit 0, child_survived_owner_close true. The proof
terminated its own child and exited 0; no provider was launched. WorkspaceRpc
now starts an owned POSIX session/process group. Explicit owner shutdown signals
that group on timeout and removes remaining group members after leader exit.
This also handles a child retaining stdout after the leader exits. Browser
disposal still never closes the owner. Processes deliberately escaping the group
are not covered; Windows tree containment and safe crash recovery remain open.
Do not add automatic resume based solely on the parent process disappearing.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_rpc.py -q`: exit 0, 7 passed in 2.32s; real subprocess child cleanup with inherited and detached stdout, distinct host/child process groups.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_claude_transport.py tests/test_workspace_host.py -q`: exit 0, 67 passed in 5.90s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_rpc.py tests/test_workspace_rpc.py`: initial exit 1, import order and suppress style; corrected, final exit 0.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py`: exit 0, native commands/history/reopen and desktop/mobile output, page-close preservation and owner cleanup.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: exit 0, exact native session input/output, plugin/skill controls, fork, lease and process cleanup.
- `git diff --check`: exit 0.
Source only; not rebuilt, installed, released, or a full recovery implementation.

Claude plugin reload (2026-09-09): Commands and skills now includes an explicit
Reload plugins control backed by the pinned SDK's public Query.reloadPlugins().
It refreshes the command catalog, retains native plugin/agent/MCP metadata in the
event journal, and displays the native plugin count and error count. It neither
installs plugins nor starts a different coding session. Busy rejection is
retryable before native execution; uncertain delivery retains its stable receipt.
The adapter validates the result before replacing its cached command catalog.
The installed SDK 0.3.266 sdk.d.ts Query and SDKControlReloadPluginsResponse are
the contract source; actual installed CLI execution confirms the control works.
Verification (all exit 0):
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_client.py tests/test_workspace_host.py tests/test_workspace_pane.py::test_plugin_reload_is_explicit_refreshes_commands_and_reports_native_errors -q`: 38 passed in 10.05s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_pane.py::test_plugin_reload_is_explicit_refreshes_commands_and_reports_native_errors -q`: 28 passed in 1.45s.
- `node --test tests/workspace-connection.test.mjs`: 17 passed, including stable reload receipt and no automatic action.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_plugin_reload_is_explicit_refreshes_commands_and_reports_native_errors -q`: 2 passed in 1.77s at 390px and 1600px with no dialog overflow.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_command_discovery_is_provider_scoped_and_never_submits -q`: 1 passed in 0.61s after adding busy/retry coverage.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: native plugin reload through Python/SDK, same session/PID, zero plugin errors; isolated no-auth local-command, skills, fork, input/output, lease and cleanup proofs also passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_client.py core/workspace_host.py tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-claude-transport.py`: all checks passed.
- `git diff --check`: clean.
Plugin installation/removal, trust management, arbitrary plugin behavior, and
full provider parity remain outside this completed reload slice. Not released.

Owner reconnect history (2026-09-09): closing a Codex owner now clears native
history cursors and process-local fork notification IDs. Previously reopening
the same adapter rejected an already-consumed but newly valid cursor as a loop.
The regression was reproduced before correction: the paginated-resume test
exited 1 with `Codex history pagination did not advance`. Verification after fix:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py -q`: exit 0, 27 passed in 0.11s.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py tests/test_workspace_codex.py scripts/verify-workspace-codex-history.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py`: exit 0; now additionally closes/reopens the same adapter with the actual CLI and reads the oldest native page again before checking input/output and desktop/mobile views.
This is explicit owner shutdown/reopen, not automatic process-crash recovery.

Antigravity revalidation (2026-09-09): an isolated-HOME subprocess probe sent
only `control_request` and `control_response` input events to installed `agy`
with `--input-format stream-json --output-format stream-json`. Both native
processes exited 2 with an ERROR result saying that event is not supported yet;
both reported zero turns and zero tokens. The outer marked Python proof exited
0 and removed its temporary home. No user credentials or sessions were used.
`SERENA_EVIDENCE_KIND=live agy help remote-control` exited 0 and exposed only
start/status/stop. No daemon was registered or started. Official sources checked
again: https://antigravity.google/docs/cli/headless/ (unsupported control messages),
https://antigravity.google/docs/sdk/overview/ (API key/Vertex SDK setup), and
https://antigravity.google/docs/remote-control/ (hosted dashboard and OS service).
These sources do not establish a supported local subscription-compatible custom
UI control path. Gemini full parity is still unresolved, not waived.

Pending command rendering (2026-09-09): native commandExecution items with null
output show their command/status without dumping raw event JSON. Streamed stdout
and the final exit code retain the expanded tool entry. Unknown event types remain
inspectable. Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q`: exit 0, 60 passed in 37.07s.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py`: exit 0; 51 native print-only turns, exact history pagination, once-only shell delivery, desktop/mobile actual stdout, owner survival after page close, isolated cleanup.
- `git diff --check`: exit 0.
This source rendering fix is not yet rebuilt into the frozen sidecar or released.

Frozen Codex admission and fork ownership (2026-09-09): the packaged proof now
seeds an isolated real catalog through normal registration and launches the
built sidecar with normal index/admission logic. It initially failed with 404:
startup indexing pruned app-server-origin sessions because their origin is
extension-like and only CLI or explicitly Serena-owned work survives scanning.
Validated Codex registration now calls the existing set_resident_work marker
under the index lock before upsert. It does not admit arbitrary extension chats,
rewrite transcripts or create linked groups. Invalid targets still fail before
metadata/catalog writes. The binary was rebuilt with this correction.

The frozen desktop/mobile browser proof now passes exact-session attach, native
command execution, precise stdout and completed state, older history loading,
and page-close owner preservation. It also creates a native fork through the
packaged UI, verifies persisted scanner ownership, and opens the new view without
launching a second owner. Its catalog and HOME are isolated; no user auth/session
or installed host is touched. Screenshot inspection caught an overly broad
output assertion (pending JSON also contained the command); tightened to exact
stdout plus completed state, rerun successfully, then inspected mobile again.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py -q
# exit 0: 12 passed in 0.08s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_catalog.py tests/test_workspace_catalog.py scripts/verify-workspace-codex-history.py
# exit 0: All checks passed!
# cwd apps/desktop
env SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar
# exit 0: rebuilt with ownership fix; existing capability-refusal smoke passed
# cwd repo
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: final strict-output native frozen desktop/mobile proof, normal admission,
# UI fork/catalog/ownership, history, command receipts and child cleanup.
```

Generated screenshots: apps/desktop/build/workspace-proof/codex-frozen-desktop.png
and codex-frozen-mobile.png. The proof accepts an optional frozen binary; without
one it retains the source HTTP harness. This verifies the packaged Linux backend
in Chromium, not the installed Electron shell or Windows. Remaining provider
parity and full delivery gates are unchanged.

Rebuilt desktop sidecar regression (2026-09-09, source 67bd697): rebuilt the
actual Linux sidecar after Codex fork/history/shell additions. PyInstaller's
PYZ table contains core.codex_history, core.workspace_catalog and
core.workspace_codex from this feature worktree. This proves inclusion, not
native execution of Codex through the frozen binary. The real frozen Claude
browser proof passed at desktop/mobile sizes, including skill refresh, native
local-command output, page reload and view-close owner preservation. The mobile
screenshot was visually inspected; controls/text fit without horizontal overflow.

```sh
# cwd apps/desktop
env SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar
# exit 0: sidecar built, peer MCP startup/contract/capability-refusal smoke passed
npm test
# exit 0: 72 passed
# cwd repo
SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: native driver/transport/owner/fork checks, actual frozen HTTP/native
# desktop/mobile browser input/output/skill refresh, no browser errors or overflow;
# all isolated children reaped, no user sessions/auth/settings used.
```

Existing optional build warnings included missing tensorboard, pycparser tables,
AMD HIP and Windows ctypes libraries; build and native proof still passed. The
build's peer smoke is a capability refusal check, not a Fleet run. Generated
binary/screenshots remain under apps/desktop/build; no release, installation,
default activation, user terminal launch or host restart was performed.

Full completion remains unproven: Codex frozen interaction, installed Electron
and Windows QA, full new/clear lifecycle, provider feature parity (especially
Gemini), durable process-restart recovery and integration with existing runtime
ownership/linked-work flows remain delivery work, not waived requirements.

Native Codex HTTP/browser proof (2026-09-09): the history verifier now mounts
the real workspace Flask blueprint/page/static assets around its isolated native
session and drives them through Playwright. Desktop 1440x900 and mobile 390x900
explicitly click Resume, confirm/run print-only commands, expand command output,
and observe native stdout/exit status. Desktop also loads the oldest native page
through the real history button. Both pages share the same native owner PID;
page close leaves it alive, and proof teardown closes it explicitly. Initial page
load is asserted not to create an owner. No model credentials or inference used.

The resolver/descriptor admit only the proof's known isolated session, rather
than testing production index admission. The rest is the real HTTP command,
journal, adapter, native executable, renderer and event polling path. No console,
page or HTTP errors were observed. Both screenshots were visually inspected:
output/exit status readable, controls within viewport, no horizontal overflow.
Artifacts: `apps/desktop/build/workspace-proof/codex-native-desktop.png` and
`apps/desktop/build/workspace-proof/codex-native-mobile.png` (generated, not tracked).

```sh
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py
# exit 0: native history/receipt checks plus real desktop/mobile HTTP attach,
# shell output rendering, oldest-page load and unchanged owner after page close.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py tests/test_workspace_pane.py::test_shell_dialog_requires_explicit_confirmation_and_keeps_chat_draft tests/test_workspace_pane.py::test_native_older_history_is_explicit_and_preserves_scroll -q
# exit 0: 5 passed in 6.31s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-history.py
# exit 0: All checks passed!
```

First browser proof exited 1 because it waited for output inside a collapsed
command detail; it now explicitly expands that real control. Initial lint
flagged unbound loop variables in error callbacks; bound each page's error list
and reran successfully. This does not prove installed Electron, Windows, model
generation, approval interactions or full cross-provider parity. Those delivery
gates remain open.

Native Codex shell action (2026-09-09): the pane now offers an explicit Run shell
command dialog. Its outside-sandbox confirmation is required by the backend,
not merely the UI. Commands use `thread/shellCommand` on the exact owned session,
preserve native timeout defaults and stream through existing real command-output
rendering. They are not submitted as model prompts. Idle and running sessions
are supported; unavailable/uncertain or approval-blocked sessions refuse input.
Unconfirmed delivery becomes uncertain and cannot be silently repeated. Stable
host receipts ensure a repeated confirmed click/request does not execute twice.
The original chat composer draft is unchanged; creating/closing the dialog never
runs a command. This action is currently Codex only.

Official semantics checked at https://developers.openai.com/codex/app-server/
(accessed 2026-09-09), thread/shellCommand: explicit commands run outside the
Codex sandbox, as standalone idle turns or auxiliary actions in an active turn.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py -q
# exit 0: 27 passed in 0.14s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_shell_dialog_requires_explicit_confirmation_and_keeps_chat_draft -q
# exit 0: 2 passed in 1.67s, 390px and 1600px layouts; no modal overflow
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_pane.py scripts/verify-workspace-codex-history.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py
# exit 0: actual explicit host/owner command produced native output and exit 0,
# exactly one completed turn after replaying its receipt; pagination still passed.
```

The first browser check exited 1 because the test locator matched both Codex
composers; scoped to the intended pane and rerun. Native proof is an isolated
print-only command with no model/auth use. It verifies idle command execution,
not active-turn auxiliary output or installed browser interaction; those remain
integration checks, along with Claude parity and full delivery gates.

Combined regression and history retry (2026-09-09): all workspace Python/browser
tests plus Codex history readers passed together after fork/pagination integration.
Code review then found a confirmed failed history read remained pinned to its
failed request ID. Read-only load_earlier errors now permit a fresh receipt;
the owner still validates the exact cursor, and confirmed successes are replayed
without rereading. Uncertain writes/forks/sends retain their existing policy.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace*.py tests/test_codex_history.py tests/test_codex_records.py -q
# exit 0: 270 passed in 56.16s, before the narrow history-retry correction
node --test tests/workspace-markdown.test.mjs tests/workspace-claude-channel.test.mjs tests/workspace-claude-sdk.test.mjs tests/workspace-connection.test.mjs tests/workspace-events.test.mjs
# exit 0: 40 passed, before adding the retry regression
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_failed_history_read_can_retry_without_mutating_work tests/test_workspace_host.py::test_older_history_uses_exact_owner_cursor_and_receipt -q
# exit 0: 2 passed in 0.34s, final correction
node --test tests/workspace-connection.test.mjs
# exit 0: 16 passed, including confirmed read failure/new receipt/same cursor
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py tests/test_workspace_host.py scripts/verify-workspace-codex-history.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py
# exit 0: native 51 print-only turns, bounded resume, real host command/journal
# path with retryable stale-cursor refusal followed by valid native older page.
```

The proof binds its already-admitted isolated native owner directly to the host
on the same event loop. It does not prove HTTP admission or installed browser
integration. Full delivery/parity and the remaining provider gates stay open.

Native Codex history pagination (2026-09-09): paginated sessions now request
the newest 50 turns with `itemsView:full`, reverse them into chronological order,
and retain an opaque older-page cursor. Current/in-progress resume turns take
precedence over corresponding historical snapshots. Explicit Load earlier reads
the exact owned session/cursor, persists a historyPage event, and prepends older
turns without overwriting live turns or changing working state. Cursor cycles,
malformed pages and stale requests reject without advancing. Stable host receipts
prevent repeating the same confirmed page action.

The renderer preserves its visible anchor when a page arrives after command
acknowledgement. Automatic top-scroll expansion applies only to already-loaded
messages, not native page fetches. Normal DOM windowing remains in place.
Initial investigation suspected missing resume turns; the actual native fixture
returned all 51 on resume. Therefore this is verified bounded pane history and
explicit paging, not evidence that the installed CLI was dropping those turns.
The initial native resume response and retained journal can still be large;
this change does not claim a fully bounded backend-memory implementation.

Official paging contract: https://developers.openai.com/codex/app-server/
(accessed 2026-09-09), List thread turns, full items and descending cursor order.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_older_history_uses_exact_owner_cursor_and_receipt tests/test_workspace_codex.py tests/test_workspace_pane.py::test_native_older_history_is_explicit_and_preserves_scroll -q
# exit 0: 27 passed in 0.96s
node --test tests/workspace-events.test.mjs tests/workspace-connection.test.mjs
# exit 0: 24 passed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-codex-history.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py
# exit 0: 51 native print-only shell turns, fresh owner resume with 50 recent
# full turns, explicit final older turn; project untouched, owners closed.
```

Earlier proof exited 1 because all resume turns were inadvertently merged back
into the bounded page; corrected to keep matching/current turns only. The first
browser run exited 1 because the old scroll handler fetched a native page without
a click; corrected and rerun. No credentials or model calls are used by the
native proof, only isolated explicit `printf` commands. This is not installed
Electron QA or completion of the full provider-parity contract.

Codex fork controls (2026-09-09): idle exact-owner `thread/fork` now feeds the
same explicit create/register/open workflow as Claude. The owner validates the
new UUID/project, retains its source identity/PID and routes confirmed fork
lifecycle notifications away from source output. Unrecognized session events
and cross-session interactive requests still fail closed. Busy turns refuse
creation; no model turn is submitted. Fork/checkpoint/registration receipts now
support both providers, and the pane exposes its existing dialog for Codex.

Catalog registration resolves one exact native transcript in its configured
store, validates the header identity/project and uses the normal index upsert.
The Codex scanner also resolves inherited messages, keeping titles/counts through
later scans. Missing ancestry makes scanner metadata unavailable, not a partial
tail advertised as a complete chat. No global rescan, linked-chat creation or
automatic new owner launch is added.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_workspace_codex.py tests/test_workspace_host.py::test_fork_receipt_keeps_identity_even_if_indexing_fails tests/test_workspace_host.py::test_interrupted_fork_receipt_recovers_only_unique_checkpoint tests/test_workspace_journal.py -q
# exit 0: 46 passed in 3.34s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_fork_dialog_never_creates_or_opens_automatically tests/test_workspace_catalog.py tests/test_codex_records.py tests/test_codex_history.py -q
# exit 0: 32 passed in 5.76s (before six added Codex catalog rejection cases)
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_catalog.py core/workspace_host.py core/workspace_journal.py tests/test_workspace_codex.py tests/test_workspace_catalog.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-codex-lifecycle.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-lifecycle.py
# exit 0: native owner fork preserved source identity/PID/state; fork lifecycle
# not published as source output; isolated real catalog registered identical
# fork twice as one row with inherited fixture title/count; children reaped.
```

Ruff including core/codex_scanner.py exits 1 on F401/UP035/SIM108. Running
`git show HEAD:core/codex_scanner.py | /home/raghav/Documents/Projects/serena/.venv/bin/ruff check --stdin-filename core/codex_scanner.py -`
against the pre-change HEAD also exited 1 with exactly those three findings.
Unrelated baseline lint was left untouched.

Browser coverage is controlled-callback mobile UI, native coverage is the real
adapter and SQLite registration in isolated storage, not a full native browser
roundtrip or installed desktop run. Busy forks, compressed ancestors, full
new/clear lifecycle, restart recovery and remaining provider parity gates are
still open. This does not activate or release the replacement.

Codex shared-history reader (2026-09-09): `read_messages` now reconstructs native
fork prefixes through `codex_history.history_segments`. Ancestors are resolved
only inside the same sessions/archived_sessions store, with canonical UUIDs,
exact identity, byte-aligned prefix and monotonic ordinal validation. Missing,
ambiguous, truncated, escaping or cyclic references raise HistoryUnavailable
rather than silently presenting the fork's local tail as its full history.
Nested ancestry is capped at 64. Legacy event/item preference is applied per
file segment, so modern child messages are not dropped beneath legacy parents.
Ordinary non-fork files retain streaming reads. Fork reconstruction currently
materializes bounded prefixes; history memory/retention remains unfinished.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_codex_history.py tests/test_codex_records.py -q
# exit 0: 23 passed in 0.12s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/codex_history.py core/codex_records.py tests/test_codex_history.py scripts/verify-workspace-codex-lifecycle.py
# exit 0: All checks passed! (initial import-order failure corrected)
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-lifecycle.py
# exit 0: real native fork and fresh-process resume; Serena read_messages
# returns exactly the inherited fixture message before and after resumption.
```

The native fixture is ungrouped injected input, not a generated completed turn.
Codex fork control, catalog metadata/title handling, browser presentation of
unavailable ancestry, compressed ancestors and whole-session lifecycle remain
open. This is not an installed UI change or a completed parity claim.

Codex lifecycle investigation (2026-09-09): installed native 0.153.4 supports
`thread/fork`, but its paginated history stores a `history_base` parent thread,
exclusive ordinal and byte offset in session metadata. A fork's local response
records alone are NOT its full history. Current `core/codex*` readers contain no
handling for this reference. Do not enable Codex fork/catalog UI by reusing the
Claude file-copy assumptions; implement bounded, validated ancestor traversal
and coverage first, including missing ancestors, cycles and subsequent parent
messages. Native `thread/read`/`thread/items/list` returned empty lists for the
injected ungrouped fixture, so this proof does not claim rendered turn history.

Official protocol checked at https://developers.openai.com/codex/app-server/
(accessed 2026-09-09): `thread/fork` copies stored context and `thread/inject_items`
persists items without starting a turn. The executable's actual persisted format
is the evidence for the reference semantics above. The proof seeds only isolated
fixture input, checks the exact referenced prefix and unchanged source, shuts
down the first process, then resumes the exact fork in a fresh process. It uses
no credentials, no `turn/start` and a loopback-only OpenAI base URL. It does not
prove model generation or a complete Codex fork user interface.

```sh
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-lifecycle.py
# exit 0: bounded fork history, exact fresh-process resume, source/project
# unchanged, native children reaped, no credentials or model turns.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_rpc.py -q
# exit 0: 22 passed in 0.46s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-lifecycle.py
# exit 0: All checks passed!
```

Earlier proof attempts exited 1: missing isolated CODEX_HOME directory (fixed),
then incorrect assumptions that injected items appear as turns and forks contain
copied local records. The final proof validates actual native shared-history
metadata instead. Initial unauthenticated native prewarming logged HTTP 401;
the final run directs that endpoint to loopback and never borrows user auth.

Interrupted fork confirmation (2026-09-09): fork events now bind the created
identity to the exact creation request ID. A retry with an unfinished command
receipt can recover a unique persisted checkpoint, register the existing fork
and finish the receipt without invoking the native creator. Missing checkpoints
remain uncertain; ambiguous checkpoints reject recovery rather than guessing.
Older events lacking the request binding are not inferred from timestamps.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_journal.py tests/test_workspace_host.py::test_interrupted_fork_receipt_recovers_only_unique_checkpoint tests/test_workspace_host.py::test_fork_receipt_keeps_identity_even_if_indexing_fails -q
# exit 0: 9 passed in 2.86s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_journal.py core/workspace_host.py tests/test_workspace_journal.py tests/test_workspace_host.py scripts/verify-workspace-claude-transport.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron
# exit 0: actual native fork checkpoint and unfinished receipt reopened;
# registration/confirmation restored with unchanged native transcript set and
# owner PID. Existing native resume, skill reload and cleanup checks passed.
```

The proof reopens durable state around a real already-admitted owner; it does not
kill the app or demonstrate full host restart. The smaller window before any
fork checkpoint is durable remains a deliberate fail-closed ambiguity boundary.

Fork catalog recovery (2026-09-09): completed fork identity/creation receipt is
saved in session-scoped browser storage before clearing the pending request.
After reload, the dialog can retry registration of that exact recorded fork.
The backend resolves only this source session's confirmed creation receipt;
recovery cannot invoke native fork or start its owner. Registration is idempotent
and retryable after storage failure, including while the source is working.
Creating another fork explicitly clears the saved completed-fork display.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_fork_receipt_keeps_identity_even_if_indexing_fails tests/test_workspace_pane.py::test_saved_fork_recovery_never_recreates_session tests/test_workspace_pane.py::test_fork_dialog_never_creates_or_opens_automatically -q
# exit 0: 5 passed in 3.64s
node --test tests/workspace-connection.test.mjs
# exit 0: 15 passed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-frozen.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py
# exit 0: temporarily replace only the isolated catalog path with a directory,
# force real SQLite-open failure after native fork creation, restore catalog,
# reload browser, recover identical fork and open its view without starting it.
# Desktop/mobile native input/output, skill reload, history, ownership and
# post-navigation console/HTTP/page-error checks also passed; children reaped.
```

This covers confirmed creation followed by catalog failure. A host crash between
native fork creation and durable confirmation is still an ambiguity boundary:
do not recreate the fork automatically. Full host-crash recovery and remaining
provider/session lifecycle gates are not claimed complete.

Native fork UI (2026-09-09): explicit create/open actions now use the native
backend. Neither opening the dialog nor opening a created fork view starts its
owner. The embedded pane sends an origin/source/session-checked navigation
request to the main app; standalone panes navigate to the exact fork route.
Catalog failures retain the ID and hide Open. Creation remains single-flight
even when the modal is closed mid-request. Lost responses reuse their request
across reload; an explicitly refused busy preflight allows a fresh later intent.
Drafts are unchanged, and creating another fork requires a separate action.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_fork_dialog_never_creates_or_opens_automatically tests/test_workspace_pane.py::test_closing_pending_fork_cannot_start_second_creation tests/test_workspace_host.py::test_fork_receipt_keeps_identity_even_if_indexing_fails tests/test_workspace_app.py -q
# exit 0: 7 passed in 6.91s
node --test tests/workspace-connection.test.mjs
# exit 0: 14 passed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py tests/test_workspace_host.py tests/test_workspace_pane.py tests/test_workspace_app.py scripts/verify-workspace-frozen.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py
# exit 0: actual source HTTP app and native Claude; desktop/mobile browser
# created forks, registered them and opened exact views with no fork owner
# launched. Original child process set unchanged. Existing skill reload,
# input/output/history, exact fork resume and cleanup checks also passed.
```

`verify-workspace-frozen.py` now also accepts the source sidecar .py entrypoint;
its source/frozen labels distinguish the artifacts. Screenshots are under
`apps/desktop/build/workspace-proof/source-fork-{desktop,mobile}.png`. Mobile
visual inspection found readable identity/actions with no overlap. The embedded
main-app routing is browser-tested with controlled callbacks; the native browser
proof uses the standalone page. Busy-session forks, catalog retry/recovery,
Codex/new-session lifecycle and packaged/installed UI verification remain open.

Native fork backend (2026-09-09): explicit `fork_session` controls now use the
owned Claude session/project only, require idle state, and return a dormant
fork rather than starting a second owner. The host records the fork identity
before registering its exact native transcript through the existing index
upsert. Registration never performs a global scan/prune. Missing, ambiguous,
outside-store or mismatched identity/project files are rejected before writing.
Catalog failure returns the created ID with `indexed:false`; the stable command
receipt prevents the same request from forking again. No sibling link is created.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_host.py::test_fork_receipt_keeps_identity_even_if_indexing_fails tests/test_workspace_app.py -q
# exit 0: 30 passed in 5.48s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py -q
# exit 0: 5 passed in 0.05s
node --test tests/workspace-claude-sdk.test.mjs
# exit 0: 8 passed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_catalog.py core/workspace_claude.py core/workspace_claude_client.py core/workspace_host.py ui/workspace_app.py tests/test_workspace_catalog.py tests/test_workspace_claude.py tests/test_workspace_host.py scripts/verify-workspace-claude-transport.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron
# exit 0: idle native owner forked through control, exact fork registered in
# real isolated SQLite catalog, original native PID/session unchanged;
# additional persisted-fork resume/source-preservation proof passed.
```

UI creation/navigation, indexing-failure recovery, busy-session snapshotting,
Codex lifecycle and cross-provider context forks remain open. The current
frozen build predates this backend slice. Do not treat this as full fork delivery.

Native fork feasibility (2026-09-09): the pinned SDK's exported `forkSession`
copies a persisted conversation without submitting a turn. Its declarations
specify fresh message UUIDs and no copied file-checkpoint history. Official
session semantics checked at https://code.claude.com/docs/en/agent-sdk/sessions
(accessed 2026-09-09) distinguish exact resume from continuing the latest session.
The isolated proof now creates a fork of its seeded native history, verifies
new session/message identities and identical message content, explicitly resumes
that fork, submits a zero-inference local command, and verifies source history
remains unchanged afterwards. All native owners are sequential and reaped.

```sh
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron
# exit 0: native fork identity/history/resume/local-output/source-preservation
# checks passed, along with existing exact-owner and skill-reload checks.
node --test tests/workspace-claude-sdk.test.mjs
# exit 0: 7 passed
```

This establishes a native lifecycle primitive, not a finished fork control.
The application still needs guarded owner routing, durable fork receipts,
index/catalog registration and explicit navigation before that control can be
enabled. Do not use the existing context-fork launcher as a substitute: it
launches the legacy terminal path. Codex lifecycle and cross-provider context
forks remain separate open requirements.

Combined workspace regression (2026-09-09), after skill reload and command
presentation changes:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace*.py -q
# exit 0: 208 passed in 52.56s
node --test tests/workspace-markdown.test.mjs tests/workspace-claude-channel.test.mjs tests/workspace-claude-sdk.test.mjs tests/workspace-connection.test.mjs tests/workspace-events.test.mjs
# exit 0: 36 passed, 0 failed, 0 skipped
# From apps/desktop:
SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar
# exit 0: rebuilt onedir backend and startup/capability-refusal smoke passed
# From repository root:
SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: actual rebuilt backend, desktop/mobile native command roundtrips,
# user-skill add/remove reload from browser, no duplicate command result,
# no raw command envelope in visible history, no page/console/HTTP errors,
# no horizontal overflow, close-view owner preservation and process cleanup.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-frozen.py
# exit 0: All checks passed!
```

These cover the workspace subsystem, including browser and transport tests;
they are not proof of full provider feature parity or a release certification.
The refreshed screenshots under `apps/desktop/build/workspace-proof/` now include
`frozen-skills-desktop.png` and `frozen-skills-mobile.png`. Visual inspection of
the real mobile command picker and desktop conversation found readable controls
and no overlap. This supersedes the older artifact limitations below for the
command-history, result-deduplication and skill-reload slices only. No installer,
Windows or default-UI migration was performed.

Native skill reload (2026-09-09): Claude's command picker has an explicit refresh
control backed by public `reloadSkills()` then `supportedCommands()`. It updates
the cached catalog, removes stale initial skill names, requires an idle attached
owner, preserves drafts and uses existing idempotent command receipts. It does
not restart a session or run a skill. Provider/payload checks reject other routes.
The locked SDK 0.3.266 declarations establish the control contract; official
filesystem discovery and command semantics were checked at
https://code.claude.com/docs/en/agent-sdk/skills (accessed 2026-09-09).

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_client.py tests/test_workspace_claude.py tests/test_workspace_host.py::test_command_discovery_is_provider_scoped_and_never_submits tests/test_workspace_pane.py::test_command_picker_reload_is_explicit_and_keeps_draft -q
# exit 0: 30 passed in 2.13s, including browser interaction
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_client.py core/workspace_host.py tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-claude-transport.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron
# exit 0: add and remove a skill in the isolated native user's skill directory
# after attach; refreshed catalog reflects both changes with unchanged native PID.
# Existing exact-session input/output, history and cleanup checks also passed.
```

First live attempt exited 1: its project fixture under temporary HOME was not
discovered. Moving the fixture to the isolated CLAUDE_CONFIG_DIR skills location
passed. This proves user-skill reload; it does not establish arbitrary project
directory discovery or skill execution. No inference or user settings were used.

Persisted command history (2026-09-09): exact native slash-command envelopes
now render as readable command text, retaining the complete source record in
`providerOriginal`. A bounded structural parser rejects partial/malformed markup,
DTD/instructions, unknown structure, nested content and unmatched command labels;
those records remain verbatim rather than losing text. Actual native seeded
history was verified through the source owner. The earlier frozen build has not
yet been rebuilt with this change.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_wire.py tests/test_workspace_claude.py -q
# exit 0: 42 passed in 1.03s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_events.py tests/test_workspace_claude_wire.py scripts/verify-workspace-claude-transport.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron
# exit 0: persisted native slash commands normalized with originals retained;
# output not duplicated; exact-session ownership and cleanup checks passed.
```

Native command presentation repair (2026-09-09): suppress the second visible
result only when it exactly matches the latest root assistant message in that
same turn. Child output, previous-turn matches, different results and errors
remain visible. Raw provider records and completion evidence are unchanged.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_wire.py tests/test_workspace_claude.py -q
# exit 0: 34 passed in 0.54s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_events.py tests/test_workspace_claude_wire.py scripts/verify-workspace-claude-transport.py
# exit 0: All checks passed!
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron
# exit 0: actual native result now has exactly one visible item; source owner,
# native resume, process ownership and Electron Node-mode checks passed.
```

This correction is in source, not the previous frozen screenshots/build.
Raw command markup in replayed history remains open.

Frozen native browser roundtrip (2026-09-09): `verify-workspace-frozen.py`
starts the built sidecar with isolated HOME, index, leases and an inaccessible
private D-Bus address. It resumes only the seeded test session, sends a native
zero-inference local command over HTTP, then sends another through the actual
browser composer at 1440x1000 and 390x844. No user credentials or live chats are
used. It checks native completion, no page/console/HTTP errors, no horizontal
overflow, and owner survival after view closure. Browser tooling is imported
only into the proof process, not the frozen server.

```sh
SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: direct driver, JSONL worker, Python owner, Electron Node-mode worker,
# frozen HTTP, desktop browser and mobile browser native roundtrips passed;
# isolated processes reaped. No inference or user sessions used.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q
# exit 0: 2 passed in 7.07s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-frozen.py
# exit 0: All checks passed!
```

Initial proof attempts exited 1: browser tooling was installed in the original
user's site-packages, hidden by the isolated HOME; an explicit proof-only import
path fixed it. The next attempt expected `Ready` rather than the actual pane's
`completed` state; corrected the assertion. Ruff initially exited 1 on loop
callback captures, fixed by binding each page's error list. Invoking the shared
venv's pytest entrypoint directly exited 2 because it imported the base checkout;
`python -m pytest` selects this worktree correctly.

Screenshots: `apps/desktop/build/workspace-proof/frozen-desktop.png` and
`frozen-mobile.png`. Visual inspection confirms the native result is readable
and composer/header fit both viewports, but also exposes duplicated local-command
results and raw command markup in replayed history. Those presentation defects
remain open. This is a native local-command proof, not full CLI parity, an
inference/browser tool-use proof, installed Electron QA, or Windows execution.

Frozen Linux backend build (2026-09-09): the first attempt selected libpython
from the running AppImage's mount via inherited LD_LIBRARY_PATH. That build was
explicitly terminated (exit 143), not treated as a successful artifact. A sourced
build-environment helper restores original non-AppImage library paths before
native tools start. The SDK transport similarly removes frozen library roots
from its external worker environment without changing the host process.
Source: https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html
(external-program environment handling), accessed 2026-09-09.

```sh
node --test apps/desktop/tests/build-env.test.cjs apps/desktop/tests/shell.test.js
# exit 0: 11 passed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_transport.py -q
# exit 0: 8 passed
# From apps/desktop:
SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar
# final exit 0: libpython selected from /lib/x86_64-linux-gnu, complete onedir
# build and existing peer capability smoke passed; no Fleet workflow started.
npm test
# exit 0: 72 passed
# From repository root:
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/fleet_peer_smoke.py --binary /home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: frozen startup, tool contract and invalid-capability refusal.
# An initial manual invocation with a relative --binary path exited 1 because
# the smoke helper changes cwd to a temporary directory; absolute path fixed it.
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron
# exit 0: source owner/worker native regression with Electron Node mode.
```

The build contains all three worker .mjs files and the Python client/runtime/
transport modules in its archive index. This proves artifact construction and
the existing startup smoke. The later frozen native browser roundtrip above
extends that evidence, but does not verify installed Electron UI behavior. The
build emitted optional dependency warnings; Windows and installer QA remain open.

Desktop runtime packaging wiring (2026-09-09): both Linux and Windows recipes
now provision the locked SDK and include it outside app.asar, plus all three
native worker modules in their PyInstaller data. Desktop backend environment
supplies packaged resource and Electron executable paths. Only the SDK worker
gets Electron's Node-mode environment flag; the native Claude CLI does not.
Source: https://www.electronjs.org/docs/latest/tutorial/fuses, accessed 2026-09-09
(RunAsNode is enabled by default and controls ELECTRON_RUN_AS_NODE support).

```sh
node --test apps/desktop/tests/shell.test.js
# exit 0: 9 passed, launch environment and both packaging recipes
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_transport.py -q
# exit 0: 7 passed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_transport.py tests/test_workspace_claude_transport.py scripts/verify-workspace-claude-transport.py
# exit 0
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron
# exit 0: default owner runs public SDK worker with the real Electron executable
# in Node mode, exact native session input/output and cleanup; no GUI launched.
# From apps/desktop:
npm test
# initial exit 1: missing local js-yaml dependency (52 pass, one load failure)
npm ci --ignore-scripts --no-audit --no-fund
# exit 0: 329 packages, existing dependency deprecation warnings
npm test
# exit 0: 70 passed
```

This is build wiring plus Linux runtime evidence, not an installer build or
Windows execution proof. Frozen artifacts, shared-backend runtime discovery,
and actual installed-app QA remain required. No release, restart or deployment.

Source runtime provisioning (2026-09-09): `runtimes/claude-sdk` now pins SDK
0.3.266 with a generated npm lockfile. `workspace_claude_runtime.py` validates
installed package identity/version and resolves Node without installing or
launching anything. Missing/mismatched SDK or Node fails explicitly. The
structured host's default Claude factory now uses this public SDK client;
ordinary terminal routes and workspace activation flags are unchanged.

```sh
# From runtimes/claude-sdk:
npm install --ignore-scripts --omit=optional --no-audit --no-fund
# exit 0: 102 packages installed; node_modules ignored, lockfile retained
# From repository root:
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_runtime.py tests/test_workspace_host.py -q
# exit 0: 23 passed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_runtime.py core/workspace_host.py tests/test_workspace_claude_runtime.py scripts/verify-workspace-claude-transport.py
# exit 0
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python
# exit 0: native proofs now use provisioned dependency and default host factory,
# not an injected client factory or temporary SDK path; exact session and cleanup.
```

`SERENA_WORKSPACE_RUNTIME_ROOT` and `SERENA_WORKSPACE_NODE` support explicit
packaged locations. Frozen Electron resource inclusion and platform runtime
provisioning are NOT implemented yet; source installation is not an installed-app
release. Next: package those resources without relying on system Node or /tmp.

Native Claude MCP tool-call form verified (2026-09-09): the isolated subscription
proof now optionally resumes with ClaudeTypeScriptClient and registers a local
MCP tool fixture. Only that fixture tool is approved; its native elicitation
reaches ClaudeWorkspace, is validated by the normal answer route, and returns
the exact typed count to the MCP server. The server persists the real response
as the assertion source; the parent turn must complete successfully. The same
run verifies actual inference before/after exact resume, command discovery,
local /context output, and native context breakdown through the new client.

```sh
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference --typescript-sdk /tmp/serena-sdk-ts/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs --mcp-form
# exit 0: first native turn, exact resumed second response, native context,
# real MCP tool -> form -> validated count 2 returned to requesting server,
# parent completed, owned processes reaped, isolated auth/history cleaned up.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_claude_wire.py tests/test_workspace_claude_transport.py -q
# exit 0: 38 passed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-claude-roundtrip.py scripts/workspace-claude-form-fixture.py
# exit 0
```

The earlier discovery-time cancellation remains a distinct native behavior,
not a failed normal tool-call route. The successful proof uses an automated
fixture answer at the pane-owner boundary; real browser field entry is covered
separately, not claimed as one combined browser/native run. Runtime dependency
packaging and production client selection are next; full provider/desktop
parity gates elsewhere in this document remain open.

Claude MCP form UI route (2026-09-09): the public SDK client's elicitation
callback now reaches ClaudeWorkspace and the existing shared form renderer.
Requests use a provider-prefixed native ID, exact thread identity, form-mode
default, and the same typed schema validator as Codex. Invalid replies leave the
form pending; native cancellation retires it; explicit owner shutdown cancels
rather than accepts. Optional native content is omitted for cancel/decline,
instead of sending the renderer's nullable field to the SDK.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_pane.py::test_mcp_form_collects_typed_fields_and_safe_url -q
# exit 0: 28 passed, including browser form typing and unsafe URL rejection
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_client.py tests/test_workspace_claude.py scripts/verify-workspace-claude-transport.py scripts/workspace-claude-form-fixture.py
# exit 0
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs /tmp/serena-sdk-ts/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python
# exit 0: existing native owner/input/output/lease regression; not a form proof
```

The attempted zero-inference MCP discovery form did NOT pass. Instrumentation
proved the server entered tools/list and connected, but native Claude returned
`action: cancel, content: null` without emitting an elicitation question to this
channel. Earlier runs timed out because the fixture asserted before persisting
that response; the last instrumented run exited 1 with the actual cancellation.
The fixture/experimental `--discovery-form` flag preserve this diagnostic. This
does not establish behavior for an MCP form raised during a normal tool call.
Next: exercise that normal tool-call path through the public SDK owner. Do not
claim native form roundtrip parity based on the passing unit/browser tests.

Claude owner compatibility (2026-09-09): `ClaudeTypeScriptClient` implements the
existing ClaudeWorkspace client boundary through public SDK controls, exposes
the actual owned PID for its shared lease, forwards permission contexts/answers,
and preserves native records. ClaudeEvents now accepts TypeScript wire messages
alongside Python dataclasses: stream/canonical IDs agree, subagent provenance
does not change the parent model, tool results/tasks/completion normalize, and
unrecognized records remain inspectable without losing their original shape.

The native proof now exercises the existing ClaudeWorkspace owner with this
client factory, real persisted history, model catalog, zero-inference command
submission, rendered item/completion events, ready state and child cleanup.
This proves owner compatibility for that flow, not full provider parity. The
default client is unchanged pending dependency provisioning and MCP elicitation
UI integration; no production rollout occurred.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_client.py tests/test_workspace_claude_wire.py tests/test_workspace_claude.py -q
# exit 0: 28 passed
node --test tests/workspace-claude-sdk.test.mjs tests/workspace-claude-channel.test.mjs
# exit 0: 13 passed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_client.py core/workspace_claude_events.py tests/test_workspace_claude_client.py tests/test_workspace_claude_wire.py scripts/verify-workspace-claude-transport.py
# initial exit 1: proof-script import ordering; fixed; final exit 0
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs /tmp/serena-sdk-ts/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python
# exit 0: direct driver, Node channel, Python transport/shared lease and existing
# ClaudeWorkspace owner all exercised against isolated native saved session.
```

Python native transport (2026-09-09): `workspace_claude_transport.py` connects
the actual WorkspaceRpc implementation to the Node SDK worker. It verifies the
reported native PID belongs to that wrapper, strips metered credentials, rejects
foreign-session input/output, handles interactive requests concurrently with
native output, forwards exact RPC answers, and propagates native cancellation.
Shutdown requests child reaping before the bounded transport fallback. Admission
and SessionLease remain the caller's responsibility, as in the existing owner.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_transport.py -q
# exit 0: 6 passed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_transport.py tests/test_workspace_claude_transport.py scripts/verify-workspace-claude-transport.py
# exit 0: all checks passed
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs /tmp/serena-sdk-ts/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python
# exit 0: prior direct/Node proofs plus Python WorkspaceRpc -> real native CLI,
# exact resumed local-command result, shared lease bound to actual child PID,
# duplicate owner rejected, wrapper exit 0, child gone, lease reacquired.
# Temporary HOME/config/session only; zero inference or user authentication.
```

Next is the SDK client/message compatibility layer inside ClaudeWorkspace;
the existing production owner is not switched yet. Native MCP forms still need
a complete roundtrip, beyond the tested callback forwarding/cancellation.

Claude JSONL process boundary (2026-09-09):
`workspace_claude_channel.mjs` routes explicit open/send/control/close methods,
native message notifications and bidirectional approval/elicitation requests.
Request processing remains concurrent so an approval response cannot deadlock
behind a control waiting for it. Native cancellation retires pending requests;
late responses fail and owner shutdown never auto-accepts them. Stream failure
is reported without launching a replacement session.

`workspace_claude_worker.mjs` supplies the private Node subprocess entry point
for the existing Python `WorkspaceRpc` transport. It reports the actual CLI PID,
starts no CLI until open, and reaps its child before acknowledging close. The
parent must still reserve/bind the existing shared lease and sanitize billing
environment. Python owner integration is next; production still uses its current
adapter, and runtime dependency packaging is not complete.

```sh
node --test tests/workspace-claude-channel.test.mjs tests/workspace-claude-sdk.test.mjs
# exit 0: 13 passed
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs /tmp/serena-sdk-ts/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude
# exit 0: direct driver proof plus real JSONL subprocess, no automatic CLI,
# exact resumed session local-command input/output, native PID notification,
# child absent before close response; wrapper exit 0. Zero inference/auth use.
```

The subprocess proof currently uses a small Node JSONL client; running the same
boundary through Python WorkspaceRpc and integrating lease ownership are still
required. Native MCP elicitation is not proved by callback unit tests.

Public Claude TypeScript session driver (2026-09-09):
`core/workspace_claude_sdk.mjs` now supports explicit exact-session resume,
streaming user input/native output, public SDK controls, and caller-mediated
tool approvals/elicitation. Missing or mismatched session metadata, duplicate
starts, late launches after cancellation and foreign-session output fail closed.
Null elicitation responses are rejected rather than silently leaving a form
unanswered. Constructor/read-only metadata lookup never launches a CLI.

This is an internal driver, not yet wired into the production Python owner.
Its caller must retain admission, acquire the shared session lease before open,
bind the actual child PID through `spawnOwned`, strip metered credentials, and
reap the child before releasing ownership. It does not replace those contracts.
Next: integrate this boundary with the existing owner/journal and map native
TypeScript messages without losing the Python adapter's tested behavior.

```sh
node --test tests/workspace-claude-sdk.test.mjs
# exit 0: 7 passed
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs /tmp/serena-sdk-ts/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude
# exit 0: isolated local command persisted a real session; original CLI exited,
# driver resumed exact ID, sent another local command, received matching native
# result (zero inference/cost), and reaped resumed CLI. Public effort and agent
# controls acknowledged. No user auth/settings/session used.
```

Approval callback tests are not a native MCP elicitation roundtrip. Full SDK
dependency packaging, owner integration and provider parity remain unfinished.

Current integrated verification and next provider-runtime decision (2026-09-09):

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_*.py -q --basetemp=/tmp/serena-workspace-integration-current
# exit 0: 171 passed in 62.47s
node --test tests/workspace-*.test.mjs
# exit 0: 23 passed
```

Package research inspected Python SDK 0.2.152 separately from the installed
0.2.121, and TypeScript SDK 0.3.266. The newer Python public client still lacks
elicitation and session flag-setting controls. TypeScript declares onElicitation,
applyFlagSettings, supportedAgents and reloadSkills. Its public spawn callback
also supplies a verifiable owned process. The isolated native probe confirms
initialization, effort apply/clear, agent inventory and skill reload on one CLI,
without a user message, copied login, user settings or inference:

```sh
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-ts.mjs /tmp/serena-sdk-ts/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude
# exit 0: public controls acknowledged; owned CLI exit 0
```

Research installs were temporary (`npm install --prefix /tmp/serena-sdk-ts
--ignore-scripts --omit=optional --no-audit --no-fund
@anthropic-ai/claude-agent-sdk@0.3.266`); no project/runtime dependency was changed.
Sources: https://code.claude.com/docs/en/agent-sdk/typescript and published
https://www.npmjs.com/package/@anthropic-ai/claude-agent-sdk/v/0.3.266 declarations,
accessed 2026-09-09. The TypeScript reference page exceeded the browser fetch
limit; package declarations and live public calls supplied the precise evidence.
Next: replace the Python SDK control boundary compatibly, preserving exact-session
lease, journal and billing constraints. Registration alone does not prove MCP
elicitation roundtrip; that and full adapter migration remain unfinished.

Claude child-message and child-tool provenance now survives normalization,
streaming and completed messages. The pane labels subagent output, exposes the
native parent-tool ID, and shows the reported child model without updating the
parent's model selection. Independent expanded details remain open during item
updates. This does not yet supply historical sidechain discovery or a complete
agent-management view; native subagent execution was not launched for this slice.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_pane.py::test_subagent_provenance_is_visible_for_messages_and_tools -q --basetemp=/tmp/serena-subagent-provenance-final
# exit 0: 22 passed in 1.26s; native SDK record fixtures and mobile browser view
node --test tests/workspace-events.test.mjs
# exit 0: 8 passed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude.py --local-effort
# exit 0: root native-control regression proof; zero inference, child process
# reaped. This is not live subagent execution evidence.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_events.py tests/test_workspace_claude.py tests/test_workspace_pane.py
# exit 0: all checks passed
```

Escape in the focused composer now requests interruption of its displayed
running turn. The stop button shares that path and an in-flight guard. Idle
Escape, IME composition, key repeats, dialog dismissal and view disposal do not
interrupt. The browser sends expectedTurnId; the host rejects stale targets under
the session lock and preserves receipt replay without stopping a later turn.
Legacy callers omitting expectedTurnId retain the prior API contract.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_pane.py::test_escape_interrupts_only_focused_running_turn_not_dialog_or_draft -q --basetemp=/tmp/serena-scoped-interrupt
# exit 0: 21 passed in 8.77s
node --test tests/workspace-connection.test.mjs
# exit 0: 13 passed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference --bridge
# exit 0: stale stop rejected, exact native stop accepted, matching turn
# completed and owner returned ready; owned processes cleaned up
```

Claude reported the interrupted turn with native failed status; this slice does
not relabel provider failures as interruption based solely on a button click.

Queued sibling messages can now be edited by exact request ID. Dispatch reads the
latest saved text under the same session lock, preserving FIFO and the original
sender's receipt identity. Updates compare the displayed original text, reject
stale editors and already-dispatched turns, and roll back if journal publication
fails. The separate edit dialog keeps drafts intact through queue refreshes and
delivery errors; closing it never cancels or submits the queued message.

```sh
node --test tests/workspace-connection.test.mjs
# exit 0: 12 passed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_bridge.py -q --basetemp=/tmp/serena-queue-edit-final
# exit 0: 22 passed in 4.38s, both provider routes, FIFO, stale edits,
# stable receipts, late rejection and journal failure rollback
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_queue_edit_keeps_draft_and_targets_original_message -q --basetemp=/tmp/serena-queue-edit-browser
# exit 0: 2 passed in 1.87s; mobile/desktop screenshots reviewed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --bridge
# exit 1 BEFORE queue proof: isolated copied refresh token rejected as already
# used. No login/config repair attempted; Codex native edit proof remains open.
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference --bridge
# exit 0: native busy turn followed by edited queued prompt, exact resumed
# session output and original receipt retained; owned processes cleaned up
```

The browser's duplicate raw-event cache is limited to 100 records and an
approximately 1 MiB serialized-string budget. Oversized records are not retained
in that cache; ordered replay and the disk journal are unchanged. This does not
bound the entire conversation/history model or image storage, which remain gaps.
A read-only Session events inspector fetches one journal page at a time, renders
raw bodies only on expansion, and releases the page on close. Its independent
cursor never advances live replay, sends commands, attaches or stops an owner.

```sh
node --test tests/workspace-events.test.mjs tests/workspace-connection.test.mjs
# exit 0: 18 passed, including cache bounds and independent read-only paging
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_event_inspector_pages_lazily_without_session_actions -q --basetemp=/tmp/serena-event-inspector
# exit 0: 2 passed in 2.64s; desktop/mobile screenshots reviewed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q --basetemp=/tmp/serena-event-inspector-served
# exit 0: 2 passed in 5.14s; real HTTP journal access before/after attachment,
# no launch when reading an empty journal, exact-session native history visible
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py tests/test_workspace_app.py
# exit 0: all checks passed
```

Claude tool calls now stream into one pane item from native content-block start
and input-JSON deltas. Partial JSON is explicitly labelled as receiving input,
never treated as a complete command. Complete objects use the standard JSON
parser; incomplete data remains inspectable until the authoritative SDK message.
Parent/subagent streams remain separate, and late fragments/stops cannot replace
an already complete tool message. Expanded call state survives renderer updates.

The first live run exposed a real SDK ordering race (`inputJson` missing after
the complete tool record arrived before block stop), exit 1. An instrumented
rerun and targeted regression reproduced it, both exit 1. The complete record
now retires that partial stream; the final native proof passed:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_pane.py::test_streaming_tool_input_keeps_one_expanded_call -q --basetemp=/tmp/serena-tool-stream-final
# exit 0: 22 passed in 1.14s
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference --background-task
# exit 0: native partial tool JSON observed before complete input; exact resumed
# session, task stopped by native ID, parent completed, owned processes closed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_events.py tests/test_workspace_claude.py tests/test_workspace_pane.py scripts/verify-workspace-claude-roundtrip.py
# exit 0: all checks passed
```

Claude tool cards now render Bash commands and text output directly, requested
Edit before/after lines, and requested Write content. Failed edits remain labelled
failed and requested, not applied. Unknown structured output and full native
records remain inspectable. All tool-provided strings render as text, not HTML;
there are no inferred command exit codes. Browser fixtures cover desktop/mobile,
malicious markup, unknown output, and native-record access; screenshots reviewed.
The history reducer also promotes the native thread model into pane controls
without overwriting an explicit resume model.

```sh
node --test tests/workspace-events.test.mjs
# exit 0: 6 passed, 0 failed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_claude_tools_show_readable_native_output_and_requested_edits -q --basetemp=/tmp/serena-native-tool-view
# exit 0: 2 passed in 2.06s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q --basetemp=/tmp/serena-native-tool-view-final
# exit 0: 42 passed in 29.38s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py
# exit 0: all checks passed
```

Claude model identity now ignores `<synthetic>` local-command responses and
subagent models when updating the parent pane. Original native records remain
intact. History restores the last real assistant model when one exists, without
inventing a model for command-only sessions. This is last-observed model evidence,
not a claim that the SDK init cache tracks every out-of-band setting change.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py -q --basetemp=/tmp/serena-claude-model-identity-final
# exit 0: 19 passed in 0.42s
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude.py --local-effort
# exit 0: actual native synthetic response passed through ClaudeEvents;
# model identity retained, zero model turns/cost, owned CLI reaped
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_events.py tests/test_workspace_claude.py scripts/verify-workspace-claude.py
# exit 0: all checks passed
```

Claude now has an explicit reasoning-effort control. It discovers the native
effort command and current model's advertised levels, then sends `/effort LEVEL`
through the existing exact-session submit path. It does not consume draft text
or attachments, switch models, or claim application from the transport receipt;
the native response is the acknowledgement. Busy turns are rejected, and an
unconfirmed send leaves the dialog available for receipt-preserving retry.

Research: https://code.claude.com/docs/en/model-config and
https://code.claude.com/docs/en/agent-sdk/agent-loop (accessed 2026-09-09), plus the
installed SDK catalog and native CLI result. Python SDK has no public runtime
effort setter; the local slash command was verified instead. Fast mode remains
unfinished; the isolated native init reports `sdk_opt_in_required` and no silent
opt-in or metered fallback has been added.

```sh
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude.py --local-effort
# exit 0: native session-only effort acknowledgement, num_turns=0,
# duration_api_ms=0, total_cost_usd=0; isolated owned CLI reaped, exit 0
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q --basetemp=/tmp/serena-effort-browser-final
# exit 0: 40 passed in 31.11s, including effort dialog at 390/1600 pixels,
# unchanged drafts, busy rejection, failure/retry and populated toolbar layout
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-claude.py tests/test_workspace_pane.py
# exit 0: all checks passed
```

Served-page attachment verification now exercises a real multipart HTTP upload
from Chromium, provider-specific image conversion, exact-session preview access,
foreign-session rejection, and reload without resending. Provider owners in this
test are controlled fixtures; this is not an additional live inference claim.
Initial event replay failure now rejects connection instead of hiding the Resume
button and implying success. The existing owner is neither stopped nor replaced.

```sh
node --test tests/workspace-connection.test.mjs
# exit 0: 10 passed, 0 failed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q --basetemp=/tmp/serena-served-upload-final
# exit 0: 2 passed in 4.05s
```

A separately recorded `SERENA_EVIDENCE_KIND=live node --input-type=module -e ...`
probe used a real isolated HTTP listener: event replay returned 503, connect
rejected without automatic polling, then explicit retry succeeded (exit 0).
No real provider, user session, or production service was launched by that probe.

Integrated workspace verification after the control additions:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_*.py -q --basetemp=/tmp/serena-workspace-integrated-check
# exit 0: 147 passed in 33.47s
node --test tests/workspace-*.test.mjs
# exit 0: 16 passed, 0 failed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_complete_control_surface_fits_without_auto_actions -q --basetemp=/tmp/serena-combined-controls-final
# exit 0: four provider/viewport cases passed; populated idle controls, selected
# model/effort/speed and queued-message control stay in bounds without overlap
```

The new browser cases use supplied control fixtures, not live provider sessions.
They assert no construction-time actions and inspect actual DOM geometry at
390/1600 pixels. Screenshots were reviewed while developing the cases. These
results do not prove the complete installed Electron application, Windows,
Gemini, or remaining command/session lifecycle parity. No rollout was performed.

Codex permission profiles now use native permissionProfile/list and
thread/settings/update. Profiles are discovered for the owner's project with
bounded pagination; managed disallowed entries are disabled in the picker and
rejected again at submission. Every profile change requires confirmation and an
idle owner, changes only subsequent-turn settings, and never writes user config
or launches another turn. The last acknowledged profile is shown; initial legacy
sandbox settings are not mislabeled as a named profile. Legacy sandbox editing,
granular approvals and automatic live profile tracking remain unfinished.
Sources: https://learn.chatgpt.com/docs/app-server and
https://learn.chatgpt.com/docs/permissions (accessed 2026-09-09), installed native
PermissionProfileList and ThreadSettingsUpdate schemas.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_pane.py::test_codex_permission_profile_picker_disables_managed_denials -q --basetemp=/tmp/serena-codex-profiles-final
# exit 0: 18 passed in 0.89s; disabled DOM property and keyboard selection checked
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-mcp.py --inventory-only
# exit 0: native :read-only profile allowed and acknowledged on exact idle thread,
# no inference/tool call/copied auth/user session, owned process reaped
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_pane.py scripts/verify-workspace-mcp.py
# exit 0: All checks passed!
```

Initial combined run exited 1 (69 passed): Playwright's is_disabled helper did
not recognize the disabled option. Direct DOM inspection and keyboard navigation
verified it was disabled; the regression test now checks both explicitly.

Claude permission modes now use native set_permission_mode on the existing owner,
with an explicit Apply action and a separate confirmation for bypassPermissions.
Changes require an idle owner with no pending questions; native rejection leaves
the confirmed mode unchanged. The UI labels the result "Last confirmed": SDK
server-info is initialization metadata, not a live feed of automatic transitions.
Auto/bypass availability is still enforced by the CLI and may be rejected; neither
is enabled at startup. Native live proof changes only isolated plan/default modes.
Source: installed ClaudeSDKClient.set_permission_mode and public Python reference.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_pane.py -q --basetemp=/tmp/serena-permission-mode-verification
# exit 0: 50 passed in 18.33s; mobile dialog reviewed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_permission_mode_control_requires_explicit_boolean_confirmation -q --basetemp=/tmp/serena-permission-mode-routing
# exit 0: 1 passed in 0.21s; exact owner, strict confirmation, replay deduplication
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude.py
# exit 0: native plan/default acknowledgements, no turn/tool execution,
# isolated configuration, owned process exit 0
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_host.py tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-claude.py
# exit 0: All checks passed!
```

Claude context breakdown is now an explicit native control, not another /context
conversation turn. The dialog displays provider totals, capacity, percentage,
model and category counts, marks deferred entries, and clears stale data on
refresh failure. Unknown/nonfinite data is rejected rather than guessed. Native
memory-file paths and detailed tool/agent metadata are not returned by this view.
Opening or closing the pane never requests a context read or launches an owner.
Source: installed ClaudeSDKClient.get_context_usage and ContextUsageResponse,
documented at https://code.claude.com/docs/en/agent-sdk/python (accessed 2026-09-09).

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_pane.py -q --basetemp=/tmp/serena-context-breakdown-verification
# exit 0: 48 passed in 18.02s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_context_control_reads_attached_claude_without_query -q --basetemp=/tmp/serena-context-routing
# exit 0: 1 passed in 0.23s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_context_breakdown_is_explicit_and_clears_stale_data_on_failure -q --basetemp=/tmp/serena-context-layout
# exit 0: 1 passed in 0.76s; missing dialog icons fixed, mobile screenshot reviewed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference
# exit 0: exact persisted session, native context control without another turn,
# owned processes reaped and isolated storage removed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_host.py tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-claude-roundtrip.py
# exit 0: All checks passed!
```

Skill inputs now also work for Codex steering. The adapter validates selections,
then rechecks the active turn after discovery before sending native turn/steer;
there is no turn/start fallback. The browser/host carry skills separately from
model settings, and plain-text steering retains its previous payload shape.
Fixed a skill-only send defect: the connection previously sent an empty inputs
list, rejected by upload validation, despite the pane fixture passing. It now
sends an empty text part alongside the native skill selection. Transport and
host routing tests cover this path rather than only a mocked pane callback.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_pane.py::test_codex_running_composer_steers_exact_turn tests/test_workspace_codex.py -q --basetemp=/tmp/serena-skill-steer-final
# exit 0: 34 passed in 3.51s
node --test tests/workspace-connection.test.mjs
# exit 0: 9 passed, 0 failed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --skills
# exit 0: real skill-only turn plus native skill steering on the original active
# turn, marker response, exactly one completion, owned processes reaped
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py scripts/verify-workspace-codex-roundtrip.py
# exit 0: All checks passed!
```

Initial combined tests exited 1 (62 passed): plain steering unnecessarily added
an empty options object. It now omits options when there are no selected skills.

Codex skills are now selectable through native skills/list for the exact project.
The picker shows the skill path, keeps selected skills with the draft, and never
auto-sends. Submission re-reads the native catalog, rejects stale/disabled/foreign
paths, and sends typed skill inputs with exact name/path on the existing thread.
Selections survive view recreation and failed sends; successful sends clear only
the submitted selections. Skill-bearing steering is covered above. Skill configuration editing and the
remaining Codex CLI commands are not implemented by this picker.
Source: https://learn.chatgpt.com/docs/app-server (accessed 2026-09-09), installed
SkillsListParams/Response and native user-input schemas.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_pane.py tests/test_workspace_host.py -q --basetemp=/tmp/serena-native-skills-final
# exit 0: 62 passed in 21.74s; mobile skill picker screenshot reviewed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_codex_skill_selection_persists_and_sends_exact_path_only_on_submit -q --basetemp=/tmp/serena-native-skills-browser
# exit 0: 1 passed in 0.69s
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --skills
# exit 0: exact resumed thread, real local skill invocation and marker response,
# unchanged project fixture bytes, owned process cleanup
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_pane.py scripts/verify-workspace-codex-roundtrip.py
# exit 0: All checks passed!
```

Initial browser run exited 1 (45 passed): the fixture reused replay sequence
numbers after recreation and selected both panes' send buttons. The fixture now
replays from sequence 1 and selects its exact pane. Initial live skill proof
exited 1 after successful invocation because the old empty-project assertion
rejected the proof's own skill file; it now checks an exact baseline file snapshot.

Codex MCP inventory is now available in the shared connections panel. It uses
native mcpServerStatus/list with the exact threadId, bounded cursor pagination,
and toolsAndAuthOnly. Runtime connection state, auth state and tool count remain
separate: null runtime status displays unknown, even with OAuth or cached tools.
No server metadata, schemas or configuration credentials are returned to the UI.
Codex reconnect/toggle controls are not exposed as Claude SDK calls: Codex uses
different configuration/OAuth operations, which remain to be integrated.
Source: https://learn.chatgpt.com/docs/app-server (accessed 2026-09-09) and installed
ListMcpServerStatusParams/Response schemas.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py -q --basetemp=/tmp/serena-codex-mcp-inventory
# exit 0: 14 passed in 0.12s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_pane.py -k mcp -q --basetemp=/tmp/serena-codex-mcp-ui
# exit 0: 7 passed, 39 deselected in 3.34s; mobile screenshot reviewed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-mcp.py --inventory-only
# exit 0: actual native thread inventory reports local MCP connected with one
# tool through the adapter; no inference/auth copy/tool call, process reaped
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py scripts/verify-workspace-mcp.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py
# exit 0: All checks passed!
```

Claude background tasks now appear in the shared task dialog, from native
TaskStarted/Progress/Updated/Notification events on the exact owned session.
Stopping calls the public SDK stop_task with the observed task ID, never an OS
PID or parent interrupt. An acknowledgement shows "Stop requested" until a
native terminal lifecycle event removes the task from the active list. Both
notification and patch-only completion are handled; late updates cannot revive
a terminal task. Paused tasks remain eligible for explicit stop. Discovery is
limited to lifecycle events observed by this owner; reconstructing task inventory
after owner recovery remains unverified. Dialog refresh is explicit.
Source: installed claude-agent-sdk 0.2.121 TaskUpdatedMessage lifecycle contract
and ClaudeSDKClient.stop_task; public Python reference linked below.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_pane.py -q --basetemp=/tmp/serena-claude-task-verification
# exit 0: 58 passed in 22.29s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py -q --basetemp=/tmp/serena-claude-task-terminal-verification
# exit 0: 15 passed in 0.38s; terminal late-update regression
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference --background-task
# exit 0: isolated persisted session resumed; real background sleep task observed,
# stopped through native exact-ID control, parent completed, owned processes reaped
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_events.py core/workspace_host.py tests/test_workspace_claude.py tests/test_workspace_pane.py scripts/verify-workspace-claude-roundtrip.py
# exit 0: All checks passed!
```

Claude MCP connections now have an explicit pane dialog for native status,
reconnect, enable and disable. Opening/closing a pane does not change connections.
Mutations validate the server against the current owner's native inventory and
require an idle owner, preserving tools during active turns. Stable command IDs
prevent replays from repeating mutations. Configs, headers, credentials and raw
connection errors are omitted from status responses/receipts; only name/status
are exposed. Authentication/config editing and detailed sanitized diagnostics are
not yet implemented. Python SDK 0.2.121 exposes these public controls but no
elicitation callback; Claude form handling remains a real parity gap.
Source: https://code.claude.com/docs/en/agent-sdk/python (accessed 2026-09-09) and
installed `ClaudeSDKClient` public methods. No private protocol monkeypatch added.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_pane.py -q --basetemp=/tmp/serena-claude-mcp-control-verification
# exit 0: 56 passed in 24.65s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_mcp_connections_explicit_controls_and_failure_state -q --basetemp=/tmp/serena-claude-mcp-layout
# exit 0: 1 passed in 0.75s; mobile overflow fixed and screenshot reviewed
node --test tests/workspace-connection.test.mjs
# exit 0: 8 passed, 0 failed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude.py
# exit 0: installed CLI reports local MCP connected, disabled, connected after
# enable and reconnect; no inference/tool call/user session; owned child exit 0
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_host.py tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-claude.py
# exit 0: All checks passed!
```

Native Codex MCP form and URL requests now have explicit answer controls. Typed
fields are checked against the requested schema on the owner before sending;
invalid/stale answers are rejected. Tool-call permission requests show arguments
as inert text. Unsupported modes can be declined/cancelled, not submitted.
URL requests offer an explicit HTTP(S) link and never open automatically.
Nullable native defaults no longer create optional answers or break array fields.
Command receipts store answer fingerprints instead of raw form values, in both
the host journal and browser session storage. This does not remove values from
provider transcripts or tool outputs that return them. Full MCP management,
extended OpenAI forms, and persistent approval scopes remain unfinished.

Evidence for this slice:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_elicitation.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py -q --basetemp=/tmp/serena-mcp-final-verification
# exit 0: 56 passed in 17.78s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -k mcp -q --basetemp=/tmp/serena-mcp-layout-final
# exit 0: 3 passed, 24 deselected in 1.83s; final tool-argument rendering and defaults
node --test tests/workspace-connection.test.mjs
# exit 0: 7 passed, 0 failed; hashed receipts preserve retry identity
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-mcp.py --allow-inference
# exit 0: real installed Codex subscription turn, local tool approval, typed form
# answer/result, owned process reaped and isolated configuration removed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_elicitation.py core/workspace_codex.py core/workspace_host.py tests/test_workspace_elicitation.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-mcp.py
# exit 0: All checks passed!
```

The initial live proof attempts exited 1. Direct tool calls declined/cancelled;
an interactive turn exposed the separate empty-schema tool approval. The actual
form was still cancelled until the proof server removed Pydantic's root `title`,
which is outside Codex's typed MCP root schema. The passing proof uses a compatible
schema and receives/answers both requests; production does not silently rewrite
third-party server schemas. This is a provider interoperability limitation, not
proof that every existing MCP server works. The fixture mobile screenshot was
reviewed; full integrated-app provider/browser parity remains unfinished.

Sources: https://learn.chatgpt.com/docs/app-server and
https://python-jsonschema.readthedocs.io/en/stable/validate/ (accessed 2026-09-09),
plus installed native MCP elicitation schemas and MCP Python SDK implementation.

Queued sibling messages now have a native queue panel with exact-request cancel.
Cancellation and dispatch share the owner lock; a request leaves the cancellable
queue before submission. A late cancel cannot interrupt a running turn. Queue
events include the prompt and ID, and list updates are serialized under the same
lock. Closing the panel sends nothing. Editing queued prompts and post-crash
recovery remain unfinished.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_bridge.py tests/test_workspace_pane.py -q --basetemp=/tmp/serena-queue-cancel-verification
# exit 0: 40 passed in 26.39s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_queue_dialog_cancels_only_selected_request_and_waits_for_host -q --basetemp=/tmp/serena-queue-cancel-layout
# exit 0: 1 passed in 1.04s; mobile screenshot reviewed
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_bridge.py::test_cancel_queue_cannot_interrupt_already_dispatched_turn -q --basetemp=/tmp/serena-queue-cancel-race-final
# exit 0: 2 passed in 0.67s
node --test tests/workspace-events.test.mjs tests/workspace-connection.test.mjs
# exit 0: 11 passed, 0 failed
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --bridge
# exit 0: native running turn preserved, cancelled queued prompt absent from user
# turns, next queued reply completed, receipt replay and owned process cleanup
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py tests/test_workspace_bridge.py tests/test_workspace_pane.py scripts/verify-workspace-codex-roundtrip.py
# exit 0: All checks passed!
```

The first late-cancel test exited 1 because 5ms did not prove dispatch had
occurred. It now observes the actual submission before testing the late cancel.

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
