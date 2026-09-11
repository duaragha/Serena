# Rich Workspace Integration QA

## Release Candidate 0.2.46 (2026-09-11)

The structured workspace is default-on for Claude and Codex. Gemini is deferred.
This release-candidate record supersedes the older checkpoint qualification
immediately below.

- Python workspace matrix: exit 0, 1,242 passed, 12 platform-only skipped.
- Shared pane browser matrix: exit 0, 301 passed at responsive desktop/mobile
  widths after the final focus regression repair.
- Windows packaging and merged runtime gate: exit 0, 37 passed, 12 native-Windows
  skipped on Linux; both frozen workspace and Fleet replay dispatches guarded.
- Frozen Linux sidecar: build exit 0, followed by real Electron/native proof
  exit 0 with exact Claude/Codex session input, output, linked panes and cleanup.
- AppImage: build exit 0; packaged smoke exit 0 with sidecar startup and clean
  shutdown. Screenshots were inspected for linked, standalone and mobile panes.

2026-09-10. Worktree: `_artifacts/serena-interactive-workspace`.
Full Claude/Codex delivery remains incomplete and unreleased; this is an
integration checkpoint, not a claim that every CLI capability is finished.

## Source Checkpoint: bfa2383

2026-09-10. Includes account/session usage, guarded exit/quit and Codex fresh
context with the original writer released before creation. The source was clean
and unchanged during these checks. No installed application was updated.

```sh
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py tests/test_workspace_journal.py tests/test_workspace_codex.py -q --tb=short
```

Exit 0: 358 passed in 29.82s. This covers the full owner/host/journal suites,
including existing job, bridge, lifecycle, receipt and restore regressions.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q --tb=short
```

Exit 0: 267 passed in 360.57s. Full shared-pane browser regression suite,
including command dispatch, approval/dialog interactions, attachment/draft
handling, recovery and responsive layouts. This was a single uninterrupted run;
no production edits were made while it ran. Its controlled provider fixtures do
not substitute for positive authenticated native workflows.

```sh
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
```

Exit 0: real source backend and actual Electron main/preload. Native 51 print-only
turns, 50+1 history, exact job reservation, same-owner replay, file mentions,
skills, explicit shell output/exit 0, exit cancellation and quit/resume passed.
Desktop/mobile visibility refresh: 58ms/58ms. Actual Electron clipboard,
native Claude/Codex creation, title preservation, login start/cancel, linked
focus and drafts, and view-close owner preservation passed. Native owners were
reaped, temporary project unchanged, no credentials or inference used.

The linked-pane Electron screenshot was inspected. It verifies real native
owners, layout and drafts, not full mockup parity with model-generated content.
Codex clear's full source HTTP/browser round-trip is separately recorded in
`workspace-command-parity.md`; this Electron script does not yet exercise that
new action. Packaged binaries below predate this source checkpoint. Full
authenticated workflows, remaining provider commands and release remain open.

## Latest Verified Linux Package: 3725a32

2026-09-10. This checkpoint includes persisted session speed, explicit account
connection checks, isolated proof authentication, and confirmed failed-preference
recovery. Source remained clean and unchanged throughout the build/proofs.
The feature-specific test receipts remain in `workspace-command-parity.md`;
this checkpoint adds integration evidence, not another full-suite claim.

```sh
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
# exit 0: 51 native print-only turns, exact 50+1 history pagination, native
# job reservation exclusion, same-session resume, one-time shell output,
# project mentions and skill settings. Desktop/mobile visibility refresh
# 45ms/38ms. Actual Electron clipboard, native provider creation, login
# start/cancel, linked focus/drafts, and view-close owner preservation passed.
# Native owners reaped; isolated project untouched; no credentials/inference.

env PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m PyInstaller --noconfirm --distpath apps/desktop/build/sidecar --workpath apps/desktop/build/pyinstaller-work apps/desktop/build/pyinstaller-work/serena-web-sidecar.spec
# exit 0: build complete at approximately 211 seconds. Existing optional
# TensorBoard, pycparser table, HIP and Windows-library warnings remain.

env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: the same native and actual Electron scenarios passed against the
# frozen backend. Desktop/mobile visibility refresh 60ms/37ms. Native owners
# reaped; isolated project untouched; no credentials/inference.

