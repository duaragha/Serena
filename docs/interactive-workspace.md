# Interactive Workspace Delivery Contract

Status: implementation in progress. Not a delivered replacement.

## Linked Native Context (2026-09-10)

The native frame asks its verified parent for current split membership on focus
and the context heartbeat. The parent responds only to the exact frame/session,
with mounted members of the active Chats/Code split. The frame includes this in
its existing sequenced view report. Backend validation rejects duplicate,
malformed or self-excluding identities; snapshots omit missing/closed owners.
Neither reporting path attaches a missing partner.

The aggregate runtime endpoint now takes split membership from the same context
as the selected focused session. It no longer combines a freshly focused native
session with an unrelated older GTK/PTY split. This is context publication;
native reusable-work admission and idle process sleeping remain unfinished.

Scoped verification:
```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_split_context_stays_with_its_focused_owner tests/test_workspace_host.py::test_view_context_auth_order_expiry_and_draft_retention tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short
# exit 0: 4 passed in 17.97s; both-provider browser layout reporting,
# no partner auto-launch, exact split/focus, expiry and validation
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
# exit 0: real Electron linked Claude/Codex focus, split partners and drafts;
# native creation/clipboard/local commands/reload/view closure preserved owners;
# source backend, no inference or credential changes, isolated children reaped
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py tests/test_workspace_host.py tests/test_workspace_app.py
# exit 0
node --check ui/static/workspace-page.mjs
# exit 0
node --check scripts/verify-workspace-electron.cjs
# exit 0
```

## Native View Context (2026-09-10)

Native panes now publish boolean focus, visibility and draft presence through an
authenticated local endpoint. Draft presence includes attachments and selected
skills; no draft content crosses this endpoint. Reports are session/view-bound,
monotonically sequenced across reload, and deduplicated within the two-second
heartbeat. Reads and reports never start an owner or owner loop.

Focus expires after six seconds without a fresh report. Closing the page clears
focus using a keepalive report, not a provider command; draft flags remain until
updated by that view. Runtime rows expose `draft_known` separately from `draft`
so missing/stale reports cannot be interpreted as proof of an empty composer.
Delayed reports cannot overwrite newer sequences. The aggregate local context
prefers the most recently focused surface rather than always preferring GTK.

This is context publication, not complete reusable-session admission: linked
split context, all native activity/reservation checks and native router admission
still need integration. Idle process sleeping and packaged rollout remain open.

Verification:
```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_view_context_auth_order_expiry_and_draft_retention tests/test_workspace_host.py::test_native_runtime_context_is_read_only_and_local tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short
# exit 0: 4 passed in 18.26s; both provider browser panes, no automatic owner,
# exact draft/focus reporting, authentication, malformed reports, expiry/order
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py -q --tb=short
# exit 0: 66 passed in 28.48s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py ui/workspace_web.py tests/test_workspace_host.py tests/test_workspace_app.py
# exit 0: All checks passed
node --check ui/static/workspace-page.mjs
# exit 0
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
# exit 0: real Codex desktop/mobile composer focus and unsent draft reported;
# no input submission/replacement owner; native history, commands, reload,
# disconnect/resume and fork verified; isolated children reaped; no inference
```

The first browser regression run exited 1 because it counted view telemetry as
a provider mutation. Its no-resume/no-command GET assertion now excludes only
the dedicated view-context endpoint, whose behavior is independently tested.

## Native Owner Inventory (2026-09-10)

The local-only runtime-context endpoint now includes existing structured owners,
their exact provider/session/project, native state, active turn/transition busy
flag and bridge queue reservation. Reads never resolve, attach, submit or create
an owner loop. No terminal ID or fabricated focus/draft is reported.

Historical work routing now excludes every reported living owner, not just
owners whose states happen to be the PTY-specific `live` or `paused`. This
prevents a native `ready`/`running`/transition owner being treated as an unowned
saved transcript. Native reuse is still unavailable until the focus, draft and
complete activity/admission contract is connected; inventory is not that contract.

Verification:
```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_native_runtime_context_is_read_only_and_local tests/test_work_session_router.py -q --tb=short
# exit 0: 52 passed in 4.16s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py -q --tb=short
# exit 0: 65 passed in 34.89s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/work_session_router.py tests/test_workspace_host.py tests/test_work_session_router.py
# exit 0: All checks passed
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
# exit 0: real Codex owner in local runtime context with unchanged owner PIDs;
# desktop/mobile native history, commands, reload, disconnect/resume and fork;
# isolated children closed, no credentials or inference used
```

This proof uses the source backend, not a new installed or frozen release.

## Native Pane Focus (2026-09-10)

Native frames now report real focus/pointer interaction to the parent. The parent
checks origin, exact frame identity, exact session, actual focused iframe,
document focus and visible geometry before updating its selected runtime,
highlight and status and acknowledging that chat. It does not call the composer
focus function, replace the iframe, start a provider or send input. Focus listeners
are removed with the view. Toolbar/dialog focus therefore is not stolen by the
parent when the user clicks a control inside the other half of a split.

Verified with both-provider mounted-browser regressions, including a real input
click, toolbar focus preservation and rejection of hidden/forged frame messages:
```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short
# exit 0: 2 passed in 13.76s
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
# exit 0: actual Electron and native linked Claude/Codex sessions; clicking each
# composer selected its exact parent runtime, toolbar dialogs retained focus,
# drafts survived, no extra owners, all isolated processes reaped
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_app.py
# exit 0
node --check scripts/verify-workspace-electron.cjs
# exit 0
```

The live check also retained all existing attention, clipboard, login controls,
native command/history, linked-creation and view-close checks. No inference or
user session changes occurred. This establishes renderer focus routing, not
resident work-router focus publication: `_reportWebRuntimeContext` still assumes
a PTY websocket. That host-context integration, idle-process sleep, Windows refresh
and final release/default activation remain open. No installed app was replaced.

## Attention Acknowledgement (2026-09-10)

Native output used to call `_markActive()` unconditionally, which also cleared
attention for hidden chats. Runtime-state updates now acknowledge attention only
when that exact pane is selected in Code, the Chats tab is selected, and the
document has focus. Explicit focus callers retain their previous acknowledgement
behavior. Receiving background output is not equivalent to the user seeing it.

The real mounted-page regression covers both providers and background sessions,
Read mode, another tab, an unfocused window and the genuinely viewed foreground.
Final focused command:
```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short
# exit 0: 2 passed in 13.50s (earlier intermediate runs also exited 0)
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
# exit 0: real Electron, source backend, exact native background shell output
# preserved attention; explicit sidebar focus acknowledged it exactly once;
# existing clipboard/login/linked-creation/owner-retention flows also passed
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-history.py tests/test_workspace_app.py
# exit 0
node --check scripts/verify-workspace-electron.cjs
# exit 0
```

The attention flag supplied to this live proof is a controlled GET response;
the background command, session process, output stream and acknowledgement
request are real. This does not prove notification generation or delivery hooks.
The verifier now accepts the Python sidecar entry point as well as a frozen
binary, labels source/frozen output correctly, and uses the same isolated homes,
owner checks and cleanup. This allows source UI iteration without rebuilding the
package for every attempt; it is not substituted for final packaged verification.
No inference, user session changes, release or installed-app changes occurred.

Remaining lifecycle work includes native iframe focus reporting: clicking inside
the opposite rich pane must update the parent's focused session, not merely focus
the child document. Idle-process sleep and final provider/Windows/release gates
also remain open. The prior Linux package does not yet contain this attention fix.

## Native Sidebar Ownership And Packaged Recovery (2026-09-10)

The session-list endpoint now decorates rows with an exact native-owner snapshot,
independent of mounted panes. It does not start an event loop, resolve a session,
resume a provider or send input. Active grouping and row styling consume that
snapshot, continue refreshing while background owners exist, and update promptly
from live pane state. Unavailable owners no longer remain marked active locally.

The packaged lifecycle proof uncovered two issues. First, its hidden-button
check could succeed before a new iframe loaded, racing creation-pane cleanup.
It now waits for actual native state and completed handoff. Second, the real
background discovery follower treated explicitly created linked chats as new
external discoveries and reopened their closed panes on the next refresh.
Exact creation adoption now consumes those discovery markers; unrelated external
chat discovery behavior is unchanged. The browser regression covers that removal.

Verification commands, run separately from this worktree:
```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_observation_never_starts_or_replaces_an_owner tests/test_workspace_app.py tests/test_fleet_chat_sidebar.py -q --tb=short
# exit 0: 19 passed in 47.49s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py -q --tb=short
# exit 0: 64 passed in 25.76s, including exact HTTP sidebar ownership
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short
# exit 0: 2 passed in 13.11s after the discovery-follower correction
env PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m PyInstaller --noconfirm --distpath apps/desktop/build/sidecar --workpath apps/desktop/build/pyinstaller-work apps/desktop/build/pyinstaller-work/serena-web-sidecar.spec
# exit 0: final build completed in 114.6s; earlier two builds also exited 0
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# final exit 0; earlier attempts exited 1 at the two issues described above
env SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: native Claude controls, queue, clear/fork and frozen desktop/mobile;
# this ran before the final parent-only discovery-follower correction
```

Final Linux binary SHA256:
`c97745f1830ac268b9ab6cefbcb95f715897bf09df8e6d860c282fb5dfd93d67`.
Actual Electron proved standalone and linked native creation, closing both linked
views with neither view mounted nor locally marked active, both real owners still
listed in Active, and reopening the same session. Closing Electron retained all
existing owners and exactly two newly created owners per provider. Real native
history, local input/output, clipboard, login start/cancel, desktop/mobile reload
with GET-only observation, and final disposable-child cleanup passed. No inference,
user credential changes, installed-app replacement or service restart occurred.
The linked-created screenshot was inspected; this is native empty-session state,
not a fabricated conversation or evidence of a model-generated linked turn.

Scoped Ruff and JavaScript syntax checks exited 0. Including all of `ui/web.py`
in Ruff exited 1 with 25 findings; a baseline/current comparison exited 0,
confirming the same 25 findings and no new lint diagnostics. Windows source
verification found three latest files not yet synced, so its older package was
not represented as proving these changes. Windows refresh, idle-process sleep,
remaining command/attention parity and release/default activation remain open.

## Existing-Owner View Recovery (2026-09-10)

Reopened native panes now observe an existing owner and replay its real journal
without another Resume click. The authenticated GET `/observe` inspects only the
exact in-memory owner on its existing event loop. It never creates that loop,
resolves a session, starts a provider, sends input or resumes a closed process.
Absent, opening, closed and unavailable owners still require explicit attachment.
Saved clear handoffs retain their explicit original-conversation confirmation.
Closing the view only stops observation; provider work remains independent.

Source verification, commands run separately from this worktree:
```sh
node --test tests/workspace-connection.test.mjs
# exit 0: 43 passed, including GET-only observation and foreign-ID rejection
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_app.py -q --tb=short
# exit 0: 78 passed in 41.25s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short
# exit 0: 2 passed in 14.24s after adding GET-only reload assertions
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py
# exit 0: 51 real native shell turns; desktop/mobile reload uses only GETs,
# exact PID retained, no resume/command/replacement; disconnect/retry and cleanup
env SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py
# exit 0: native Claude SDK/local command/queue/clear/fork, source desktop/mobile
# observe existing owner, no console/HTTP errors, every isolated child reaped
```

Initial scoped run: exit 1, 2 failed/14 passed because a controlled HTTP fixture
did not implement the new observe response. Updated its exact-ID read-only response;
no production behavior was weakened. First Codex proof invocation incorrectly
passed `--browser` as a binary path (exit 1 after native checks). Correct no-argument
invocation above completed successfully. Scoped Ruff exited 0. Screenshots
`source-mobile.png` and `codex-native-desktop.png` were inspected: neon black,
native output, usable wrapped mobile controls and preserved composer draft.
Proof scripts now expect passive observation for owners that are already alive;
unattached forks and closed sessions still require an explicit Resume action.
These latest source changes have not yet been rebuilt into either package.

## Windows Integration And Normal Authentication (2026-09-10)

Before the observation changes above, the Windows frozen Codex/Electron proof
completed with exit 0 using current `728507d` proof scripts and integrated
production source. Their SHA256s matched the laptop before execution. The previous
build handle was no longer present; a read-only process check confirmed no
PyInstaller process and an existing 18,960,681-byte executable. Its final build
exit was not recovered, so the runtime proof, not an assumed build exit, is evidence.

Invoked `scripts/verify-workspace-codex-windows.py --frozen
C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\dist\serena-web-sidecar\serena-web-sidecar.exe`
through SSH and the PC's existing Python, setting the documented Windows Edge,
Electron and Playwright proof paths. No PC source was written or app installed.
Observed native 51-turn history, exact resume, desktop/mobile input/output,
mentions, skill changes, disconnect, native fork, real Electron clipboard,
browser-login start/reopen/cancel, standalone and linked provider creation,
unchanged existing owners and exactly two new owners per provider after closing
Electron. All disposable children were reaped; no inference or credentials used.
The linked creation view in that package predates the observation fix above.

Normal laptop profile verification also passed: `codex login status` exited 0
with `Logged in using ChatGPT`. A real app-server account/read followed by
account/rateLimits/read returned ChatGPT subscription identity and authenticated
rate limits, then reaped its child (exit 0). A read-only process-environment probe
confirmed the running desktop backend uses that same `~/.codex` profile. No
credentials were printed/copied and no coding thread or inference was created.
This resolves the verified laptop sign-in blocker, not the remaining release,
installed rich-pane activation or complete provider command/lifecycle gates.

## Integrated Linux Package Proof (2026-09-10)

Current production source `56f5062` was rebuilt and verified with the expanded
Electron proof below. The native binary SHA256 is
`7a77fec7eb14e352a67c905e40c4d4c6e93edaf8e7b25d5be87bf142f60d3a8a`.
No installed app or running user service was replaced or restarted.

The initial build failed with ENOSPC, desktop tests exited 228, and the concurrent
Python suite was interrupted after filesystem failures (exit 1). Removed only
three disposable Gemini proof dependency files (2.6 GiB) inside this worktree's
ignored build directory. Space remained unavailable until a subsequent external
change left 17 GiB free. The interrupted owned processes were confirmed gone;
build resumed from the unchanged saved analysis rather than repeating it.

Commands run separately from repository root unless noted:
```sh
env PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m PyInstaller --noconfirm --distpath apps/desktop/build/sidecar --workpath apps/desktop/build/pyinstaller-work apps/desktop/build/pyinstaller-work/serena-web-sidecar.spec
# exit 0: frozen sidecar collected successfully
/home/raghav/Documents/Projects/serena/.venv/bin/python scripts/fleet_peer_smoke.py --binary /home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: bundled capability-refusal smoke; no Fleet run or worker started
# initial relative-path invocation exited 1 because smoke changes cwd
npm test
# from apps/desktop; exit 0: 73 tests passed
env PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace*.py -q --tb=short
# exit 0: 632 passed, 10 skipped, forkpty warning, 207.38s
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: native Codex + actual Electron flows described below
env SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: native Claude frozen desktop/mobile flows described below
```

Expanded Electron verification selects exactly the intended picker agents, then
creates standalone Codex, standalone Claude and an actual linked Claude/Codex pair.
Each receives its exact persisted identity and chosen title. The pair shares a
persisted group before any model input. Closing Electron preserves exactly two
new owned processes per provider, plus all pre-existing proof owners. No duplicate
writer, credentials or inference are used; every isolated child is reaped at end.
The account dialog starts real native ChatGPT browser login, survives dismissal,
then cancels explicitly by native ID without opening a browser or replacing auth.
Clipboard output-copy and multiline paste, corrupted-creation refusal, history,
native shell commands, mentions, skills, fork and disconnect/resume all pass.

Claude separately proves actual SDK queued UUIDs, local command output, native
effort/agent controls, Python/Electron worker ownership, skills/plugins reload,
mentions, history, clear and fork on desktop/mobile with no console/HTTP errors.
Screenshots in `apps/desktop/build/workspace-proof/` were inspected, including
`electron-native-workspace.png` and `electron-native-linked-created.png`.
The latter intentionally shows empty newly created panes awaiting explicit
view attachment; it is not evidence of linked inference or auto-attachment.

