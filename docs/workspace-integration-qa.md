# Rich Workspace Integration QA

2026-09-10. Worktree: `_artifacts/serena-interactive-workspace`.
Full Claude/Codex delivery remains incomplete and unreleased; this is an
integration checkpoint, not a claim that every CLI capability is finished.

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

## Remaining Gates

See `interactive-workspace.md` (Required Delivery Gates) and
`workspace-command-parity.md`. The current QA does not replace fresh packaged
Windows/Linux proof, final mockup comparison with every production control,
remaining command parity, safe existing-session migration, or release/default
enablement. Gemini remains deferred.