sha256sum apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: 4751f6aa6c239584fcde1cc181eb348fd91c12b2f930cecc9a0ea03746cf7699
```

Both actual Electron linked-pane screenshots were inspected. They show the real
shell and native owners with retained drafts; empty conversation panes are not
evidence of full mockup parity with model output. No installed app was changed,
no release published, and no default enabled. Windows was subsequently brought
to the same runtime revision below. Fresh dedicated authentication, positive model/child workflows,
remaining command parity, final visual parity, and final delivery remain open.

### Current Windows Package: 3725a32

2026-09-10, documentation HEAD `1b354fe`. Read-only SHA-256 comparison covered
102 workspace runtime, UI, proof, desktop entrypoint and specification files;
all matched the laptop (inspection exit 0, remote exit 0). No source was edited
on the PC. Only derived build/proof output was created there.

```sh
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -B -m PyInstaller --noconfirm --distpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\dist --workpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\work C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\windows\sidecar-win.spec"
# exit 0: build complete in approximately 87s. Existing optional pycparser
# table and OpenConsole UI automation DLL warnings; no build error.

env SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc powershell -NoProfile -Command - <<'PS'
$r='C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'; $env:SERENA_PROOF_PYTHONPATH="$r\apps\desktop\build\proof-tools\windows-python"; $env:SERENA_PROOF_BROWSER_EXECUTABLE='C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'; $env:SERENA_PROOF_ELECTRON="$r\apps\desktop\build\proof-tools\windows-electron\node_modules\electron\dist\electron.exe"; $env:SERENA_PROOF_PLAYWRIGHT="$r\apps\desktop\build\proof-tools\windows-python\playwright\driver\package"; & 'C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe' -B "$r\scripts\verify-workspace-codex-windows.py" --frozen "$r\apps\desktop\build\windows-proof-5cadd13\dist\serena-web-sidecar\serena-web-sidecar.exe"; exit $LASTEXITCODE
PS
# exit 0: gated startup, exact native resume, 50+1 history, real command exit 0,
# owner retention and cleanup. Desktop/mobile visible refresh 35ms/23ms.
# Actual Electron clipboard, native Claude/Codex creation, login start/cancel,
# linked focus/drafts, and owner preservation after view/shell close passed.
# No credentials or model inference used.

ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc powershell -NoProfile -Command "(Get-FileHash 'C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\dist\serena-web-sidecar\serena-web-sidecar.exe' -Algorithm SHA256).Hash"
# exit 0: 8e587a9f40a4474fbde25008bc5ffc53f266b38c058a5b4a902f11d195c91f58
```

The actual Electron screenshot was copied from the PC and inspected at
`apps/desktop/build/workspace-proof/windows-electron-linked-3725a32.png`.
The 1024px window shows wrapped composer controls and separate linked panes
without overlap. These are development-package integration results, not proof
of authenticated model workflows, full command parity, or release/install.

## Earlier Source Checkpoint: e07b5ce

2026-09-10. Integrated agent inspection, steering, attachments, receipt recovery
and explicit idle continuation are included. No runtime source changed during
this checkpoint. Full delivery remains incomplete.

```sh
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_journal.py -q --tb=short
# exit 0: 373 passed in 25.66s.
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q --tb=short
# exit 0: 251 passed in 276.33s.
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
# exit 0: 51 native print-only turns, 50+1 history pagination, same-owner
# reservation exclusion, exact resume, one-time shell output, native skills
# and mentions. Desktop/mobile visibility refresh 43ms/53ms. Real Electron
# clipboard, login cancellation, native Claude/Codex creation, linked identity,
# focus, draft recovery and view-close preservation passed. Owners reaped,
# isolated project unchanged, no inference or credentials used.
```

The actual Electron linked-pane screenshot was inspected. It proves real shell
integration and layout with empty sessions/drafts, not full mockup parity with
model prose or authenticated child-agent work. Those gates remain separate.

### Earlier Linux Package

The package was rebuilt from the same `e07b5ce` runtime source and exercised
through the actual Electron shell, without installing it:

```sh
env PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m PyInstaller --noconfirm --distpath apps/desktop/build/sidecar --workpath apps/desktop/build/pyinstaller-work apps/desktop/build/pyinstaller-work/serena-web-sidecar.spec
# exit 0: build completed in approximately 215s. Optional TensorBoard,
# pycparser table, HIP and Windows library warnings remain; no build error.
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: all source-proof scenarios above passed against the frozen backend.
# Visible refresh 38ms desktop / 35ms mobile. Actual Electron linked views,
# clipboard, native provider creation and owner preservation all passed.
# Disposable project untouched; owners reaped; no inference or credentials.
sha256sum apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: b6788f4e933230d62ef6e1c8fb9d09f27958e9956178df328d1dfbffd179fe8d
```

This refreshes Linux packaging evidence only. Windows packaging, authenticated
positive subagent workflows, remaining command parity, final visual parity and
release/default enablement are not established by these checks.

### Earlier Windows Package

Executed 2026-09-10 against `e07b5ce` runtime source (documentation HEAD
`f5b27cb`). Read-only SHA-256 comparison of 94 workspace, UI, proof and desktop
entry/spec files between laptop and PC exited 0 with no mismatches. No PC source
was edited; only derived build/test artifacts were created.

```sh
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -B -m PyInstaller --noconfirm --distpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\dist --workpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\work C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\windows\sidecar-win.spec"
# exit 0: build completed in approximately 87s. Optional pycparser table and
# OpenConsole UI automation DLL warnings; no build error.
env SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc powershell -NoProfile -Command - <<'PS'
$r='C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'; $env:SERENA_PROOF_PYTHONPATH="$r\apps\desktop\build\proof-tools\windows-python"; $env:SERENA_PROOF_BROWSER_EXECUTABLE='C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'; $env:SERENA_PROOF_ELECTRON="$r\apps\desktop\build\proof-tools\windows-electron\node_modules\electron\dist\electron.exe"; $env:SERENA_PROOF_PLAYWRIGHT="$r\apps\desktop\build\proof-tools\windows-python\playwright\driver\package"; & 'C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe' -B "$r\scripts\verify-workspace-codex-windows.py" --frozen "$r\apps\desktop\build\windows-proof-5cadd13\dist\serena-web-sidecar\serena-web-sidecar.exe"; exit $LASTEXITCODE
PS
# exit 0: gated native startup, 50+1 history, exact resume, native command
# exit 0, same-owner history and cleanup. Desktop/mobile visible refresh
# 29ms/18ms. Actual Electron clipboard, login cancellation, native Claude/Codex
# creation, linked identity/focus and view-close owner preservation passed.
# No inference or credentials used.
```

Windows executable SHA-256 (read-only remote inspection, exit 0):
`2fa771f0e6fc355005f65c5d40f348a418fe92330345f1c8bdd0aea32f903f1c`.
Screenshot copied from PC and inspected at
`apps/desktop/build/workspace-proof/windows-electron-linked-e07b5ce.png`.
These are development package receipts, not installer publication, default
enablement or authenticated feature completeness.

## Earlier Integration Checkpoint: 064b97e

2026-09-10. No runtime source changed during these checks. Disposable profiles
only; no installed app restart, release or default enablement.

```sh
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_journal.py -q --tb=short
# exit 0: 328 passed in 29.53s.
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q --tb=short
# exit 0: 237 passed in 229.23s.
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
# exit 0: 51 print-only native turns; 50+1 pagination; exact resume and one-time
# shell execution; job reservation exclusion; native skills and mentions;
# desktop/mobile reload and retained drafts; visible refresh 52ms/56ms.
# Actual Electron clipboard, cancelled login, native Claude/Codex creation,
# linked focus/identity and view-close preservation passed. Children reaped,
# disposable project untouched, no inference or credentials used.
env PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m PyInstaller --noconfirm --distpath apps/desktop/build/sidecar --workpath apps/desktop/build/pyinstaller-work apps/desktop/build/pyinstaller-work/serena-web-sidecar.spec
# exit 0: build completed in approximately 227s. Warnings included optional
# TensorBoard, HIP and Windows libraries unavailable in the Linux environment.
```

Real Electron linked-pane screenshot inspected. Empty linked conversations and
retained drafts prove layout/ownership, not visual parity with model prose and
all tool types. Subagent navigation remains a concrete uncovered interaction;
see the current subagent audit in `workspace-command-parity.md`.

### Linux Frozen Proof

The same `064b97e` build was then exercised with the real Electron shell:

```sh
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: all source-proof scenarios above passed against the frozen backend.
# Visible refresh 144ms desktop / 94ms mobile. Native child cleanup completed;
# no credentials used and isolated project untouched.
sha256sum apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
# exit 0: fa735a23b3834d76e6cafd95ae1397d7839502138c94c48beb05cf2c1b4ce9d3
git diff --check
# exit 0: no whitespace errors.
```

This does not refresh Windows packaging evidence, prove all model-backed
commands, or install/publish an update. The latest Windows packaged checkpoint
remains the earlier source revision documented separately.

## Earlier Source Regression Check

Source `5eae176` with the test-only timing corrections below, 2026-09-10.
The initial full pane run exited 1: 227 passed, 2 failed in 147.01s. Both
failures read asynchronous DOM state synchronously: the copy button updates on
the next animation frame, and focus restoration runs on the dialog close event.
Assertions now wait for the same enabled/focus states using Playwright's
retrying expectations. No runtime behavior or acceptance condition was removed.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_copy_after_revert_waits_for_new_completed_output tests/test_workspace_pane.py::test_codex_status_is_read_only_updates_and_does_not_invent_values -q --tb=short
# exit 0: 3 passed in 2.87s.
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q --tb=short
# exit 0: 229 passed in 171.27s (full pane regression rerun).
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
# exit 0: 51 native print-only turns, 50+1 history pagination, exact resume,
# one-time native shell output, reservation exclusion, native skills/mentions,
# desktop/mobile reload and drafts; visible refresh 21ms at both sizes.
# Real Electron clipboard, login controls, native Claude/Codex creation,
# linked identity/focus and view-close ownership preservation all passed.
# No inference/credentials used; disposable native owners and project cleaned up.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py
# exit 0: All checks passed.
```