Windows current-source packaging, installed-profile auth, remaining command/
lifecycle parity and release/default activation remain unproven. Gemini remains
deferred by the user's explicit scope change.

## Current Desktop Integration (2026-09-10)

Integrated origin/master through `559ffeb` into this feature branch, including
the multi-agent picker and progressive group linking, current workspace sizing,
project filtering and explanatory terminal writer-lock errors. No main-branch
merge, release, install or default activation was performed.

Structured creation now adopts only the exact ID returned by its owned iframe,
preserving names, provisional group membership and every pending partner mapping.
It removes resolved placeholders and links returned native IDs without requiring
messages or using transcript/cwd/time heuristics. Duplicate concurrent creation
notifications and changed returned identities are guarded. The browser integration
test resolves Claude then Codex, checking exact links, titles, placeholders and
unchanged existing owner count. This uses controlled provider creation events;
packaged dual-native creation remains a separate delivery gate.

Verification commands, run separately:
```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch tests/test_multi_agent_new_chat_links.py tests/test_new_chat_multi_agent.py tests/test_linked_terminal_startup.py -q
# exit 0: 29 passed in 16.49s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_browser.py tests/test_codex_writer_lock_is_explained.py tests/test_fleet_workers_are_not_projects.py -q
# exit 0: 30 passed in 12.49s; forkpty multithreading warning
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_pty_stranded_sweep.py -q
# exit 0: 3 passed, 2 skipped in 10.21s
env SERENA_EVIDENCE_KIND=live SERENA_STRUCTURED_WORKSPACE=1 /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace.py --screenshots apps/desktop/build/workspace-proof/integrated --pane-presentation
# exit 0: actual index (570 chats), nonempty Read transcript, Git/Tooling,
# 390px/1600px layouts and native-pane presentation; no runtime launches/JS errors
```

The initial combined incoming-test run exited 1: two orphan fixtures were adopted
by this runner's subreaper, not PID 1. They now clean up even on constructor failure
and explicitly skip that unavailable kernel precondition; production sweep logic
is unchanged. The initial screenshot proof captured Loading despite passing;
it now waits for actual message elements before capture. Updated desktop/mobile
screenshots were visually inspected and contain rendered transcript text.

Ruff on `tests/test_workspace_app.py` and `scripts/verify-workspace.py` exited 0;
including the imported orphan test reports two pre-existing SIM105 style findings
in untouched cleanup blocks. `git diff --check HEAD` exited 0.

## Native Browser Sign-In (2026-09-10)

The Codex account dialog now has an explicit Sign in with ChatGPT control. Its
existing app-server starts `account/login/start` with `type:chatgpt`, never an
API key or device code. The returned HTTPS OpenAI authorization link is clickable;
Serena does not automatically open a browser. Pending login can be cancelled by
its exact native ID. Opening/closing the dialog never signs out or cancels work.
Two-second status polling stops when the dialog closes or login ends.

A machine-local operation lease blocks concurrent callbacks across app panes and
processes. It binds the actual app-server PID for crash recovery, and releases
only on native completion/cancellation or confirmed process teardown. An ambiguous
start stays uncertain and is not repeated. Early completion events, mismatched IDs,
unsafe URLs, payload injection, and active-turn rejection are covered. Account
status continues to distinguish stored credentials from verified inference.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py -q
# exit 0: 118 passed in 15.60s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_browser_login_is_single_owner_subscription_only_and_exact tests/test_workspace_codex.py::test_account_status_uses_exact_owner_without_refresh_or_inference tests/test_workspace_host.py::test_browser_login_controls_are_receipted_and_subscription_only tests/test_workspace_host.py::test_account_status_requires_explicit_owner_and_rejects_mutations tests/test_workspace_pane.py::test_account_status_is_explicit_honest_and_preserves_draft tests/test_workspace_pane.py::test_browser_login_requires_click_and_closing_does_not_cancel -q
# exit 0: 13 passed in 4.29s, including mobile/desktop dialogs
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --browser-login
# exit 0: actual installed Codex OAuth start + duplicate-call guard + native cancel,
# same owner/session, no browser opening or inference, unsigned temporary profile
# and owned child cleaned up. No normal credentials changed.
```

Protocol verified against installed generated LoginAccountParams and
CancelLoginAccountResponse schemas. The normal installed app login, successful
browser callback through this UI, Windows packaged path and release remain
delivery gates; this proof does not claim those are completed.

## Native Account Status (2026-09-10)

The Codex account button explicitly reads `account/read` with `refreshToken:false`
from the pane's existing owner. It never starts an owner, login, or inference.
Only account type, email, and plan are forwarded; saved credentials are explicitly
not presented as verified. Running work and drafts remain unchanged. Browser
login was added in the subsequent section above; installed-profile recovery
is still pending.
Official protocol checked 2026-09-10: https://learn.chatgpt.com/docs/app-server
(Account and authentication section).

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_account_status_uses_exact_owner_without_refresh_or_inference tests/test_workspace_host.py::test_account_status_requires_explicit_owner_and_rejects_mutations tests/test_workspace_pane.py::test_account_status_is_explicit_honest_and_preserves_draft -q
# exit 0: 5 passed in 4.37s, including 390px and 1600px browser dialogs
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py
# exit 0: real unsigned Codex owner, native account read, same process/session,
# no login/inference, child reaped and temporary profile removed.
# Initial proof exited 1 because its CODEX_HOME directory did not exist; fixed.
```

Scoped Ruff, node syntax check and git diff --check each exited 0. This does not
prove the normal app's existing login can refresh, nor complete overall delivery.

## Installation Diagnostics (2026-09-10)

Claude `/doctor` opens an explicit native-report dialog. Run invokes only the
installed `claude doctor`, in the owned session's project and environment, with
metered fallback stripped. No user prompt, coding session, repair or approval is
created. Output is plain text with credential redaction and actual numeric exit
code. It is bounded to 20 seconds / 1 MiB, and its process group (POSIX) or gated
job (Windows) is cleaned up. Existing owner identity/draft stays unchanged and
running turns refuse diagnostics. This is installation health reporting, not the
agentic `/doctor` skill's broader setup-repair workflow.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_diagnostics.py tests/test_workspace_claude.py::test_diagnostics_keep_exact_owner_and_refuse_a_running_turn tests/test_workspace_claude.py::test_command_catalog_and_session_switch_guard tests/test_workspace_host.py::test_diagnostics_route_is_exact_receipted_and_never_submits tests/test_workspace_pane.py::test_installation_diagnostics_are_explicit_and_show_native_exit_without_sending -q
# exit 0: 8 passed; initial browser fixture run had 2 failures from an unescaped
# newline in test JavaScript, corrected to a raw string
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-diagnostics.py
# exit 0: Linux native doctor 2.1.267, unsigned disposable profile, no repair
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_diagnostics.py core/workspace_claude.py core/workspace_host.py tests/test_workspace_diagnostics.py tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-diagnostics.py
# exit 0
node --check ui/static/workspace-pane.mjs
# exit 0
```

Windows proof also exited 0 against native Claude 2.1.260. New files had not yet
Syncthing-synced, so current module/proof sources were streamed over SSH into
Python memory, not written into the PC checkout. Executed module SHA256:
`894543ec59885abb500a98b1b8d9844c2103af596fae6d5a968f9ffb4a65388e`.
The first Windows proof exited 1 only when printing a Unicode arrow through
cp1252, after successful diagnostics. Proof output now uses ASCII JSON and the
rerun passed. Warnings about absent credentials/config in these proofs describe
their intentionally empty profiles, not Raghav's real installation health.

## Hidden Pane Polling (2026-09-10)

The actual iframe now reports visibility through IntersectionObserver plus
document visibility. Hidden views poll every 2000ms instead of 250ms; becoming
visible immediately refreshes the already-observed event stream. Construction or
visibility changes before explicit connection cannot start polling or a provider.
An in-flight poll is not duplicated, and disposal still never stops the owner.
This is renderer traffic reduction, not suspension of the agent process or a
claim that native idle-process sleep policy is implemented.

```sh
node --test tests/workspace-connection.test.mjs
# exit 0: 40 passed, including visibility/no-launch/in-flight/disposal checks
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q
# exit 0: both provider iframe flows, parent-hidden visibility, unchanged owner
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py
# exit 0: 51 real native print-only turns, exact history/resume/commands,
# desktop visible refresh 85ms and mobile 35ms after hidden throttling,
# owners and draft unchanged; explicit disconnect/resume; no auth/inference
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-history.py tests/test_workspace_app.py
# exit 0: All checks passed
node --check ui/static/workspace-page.mjs
# exit 0
```

Those latency numbers are observations from this local proof, not guarantees.
Installed-app behavior remains unchanged until rollout.

## Prompt Presentation (2026-09-10)

Claude `/color` now maps to native UI swatches; the palette control is also
available in Codex panes. Choices change only the composer border, keep the
neon-black background, and persist independently per provider/session in browser
session storage. Default restores the original border. Invalid values cannot
become CSS, and no model input or provider setting mutation is performed. Mobile
header controls now wrap rather than clipping off-screen.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py::test_command_catalog_and_session_switch_guard tests/test_workspace_pane.py::test_prompt_color_is_session_scoped_persistent_and_never_sent -q
# exit 0: 3 passed in 2.81s
env SERENA_EVIDENCE_KIND=live SERENA_STRUCTURED_WORKSPACE=1 /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace.py --screenshots apps/desktop/build/workspace-proof/presentation --pane-presentation
# exit 0: actual 566-session index, Read/Git inspector/Tooling; both providers'
# actual saved pane pages at 390px and 1600px; color restore and control bounds;
# zero owner loops/runtime launches and no JavaScript errors
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py tests/test_workspace_claude.py tests/test_workspace_pane.py scripts/verify-workspace.py
# final exit 0; initial exit 1 required explicit strict=True on zip
node --check ui/static/workspace-pane.mjs
# exit 0
```

Desktop/mobile screenshots were inspected. The proof uses an independent
read-only loopback server and disposable browser, not the installed app. A
read-only `git merge-tree --write-tree HEAD origin/master` check at `ea7c695`
returned exit 0 with no textual conflicts; no branch merge or rollout occurred.

## Current Command Audit (2026-09-10)

Native Claude initialization advertises 46 commands. A local `/effort high`
control turn reports only `doctor`, `color`, and `reload-plugins` as terminal-only.
The full native definitions can now be reproduced with `--inventory`; names
alone are not treated as proof of working invocation. Reload plugins and skills
now map to existing public SDK controls from both typed slash commands and the
command picker, rather than submitting model input or disabling the supported
plugin action. The reload UI prevents overlapping reload operations and retains
the draft. Later slices add installation diagnostics and prompt color controls.
The diagnostic report does not stand in for the agentic setup-repair skill.

Commands and observed exits:

```sh
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude.py --local-effort --inventory
# exit 0: actual catalog and terminal-only list, model/mode/MCP controls,
# local effort acknowledgement with zero API duration/cost; process reaped
env SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-ts.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude
# exit 0: public SDK initialization, effort apply/clear, agents, skills/plugins
# reload; no inference/authentication; child reaped with exit 0
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py::test_command_catalog_and_session_switch_guard tests/test_workspace_pane.py::test_reload_commands_use_native_controls_without_model_input tests/test_workspace_pane.py::test_plugin_reload_is_explicit_refreshes_commands_and_reports_native_errors -q
# exit 0: 7 passed in 2.46s
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py tests/test_workspace_claude.py tests/test_workspace_pane.py scripts/verify-workspace-claude.py
# exit 0: All checks passed
node --check ui/static/workspace-pane.mjs
# exit 0
```

Integration baseline at `245791b`, before this reload mapping:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace*.py -q
# exit 2: collection required optional hjson, absent in the shared venv
env PYTHONPATH=apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace*.py -q
# exit 0: 606 passed, 10 skipped, 1 forkpty multithread warning, 112.74s
node --test tests/workspace-*.test.mjs
# exit 0: 94 passed, 1 Windows-only skip
```

This broad suite includes the deferred Gemini tests for regression detection; it
does not reinstate Gemini delivery scope. The passing suite does not establish
complete command parity, idle-runtime sleep policy, installed authentication,
default activation, or release. These remain separate delivery checks.

## Native Handoff Routing (2026-09-10)

The main app's Handoff action previously waited for a terminal WebSocket even
when the destination was a structured pane. Existing-session handoffs now travel
through an origin/source-checked iframe request and authenticated HTTP route,
then the exact owner's existing bridge queue. Explicit handoff uses ordinary
admission/lease checks to attach the destination; no session creation fallback
exists. Composer drafts are untouched. View removal only stops observing the
acknowledgement, not background delivery. Pending acknowledgements are reported
as pending, never as completed delivery. A new destination instead uses the
existing explicit Create and send flow with its briefing as seed context, keeping
the source title and pending linked-group metadata.

Verification:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q
# exit 0: 22 passed (both providers, authenticated route, rejection, exact target,
# unchanged draft/owner, no automatic launch)
node --test tests/workspace-handoff.test.mjs
# exit 0: 2 passed; actual app handoff function takes seeded creation, not paste
env SERENA_EVIDENCE_KIND=live CODEX_HOME=/tmp/serena-codex-browser-login-mltgygq4 /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --bridge
# exit 0: real authenticated HTTP handoff returned exact native session output;
# repeated request reused the same receipt; queue edit/cancel checks passed;
# isolated owned processes and temporary auth/history were cleaned up
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check ui/workspace_web.py tests/test_workspace_catalog.py tests/test_workspace_app.py scripts/verify-workspace-codex-roundtrip.py
# exit 0: All checks passed
node --check ui/static/workspace-page.mjs
# exit 0
```

This is source-branch verification, not installed-app delivery or complete parity.

Current user scope (2026-09-10): finish **Claude and Codex**; Gemini is deferred
at the user's explicit request. Historical three-provider requirements below
are retained as history, not a reason to block this two-provider delivery.

## Authenticated Claude Verification

### Saved Conversation Picker (2026-09-10)

Both providers now have a native saved-conversation picker, including `/resume`.
It reads authenticated, provider-filtered, paginated catalog rows with custom
titles and exact IDs. Search treats wildcard characters literally. Selection
navigates through the existing app conversation route (or the standalone pane
route); it does not submit text, disconnect the source, or start another owner.
Destination attachment still goes through normal admission and explicit resume.
The source draft remains stored, and closing the view does not cancel work.

Verification commands and results:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_workspace_pane.py::test_saved_session_picker_does_not_submit_or_stop_running_work tests/test_workspace_claude.py tests/test_workspace_app.py -q
# exit 0: 88 passed in 36.72s
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_workspace_pane.py::test_saved_session_picker_does_not_submit_or_stop_running_work -q
# exit 0: 23 passed in 4.23s after final dark search-field styling
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_catalog.py core/workspace_claude.py ui/workspace_web.py tests/test_workspace_catalog.py tests/test_workspace_pane.py
# exit 0: All checks passed
node --check ui/static/workspace-pane.mjs
# exit 0
node --check ui/static/workspace-page.mjs
# exit 0
```

An authenticated Flask runtime probe against the actual local index returned 50
Claude and 50 Codex rows, each with a next page, exit 0. It used an inert object
instead of an owner, so no provider or owner could launch. Browser checks covered
both providers at 390px and 1600px, paging/search/exact selection, `/resume`, draft
preservation, running-turn preservation and no unintended control calls. Mobile
screenshot inspection caught and corrected a white search field. No installed
app change or default activation is claimed.

### Subscription Roundtrip

2026-09-10: existing subscription authentication was used only in disposable
private proof storage; metered API fallback was stripped. Claude answered a real
first turn, resumed the exact saved identity through the TypeScript workspace
adapter, answered a second turn, and exposed native context/command output.
The real MCP tool form also traversed the public SDK and owner; its validated
answer reached the fixture tool and the parent turn completed. Cleanup removed
the isolated authentication/history and reaped owned processes.

Commands, final exits **0**:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_roundtrip_arguments.py -q
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-claude-roundtrip.py tests/test_workspace_roundtrip_arguments.py
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference --typescript-sdk runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs
SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-roundtrip.py --allow-inference --typescript-sdk runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs --mcp-form
```

