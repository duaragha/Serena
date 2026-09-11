# Native Workspace Packaged QA

## Superseding 0.2.46 Linux Candidate (2026-09-11)

The merged 0.2.46 tree rebuilt the frozen sidecar and AppImage after integrating
the v0.2.45 runtime hardening. The sidecar build exited 0, including its Fleet
peer MCP contract smoke. Real Electron against that frozen binary exited 0 for
native Claude/Codex creation, exact-session input/output, linked panes, history,
clipboard, drafts, login cancellation, reconnect, ownership preservation and
clean teardown. No inference or user session mutation was used by the proof.

`Serena-0.2.46-x86_64.AppImage` then built with exit 0 and its packaged smoke
exited 0 after starting the bundled sidecar and shutting down cleanly. SHA-256:
`84b247bfdc3dfc7d3e6b735b1522c92d367d6b5112f885e07287a8bcdc12830f`.
Linked, standalone Claude, standalone Codex and 390px Codex screenshots were
visually inspected: panes are nonblank, controls fit, drafts remain visible and
no incoherent overlap was present. The tagged Windows job remains responsible
for the native Windows package proof.

Source: `748573e` on `feat/interactive-workspace`. Executed 2026-09-10.
This is a frozen-backend plus real Electron proof, not an installer release,
default enablement, or a claim of complete command parity. Gemini is deferred.

## Source Identity

Read-only SHA-256 comparison of 89 tracked workspace runtime, UI, proof and
desktop entry/spec files between laptop and PC reported zero mismatches.
The first bulk PowerShell-stdin hash inspection stalled and was terminated
(exit 143); a bounded Python read-only hash inspection returned exit 0 and the
89-file result. Neither changed PC source files. Individual key-file hash
checks also matched before building.

Both frozen bundles contain the current `workspace-actions.mjs`, SHA-256
`7365e46b141730d8ac77dca6928cdf9ef506fa2902ce4a657d3437b5ecee295e`.

| Artifact | SHA-256 |
| --- | --- |
| Linux executable | `5f0f1b5f13a7fd379e703ab8e09056343e28390f02c3a6193e859467db9f12a9` |
| Windows executable | `e4d3e2ba1f79d7c37a1271c123eeaf9167f003c22949d8ce6b029015324ef8ce` |

## Linux

Commands ran separately from the isolated worktree:

```sh
env PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps /home/raghav/Documents/Projects/serena/.venv/bin/python -m PyInstaller --noconfirm --distpath apps/desktop/build/sidecar --workpath apps/desktop/build/pyinstaller-work apps/desktop/build/pyinstaller-work/serena-web-sidecar.spec
```

Exit 0, PyInstaller 6.22.1 / Python 3.12.3: build completed in approximately
108s. Warnings included optional TensorBoard, pycparser tables, GPU libraries
and Windows ctypes libraries. This proof does not claim those unrelated
optional integrations work.

```sh
env SERENA_EVIDENCE_KIND=live PYTHONPATH=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/python-deps:/home/raghav/.local/lib/python3.12/site-packages SERENA_PROOF_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge SERENA_PROOF_ELECTRON=/home/raghav/Documents/Projects/serena/apps/desktop/node_modules/electron/dist/electron SERENA_PROOF_PLAYWRIGHT=/home/raghav/.local/lib/python3.12/site-packages/playwright/driver/package SERENA_PROOF_XVFB=/home/raghav/Documents/Projects/_artifacts/serena-interactive-workspace/apps/desktop/build/proof-tools/xvfb/usr/bin/Xvfb /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-codex-history.py apps/desktop/build/sidecar/serena-web-sidecar/serena-web-sidecar
```

Exit 0. Observed native history (50 recent + 1 older print-only turn), exact
resume, stale history rejection, one-time shell execution, competing-work
rejection, project mentions, native skill disable/re-enable, reduced hidden
polling, desktop/mobile drafts, confirmed disconnect/reopen and indexed fork.
Visible refresh: 28ms desktop, 32ms mobile, same owner retained.

## Windows

```sh
ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc "C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe -B -m PyInstaller --noconfirm --distpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\dist --workpath C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\build\windows-proof-5cadd13\work C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace\apps\desktop\windows\sidecar-win.spec"
```

Exit 0, PyInstaller 6.21.0 / Python 3.13.7: build completed in approximately
88s. Warnings included pycparser tables and optional OpenConsole UIA DLLs;
the native structured-session runtime proved below does not use OpenConsole.

```sh
env SERENA_EVIDENCE_KIND=live ssh -o BatchMode=yes -o ConnectTimeout=5 docker-pc powershell -NoProfile -Command - <<'PS'
$r='C:\Users\ragha\Projects\_artifacts\serena-interactive-workspace'; $env:SERENA_PROOF_PYTHONPATH="$r\apps\desktop\build\proof-tools\windows-python"; $env:SERENA_PROOF_BROWSER_EXECUTABLE='C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'; $env:SERENA_PROOF_ELECTRON="$r\apps\desktop\build\proof-tools\windows-electron\node_modules\electron\dist\electron.exe"; $env:SERENA_PROOF_PLAYWRIGHT="$r\apps\desktop\build\proof-tools\windows-python\playwright\driver\package"; & 'C:\Users\ragha\Projects\serena\.venv\Scripts\python.exe' -B "$r\scripts\verify-workspace-codex-windows.py" --frozen "$r\apps\desktop\build\windows-proof-5cadd13\dist\serena-web-sidecar\serena-web-sidecar.exe"; exit $LASTEXITCODE
PS
```

Exit 0. Real gated native startup, empty isolated catalog, 50+1 turn history,
exact resume with native shell exit 0, unchanged history owner and cleanup.
Frozen desktop/mobile flows above passed; visible refresh was 34ms desktop,
23ms mobile. No inference.

## Electron On Both Platforms

Both commands also ran the real Electron main/preload against their frozen
backend. Passed: sandbox/context isolation and Node integration disabled; native
clipboard copy and multiline paste without sending; native login start/reopen/
cancel preserving drafts without browser launch or credential changes; standalone
Claude/Codex creation with selected titles; corrupt creation-record refusal;
linked Claude/Codex identities and one retained group title; exact composer focus;
session actions opening their dialogs; drafts and owners surviving view closure.
Closing Electron left existing owners and exactly two additional owners per
provider intact. The harness then closed all disposable owners and profiles.
No installed app, user profile, real project or active session was modified.

Viewed screenshots:
- `apps/desktop/build/workspace-proof/electron-native-linked-created.png` (Linux)
- `apps/desktop/build/workspace-proof/windows-748573e/electron-native-linked-created.png`
- `apps/desktop/build/workspace-proof/windows-748573e/codex-frozen-mobile.png`

Headers are aligned, controls and drafts remain visible, and mobile content fits.
These runs exercise real local native commands, not an authenticated model's
full tool/approval workflow. Existing command-parity and delivery gates remain in
`workspace-command-parity.md` and `interactive-workspace.md`; packaging success
does not waive them. No release, installation, merge or default enablement ran.