Inspected the real Electron linked-view screenshot at
`apps/desktop/build/workspace-proof/electron-native-linked-created.png`.
This is source-backend proof, not a rebuilt package or authenticated full-tool
workflow. The existing Windows package receipt predates these changes.

## Findings and Repairs

- Plain Codex steering acquired an empty `options` object while adding connector
  selections. Restored the original payload shape when no app or skill is selected;
  selected attachments still carry their native options.
- Two receipt-corruption browser assertions predated view-context telemetry and
  incorrectly expected only an observation request. They now explicitly permit
  context reports while continuing to forbid coding commands and uploads.
- Inspection exposed a real admission gap: unreadable saved receipts could report
  an empty, healthy composer. Known receipt-storage failures now report unresolved
  draft state, preventing background work admission. Such views cannot request
  sibling sleep. The corrupt data remains intact and the existing owner is not
  cancelled, replaced or closed.

## Commands and Results

All commands ran from the worktree root; exit codes came from executed commands.

```sh
env PLAYWRIGHT_BROWSERS_PATH=apps/desktop/build/proof-tools/playwright /home/raghav/Documents/Projects/serena/.venv/bin/python -m playwright install chromium --only-shell
```

Exit 0. Installed Chromium headless shell 145 / Playwright build 1208 plus FFmpeg
into ignored build artifacts. No user browser installation was modified.
Playwright warned that Mint uses its Ubuntu 24.04 fallback build.

