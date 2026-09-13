# Workspace Stability Audit

## Causes Confirmed

- Shared Python servers outlive checkout updates, but Flask normally reads static
  files from disk on each request. This can mix new workspace JavaScript with old
  backend routes and provider adapters. Workspace modules, styles, and their
  vendored dependencies are now captured at mount time and served without browser
  caching. A backend restart activates the next matched frontend snapshot.
- The previous desktop restart path accepted the old PID while the detached
  restart helper was still waiting. Version 0.2.66 waits for the replacement PID
  and awaits a fresh root navigation. Workspace tokens rotate on restart, making
  reload ordering necessary, not cosmetic.
- Read polling reported transient failures without a recovery signal. Successful
  polling now clears only its own error, preserving later command failures and
  uncertain send receipts. Failed polling backs off; no command is replayed.
- Codex reasoning delta events were retained only in the event inspector. The
  renderer now accumulates native summary/content parts by item and part index.
  The installed Codex app-server JSON schema was used to verify field names.
- Claude tools expanded by default; Codex command summaries could contain whole
  scripts. Tool rows now default to compact summaries with visible status and
  expandable complete input/output. Native failure details remain inspectable.
- Markdown output inherited `white-space: pre-wrap`, preserving formatting
  whitespace as visible paragraph spacing. Prose now uses normal HTML whitespace;
  code blocks and user input retain their whitespace.
- Release checks exercised updater/menu behavior, not the workspace. The Linux
  publish job now requires desktop tests, JavaScript workspace contracts, and the
  Python workspace suite with Playwright installed. Windows publishing depends on
  that job. Test results are uploaded even on failure.

## Verification Boundary

Browser tests cover creation, automatic opening, linked identity adoption, replay,
streaming, commands, approvals, uploads, error recovery, and narrow/wide layouts.
Provider tests include protocol fixtures and ownership checks; they are not a
claim that every installed provider/account combination has passed live inference.
Platform-only checks can skip on Linux. Existing user chats were not restarted,
interrupted, or used for test prompts during this audit.

Serving a frozen asset snapshot prevents frontend/backend asset drift; it does
not make a mutable checkout an immutable deployment. Python modules loaded lazily
after a checkout update and external CLI updates remain separate risks. Keep
updates at backend restart boundaries, and retain the explicit ownership checks.