The first Claude proof attempt exited **1** after a successful first response:
the relative SDK argument resolved under the isolated project and the worker
closed its pipe. The proof now resolves/validates it before authentication or
launch. The focused prerequisite regression test passed (**1 passed**), followed
by both successful full proof runs above.

## Authenticated Codex Verification

2026-09-10: Raghav completed normal browser OAuth in a separate private profile.
The prior refresh-token rejection is resolved for this proof profile; existing
user credentials and sessions were not overwritten. Authentication status alone
was not used as evidence: the actual workspace transport completed real turns.

Commands, exits **0**:

```sh
env SERENA_EVIDENCE_KIND=live CODEX_HOME=/tmp/serena-codex-browser-login-mltgygq4 codex login status
env SERENA_EVIDENCE_KIND=live CODEX_HOME=/tmp/serena-codex-browser-login-mltgygq4 /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference
env SERENA_EVIDENCE_KIND=live CODEX_HOME=/tmp/serena-codex-browser-login-mltgygq4 /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --permissions --compact --review --skills
env SERENA_EVIDENCE_KIND=live CODEX_HOME=/tmp/serena-codex-browser-login-mltgygq4 /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-roundtrip.py --allow-inference --bridge
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py -q
```

Observed: ChatGPT login; first response and exact persisted ID/history resume;
native background-task discovery; native skill invocation and steering on the
same active turn; permission request denied through the exact owner and resolved;
context compaction completed with owner ready; review completed inline on the
same persisted thread. The bridge proof also verified edited queued text reached
the exact native session with its original receipt, cancelled queued text never
became a native turn, the running turn was preserved, and duplicate bridge
requests reused their receipt. All three inference runs reaped owned processes and removed
their disposable auth/history copies. Focused tests: **108 passed in 8.38s**.

The profile path above is a historical local proof prerequisite, not packaged
configuration or a credential to ship. This does not migrate authentication into
the installed app, enable the feature by default, or establish full CLI parity.

## Current Windows Packaged Verification

2026-09-10: rebuilt Windows from synced source after verifying the pane JavaScript
and ACP controller SHA-256 match the laptop. Reused the ignored build directory
`windows-proof-5cadd13` (its name is historical; the executable was rebuilt now).
Build and both native proof commands exited **0**. No PC source edits, installed
app changes, user credentials, signed-in sessions or inference were involved.

The exact commands used are the existing Windows PyInstaller command and the
`verify-workspace-codex-windows.py --frozen` and `verify-workspace-claude-driver.mjs`
frozen Windows invocations recorded below under prior Windows proof entries;
their complete current executions and exit codes are recorded in this chat's
command events. The Codex runner was streamed from the laptop over stdin.

Codex verified gated ownership, exact resume, 50 recent plus one older turn,
native output/exit 0, file mentions, skill configuration, disconnect/reopen,
fork/indexing and unchanged ownership on history reads. Real Windows Electron
verified clipboard, native input/output, retained new-chat titles for both
providers, corrupt-creation refusal, sandbox/context isolation and background
owners surviving window closure. The screenshot was copied back and inspected.
Cleanup and isolated temporary-directory removal passed.

Claude verified queued exact UUID correlation, effort/agent controls, public SDK
transport, exclusive lease, native plugin/skill reloads, exact fork recovery,
same-process clear handoff and source-preserving forks. Frozen desktop/mobile
checks reported no page/console/HTTP errors or horizontal overflow. All isolated
processes were reaped. PyInstaller retained pycparser-table and OpenConsole DLL
warnings. These results do not establish authenticated inference or Gemini
admission, and do not authorize activating the replacement by default.

## Current Linux Packaged Verification

2026-09-10, source `7c2cb28`: rebuilt the Linux sidecar and exercised actual native
Codex/Claude sessions through the frozen HTTP backend and real Electron shell.
Each command below exited **0**. No installed app, user credentials, existing
chat, or live service was changed. These are local-command lifecycle proofs,
not authenticated model inference or a completed three-provider replacement.

From `apps/desktop`:

```sh
env PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar
```

Build and bundled capability-refusal smoke passed. PyInstaller emitted optional
dependency/library warnings (including tensorboard, pycparser tables, HIP and
Windows libraries); do not interpret this as a warning-free build.

From the worktree root:

```sh
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
env SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
```

Codex: 51 print-only turns, 50-plus-older history, exact resume, duplicate receipt
suppression, mentions, skill settings, disconnect/reopen and fork passed. Real
Electron main/preload verified sandbox/context isolation, Node integration off,
clipboard copy/multiline paste, Codex/Claude creation with retained titles,
corrupt-creation refusal and surviving owners after window close. Desktop Electron
and mobile frozen-pane screenshots were inspected. Cleanup reaped all isolated
owners and left the fixture project unchanged.

Claude: exact queued UUID completions, effort/agent acknowledgment, Python/SDK
transport, exclusive lease, reloads, history and fork recovery passed. Frozen
desktop/mobile checks reported no page/console/HTTP errors or horizontal overflow.
Clear handed off identity within the same process; fork left its source unchanged.
All isolated processes were reaped. Windows latest-source rebuild and authenticated
provider flows remain separate outstanding verification gates.

## Shared Image Viewer

2026-09-10: decoded history, assistant and tool images open in a native modal
with fit and actual-size views. Keyboard activation and Escape preserve the
draft and restore focus. The viewer reuses the bounded blob URL; source replacement
closes it before revocation, and pane disposal closes it without stopping a provider.
No remote image loading, duplicate blob creation or provider commands are added.

Commands executed separately:

```sh
env PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -k image -q --tb=short
node --check ui/static/workspace-pane.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py
```

Final exits **0**: **14 passed, 145 deselected**, syntax and Ruff clean. Initial
browser run exited **1**, **12 passed, 2 failed**, because the assertion inspected
naturalWidth before the modal image loaded; it now waits for decoding. Tests cover
Claude, Codex and Gemini user/assistant/tool images at 390px and 1600px, unsupported
images, keyboard opening, actual size, no document overflow, preserved draft,
focus restoration and source replacement cleanup. Mobile Codex and desktop Gemini
tool screenshots were visually inspected. These use controlled image content,
not authenticated provider-generated images or installed-app verification.

## Codex Pane Command Routing

2026-09-10: `/fork`, `/review`, `/mcp`, `/permissions` and `/skills` now invoke
the existing pane controls instead of becoming model prompts. The command picker
also exposes these actions and `/compact`, disabling unavailable controls.
Fork retains its confirmation dialog; drafts are preserved when opening dialogs.
Arguments, attachments and selected skills are refused for these control commands.
The existing `/compact` submission path retains provider completion handling and
clears its draft only after acknowledgment. This does not claim all CLI commands
are implemented, and Gemini/Claude command catalogs are unchanged.

Verification commands executed separately:

```sh
env PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q --tb=short
env PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -k 'compact_command or codex_local_commands or codex_picker or session_slash_commands or codex_unavailable_or_argument' -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py
node --check ui/static/workspace-pane.mjs
```

The initial full browser module exited **1**: **158 passed, 1 failed**, exposing
the compact-route overlap and a Claude-constructed fixture's hidden Codex button.
After preserving the original compact submission path and correcting that fixture,
the focused rerun exited **0**: **24 passed, 135 deselected**. Ruff and JavaScript
syntax check each exited **0**. Browser checks exercise actual controls and fork
confirmation at mobile/desktop widths with controlled provider callbacks, not
authenticated inference or installed-app rollout.

## Combined Regression Pass

At source revision `1aa5cb1`, the complete workspace test family was run together
after the Windows discovery, Gemini lifecycle/image, and Claude composer changes.
Commands were executed separately:

```sh
env PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages:apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace*.py -q --tb=short
node --test tests/workspace-*.test.mjs
```

Both exited **0**. Python/browser: **573 passed, 10 skipped**, 89.18 seconds.
One warning: the PTY/custom-session exclusivity test invokes `forkpty` in a
multithreaded process (Python deprecation warning); this is not a warning-free
run. JavaScript: **92 passed, 1 skipped**, no failures. This combines the current
transport, host, event, storage, receipt, control and browser regression coverage;
it does not substitute for authenticated provider flows or cross-platform live
proof. Skips are not counted as successful verification.

Current source still gates registration on `SERENA_STRUCTURED_WORKSPACE=1`
(`ui/web.py`) and includes only Codex and Claude in `WorkspaceHost`'s default
factories. Gemini default admission, full CLI-parity evidence and installed-app
migration remain delivery gates. No default was enabled, release built, or
installed app modified during this pass.

## Claude Composer Effort

Claude's existing session-effort dialog remains available. The model picker now
also preserves advertised effort levels rather than discarding them, allowing a
model and effort to be selected before sending the next message. Only explicit
model selections expose this composer control; unknown capabilities are not
guessed. Backend validation checks both selections before changing settings or
sending input. The TypeScript client uses `applyFlagSettings({effortLevel})` on
the same stream, and publishes the new effort only after acknowledgement. These
are session-scoped settings, not an automatic one-turn override or a settings
file write. Failed application leaves the draft unsent; a preceding acknowledged
model change remains reported honestly.

The pinned SDK declaration (`sdk.d.ts`, `applyFlagSettings` and `ModelInfo`)
provides the contract. A separate isolated initialization-only native probe exited
0 and exposed the installed command catalog without sending a prompt. This
reconfirmed that a command catalog is not proof that every CLI capability has
already been implemented.

Verification, each command separately, final exit **0**:

```sh
env PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py::test_advertised_model_selection_uses_existing_client tests/test_workspace_claude.py::test_advertised_effort_is_validated_before_settings_and_input tests/test_workspace_claude_client.py::test_compatibility_controls_and_native_records tests/test_workspace_pane.py::test_advertised_model_effort_selection_reaches_submit_and_header tests/test_workspace_pane.py::test_claude_effort_uses_native_command_without_consuming_draft -q --tb=short
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_client.py tests/test_workspace_claude.py tests/test_workspace_claude_client.py
node --check ui/static/workspace-pane.mjs
```

**7 tests passed**, including invalid/unsupported choices, acknowledgement failure,
exact-session submission, client control routing, mobile composer selection and
the unchanged session-effort dialog. The initial browser run exited 1 because its
locator excluded the now-correctly hidden selector after clearing the model;
the updated assertion explicitly verifies it is hidden and empty. Native proof
acknowledged flag settings and local effort commands, exact resume, queue receipts,
clear/fork and process cleanup, without inference or user credentials. This does
not prove model reasoning behavior or the full remaining parity matrix.

## Windows Electron Native Workflow

The full app proof found and fixed a real catalog mismatch: the Codex scanner
ignored `CODEX_HOME`, although the native runtime and fork registration honor it.
The scanner now uses that configured home, retaining the default for an empty or
unset variable. Regression tests import the scanner in a fresh process from the
tested checkout so installed editable packages cannot shadow it.

The existing Electron proof now runs on Windows without Xvfb, uses portable local
commands, and preserves supported clipboard contents (refusing unsupported
formats before writing). Its backend and proof descendants are job-owned for
cleanup. A read-only SQLite inspection connection is explicitly closed: a
transaction context alone left the database open on Windows. Cleanup success is
reported only after temporary profile deletion.