The first broad collection attempt exited 2 because the base Python environment
lacked optional Gemini `hjson`. A second attempt using `--ignore` still explicitly
supplied those paths via shell expansion and exited 2 for the same reason. The
actual integration command below excludes those two files before collection and
uses the existing isolated proof dependencies:

```sh
env PYTHONPATH=apps/desktop/build/proof-tools/python-deps PLAYWRIGHT_BROWSERS_PATH=apps/desktop/build/proof-tools/playwright SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest $(rg --files tests | rg '^tests/test_workspace_.*\.py$' | rg -v '/test_workspace_gemini') -q --tb=short
```

Exit 1 before repairs: 805 passed, 3 failed, 12 skipped, 1 warning in 294.00s.
Failures were the steering assertion and both receipt-corruption viewports above.
The warning concerned `forkpty` in a multithreaded exclusive-ownership test.
This was workspace-scoped, not the entire Serena repository suite.

```sh
env PLAYWRIGHT_BROWSERS_PATH=apps/desktop/build/proof-tools/playwright SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_codex_running_composer_steers_exact_turn tests/test_workspace_pane.py::test_app_picker_selects_exact_ids_preserves_failed_draft_and_never_auto_loads tests/test_workspace_app.py::test_corrupt_receipts_keep_page_viewable_without_sending_commands tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short
```

Exit 0 after repairs: 7 passed in 27.62s. Covers plain steering, connector drafts,
both corrupted-receipt viewports, mounted healthy Claude/Codex pages, ownership,
uploads, context reports, rename/sidebar behavior and no-auto-launch.
The full 820-case run was not repeated after this scoped repair.

```sh
node --test tests/workspace-*.test.mjs
```

Exit 0: 104 passed, 1 Windows-specific skip, 0 failed, 294.071266ms. Executed
before the two page/pane edits; those edits are covered by the browser run above.

```sh
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-receipt-guard.py
```

Exit 0. Real native Codex owner, actual Flask workspace routes and Chromium page:
unreadable browser receipts reported unresolved work; `reserve_work` rejected it
specifically as an unsent draft. Data and PID stayed unchanged, no browser coding
commands or uploads occurred. After closing that view, a clean browser context
observed the same owner and successfully reserved/released an in-memory work slot.
No durable coding job or model turn was created. One print-only shell command
materialized the disposable native session. All children, server and temporary
profile were cleaned up; no inference or user-profile mutation.

`ruff check scripts/verify-workspace-receipt-guard.py tests/test_workspace_app.py`
using `/home/raghav/Documents/Projects/serena/.venv/bin/ruff` exited 0: all checks
passed. `node --check ui/static/workspace-page.mjs`,
`node --check ui/static/workspace-pane.mjs` and `git diff --check` each exited 0
with no output.

## Compact Session Actions (2026-09-10)

The source-sidecar/native Electron proof exposed a visual mismatch: the complete
Codex action strip wrapped and made its header taller than Claude's. Both panes
now have a fixed 48px header and one Session actions button. The actual existing
controls move into a labeled popover; their provider visibility, disabled states,
confirmation dialogs, exact-session handlers and slash-command routing remain
unchanged. No alternative session or transcript renderer was introduced.

The browser handles light dismissal. Arrow keys, Home/End and Escape support
keyboard navigation; clicking an action dismisses the popover before its modal
opens. Long model names truncate without changing header height. Tests and proof
scripts now explicitly open the menu before clicking a moved action.

Commands were executed separately from this worktree:

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py -q --tb=short
```

Exit 1: 215 passed, 3 failed in 225.15s. All failures were the new hidden-control
assertions matching both fixture panes. Scoping them to the intended pane fixed
the selectors without weakening the disabled-state checks.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_clear_requires_confirmation_and_recovers_exact_target_without_repeating tests/test_workspace_pane.py::test_closing_pending_fork_cannot_start_second_creation tests/test_workspace_pane.py::test_session_actions_keep_headers_aligned_and_support_keyboard -q --tb=short
```

Exit 0: 5 passed in 7.57s. The complete pane file was not repeated after the
selector-only repair.

```sh
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q --tb=short
```

Exit 1: 12 failed, 3 passed in 29.58s. The default Chromium tests lacked the
explicit path to the already installed proof browser; no product defect.

```sh
env PLAYWRIGHT_BROWSERS_PATH=apps/desktop/build/proof-tools/playwright SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py -q --tb=short
```

Exit 0: 15 passed in 48.45s, including mounted native-pane controls, no-auto-launch,
corrupt receipt/creation guards and the linked frame path.

```sh
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/sidecar.py
```

Exit 0 both before and after the menu change. Actual source sidecar, native
Codex and Claude children, Edge mobile/desktop pages and Electron main/preload:
51 print-only native turns; real shell I/O; exact resume, fork and new-chat
identities; real clipboard; login start/reopen/cancel without credential changes;
linked composer focus and drafts; view closure preserving owners; stale-history
and competing-work rejection. Post-change visible refresh was 59ms desktop and
31ms mobile. All disposable owners closed and the isolated project remained
untouched. No inference, user-profile edits or installed-app restart.

Viewed `apps/desktop/build/workspace-proof/actions-1400.png`,
`codex-source-mobile.png` and `electron-native-linked-created.png`: the real
linked headers are now aligned. These are source checks, not a fresh packaged
Windows/Linux release proof.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_session_actions_keep_headers_aligned_and_support_keyboard -q --tb=short
```

Exit 0: 2 passed in 3.25s after extending keyboard coverage to Home/End and the
full Codex action list. Viewed `actions-menu-1400.png`: labels and disabled
states fit the popover with the expected neon-black styling.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py tests/test_workspace_app.py scripts/verify-workspace-claude-clear-transport.py scripts/verify-workspace-codex-create.py scripts/verify-workspace-codex-history.py scripts/verify-workspace-copy.py scripts/verify-workspace-frozen.py scripts/verify-workspace.py
node --check ui/static/workspace-actions.mjs
node --check scripts/verify-workspace-electron.cjs
git diff --check
```

Each command ran separately and exited 0. Ruff reported all checks passed;
the other checks produced no output.

## Remaining Gates

See `interactive-workspace.md` (Required Delivery Gates) and
`workspace-command-parity.md`. Fresh source-748573e frozen Windows/Linux proofs
are recorded in `workspace-packaged-qa-2026-09-10.md`. These do not replace
final mockup comparison with every production control,
remaining command parity, safe existing-session migration, or release/default
enablement. Gemini remains deferred.