Verification commands, each executed separately:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_codex_scanner_resident.py tests/test_codex_scanner_skips_copies.py -q --tb=short
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -m pytest C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\tests\test_codex_scanner_resident.py C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\tests\test_codex_scanner_skips_copies.py -q --tb=short"
node --test apps/desktop/tests/shared-backend.test.js apps/desktop/tests/shell.test.js
node --check scripts/verify-workspace-electron.cjs
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_codex_scanner_resident.py scripts/verify-workspace-codex-windows.py scripts/verify-workspace-codex-history.py
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -B -m PyInstaller --noconfirm --distpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\dist --workpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\work C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\windows\sidecar-win.spec"
SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys; from pathlib import Path; r=Path(r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'); __file__=str(r/'scripts/verify-workspace-codex-windows.py'); os.environ['SERENA_PROOF_PYTHONPATH']=str(r/'apps/desktop/build/proof-tools/windows-python'); os.environ['SERENA_PROOF_BROWSER_EXECUTABLE']=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'; os.environ['SERENA_PROOF_ELECTRON']=str(r/'apps/desktop/build/proof-tools/windows-electron/node_modules/electron/dist/electron.exe'); os.environ['SERENA_PROOF_PLAYWRIGHT']=str(r/'apps/desktop/build/proof-tools/windows-python/playwright/driver/package'); sys.argv.extend(['--frozen',str(r/'apps/desktop/build/windows-proof-5cadd13/dist/serena-web-sidecar/serena-web-sidecar.exe')]); exec(compile(sys.stdin.read(),__file__,'exec'))\"" < scripts/verify-workspace-codex-windows.py
```

Final results: all commands above exited **0**. Scanner tests: **10 passed** on
Linux and **10 passed** on Windows. Shell tests: **18 passed**. Syntax and scoped
Ruff checks passed. PyInstaller rebuilt the isolated executable, retaining its
existing pycparser/OpenConsole dependency warnings. Scanner Ruff itself has three
pre-existing findings (F401, UP035, SIM108); checking the HEAD version separately
also exited 1 with the same three findings, so this is not a clean-file lint claim.

The live run verified 51 native local-command history turns, pagination, exact
resume, desktop/mobile browser flows, forks, real Electron main/preload,
sandbox/context isolation, native shell input/output, skill discovery, multiline
clipboard paste and native-output copy, and new Codex/Claude chats with preserved
titles and indexing. Corrupt creation receipts refused submission. Closing the
shell retained all existing owners plus exactly one new owner for each provider.
Temporary profile deletion completed. No credentials or inference were used.
The Windows Electron screenshot `apps/desktop/build/workspace-proof/electron-native-workspace.png`
was inspected: native output and composer render inside the app shell.

Earlier attempts exited 1: catalog readiness failed before the scanner fix;
then all UI assertions passed but the proof's SQLite handle blocked cleanup.
The first Windows scanner-test invocation also exposed test import shadowing,
fixed by setting the child process cwd. These failures were not counted as passes.
This is an isolated development Electron shell with a frozen backend, not an
installed-app release or evidence of authenticated model-turn/Gemini parity.

## Windows Codex Browser Workflow

Extended the Windows native proof with the existing browser workflow, using
installed Edge against the real source workspace routes and native Codex owner.
Only browser executable discovery and the print-only shell command were made
portable; no mock provider or replay-only substitute was used.

```sh
SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys; __file__=r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\scripts\verify-workspace-codex-windows.py'; os.environ['SERENA_PROOF_PYTHONPATH']=r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\proof-tools\windows-python'; os.environ['SERENA_PROOF_BROWSER_EXECUTABLE']=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'; sys.argv.append('--browser'); exec(compile(sys.stdin.read(),__file__,'exec'))\"" < scripts/verify-workspace-codex-windows.py
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py -k 'history or pagination' -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-windows.py scripts/verify-workspace-codex-history.py
```

All exited 0. Native/browser proof: 51 seeded local-command turns, exact resume,
pagination, file picker and inline mentions without sending, persistent skill
disable/re-enable with draft retained, shell output, and explicit disconnect then
same-session resume passed at 1440px and 390px. Cancelling disconnect and closing
the page preserved the owner. No browser errors or horizontal overflow. Mobile
screenshot `apps/desktop/build/workspace-proof/codex-native-mobile.png` inspected:
wrapped commands and controls remain contained. No credentials or inference used.
Focused tests: 4 passed, 45 deselected. This is a source-backed browser proof,
not the Windows Electron shell or a release.

## Native Windows Codex Ownership

The proof now persists 51 print-only turns and checks actual native pagination:
resume returns the newest 50 turns without the oldest marker; explicit history
loading returns exactly the oldest turn and exhausts the cursor. The owning PID
stays unchanged, and a subsequent native command completes with exit 0. The live
command below was rerun and exited 0 with `recent_turns: 50`, `older_turns: 1`,
`history_kept_owner: true`, `cleanup: true`, and `inference: false`.
`/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py -k 'history or pagination' -q --tb=short`
exited 0 (4 passed, 45 deselected). Ruff also exited 0. This is native adapter
pagination evidence, not a Windows Electron UI proof.

Verified the installed npm Codex launcher through the actual Windows gated RPC
path. An isolated profile starts with an empty catalog, persists a print-only
shell command, closes the original runtime, resumes that exact session through
`CodexWorkspace`, and runs a second shell command with real output and exit 0.
The resumed history contains the first command. Both runtimes are closed before
temporary profile deletion; no inference or user credentials are involved.

The initial probe exited 1 because its new CODEX_HOME directory was missing.
Creating that isolated directory fixed the fixture; no runtime change was needed.
Source was sent over stdin for execution while Syncthing caught up, not written
to the PC checkout. Final command, exit 0:

```sh
SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import sys; __file__=r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\scripts\verify-workspace-codex-windows.py'; exec(compile(sys.stdin.read(),__file__,'exec'))\"" < scripts/verify-workspace-codex-windows.py
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_rpc.py -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-windows.py
```

Observed JSON: gated startup, empty isolated catalog, exact resume and cleanup
true; native command exit code 0; inference false. RPC tests exited 0 (7 passed,
1 Windows skip), Ruff exited 0. Windows Electron UI interaction with this Codex
session and authenticated model turns are still separate unverified gates.

## Damaged Browser Receipt Handling

Malformed JSON or invalid map shapes in session storage no longer crash connection
construction. Pending-command signatures and IDs are validated before reuse;
upload, clear and fork records are also checked. A failure latches a command
refusal without clearing or rewriting saved bytes. Explicit attachment and event
replay still work, with a visible warning. Uploading, new commands and receipt
cleanup remain blocked so damaged state cannot manufacture a fresh retry identity.
This is honest viewing-only fallback, not automatic receipt repair.

Verification, all exit 0:

```sh
node --test tests/workspace-connection.test.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -k corrupt_receipts -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_app.py
env SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py
```

Node: 38 passed. Browser: 2 passed, 13 deselected at desktop/mobile widths;
malformed storage retained exactly, no page exception, no command/upload delivery
and no automatic attachment. Native Linux source proof: real session commands,
queued inputs, skills/plugins, file mentions, fork/reload recovery after catalog
failure, clear handoff and process cleanup passed without inference. Healthy
receipt recovery remains functional. These changes are not yet in a rebuilt
Windows package or an installed release.

## Packaged Windows Browser Pass

Rebuilt the Windows onedir sidecar from 5cadd13 plus the favicon fix below. The
runtime was tested with an isolated Electron 43.1.1 Node-mode worker, installed
Claude 2.1.260, and headless Edge. No installed Serena instance was replaced.

The verifier now uses a Windows Job for server teardown, explicit browser
discovery, and the lease's PID/birth identity plus parent chain for persistence
checks. Short-lived helper processes are not mistaken for lost owners. Earlier
attempts exited 1 at transient-helper checks and isolated Edge discovery; those
were verifier defects. The next attempt exercised the desktop workflow but
exited 1 on a browser 404. Workspace pages now reference the existing Serena
favicon instead of causing Edge's missing `/favicon.ico` fallback request.

Build (both initial and favicon rebuild exited 0, about 56 seconds):

```sh
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -B -m PyInstaller --noconfirm --distpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\dist --workpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\work C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\windows\sidecar-win.spec"
SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\scripts\verify-workspace-frozen-windows.py C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\dist\serena-web-sidecar\serena-web-sidecar.exe"
```

The initial frozen gate proof exited 0: no gate refused launch (child exit 2),
echo returned exact bytes (child exit 0), descendant test observed two remaining
processes then reaped all (child exit 0). Provider started: false.

`/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -k app_route_bootstrap -q --tb=short`
exited 0 (2 passed, 11 deselected). Initial assertions were inserted before Flask
route registration and failed both cases; moving them into the existing response
checks fixed the fixture. Ruff exited 0 for the verifier, page module and test.

Final packaged browser command, exit 0:

```sh
SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys,subprocess; from pathlib import Path; r=Path(r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'); env=dict(os.environ,SERENA_PROOF_PYTHONPATH=str(r/'apps/desktop/build/proof-tools/windows-python'),SERENA_PROOF_BROWSER_CHANNEL='msedge',SERENA_PROOF_BROWSER_EXECUTABLE=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'); args=['node',str(r/'scripts/verify-workspace-claude-driver.mjs'),str(r/'runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs'),r'C:\Users\ragha\.local\bin\claude.exe',sys.executable,'',str(r/'apps/desktop/build/proof-tools/windows-electron/node_modules/electron/dist/electron.exe'),str(r/'apps/desktop/build/windows-proof-5cadd13/dist/serena-web-sidecar/serena-web-sidecar.exe')]; sys.exit(subprocess.run(args,env=env).returncode)\""
```

Observed at 1440x1000 and 390x844: exact native attach and command completion,
explicit skill/plugin reload, project file picker and inline mentions, reload and
view-close owner retention, zero console/page/HTTP errors, no horizontal overflow.
Windows screenshots under `apps/desktop/build/workspace-proof/frozen-*.png` were
inspected: readable wrapping and no overlapping controls. Process/profile cleanup
also passed. This proves the packaged backend and real browser pane, not the
installed Electron window, model inference, or full provider parity.

## Receiptless Native Clear Handoff

The single-in-flight compatibility path now includes `/clear`. It attributes a
receiptless completion only after the dedicated clear input was delivered. It
still rejects failed results, unchanged or conflicting session IDs and explicit
wrong receipts. New input remains blocked until the owning layer commits the
handoff; no process replacement occurs. Attributed records carry the same
`workspaceReceiptSource` marker as ordinary receiptless completions.

`node --test tests/workspace-claude-sdk.test.mjs` exited 0 on Linux
(24 passed, 1 Windows skip). The exact Windows equivalent,
`ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "node --test C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\tests\workspace-claude-sdk.test.mjs"`,
exited 0 (25 passed). Python transport/owner regressions:
`/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_transport.py tests/test_workspace_claude.py -q --tb=short`
exited 0 (67 passed, 1 Windows skip). The native proof commands in the next section
now additionally clear the isolated fork, commit its new identity, and submit a
local command through the same process. Linux and Windows both exited 0,
including cleanup. No inference, installed application changes or user session
mutation occurs.

## Older Claude Completion Compatibility

An actual isolated Windows Claude 2.1.260 run exposed a missing receipt contract:
its local-command results omit `user_message_uuid` and `user_message_uuids`.
The first native Windows proof exited 1 at its 90-second deadline; a bounded
diagnostic reproduced three uncleared inputs despite three received results.

The SDK boundary now holds queued messages until the first delivered input
completes. A matching explicit receipt enables concurrent delivery. Without
native receipts, only one input is delivered at a time through the same process;
the result is correlated to that unique in-flight input, marked
`workspaceReceiptSource: single-inflight`. This is adapter correlation, not a
claim that the CLI supplied a receipt. A receiptless result for multiple in-flight
inputs fails closed. Queued messages remain ordered and no second owner is
spawned. Native clear still requires a successful new-identity handoff; the
receiptless compatibility extension is documented above.

The proof environment preserves Windows system executables but isolates profile,
AppData and temporary paths, excluding inherited credentials and Node injection
settings. Windows profile cleanup hit EBUSY after CLI reaping, including when
attempted from a parent after the SDK process exited. A later standalone Node
cleanup succeeded (exit 0), so the unhelpful parent wrapper was removed. Cleanup
now allows 20 bounded retries with 250ms linear backoff. This changes the verifier,
not application cleanup policy; the external lock holder is not identified.

Scoped commands and results:

```sh
node --test tests/workspace-claude-sdk.test.mjs tests/workspace-proof-env.test.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_claude_transport.py -q --tb=short
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "node --test C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\tests\workspace-claude-sdk.test.mjs C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\tests\workspace-proof-env.test.mjs"
env SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python
SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "node C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\scripts\verify-workspace-claude-driver.mjs C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\runtimes\claude-sdk\node_modules\@anthropic-ai\claude-agent-sdk\sdk.mjs C:\Users\ragha\.local\bin\claude.exe C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe"
```

Linux Node tests exited 0 (24 passed, 1 Windows skip); Python exited 0
(67 passed, 1 Windows skip). Windows Node tests exited 0 (25 passed).
The final Linux and Windows native proofs both exited 0. Windows exercised the
receiptless compatibility path with the installed Claude 2.1.260: exact session
resume, two queued inputs, actual worker and CLI PID, Python transport and lease
duplicate rejection, skill/plugin reload, fork checkpoint recovery, history/model
catalogs, and final profile removal. Earlier post-fix Windows runs exited 1 solely
at profile cleanup; the final bounded-cleanup run passed. No model inference or
user credentials were used. This is not an installed Electron end-to-end check.

## Claude Gated Windows Process Identity

The Windows bootstrap adds one process level above the Node worker. Claude's
transport still required the CLI to be a direct child of WorkspaceRpc's process,
so it would reject a valid Windows launch. WorkspaceRpc now exposes its active
gated state, and Claude checks the exact bootstrap -> worker -> CLI chain in that
case. Ungated transport retains the original direct-parent requirement. Unrelated
grandparents remain rejected. No arbitrary-depth descendant admission is added.

Verification commands:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_transport.py tests/test_workspace_rpc.py -q --tb=short
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys; os.chdir(r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'); sys.path.insert(0,os.getcwd()); sys.dont_write_bytecode=True; import pytest; sys.exit(pytest.main(['tests/test_workspace_claude_transport.py','tests/test_workspace_rpc.py','-q','-p','no:cacheprovider','--tb=short']))\""
env SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_transport.py core/workspace_rpc.py tests/test_workspace_claude_transport.py
```

Linux scoped tests exited 0: 26 passed, 2 Windows skips in 2.36s. Windows initially
exited 1 (27 passed, 1 failed): the Linux environment simulation retained Windows'
path separator. After explicitly simulating the Linux separator, Windows exited
0: 28 passed in 3.48s. The Windows-only test uses real gated Python processes
speaking the worker protocol, not a Claude provider; native PID admission and
cleanup both passed. Ruff exited 0.

The native Linux proof exited 0: exact persisted-session resume, queued input
UUID acknowledgement, native input/output, shared-lease duplicate rejection,
skill/plugin reload, fork recovery and process cleanup all passed. It used local
commands with zero inference and no user credentials. Native Windows SDK
inference and a rebuilt packaged provider flow remain outstanding. No installed
application was changed or activated.

## Windows Edge Pane Verification

The full pane contract file now runs against installed Edge on Windows using
`SERENA_PROOF_BROWSER_CHANNEL=msedge`. The fixture shares a browser process but
creates and closes a fresh context for every case, preserving storage isolation.
The provider badge now uses X for Codex rather than the same C as Claude, matching
the approved mockup. Browser automation dependencies were installed only into the
ignored proof-tools directory, not the shared Python environment.

Exact verification commands:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_provider_badges_distinguish_linked_panes tests/test_workspace_pane.py::test_pasted_image_drop_preview_and_mobile_cleanup tests/test_workspace_pane.py::test_markdown_code_copy_and_mobile_layout -q --tb=short
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys; os.chdir(r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'); sys.path[:0]=[os.getcwd(),os.path.join(os.getcwd(),'apps','desktop','build','proof-tools','windows-python')]; sys.dont_write_bytecode=True; os.environ['SERENA_PROOF_BROWSER_CHANNEL']='msedge'; import pytest; sys.exit(pytest.main(['tests/test_workspace_pane.py','-q','-p','no:cacheprovider','--tb=short','--basetemp=apps/desktop/build/windows-pane-proof-contexts']))\""
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py
```

Final exits all 0: Linux 3 passed in 1.18s; Windows 137 passed in 55.60s. Test and
renderer hashes matched both machines before execution. Inspected actual Windows
390px and 1600px screenshots: badges, queued reply markers, composer and content
fit without overlap. Coverage includes uploads, typed approvals, model controls,
commands, queue recovery, rendering, draft preservation and scroll behavior.

The initial per-test-browser Windows run showed one setup error and slow progress;
it was deliberately stopped (exit 1), after identifying its exact Python parent
PID, and its owned browser tree was terminated. It was not counted as passing.
The revised context-isolated run completed all cases. Playwright 1.58.0 was
installed in `apps/desktop/build/proof-tools/windows-python`; no browser install
or app update was performed. This is controlled Edge UI verification, not Windows
Electron OS clipboard integration or native provider inference.

## Atomic Browser Receipt Cleanup

After a server accepted a message, a failure writing cleaned-up sessionStorage
could previously delete the request ID from memory while leaving it on disk.
Retrying in the same pane then created a new request ID and risked another turn.
Cleanup now persists a replacement pending map before swapping the in-memory map.
Failed cleanup retains the original receipt in both places. The same operation
is used after retryable refusals. No server receipt or provider behavior changes.

Verification:

```sh
node --test --test-reporter=dot tests/workspace-connection.test.mjs
node --check ui/static/workspace-connection.mjs
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "node --test --test-reporter=dot C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\tests\workspace-connection.test.mjs"
```

All exit 0; 29 connection tests passed on Linux and Windows, with matching source
and test hashes before Windows execution. New cases inject cleanup-storage failure
after acceptance for submit and queue_input, with and without view recreation.
They verify a receipt-deduplicating server executes once across recovery, and an
explicit subsequent new send gets a new identity after successful cleanup.
This is controlled transport evidence, not authenticated provider inference.

## Combined Reply Visibility

The pane now marks a completed queued input as `Included in combined reply` when
the provider's completion links it to another known turn. It does not fabricate a
second reply or duration. Self/missing turn references do not create the marker.

Final commands, exit 0:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_grouped_reply_marks_queued_input_without_duplicate_duration -q --tb=short
node --test --test-reporter=dot tests/workspace-events.test.mjs
node --check ui/static/workspace-pane.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys; os.chdir(r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'); sys.path.insert(0,os.getcwd()); sys.dont_write_bytecode=True; import pytest; sys.exit(pytest.main(['tests/test_workspace_host.py','-q','-p','no:cacheprovider','--tb=short']))\""
```

Browser: 2 passed in 1.25s, 390px and 1600px screenshots inspected, no page errors,
overflow, duplicate input or automatic controls. Initial browser assertions raced
the scheduled render (exit 1); changed to Playwright's waiting text assertion.
Event reducer: 12 passed. Windows host contracts: 58 passed, 1 skipped in 10.71s.
These are controlled browser/session contracts, not authenticated inference or
installed desktop activation. Full replacement remains in progress.

## Claude SDK Windows Casing

Claude's JS resume boundary now accepts alternate Windows casing only when both
paths resolve to the same real directory. It retains the admitted cwd and exact
session ID, refuses unrelated paths before filesystem lookup, and preserves the
post-lookup cancellation check. A real Windows temporary-directory test covers
the casing variant; existing wrong-project and duplicate-spawn checks remain.

Commands, all exit 0:

```sh
node --test --test-reporter=dot tests/workspace-claude-sdk.test.mjs
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "node --test --test-reporter=dot C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\tests\workspace-claude-sdk.test.mjs"
env SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python
```

Windows: 21 tests passed, source hashes matched before the run. Linux: 20 passed
and the Windows-only case skipped. Native Linux proof passed exact-session resume,
queued local-command UUIDs, SDK controls, skill/plugin reload, fork recovery and
process cleanup. It used no user authentication, sessions/settings or inference;
all isolated processes were reaped. Native Windows provider inference and the full
Electron replacement remain unverified. Existing frozen builds predate this edit.

## Windows Provider Project Identity

The first actual Windows Codex/Claude owner run found 18 failures (107 passed).
Most fixtures assumed their temporary path retained exact casing after resolve;
Codex also used raw string equality on returned project roots, rejecting native
responses with equivalent Windows casing. Fixtures now use resolved input paths.
Codex creation, fork, skill catalogs and file-search roots verify matching admitted
path spelling plus filesystem identity. Unrelated paths are rejected before any
filesystem lookup; relative, missing and malformed paths remain rejected.

Verification commands:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_claude.py tests/test_workspace_claude_wire.py -q --tb=short
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys; os.chdir(r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'); sys.path.insert(0,os.getcwd()); sys.dont_write_bytecode=True; import pytest; sys.exit(pytest.main(['tests/test_workspace_codex.py','tests/test_workspace_claude.py','tests/test_workspace_claude_wire.py','-q','-p','no:cacheprovider','--tb=short']))\""
env SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -B C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\scripts\verify-workspace-windows.py"
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py tests/test_workspace_codex.py tests/test_workspace_claude.py scripts/verify-workspace-windows.py
```

Linux final tests: exit 0, 127 passed in 0.56s. Windows final tests: exit 0,
127 passed. Runtime/test hashes matched both machines before the run. Live proof
exit 0: directory_identity true, bidirectional true, owned_processes 3, closed
true, provider_started false. Ruff final exit 0 (an initial import-order failure
was corrected). The proof verifies real Windows directory identity with swapped
casing and rejects a different directory without opening the Codex owner. No AI
provider or user session starts. The previously frozen binary predates this fix.

## Frozen Windows Gate Verification

Built the actual `console=False` onedir sidecar on Windows 11 / Python 3.13.7 /
PyInstaller 6.21.0, from runtime commit fd4283f. The safe frozen probe exercises
that executable's `--workspace-child` dispatch with inherited pipes and a real
Windows job. It checks EOF before authorization, exact echo input/output, and
termination of surviving descendants. It starts no AI provider or Flask listener.
`build-win.ps1` now runs this probe and checks its exit before installer packaging.

Exact commands:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -B -m PyInstaller --noconfirm --distpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-fd4283f\dist --workpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-fd4283f\work C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\windows\sidecar-win.spec"
env SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -B C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\scripts\verify-workspace-frozen-windows.py C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-fd4283f\dist\serena-web-sidecar\serena-web-sidecar.exe"
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest apps/desktop/windows/test_windows_packaging.py::test_build_checks_frozen_workspace_before_packaging -q
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-frozen-windows.py
```

Build exit 0, 85.5s. Warnings included pycparser generated tables and OpenConsole
UI Automation DLL names; this gate proof does not establish PTY behavior.
Live proof exit 0: no-gate child exit 2 (expected), echo exit 0, descendant exit 0,
all jobs confirmed empty after cleanup, provider_started false. Probe source hash
matched both machines before invocation. Static build guard: final exit 0, 1 test
passed in 0.02s (initial red test exited 1 before the build-script edit). Ruff exit 0.

This closes the frozen bootstrap/stdio verification gap, not Windows Electron
visual QA, native provider inference, or a frozen host's complete session workflow.
No installation, publishing, release, running-user-session changes, or host restart.

## Windows Gated Transport Integration

WorkspaceRpc now launches a minimal Windows bootstrap, assigns it to its owned
job, and only then supplies the provider command. Source launches use the base
Python executable with isolated/no-site startup (not a venv redirector). The
bootstrap consumes exactly one gate line and passes subsequent stdin/stdout to
the child without buffering provider input. The Windows sidecar exposes an early
`--workspace-child` dispatch that restores inherited pipes without starting Flask.

Explicit shutdown terminates the job, waits for zero active processes, and only
then drops ownership. Inherited output pipes cannot indefinitely preserve the
old shutdown wait. Assignment failure kills the bootstrap without running any
provider code. Renderer disconnect behavior is unchanged. This supersedes the
earlier Windows transport/primitive integration gaps below, but frozen Windows
binary execution and provider-level integration remain unverified.

Final verification commands (all exit 0):

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_rpc.py tests/test_workspace_lease.py tests/test_workspace_windows_job.py tests/test_workspace_windows_bootstrap.py -q --tb=short
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys; os.chdir(r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'); sys.path.insert(0,os.getcwd()); sys.dont_write_bytecode=True; import pytest; sys.exit(pytest.main(['tests/test_workspace_rpc.py','tests/test_workspace_lease.py','tests/test_workspace_windows_job.py','tests/test_workspace_windows_bootstrap.py','-q','-p','no:cacheprovider','--tb=short']))\""
env SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -B C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\scripts\verify-workspace-windows.py"
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_rpc.py core/workspace_windows_bootstrap.py tests/test_workspace_rpc.py tests/test_workspace_windows_bootstrap.py scripts/verify-workspace-windows.py
```

Linux: 22 passed, 9 platform skips in 2.98s. Windows: 26 passed, 5 platform skips
in 4.52s. Final transport SHA256 matched both machines before testing. The live
proof returned win32, bidirectional true, owned_processes 3, closed true,
provider_started false. Its first invocation exited 2 before the script synced;
after confirming the file hash, the same proof exited 0. No provider, installed
app, release, user session, or background job was started or modified.

## Windows Job Ownership Primitive

`core/workspace_windows_job.py` wraps unnamed, non-inheritable Windows jobs with
kill-on-close and no breakaway flags. It validates child PID input, propagates
Win32 errors, exposes active process accounting, and supports explicit tree
termination and idempotent handle closure. It is not yet wired into WorkspaceRpc:
the launcher must first gate provider execution until assignment succeeds. A
post-spawn assignment alone would let tools escape before ownership is established.

Primary evidence accessed 2026-09-10:
- https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
- https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject
- https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information
- https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812

Verification commands:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_windows_job.py -q
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_windows_job.py tests/test_workspace_windows_job.py
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys; os.chdir(r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'); sys.path.insert(0,os.getcwd()); sys.dont_write_bytecode=True; import pytest; sys.exit(pytest.main(['tests/test_workspace_windows_job.py','-q','-p','no:cacheprovider','--tb=short']))\""
env SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys,json; os.chdir(r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'); sys.path.insert(0,os.getcwd()); sys.dont_write_bytecode=True; from core.workspace_windows_job import WindowsJob; job=WindowsJob(); active=job.active_processes(); job.terminate(); job.close(); print(json.dumps({'platform':sys.platform,'active_processes':active,'closed':job.handle is None})); assert active==0 and job.handle is None\""
```

Linux: exit 0, 1 passed / 3 Windows skips. Ruff: exit 0. Windows final tests:
exit 0, 3 passed / 1 Linux skip in 2.96s. Tests use a gated base Python process,
not the venv redirector; they verify a live descendant survives leader exit and
then dies on either terminate or close. Final source hashes matched on both
machines before the Windows run. Initial remote attempts exited 4 before sync,
then 1 using the older venv-launcher fixture; neither counted as passing evidence.
Live proof: exit 0, win32, active_processes 0, closed true. No provider launched,
user session modified, runtime activated, or installed app changed.

## Windows Transport Verification

Ran the synced source tests on RaghavsGamingPC through SSH, without editing
source files on Windows. The Claude fork test incorrectly expected `/project`
instead of the platform-resolved path; its assertion now uses `path.resolve`.
Production routing already resolves the directory and did not need changing.

Commands and observed results:

```sh
node --test --test-reporter=dot tests/workspace-connection.test.mjs tests/workspace-events.test.mjs tests/workspace-claude-sdk.test.mjs
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "node --test --test-reporter=dot C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\tests\workspace-connection.test.mjs C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\tests\workspace-events.test.mjs C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\tests\workspace-claude-sdk.test.mjs"
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -c \"import os,sys; os.chdir(r'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'); sys.path.insert(0,os.getcwd()); sys.dont_write_bytecode=True; import pytest; sys.exit(pytest.main(['tests/test_workspace_rpc.py','tests/test_workspace_lease.py','-q','-p','no:cacheprovider','--tb=short']))\""
```

Both final JavaScript runs exited 0 (57 tests each). Windows Python exited 0:
15 passed, 6 skipped in 6.20s. The skipped tests require POSIX process groups.
The initial Windows JavaScript run exited 1 on the path assertion; an initial
Python invocation from the remote home directory exited 2 on module imports,
corrected by explicitly selecting the synced worktree and import path above.

This verifies actual Windows pipe I/O, approval routing, receipt handling and
file locks, not native provider inference or packaged desktop behavior. Windows
descendant containment remains unimplemented: transport shutdown currently
terminates only the leader on Windows. Do not treat the POSIX skips as coverage
of that gap or enable the replacement by default on this evidence.

## Explicit Queued Receipt Recovery

Unconfirmed queued messages now have a recovery control in the Claude pane.
It previews the original text and attachment count, then retries only the saved
request ID/payload when explicitly clicked. Opening the dialog does not submit.
No attachment re-upload or replacement prompt is needed, and an already resolved
ID cannot create a new request. A newer draft is retained; an unchanged text-only
draft clears on confirmation. Disposed panes cannot clear a reopened view's draft.

Verification, each exit 0:

```sh
node --test tests/workspace-connection.test.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_explicit_queue_recovery_preserves_newer_draft tests/test_workspace_pane.py::test_disposed_queue_recovery_does_not_clear_reopened_draft -q --tb=short
node --check ui/static/workspace-pane.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py
```

25 connection tests and 9 browser tests passed. Browser cases cover 390px/1600px,
confirmation/failure, unchanged/newer drafts, explicit-only dispatch and late
completion after disposal. Inspected the 390px recovery screenshot. This is
source browser/transport verification; no provider launched or installed app
changed. Multi-input cancellation and native crash reconciliation remain open.

## Claude Busy Composer Queue

The Claude pane now offers Queue message while running. Text and session-bound
uploads go through the existing owner to the native SDK input stream with a
separate UUID, without another process or changing the running model. The
displayed oldest active turn is required at admission; a stale turn is rejected
without falling back to new submission. Stop targets that same oldest active
turn even when later inputs are accepted. Native cancellation semantics for
multiple model turns remain to be verified; no cancellation outcome is invented.

Host receipts deduplicate queued delivery. Browser retries after response loss
retain the original queue receipt even if the pane has become idle or the active
turn has advanced. Different input cannot bypass an unresolved queue receipt.
Confirmed pre-admission stale rejection is retryable; ambiguous native delivery
is not. Completing an older input does not clear uncertainty for a later input.
Drafts/files remain after failure and uploads are reused, not repeated.

Verification commands, each exit 0:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_claude_wire.py tests/test_workspace_host.py tests/test_workspace_pane.py -q --tb=short
node --test tests/workspace-connection.test.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_claude_wire.py -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_stop_with_queued_claude_inputs_targets_oldest_active_turn -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_host.py tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-claude-transport.py
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python
```

Results: combined regression run 261 passed in 86.87s; connection 24 passed;
final uncertainty guard rerun 78 passed; Stop browser test 1 passed; lint clean.
Mobile/desktop queue controls, preserved failed drafts, image input, exact host
receipt routing, uploads and mode-crossing lost-response recovery are covered.
The native proof queued a local follow-up through the Python owner while an
earlier native completion was deliberately held at the publication boundary;
both exact UUIDs completed in the same PID with zero inference. Existing native
history, skills/plugins, fork and cleanup checks also passed.

This enables the source feature within the opt-in workspace, not the installed
app. True mid-inference behavior, multi-input cancellation, queued-session crash
reconciliation and user-facing recovery of edited ambiguous drafts remain open.
The frozen backend predates this change. Full delivery is not complete.

## Queued Turn Accounting Foundation

Claude submission now registers its input with the event translator rather than
overwriting a lone turn field. The translator retains ordered pending identities,
routes an echoed queued user message to its own turn, and advances only through
the acknowledged prefix. Grouped results emit one reply and complete the covered
inputs; secondary completions reference the primary turn without copying its
duration. Unknown, out-of-order or missing acknowledgements with multiple inputs
fail without dropping the pending list. Legacy single-input results still work.

The owner derives its active/ready state from the remaining input tracker. The
browser also remains running while another accepted turn is in progress, and
normalizes omitted start/completion status fields so it cannot become stuck busy.

Verification commands (each exit 0):

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_wire.py tests/test_workspace_claude.py -q --tb=short
node --test tests/workspace-events.test.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_questions_resolve_only_from_provider_and_stream_does_not_collapse_tools tests/test_workspace_pane.py::test_acp_stream_deltas_render_exactly_before_and_after_completion -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_pending_turn_keeps_claude_busy_after_another_turn_completes -q --tb=short
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_events.py tests/test_workspace_claude_wire.py
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python
```

Results: 76 Python tests, 12 reducer tests, 2 browser regressions and 1 new busy
state browser test passed; lint clean. Native driver/Python owner proof passed,
including two queued local commands with exact UUID acknowledgements, existing
single-input rendering and cleanup. No authentication or model inference.

This is the accounting foundation, not an enabled user queue: the public Claude
submit guard still requires ready state. Explicit queued-submit admission,
composer controls, ambiguous-delivery/restart recovery and mid-inference proof
remain required. No installed app/default activation was changed.

## Claude Queued Input Identity

Rechecked https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode
on 2026-09-09: persistent streaming input supports sequential queued messages.
The production TypeScript driver accepts multiple inputs, but the Python owner
still admits one active turn, so this is not yet enabled in the composer.

Extended the native driver proof to send two local `/effort` commands before
awaiting either response. The installed CLI returned two results acknowledging
their exact UUIDs in the same process/session, with zero model turns and zero
cost. This proves native queue transport for local commands, not mid-inference
steering or model-turn grouping. Driver regressions also cover grouped
acknowledgements and ensure unrelated results cannot clear pending inputs.

The shared Claude event translator now refuses a result whose provided input
acknowledgements do not include the active turn. A delayed old result therefore
cannot silently complete a newer input. Legacy records without these fields
retain their existing path. The owner handles this protocol error as unavailable.

Commands, each exit 0:

```sh
node --test tests/workspace-claude-sdk.test.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_wire.py tests/test_workspace_claude.py -q --tb=short
node --check scripts/verify-workspace-claude-driver.mjs
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_events.py tests/test_workspace_claude_wire.py
SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python
```

Results: 20 JavaScript tests passed; 71 Python tests passed; syntax/lint clean.
Native proof also passed Python owner conversion, exact resume, lease exclusion,
plugin/skill controls, fork recovery and cleanup. No user credentials or sessions
were used. Full queued-turn state/rendering and mid-inference verification remain
required before enabling Claude follow-up input while running. No installed app
change; the frozen backend predates this latest result-identity guard.

## Frozen Backend Refresh After Gemini Controls

Source through `849092c`, plus the Linux packaging fix below, was rebuilt and
verified with the isolated Electron shell. Linux now explicitly includes
`core.workspace_gemini` even though default admission is disabled, and checks
that `hjson` imports before replacing build output. Windows already installs
the project dependencies and collects core submodules; Windows execution was
not performed in this pass.

Commands and results:

- Repository root: `node --test apps/desktop/tests/shell.test.js`: exit 0,
  10 passed, including the new hidden-import/dependency preflight regression.
- From `apps/desktop`: `env PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar`: exit 0. Build completed in approximately 87s, followed by the bundled peer capability-refusal smoke. No Fleet workers were launched. Optional-library warnings remain.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: exit 0. Native 51-turn history/paging, desktop/mobile file mentions and skills, exact disconnect/resume, fork indexing, actual Electron clipboard copy/multiline paste, Codex/Claude new-chat titles and corrupt-creation refusal all passed. Shell close preserved owners; final isolated cleanup reaped them. No credentials or inference used.

Static inspection of the executable's embedded PYZ confirmed presence of
`core.workspace_gemini`, `core.workspace_acp`, `core.workspace_acp_session`,
`core.workspace_acp_events`, `hjson`, `hjson.decoder` and `hjson.scanner`.
Inspected regenerated `apps/desktop/build/workspace-proof/electron-native-workspace.png`.
These are packaged-backend checks, not an installed AppImage/Windows test,
authenticated Gemini proof or rollout. The full delivery contract stays open.

## Integrated Verification, 2026-09-09

Verified source through `a17ba35`, then rebuilt the frozen Linux backend. No
installed-app replacement, default activation, release or user-host restart.

- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp.py tests/test_workspace_acp_events.py tests/test_workspace_acp_session.py tests/test_workspace_gemini.py tests/test_workspace_host.py tests/test_workspace_uploads.py tests/test_workspace_pane.py -q --tb=short`: exit 0, **201 passed in 74.40s**. Combined protocol, owner, durable host, upload and browser regression coverage.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-antigravity-acp.py apps/desktop/build/proof-tools/antigravity-acp/agy_acp_server.par`: exit 0. Actual Google initialization, exact missing-session rejection, unchanged CLI-only fixture, no replacement; native proof process reaped. No authentication or inference.
- In `apps/desktop`, `env SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar`: exit 0. Frozen backend built; capability-refusal smoke passed. Optional-library warnings remain.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: exit 0. Real Electron shell/frozen backend: native 51-turn history and pagination, desktop/mobile input, file mentions, skills, disconnect/resume, fork/index, clipboard copy/multiline paste, Codex and Claude New Chat/title retention, damaged creation-record refusal. Closing the shell preserved owners; proof cleanup reaped them afterward. No credentials or inference used.

Inspected regenerated `apps/desktop/build/workspace-proof/electron-native-workspace.png`
and `electron-native-claude-created.png`. These show the actual isolated Electron
app with native local-command output, not fabricated model responses.

This refreshes packaged-backend evidence for Claude/Codex; it does **not** prove
authenticated Gemini, full CLI feature parity, Windows execution, installed
AppImage behavior or rollout. Gemini remains unavailable by default pending
exact saved-session compatibility and authenticated execution verification.

## Earlier Implementation Evidence

Gemini owner command interface (2026-09-09): nonblocking submit with retained
turn identity, interrupt and strict permission answers now match the host-facing
method shape. Twelve focused tests passed; authenticated execution and host
admission remain disabled/unverified. See [ACP notes](workspace-antigravity-acp.md).

Gemini native-owner foundation (2026-09-09): shared lease and bound native PID,
exact ACP store/project validation, personal OAuth configuration gate, no
create/authenticate fallback, explicit cleanup. Eleven focused tests and native
pre-launch refusal proof passed. Successful authenticated load and host admission
are not yet verified or enabled. See [limitations](workspace-antigravity-acp.md).

ACP ordered event reader (2026-09-09): response handling now waits for preceding
queued updates before history/turn completion. A real subprocess burst test
preserved all 20 chunks with delayed publication; 16 scoped tests passed. Native
missing-session proof also passed with the reader active. No process teardown on
reader stop; the controller becomes unavailable. Authentication, native owner and
app admission remain open; see [ACP evidence](workspace-antigravity-acp.md).

ACP session controller (2026-09-09): exact-ID load/replay, single active prompt,
advertised input capabilities and explicit permission/cancellation controls now
exist over an already owned ACP transport. Native missing-session refusal passed
through this controller; 15 focused protocol/controller tests passed. Process
ownership, authentication and host admission are not yet connected. See
[verification and limitations](workspace-antigravity-acp.md#next-integration-work).

ACP events and permission surface (2026-09-09): added session-bound streamed text
and incremental tool translation, retained opaque content, and explicit native
permission choices in the rich pane. Eight focused adapter/browser tests passed
at desktop/mobile sizes, along with Ruff and JS syntax checks. See
[ACP integration notes](workspace-antigravity-acp.md#next-integration-work).
Not connected to an authenticated Gemini owner; real model-turn proof and
exact-session migration remain unverified. No rollout or installed-app change.

ACP session-store check (2026-09-09): native list/load against an isolated
CLI-only SQLite fixture confirms it is not an ACP session. Load returns -32002
without modifying the fixture or creating a replacement. Shipped vendor source
also shows ACP restore can rewrite trajectory metadata, so no automatic copying
or symlinking is introduced. Updated the unavailable reason, not admission.
See [store evidence](workspace-antigravity-acp.md#session-store-verification):
15 focused tests, Ruff and native rejection proof all exited 0.

Antigravity ACP discovery (2026-09-09): Google's separate `antigravity-acp`
registry binary successfully initialized through the new production ACP
transport, advertising saved-session load/resume, images/audio and Google-account
authentication. This supersedes the earlier CLI-only conclusion below, not the
remaining integration requirements. See [native evidence and next integration
steps](workspace-antigravity-acp.md). Seven real-pipe transport tests and the
no-auth/no-session native handshake exited 0. No app admission or rollout yet.

Native session slash-command routing (2026-09-09): Claude's advertised clear
and fork commands now point to the existing confirmed workspace actions instead
of being disabled. Typed `/clear`, `/reset`, `/new` and `/fork` open those dialogs;
they never enter ordinary native message submission. Opening the picker/dialog
does not mutate the session, and existing drafts are preserved. Arguments,
attachments and skills are rejected for these identity-changing commands.
The backend raw-message guard remains intact for other callers. `/resume` and
other terminal-only command gaps remain open.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py::test_command_catalog_and_session_switch_guard tests/test_workspace_pane.py -q --tb=short`: exit 1, 90 passed and one new picker fixture failed because it had not revealed the optional command button. Corrected the fixture; production native picker proof already passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py::test_command_catalog_and_session_switch_guard tests/test_workspace_pane.py::test_session_slash_commands_open_confirmed_action_without_sending tests/test_workspace_pane.py::test_session_command_picker_uses_local_action_and_keeps_draft tests/test_workspace_pane.py::test_session_command_arguments_never_reach_native_submit -q --tb=short`: exit 0, 12 passed, desktop/mobile confirmation, preserved draft, catalog metadata and raw-submit guard.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py tests/test_workspace_claude.py tests/test_workspace_pane.py scripts/verify-workspace-claude-clear-transport.py`: exit 0.
- `node --check ui/static/workspace-pane.mjs`: exit 0.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: exit 0. Desktop used the actual native command catalog; mobile typed `/clear`. Both waited for confirmation, retained the same native PID through exact identity transfer, preserved original history, recovered the receipt on reload and completed a subsequent local command without inference. Proof children cleaned up; user sessions untouched.
- `git diff --check`: exit 0.

Source/native browser proof only for this slice; frozen backend and installed
app predate this command routing change. Full provider parity and rollout remain
unfinished.

Creation recovery guard and current frozen desktop verification (2026-09-09):
malformed or mismatched browser creation records now disable submission and are
guarded inside the click handler, including synthetic events. The invalid record
is retained for recovery rather than silently replaced with a new request.
Browser regressions cover malformed JSON and invalid identity; the real Electron
proof additionally exercises a damaged record before normal native creation.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_creation.py tests/test_workspace_app.py tests/test_workspace_bridge.py -q --tb=short`: exit 0, 57 passed before the new guard tests.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q --tb=short`: exit 0, 13 passed with the new guard tests.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_app.py`: exit 0, all checks passed.
- `node --check scripts/verify-workspace-electron.cjs`: exit 0.
- `env SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar` in `apps/desktop`: exit 0; rebuilt backend includes seeded creation and queued bridge recovery. Static creation guard included and exercised below. Existing optional-library warnings remain; capability-refusal smoke passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: exit 0. Native 51-turn history, desktop/mobile input, skills, fork, disconnect/resume, real Electron clipboard and both providers' New Chat flows passed. Corrupt creation record blocked without replacement or launch. Closing Electron retained exact owners; isolated proof cleanup completed without credentials/inference. Screenshot inspected: `apps/desktop/build/workspace-proof/electron-native-claude-created.png`.
- `git diff --check`: exit 0.

This refreshes the frozen backend, not the installed app. The Electron proof
does not yet exercise seeded linked-context creation or crash recovery against
the frozen binary; their earlier source/native proofs remain the evidence for
those behaviors. Full CLI parity, Gemini, Windows, installed-package QA and
rollout remain unfinished. No user-host restart, release or default activation.

Queued bridge recovery on explicit resume (2026-09-09): attached owners now
restore unsent bridge messages from the latest durable queue snapshot, retaining
FIFO order and edits. Snapshot identity/provider/schema and unfinished command
records are validated before native launch. Reading or polling a saved bridge
receipt never starts the host. The delivery path already persists queue removal
before native submission; recovery therefore excludes in-flight/uncertain work,
cancelled entries and finished receipts. Repeated attachment does not requeue it.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_bridge.py -q --tb=short`: exit 0, 26 passed, including both providers, explicit-only restore, FIFO edits, finished/in-flight exclusion, and wrong-provider refusal before open.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_journal.py tests/test_workspace_bridge.py -q --tb=short`: exit 0, 89 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py tests/test_workspace_bridge.py scripts/verify-workspace-bridge-recovery.py`: final exit 0; explicit strict zip added after lint flagged it.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-create.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: exit 0. In addition to creation/browser coverage, runs `scripts/verify-workspace-bridge-recovery.py`: real isolated Claude process paused, first native input admitted and a second message queued through production bridge calls; test host killed, its identified children killed, exact persisted session explicitly resumed, only the queued local command completed once. Uncertain first delivery was not retried. Zero inference; crash children and recovered owner no longer live. No user host/session was touched.

Linux source/native proof only; packaged/Windows recovery, automatic background
job recovery beyond these queued bridge messages, unresolved in-flight receipts,
empty-session recovery and full CLI parity/rollout remain open. Frozen backend
and installed app have not been updated for this slice.

Explicit seeded creation (2026-09-09): structured Claude/Codex handoff panes no
longer discard/refuse supplied context. The owning parent transfers it through
an origin/source/session-checked message, never the URL. The pane displays the
exact read-only context, persists it with its request ID before POST, and requires
an explicit Create and send click. The host validates a 1 MiB text bound, stores
the immutable context in the creation command, checkpoints native identity,
then delivers one receipted submit through that same owner. It admits first-turn
indexing before submission because completion can precede the creation response.
Repeated requests/reloads/restarts never repeat the seed. An unconfirmed native
delivery is retained and shown separately from successful session creation;
the UI still permits opening that exact session. A crash with no creation receipt
remains explicitly unconfirmed rather than being automatically replayed.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_creation.py -q --tb=short`: exit 0, 20 passed, including both providers, context validation before launch, immutable payload, concurrent requests, rejected delivery and restart receipt reuse.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q --tb=short`: final exit 0, 11 passed; desktop/mobile context display, owning-frame enforcement, no auto-launch, explicit send, reload preservation and visible unconfirmed-delivery warning.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_journal.py -q --tb=short`: exit 0, 63 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py ui/workspace_app.py ui/workspace_web.py tests/test_workspace_creation.py tests/test_workspace_app.py scripts/verify-workspace-claude-create-transport.py`: exit 0.
- `node --check ui/static/workspace-create.mjs`: exit 0.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-create.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: exit 0; seeded creation at 1440/390px produced exactly one native local `/effort low` turn, retained the owner through reload/open, and reaped all proof children. Existing unseeded/name/index checks also passed. Screenshot inspected: `apps/desktop/build/workspace-proof/seeded-claude-390.png`. No credentials/model inference.

The frozen backend predates this slice. Native Codex model-turn seed execution,
the packaged linked-context action end to end, cross-chat/background routing,
empty-session/crash recovery and overall CLI parity/rollout remain unverified or
unfinished. Installed app and default activation unchanged.

Claude creation indexing and frozen Electron proof (2026-09-09): verified the
pending-to-native transition through production rename/list/read routes. A name
chosen before the first message survives indexing with exactly one sidebar row;
Read changes from an honest empty pending state to the real transcript. Extended
the real Electron main/preload proof to create both providers using the app's
New Chat dialog, open their exact embedded panes, execute only local native
commands, and preserve names through indexing. Closing Electron retains both
new owners. Claude ownership is verified from its durable creation target,
bound lease PID/birth identity and live backend descendant, not command-line
flags that the native process may replace.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_creation.py -q --tb=short`: exit 0, 11 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-claude-create-transport.py scripts/verify-workspace-codex-history.py`: final exit 0.
- `node --check scripts/verify-workspace-electron.cjs`: exit 0.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-create.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: final exit 0, including 1440/390px named single-row indexing and real Read output. An initial proof callback shadowed Flask's request object; fixed. A simultaneous SDK reinstall during the backend build interrupted another attempt; subsequent proof ran after dependency installation.
- `env SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar` in `apps/desktop`: exit 0, rebuilt current source, capability-refusal smoke passed; existing optional-library warnings remain.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: final exit 0. Includes native history, desktop/mobile file search, skills, fork, disconnect/resume, actual Electron clipboard, both New Chat flows, retained native owners and complete cleanup. Screenshot inspected: `apps/desktop/build/workspace-proof/electron-native-claude-created.png`.

Earlier frozen attempts exited 1 because the harness omitted Electron's
SDK runtime location, then because its process enumeration encountered zombies
and assumed native argv flags persisted. The final harness stages the SDK
resources and obtains environment values from actual `backendLaunch()` packaged
configuration; it validates exact live lease identity instead of argv guessing.
This proves the real Electron shell with a frozen backend and staged resources,
not an installed AppImage/Windows installer. No credentials/model inference,
release/default activation, user-host restart or installed-app change occurred.
Full CLI parity, seeded context, empty-session restart recovery and rollout
remain unfinished.

Claude New Chat admission (2026-09-09): the persistent host creation request,
journal target validation and creation screen now admit Claude as well as Codex.
The parent passes the actual selected provider instead of hardcoding Codex.
Provider/project/request identity remains immutable; repeated concurrent calls
and retries after restart reuse the stored receipt and never repeat creation.
Seeded creation and Gemini remain explicitly unavailable. No auto-launch added.

Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_creation.py tests/test_workspace_app.py -q --tb=short`: exit 0, 20 passed, including both providers, concurrent success/failure/restart requests and desktop/mobile explicit creation/reload/open.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_journal.py -q --tb=short`: exit 0, 63 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py ui/workspace_app.py tests/test_workspace_creation.py tests/test_workspace_app.py scripts/verify-workspace-claude-create-transport.py`: final exit 0; proof loop callback capture fixed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-create.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: final exit 0. Real browser-to-host-to-SDK creation at 1440/390px, explicit local command rendered, exact single owner through reload/open, owner survives page closure, cleanup reaps children. Screenshots inspected: `apps/desktop/build/workspace-proof/new-claude-output-{1440,390}.png`. No credentials/inference. The first browser extension run exited 1 because isolated HOME hid Playwright; the explicit proof-only import/browser locations correct that without changing native authentication.
Source/browser verification only. Frozen Electron/installed app unchanged;
packaged parent New Chat flow, Claude first-turn indexing, seeded context,
empty-session restart recovery, full provider parity and rollout remain open.

Claude explicit native creation foundation (2026-09-09): the SDK driver,
JSONL channel, Python transport/client and workspace owner now distinguish fresh
creation from exact resume. The owner reserves an exclusive caller-chosen UUID
and awaits a durable checkpoint before launching. Creation cannot be retried on
the same owner/transport, cannot overwrite an existing native session, and does
not inherit continue/resume/fork settings. Native input/output retains the UUID.
The host journal/API/New Chat provider admission is still Codex-only; this does
not enable Claude creation in the UI or change the installed app.

Evidence and limitations:
- Official session documentation: https://code.claude.com/docs/en/agent-sdk/sessions (accessed 2026-09-09). Installed pinned TypeScript SDK declarations expose fresh `sessionId` separately from `resume`; native proof confirms the distinction.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_claude_transport.py -q --tb=short`: exit 0, 71 passed, including lease/checkpoint-before-launch ordering, checkpoint/native failures, one-shot creation and unchanged resume/clear behavior.
- `node --test tests/workspace-claude-sdk.test.mjs tests/workspace-claude-channel.test.mjs`: exit 0, 28 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_client.py core/workspace_claude_transport.py tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_claude_transport.py scripts/verify-workspace-claude-create-transport.py`: final exit 0; initial proof import ordering corrected.
- `SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-create.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: final exit 0. Real SDK, JSONL worker, client and owner; exact creation, same-process local `/effort low`, duplicate rejection, persisted exact resume, real exclusive lease and fsynced checkpoint before launch, child reaping and lease release. Isolated HOME/config, no credentials or model inference.
- Initialization yields no session events or transcript until explicit input. The first owner proof exited 1 by checking the transcript immediately at turn completion; the corrected proof checks after process flush/close. Existing pending-index handling remains necessary; native completion does not guarantee synchronous disk visibility.
- This is source/native lifecycle verification, not authenticated model inference, packaged UI admission, restart recovery of an empty session, or Windows proof. The frozen backend has not been rebuilt for this slice.

Exact New Chat identity/name and packaged Electron path (2026-09-09): structured
pseudos are now excluded from legacy cwd/time reconciliation. Their native
returned identity alone controls handoff. The user's pending title is applied
to that exact session before opening and retiring the pseudo; rename/open
failure retains the creation pane. Rebuilt the frozen backend with these changes
and the complete preceding Codex creation stack. Electron proof now uses the
real New Chat button and naming/provider dialog, creates explicitly, opens the
embedded pane, runs a native print-only command and waits for a non-pending
indexed row retaining the exact title. Existing native owners remain alive;
exactly one new owner is added and survives window closure.
Verification:
- `node --test tests/workspace-creation-reconcile.test.mjs`: exit 0, 1 passed; newer same-directory sessions and expiry cannot steal the structured pseudo.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q --tb=short`: exit 0, 7 passed, including parent exact-target rename and pseudo retirement.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_app.py scripts/verify-workspace-codex-history.py`: exit 0.
- `node --check scripts/verify-workspace-electron.cjs`: exit 0.
- `env SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar` in `apps/desktop`: exit 0, build and capability-refusal smoke passed with existing optional-library warnings.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: exit 0, including the final strengthened indexed-row check. Existing frozen desktop/mobile history, fork, reconnect, clipboard and skills checks also passed. Screenshot inspected: `apps/desktop/build/workspace-proof/electron-native-created.png`. No credentials/inference; isolated homes and virtual display; proof children reaped.
This verifies real Electron main/preload plus the frozen backend, not installed
AppImage/Windows distribution. Other-provider creation, seeded flows and complete
parity/recovery/rollout remain incomplete. The user's installed app is unchanged.

Codex New Chat UI (2026-09-09): opt-in structured new-chat panes now load a
neon-black creation screen for an explicit Codex/project choice. No native
creation occurs on GET or mount. A sessionStorage request record must persist
before POST; reload/retry reuses it, and confirmed results expose an explicit
Open conversation action. The parent accepts only its exact iframe/source/origin
message and removes the pseudo pane only after opening the target succeeds.
Seeded handoffs remain explicitly unavailable rather than silently dropping
their required context. Claude/Gemini creation remains unavailable.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q --tb=short`: final exit 0, 7 passed, including desktop/mobile explicit creation, retry after reload, exact target opening, iframe routing and seeded-context refusal.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check ui/workspace_app.py tests/test_workspace_app.py scripts/verify-workspace-codex-create.py`: final exit 0; initial proof import ordering and loop callback binding warnings corrected.
- `node --check ui/static/workspace-create.mjs`: exit 0.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-create.py`: exit 0. At 1440px and 390px, real New Chat creation retained one native process through reload, exact target open, explicit attachment and native print-only input. Closing the page retained the owner, and proof cleanup reaped all children. Existing concurrent-HTTP, durable receipt, indexing and lease checks also passed. No credentials or inference. Screenshots inspected: `apps/desktop/build/workspace-proof/new-codex-390.png` and `new-codex-output-1440.png`.
Source/browser verification only: the rebuilt installed-Electron new-chat flow,
pre-materialization restart recovery, seeded creation, other providers and
complete rollout still remain open. The user's installed app is unchanged.

New-session pending catalog (2026-09-09): committed Codex creations now share
the pending catalog/read/metadata/deletion path with native Claude clears.
Creation records gain a cataloged marker. Placeholder rows retain their provider,
project and creation time; exact indexed rows retire them durably so deleted rows
do not revive. Completion events admit the actual native transcript, and a
missing Codex transcript is now retryable pending rather than ambiguous failure.
Unknown or uncommitted IDs still cannot mutate metadata. Existing read-only
inspection of a prepared clear identity remains unchanged.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_creation.py tests/test_workspace_journal.py tests/test_workspace_host.py tests/test_workspace_app.py tests/test_workspace_pending_catalog_api.py tests/test_workspace_catalog.py -q --tb=short`: final exit 0, 92 passed. Initial exit 1 caught an unintended restriction on the existing prepared-clear page; reverted that restriction.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pending_catalog_api.py tests/test_workspace_creation.py -q --tb=short`: exit 0, 11 passed after adding Codex coverage for rename/star/done/read, early/retired identity refusal and exact-row retirement.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py core/workspace_catalog.py tests/test_workspace_creation.py tests/test_workspace_pending_catalog_api.py scripts/verify-workspace-codex-create.py`: final exit 0; initial proof import-order warning corrected.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-create.py`: exit 0. Real creation appeared pending, its native first print-only command registered the exact transcript through the production parser/indexer in isolated storage, and its placeholder retired without duplication or revival. Concurrent HTTP/restart/lease/input/cleanup checks still passed; zero inference.
New Chat button/page wiring, unmaterialized-session restart recovery, other
providers and final packaged delivery remain open.

Persistent creation host/API (2026-09-09): `WorkspaceHost.create` now validates
explicit confirmation, canonical request UUID, supported provider and absolute
existing project directory before reserving a durable command. Concurrent
requests share that reservation. A native target is checkpointed to a unique
creation record before ownership transfer; completion and its receipt commit
together. Lost responses replay the receipt, including after host restart,
without launching another owner. Uncertain attempts remain unresolved rather
than repeating creation. The authenticated loopback-only POST
`/api/workspace/create` accepts exactly request_id/provider/cwd/confirmed.
No launch occurs on route registration or reads. New Chat UI, pending catalog
visibility and explicit recovery of unmaterialized sessions remain unwired.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_creation.py tests/test_workspace_host.py -q --tb=short`: exit 0, 63 passed. Concurrent/restarted requests, uncertainty before/after checkpoint, project/content mismatch, checkpoint immutability and authentication/origin/HTTP-method refusal covered.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py ui/workspace_web.py tests/test_workspace_creation.py scripts/verify-workspace-codex-create.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-create.py`: exit 0. Four concurrent authenticated real loopback HTTP requests created one real native owner. Exact-session shell input produced native output on the retained process. Restart replay created no process. Earlier adapter create/resume and lease assertions also passed; HTTP server and native children cleaned up, zero inference/credentials.
Source verified only; packaged binary and installed app are unchanged.

Native Codex creation adapter (2026-09-09): `CodexWorkspace.create` accepts only
an explicit provisional `new:<UUID>` identity and a durable checkpoint callback.
It sends one native `thread/start`, validates the returned identity/project and
empty non-ephemeral history, checkpoints before transferring the shared runtime
lease, then enables existing native input/output. Resume never falls back to
creation. Failed attempts cannot be repeated on the same adapter instance.
The host must still durably reserve request IDs across instances/restarts;
host/API/sidebar creation and pre-first-message recovery are NOT implemented by
this adapter step. No New Chat UI capability is claimed yet.

Official evidence accessed 2026-09-09:
https://learn.chatgpt.com/docs/app-server (redirected from
https://developers.openai.com/codex/app-server/). It distinguishes thread/start
from thread/resume and describes event subscriptions. Local native evidence
adds an important constraint: a newly created thread reports paginated history
but rejects thread/turns/list before materialization. The adapter therefore uses
the validated empty creation history, while keeping resume pagination unchanged.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py -q --tb=short`: exit 0, 47 passed, including 7 creation cases for valid identity, invalid identity/project/history, ephemeral refusal, checkpoint failure and uncertain native response.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py tests/test_workspace_codex.py scripts/verify-workspace-codex-create.py`: final exit 0; initial exit 1 for proof import ordering, corrected.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-create.py`: final exit 0. Real creation, durable journal checkpoint, competing-lease refusal, same-process print-only input, exact resume with native output, one transcript and child cleanup passed. Zero inference, no credentials, isolated project unchanged. Earlier exits 1 identified an absent fixture CODEX_HOME, premature native history read (adapter fixed), and incorrect proof journal accessor (changed to actual read API).
The frozen sidecar predates this adapter addition. Complete provider parity and
installed delivery remain open.

Embedded Electron verification (2026-09-09): rebuilt the frozen backend from
955aa29, including the pending read/delete and reconnect fixes. The Electron
proof now opens the exact chat through the actual sidebar and Code button,
interacts with its embedded iframe, and verifies the top-level page remains the
app shell. It no longer bypasses that integration by navigating directly to a
standalone workspace URL. Real native shell output, the skill catalog, native
clipboard copy/paste and retained ownership after window close passed.
Screenshot inspected: `apps/desktop/build/workspace-proof/electron-native-workspace.png`.
Verification:
- `env SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar` in `apps/desktop`: exit 0, frozen build and bundled capability-refusal smoke passed; optional-library warnings remain.
- `node --check scripts/verify-workspace-electron.cjs`: exit 0.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-codex-history.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: final exit 0. Frozen desktop/mobile history, native input/output, forks, reconnect, skills and mentions passed, followed by actual Electron sidebar/iframe/clipboard checks. No inference or credentials; all proof children cleaned up. First run exited 1: the fixture's `serena-history-proof-` directory matched the internal-project hiding rule. Renamed it to `workspace-history-proof-`; the proof now also asserts exact-session catalog visibility before clicking.
This uses the real desktop main/preload with a frozen backend in an isolated
virtual display, not an installed AppImage or Windows runtime. Provider parity,
new-session creation and rollout remain incomplete; the installed app is unchanged.

Pending read-view handling (2026-09-09): committed native clear identities now
return an explicit unindexed state instead of a false missing-session error.
The read view refreshes until the actual transcript is indexed; it never invents
messages. Failed HTTP requests do not replace titles or cache successful loads,
and late failures from a previously selected chat cannot overwrite the current
view. Uncommitted, retired and unknown identities remain unavailable.
Verification:
- `node --test tests/workspace-read-view.test.mjs`: exit 0, 3 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pending_catalog_api.py -q --tb=short`: exit 0, 3 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pending_catalog_api.py scripts/verify-workspace-claude-clear-transport.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: exit 0. Both 1440px and 390px browser flows checked the pending read response and subsequent real indexed messages/title, alongside same-process input, clear receipt recovery, ownership, deletion and cleanup. Zero inference; isolated home; no user session touched.
This is source-level verification. The frozen sidecar remains stale for these
changes; installed-app delivery and complete provider parity are still open.

Pending clear deletion (2026-09-09): single/bulk delete now route committed
uncataloged identities through an exact-session lease. A native transcript that
has appeared is registered and archived under the existing recoverable deletion
path. A truly absent transcript leaves a private recovery manifest containing
the target, metadata and retained journal location; no native transcript is
invented. The placeholder is retired only after recovery information exists.
Uncommitted/retired identities are not treated as pending; ambiguous or invalid
native metadata is rejected without deletion. Journal event history is retained.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_catalog.py tests/test_workspace_pending_catalog_api.py -q --tb=short`: exit 0, 74 passed, including missing/persisted/ambiguous transcripts, active-owner refusal, recoverable metadata, and no automatic runtime launch.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_catalog.py tests/test_workspace_host.py tests/test_workspace_pending_catalog_api.py scripts/verify-workspace-claude-clear-transport.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: final exit 0. Real newly cleared native identities refused deletion while owned, then disconnected and deleted recoverably without reappearing, at both viewport sizes. Prior browser/history/title checks and cleanup passed with zero inference. First run exited 1 because the fixture's custom owner lease directory differed from the default deletion directory; the fixture now explicitly shares its isolated directory, as production does. Inspection also changed missing-cwd metadata from a TypeError to an honest validation rejection.
Crash-time reconciliation, restoration UI, complete provider parity and final
packaged/installed-app verification remain open.

Packaged backend refresh and reconnect visual repair (2026-09-09): rebuilt the
frozen sidecar from 523034d and exercised both providers through it. Codex proof
now includes packaged disconnect/reconnect after fork navigation. Its initial
extended run exited 1 because the restored tool output was present but collapsed;
the proof now expands its native output disclosure before checking visible text.
Screenshot inspection then found a real stale Session disconnected warning after
successful reconnect. Fresh exact-session history now clears that model error,
and the pane hides only the matching alert (not unrelated draft/storage errors).
Verification:
- `env SERENA_PYTHON=/home/raghav/Documents/Projects/serena/.venv/bin/python npm run build:sidecar` in `apps/desktop`: exit 0. Frozen build and bundled peer capability smoke passed; optional-library build warnings remain, with no failure in the exercised paths.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: final exit 0. Both viewports passed native history, shell output, mentions, skills, exact fork, disconnect/reconnect and page-close ownership checks.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar`: exit 0. Frozen Claude desktop/mobile HTTP/UI paths, Electron Node worker, skills/plugins/file mentions and exact-session native input/output passed. All children reaped; no credentials/inference.
- `node --test tests/workspace-events.test.mjs`: exit 0, 10 passed, including reconnect error clearing and wrong-session/older-history rejection.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py`: exit 0 after the UI fix, including absence of the stale warning after desktop/mobile reconnect.
The frozen binary predates this final warning fix and needs rebuilding before
delivery. No installed app, full Electron window, Windows or rollout completion
is claimed by these backend/browser checks.

Recoverable deletion failure handling (2026-09-09): deletion now serializes with
index scans, refreshes the exact row under that lock, writes its recovery manifest
before moving the transcript, and only then removes database rows transactionally.
Manifest/move failures do not remove the index row. Database failure rolls back
and restores the transcript when its original path is free; an unexpected new
file is never overwritten. The recovery archive remains the fallback if restoring
the file itself fails. Crash-time filesystem/database reconciliation and pending
unindexed chat deletion still require further work.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_session_recovery.py -q --tb=short`: exit 0, 7 passed, including manifest, move and database failures.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-delete.py`: exit 0. A real SQLite abort trigger rejected deletion; the original transcript, catalog row and custom title survived. Removing the trigger allowed recoverable deletion. The cross-process lease rejection also passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_session_recovery.py scripts/verify-workspace-delete.py`: exit 0, all checks passed.

Deletion ownership and Codex disconnect verification (2026-09-09): indexed chat
deletion now holds the shared exact-session lease through catalog removal,
recoverable transcript archival and metadata cleanup. Active or ambiguous lease
ownership refuses deletion before mutation. The single-delete API reports HTTP
409 with a disconnect instruction; bulk deletion retains per-item errors and
continues with unowned selections. This covers lease-participating native/PTY
owners, not arbitrary external tools that bypass the lease. Pending unindexed
chat deletion and transactional recovery from archive failures remain unfinished.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_session_recovery.py tests/test_workspace_pending_catalog_api.py -q --tb=short`: exit 0, 7 passed; active-owner rejection, lock held throughout deletion and released on failure, recovery copy, HTTP conflict and mixed bulk results.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-delete.py`: exit 0. Disposable real SQLite catalog and real child-process lease: deletion refused without mutation while owned, then succeeded after child exit with transcript/title preserved in recovery storage. No user data used.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py`: exit 0. Real Codex desktop/mobile browsers cancelled disconnect without stopping the owner, confirmed disconnect and reaped it, then explicitly resumed the same session with persisted native command output. Existing history pagination, file mentions, skill toggles and page-close independence passed. Zero inference and isolated credentials.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_session_recovery.py tests/test_workspace_pending_catalog_api.py scripts/verify-workspace-delete.py scripts/verify-workspace-codex-history.py`: exit 0, all checks passed.

Explicit idle disconnect (2026-09-09): added a separate unplug control with a
confirmation dialog. It never runs on pane disposal. The host serializes the
action with other controls, rejects active/queued work, background tasks and
pending interactions, closes the selected owner and verifies cleanup before
reporting success. History stays in place and explicit reattachment is allowed
only under the existing ownership checks. Stable receipts prevent a lost response
from disconnecting a subsequently reattached runtime. This does not implement
deletion or force-stop running tasks.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py -q --tb=short`: exit 0, 54 passed.
- `node --test tests/workspace-connection.test.mjs`: exit 0, 22 passed, including explicit-only disconnect and exact receipt reuse after response loss/reload.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py tests/test_workspace_host.py scripts/verify-workspace-claude-clear-transport.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: exit 0. At both 1440px and 390px, opening the dialog left the selected native PID alive; explicit confirmation reaped it while the other session PID stayed alive. Source history remained unchanged, browser close did not stop the remaining runtime, and final cleanup reaped all children. Codex-specific native disconnect and installed-app/Windows proof remain open.

Automatic clear catalog materialization (2026-09-09): native turn completion now
registers a committed clear target through the real catalog callback, for both
retained and reattached owners. The browser proof no longer manually registers
the transcript. Two initial live runs exited 1 and exposed native result-before-
flush ordering: indexing had captured only the earlier /clear records. Registration
now requires the completed prompt UUID/promptId in a native user record before
writing the index. Only missing native persistence gets five bounded attempts
(50/100/200/400ms waits); ambiguous paths and other errors are not blindly retried.
Output is journaled first. Failed registration keeps the pending identity for a
later completion/scan; successful registration durably retires its placeholder.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_workspace_host.py -q --tb=short`: exit 0, 62 passed; exact prompt matching, old/partial records, non-user records, bounded retry, preserved output, committed-only registration and no launch.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_catalog.py tests/test_workspace_host.py tests/test_workspace_catalog.py scripts/verify-workspace-claude-clear-transport.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: final exit 0. Actual native completion caused indexing without a manual proof-side call at 1440px and 390px. Title/star persisted, new activity reopened done state, same PID and original history were preserved, no inference, children reaped.
Deletion inspection: current indexer deletion moves transcripts into a recovery
directory but does not coordinate with workspace ownership. Pending deletion and
safe owner/deletion coordination remain required, not implemented by this change.

Pending-chat organization (2026-09-09): star, done and bulk-done now use the
existing synced metadata for committed, uncataloged clear targets. Rename shares
the same eligibility helper. Ordinary indexed operations retain their indexer
path; unknown, uncommitted and retired targets remain rejected. Pending list rows
expose star/done state and done timestamps. No metadata action starts a runtime.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pending_catalog_api.py -q --tb=short`: exit 0, 2 passed, covering toggles, duplicate bulk IDs, idempotent explicit bulk state, invalid targets, title preservation and indexed-path delegation.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pending_catalog_api.py scripts/verify-workspace-claude-clear-transport.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: exit 0. Desktop/mobile browsers set star and done on real pending native sessions; subsequent local CLI activity indexed one row, preserved its star/title and correctly reopened its done state. All native ownership/history/cleanup assertions passed with zero inference. Pending deletion, other remaining workflows and full product parity are not complete.

Pending-chat titles (2026-09-09): committed Claude clear targets can now be
renamed before their native transcript exists, through the existing synced
custom-title store. Indexed chats keep the existing rename path. Uncommitted,
unknown and retired placeholders cannot acquire titles through this fallback;
retired placeholders also cannot reopen a phantom workspace page. Native indexing
preserves the title and retires the temporary catalog row without duplication.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_journal.py tests/test_workspace_pending_catalog_api.py tests/test_workspace_app.py::test_pending_native_clear_page_uses_durable_identity_without_launch -q --tb=short`: exit 0, 9 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py tests/test_workspace_app.py tests/test_workspace_pending_catalog_api.py scripts/verify-workspace-claude-clear-transport.py`: exit 0, all checks passed.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: final exit 0. Real native clear, browser rename, synced metadata, real transcript registration and sessions route passed at 1440px and 390px, with one row and retained native PID. Original history preserved; zero model turns/cost; isolated homes and children cleaned up. The preceding run exited 1 because its temporary project prefix matched the existing internal-session exclusion. The fixture now uses a non-internal project prefix and asserts the real indexed row is not hidden; no production exclusion was weakened.
Remaining: pending-chat deletion and other metadata/read workflows, full installed
app/Windows verification and the broader provider parity work remain open.

Visible Claude clear and pending catalog (2026-09-09): the pane now exposes an
explicit Clear context confirmation. Opening/dismissing the dialog does not clear
anything. In-flight input is blocked; success preserves the source draft/history
and offers Open new conversation for the exact returned ID. A saved clear receipt
survives renderer reloads without requiring another clear or source attachment.
The embedded pane uses the existing origin/frame/source-checked navigation path.
The sessions route overlays committed clear targets until native indexing catches
up, honoring project filters and the real indexed row/title. Once observed in the
index, an overlay is durably retired so deleting that indexed chat cannot revive
its old placeholder. Existing clear journals migrate their timestamp/catalog state
without dropping identities. No native transcript is fabricated for pending rows.
Remaining: full metadata operations on not-yet-indexed placeholders (including
deletion and read side panels; renaming is addressed above), installed-app/Windows integration,
and the broader provider-parity gates remain unfinished. Original cleared views
are non-writable until an explicit Resume original conversation succeeds; that
retires the saved navigation receipt and restores input to the exact old session,
without changing the independently retained new session.
Verification:
- `node --test tests/workspace-connection.test.mjs`: exit 0, 21 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q -k clear --tb=short`: exit 0, 3 passed, 76 deselected; confirmation, duplicate prevention, preserved draft and mobile/desktop layout.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_journal.py tests/test_workspace_app.py -q --tb=short`: exit 0, 56 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q --tb=short`: exit 0, 5 passed after adding embedded clear-navigation coverage.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pending_catalog_api.py -q`: exit 0, 1 passed against the production sessions route extracted with AST; verifies filtering, indexed title precedence, retirement and no launch.
- The first enhanced clear proof exited 1 because isolated HOME hid the user-installed Playwright package. The proof now explicitly receives its Python package/browser-cache locations without restoring credentials.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: exit 0 twice. Actual 1440px/390px browsers confirmed native clear, reloaded the source, recovered the saved target, explicitly opened/attached it and sent a local command on the same PID. Final proof waited for rendered completion and checked console/HTTP errors and horizontal overflow. Zero model turns/cost, source history unchanged, children reaped; screenshots inspected at `apps/desktop/build/workspace-proof/native-clear-{1440,390}.png`.
- The same live proof then exited 0 with an additional explicit original-session resume in both browsers: original input re-enabled under its exact old ID, the saved clear receipt retired, and the new session's PID remained unchanged. No prompt was sent into the original history.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest 'tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch[claude]' -q --tb=short`: exit 0, 1 passed after original-resume UI coverage. A prior observation handle was missing after continuation, so this exact scoped test was rerun, not a native runtime restarted.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_journal.py tests/test_workspace_pending_catalog_api.py tests/test_workspace_host.py -q -k 'clear or pending'`: exit 0, 12 passed, 41 deselected, including existing-schema migration.
- Scoped Ruff and `git diff --check`: exit 0.

Host clear checkpoint/routing (2026-09-09): added an explicit confirmed
clear_session host command and private durable workspace_clears records. Native
begin is recorded before lease transfer. The host reserves the target and removes
the source owner alias before acknowledgement, then atomically commits the clear
checkpoint with its successful command receipt. Repeated source requests return
that receipt even after the source owner moves. Unconfirmed delivery never reruns
clear, and an unresolved checkpoint blocks fresh source attachment after restart.
An occupied target is never overwritten; ordinary preflight rejection does not
disable an unchanged owner. Queued sibling messages block clear. A page for the
exact pending native ID can use read-only journal metadata when its transcript
has not yet been created; this does not launch anything or claim persisted native
history exists. This is not yet integrated with the main chat-list catalog or a
visible clear/navigation control.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_journal.py tests/test_workspace_app.py -q`: initial exit 0, 52 passed, including desktop/mobile existing pane regressions and no-launch pending page coverage.
- `SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: exit 0. Extended real-host proof recorded clear, attached the target to the retained native PID without resolving/spawning another session, replayed the exact source receipt, accepted a target local command, preserved source events and reopened the durable committed checkpoint. All native children reaped; zero model turns/cost and no user credentials.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_host.py core/workspace_journal.py ui/workspace_app.py tests/test_workspace_host.py tests/test_workspace_app.py scripts/verify-workspace-claude-clear-transport.py`: exit 0, all checks passed.
- Expanded combined tests first exited 1 (54 passed, one Codex browser resume timeout); the isolated browser node reran exit 0, 1 passed. Inspection independently identified an early-click gap: the HTML resume button was enabled before module handler installation. It is now initially disabled and enabled after registration, covered by deliberately delaying the module. The new delayed-load test initially exited 1 due to Playwright string-predicate unsafe-eval under CSP; changed test waits to function predicates without weakening CSP. `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q --tb=short`: final exit 0, 5 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py -q -k clear`: exit 0, 8 passed, 37 deselected, after rejecting occupied targets before acquiring a nested lock. Existing target owners cannot be replaced or create reciprocal lock waits.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py`: exit 0 after bootstrap change. Existing native exact ownership, source desktop/mobile controls, file mentions, skills/plugins, forks and cleanup passed; no console/HTTP errors or horizontal overflow. No installed-app or Windows proof is claimed.

Owner clear handoff (2026-09-09): ClaudeWorkspace now has private two-phase
clear methods. Active turns, permissions, elicitation and nonterminal or unknown
background tasks block clear. Native begin drains old output before changing the
converter; any late background work prevents handoff. Commit transfers the lease
first, installs the new converter/output sink and emits an empty new-session
history, then acknowledges the native transition. Both transport and client use
output fences so buffered clear-completion records cannot finish a subsequent
user turn. Input remains blocked throughout. Failure pins the owner unavailable;
cleanup releases the current lease, including a transferred lease, rather than
the retired source lease. The host must still checkpoint/reserve the target and
route it into the catalog before invoking these private methods. No clear button
or installed-app activation is claimed.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_claude_transport.py -q`: first exit 1 (1 failed, 63 passed); a new test assumed history used top-level threadId rather than the existing thread.id schema. Corrected fixture assertion; subsequent exit 0, 65 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_claude_transport.py tests/test_workspace_lease.py -q`: exit 0, 80 passed after late-background-work regression coverage.
- `SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: first extended owner proof exited 1 because it inspected lease.pid instead of lease.child.pid; corrected assertion. Subsequent exit 0: actual owner/client/native worker transferred ownership, retained its native PID, completed a new local command and preserved source events/history. No credentials or model inference.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_claude_client.py core/workspace_claude_transport.py scripts/verify-workspace-claude-clear-transport.py tests/test_workspace_claude.py tests/test_workspace_claude_client.py tests/test_workspace_claude_transport.py`: exit 0, all checks passed after correcting the new proof imports.
- Repeated the native clear proof with explicit zero-cost/zero-turn assertions on the owner result: exit 0. `git diff --check`: exit 0.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py`: exit 0. Existing source desktop/mobile native controls, commands, skills/plugins, forks, exact ownership and cleanup passed; no browser console/HTTP errors or horizontal overflow. This does not exercise a user-facing clear button, installed AppImage or Windows.

Private clear transport boundary (2026-09-09): the JSONL worker now exposes
explicit begin_clear/commit_clear requests, not generic renderer controls.
Python freezes input before beginning and installs the new identity before the
worker publishes buffered target events. Only an exact successful acknowledgement
unfreezes input. Duplicate commits, unexpected interactions, cancellation,
timeouts and malformed identities cannot replay clear or reopen input.
The host/owner/client/catalog handoff is still unimplemented; these private
methods are not exposed in the pane. No installed app or shared checkout changed.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_transport.py -q`: exit 0, 16 passed.
- `node --test tests/workspace-claude-sdk.test.mjs tests/workspace-claude-channel.test.mjs`: exit 0, 25 passed.
- `SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python`: exit 0. Real Python transport, JSONL worker and native Claude completed clear and subsequent local command on the same PID, preserved original history, and reaped the child. Zero model turns/cost; isolated home, no user credentials.
- Scoped Ruff initially exited 1 for proof-script import ordering; corrected before final verification.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude_transport.py tests/test_workspace_claude_transport.py scripts/verify-workspace-claude-clear-transport.py`: final exit 0, all checks passed. `git diff --check`: exit 0.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude_transport.py tests/test_workspace_claude.py -q`: exit 0, 48 passed including owner regressions.

Paused native clear boundary (2026-09-09): ClaudeSdkSession now has internal
beginClear/commitClear methods. Begin refuses queued or unfinished input, submits
one UUID-tagged /clear, buffers transition output and validates the matching
successful result/new UUID. It remains awaiting-handoff with input and ordinary
controls blocked. Commit accepts only that exact new ID, updates routing and
publishes its buffered events before making input ready. Conflicting identities,
unrelated receipts, native errors, stream closure and publication failures never
silently retry clear or reopen input. Ordinary input completion is tracked by
message UUID rather than decrementing for unrelated result messages.
These methods are deliberately NOT exposed by the worker control allowlist yet.
The host must checkpoint the new identity, confirm no old background work, move
the lease and install pending-session/catalog routing before acknowledgement.
The user-facing clear button therefore remains unimplemented/gated.
Verification:
- `node --test tests/workspace-claude-sdk.test.mjs`: exit 0, 15 passed initially.
- `node --test tests/workspace-claude-sdk.test.mjs tests/workspace-claude-channel.test.mjs`: final exit 0, 23 passed, including unfinished input, wrong/same/conflicting IDs, cancellation, unpublished transition output, acknowledgement mismatch and publication failure.
- `SERENA_EVIDENCE_KIND=live node scripts/verify-workspace-claude-clear.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude`: exit 0, repeated after UUID completion tracking also exit 0. Production driver stayed paused at native new ID until explicit acknowledgement, then accepted a local command on the same PID; original history remained equal. Zero model turns/cost and isolated home. This is a driver-level proof, not the complete lease/catalog/UI handoff.
- `SERENA_EVIDENCE_KIND=live SERENA_PROOF_PYTHONPATH=/home/raghav/.local/lib/python3.12/site-packages node scripts/verify-workspace-claude-driver.mjs runtimes/claude-sdk/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs /home/raghav/.local/bin/claude /home/raghav/Documents/Projects/serena/.venv/bin/python '' /home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron apps/desktop/sidecar.py`: exit 0, existing native exact-session controls, source desktop/mobile flows and cleanup passed without user auth or inference.
No installed app, provider setting or release changed.

Identity-transfer lease primitive (2026-09-09): added
SessionLease.transfer_after_transition for the pending Claude clear workflow.
It requires a bound lease and unchanged owner/child birth identities, acquires
and persists the target binding before clearing/releasing the source, and keeps
the exact live process/group. Conflicting targets and failed source writes do
not release the source runtime; partially bound targets stay fail-closed.
Both OS locks are asserted held at the source-retirement boundary. Closed or
unbound sources cannot create a target lease. No provider calls this primitive
yet: native transition acknowledgement, frozen input, absence of old-session
background work, durable pending-ID checkpoint and catalog routing are required
before enabling /clear. This does not implement the user-facing handoff alone.
Verification:
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_lease.py -q`: initial exit 0, 13 passed in 2.80s; after lock-order assertion exit 0, 13 passed in 0.79s; final exit 0, 14 passed in 0.70s with closed/unbound rejection.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-lease-recovery.py`: exit 0. Real disposable process retained PID across lease transfer; source reusable and target exclusive. Existing owner/leader-crash and orphan-tool refusal checks also passed. This is a lease-level proof, not a native /clear integration proof.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_lease.py tests/test_workspace_lease.py scripts/verify-workspace-lease-recovery.py`: exit 0.
No user process, installed app, provider session or release changed.

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
- Linked Claude/Codex isolation and routing (Gemini deferred); Fleet/bridge callers use the
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
