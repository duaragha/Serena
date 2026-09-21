# Spec: Scripted Browser Checks (CDP)

## Overview

**What**: A first-party scripted browser path — attach to Chromium over CDP and run deterministic read-first checks (url/title/text/selector) as pre/post-conditions around the Astra screenshot loop.
**Why**: Today every browser observation burns a 3–7s model turn on a paint. "Selector #invoice-total appeared" should be a millisecond assertion, not a vision round-trip. CDP checks are also display-server-independent — they work where X11 capture can't.
**Scope**: IN — attach helper + read-only checks + seal-marker verification for browser profiles + loop integration as task data. OUT — scripted clicks/fills in v1 (stay behind control lease or later); Wayland adapter (separate spec); touching user-configured stdio Playwright MCP.

## Requirements

- [x] WHEN a check runs THEN it attaches to the profile browser over loopback CDP (ephemeral port, 0600 port file) with no display server dependency
- [x] WHEN url/title/text/selector checks run THEN they return structured results (value, match bool, elapsed ms) usable as turn pre/post-conditions
- [x] WHEN check output enters a prompt THEN it is labeled untrusted task data, exactly like screen text — never authority
- [x] WHEN a sealed-profile marker check runs THEN it verifies the post-login marker without screenshots
- [x] WHEN the browser is unreachable THEN checks fail fast with a clear error (no hangs, no retries beyond one)

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/computer_browser.py` — launch/attach (ephemeral `--remote-debugging-port` on 127.0.0.1, port file 0600 per `service.json` pattern) + checks (`url/title/text/selector-count/wait-for-marker`); pattern from `scripts/verify-desktop-editions.py:103-108`
- `MODIFIED core/computer_agent.py` — watch loop calls scripted checks as deterministic pre/post-conditions around Astra turns
- `MODIFIED core/browser_profiles.py` (from its spec) — seal-marker verification uses these checks
- Deps: `playwright` in a new optional extra, OR raw CDP over websocket (`httpx`+`websockets` already in `voice` extra) — implementer picks by weight; document the pick

**Key decisions**:
- Read-only-first — checks observe; acting stays with the audited `act()` path and its grants. No new write authority invented here.
- First-party, not MCP Playwright — the stdio MCP is broker-unreachable by design; Serena needs a path it owns
- Checks as task data — CDP output gets zero trust privilege over OCR; a compromised page can't promote itself to instructions
- Wayland explicitly out — CDP checks work anywhere, but full computer-use stays X11-only until a portal adapter spec lands

## Tasks

> Execution: batch as ONE fleet coding run.

### Phase 1: Attach + checks
- [x] `core/computer_browser.py` with launch/attach + read-only checks + fast-fail behavior
  - accept: fixture page yields correct url/title/text/selector results; dead browser errors in <2s
  - engine: fleet

### Phase 2: Loop + marker integration
- [x] Watch-loop pre/post-conditions + seal-marker verification for browser profiles
  - accept: scripted pre-condition gates an Astra turn in a fixture session; marker check verifies a sealed profile with no screenshots
  - engine: fleet

## Edge Cases / Gotchas

- `--remote-debugging-port` on loopback only — never 0.0.0.0; port file 0600 like `service.json`
- Selector checks race paints — `wait-for-marker` needs a bounded wait (default ~5s), not sleep-then-hope
- `wait-for-marker` timeouts are data (condition false), not errors — callers branch on them
- v2 (scripted navigation/assert packs from `automation_runtime`) must not leak into v1 — keep the module read-only-shaped so the later addition is clean

## Testing

- [x] Check-correctness test on fixture pages (all four check types)
- [x] Fast-fail test (dead browser <2s, clear error)
- [x] Untrusted-labeling test (prompt shows task-data framing)
- [x] Marker verification test with sealed fixture profile
- [x] Loop integration test (pre-condition gates a turn)

---

## Progress Log

**Status**: Implementation complete and verified against a real Chromium-family browser
**Branch**: `serena/fleet/729961df-1547-415e-97d4-c0be40f7b747/agent-a`
**Current phase**: Review fixes applied
**Last completed task**: Postcondition frame-error gating and profile-bound CDP discovery
**Files modified**: `core/computer_browser.py`, `core/browser_profiles.py`, `core/computer_agent.py`, `core/computer_use.py`, `core/computer_mcp.py`, `pyproject.toml`, three browser test modules, this document
**Live validation**: `tests/test_computer_browser.py::test_real_chromium_fixture` runs headless
Microsoft Edge (`/usr/bin/microsoft-edge`) with no `DISPLAY`, attaches over loopback CDP,
and passes every check kind plus seal verification, `check_session`, and listener/process
cleanup. Chromium's process-singleton socket must fit a 108-byte `sun_path`, so the test
relocates only the browser's `TMPDIR` when the inherited one is too deep; it still skips
cleanly without a browser binary or loopback permission.
**Review fixes**:
- Watch postconditions now propagate `frames.error` after the awaited check and inside the
  hold loop, so a dead capture can never publish held guidance.
- Saved-profile discovery polls until the port has a live 127.0.0.1 listener whose process
  names this profile's `--user-data-dir`, so a stale or recycled port can never verify
  another browser. Attach gives the launch `LAUNCH_STARTUP_TIMEOUT` to come up.

## Implemented API and dependency decision

Install `serena[browser]`: `httpx>=0.28,<1` and `websockets>=10.4,<16`.
Raw CDP avoids Playwright's driver/browser download and default-context mutations;
both libraries overlap the existing voice extra. Imports remain lazy, so ordinary
computer-use needs neither package. The legacy websocket client is deliberate:
it supports the deployed 10.4 installation, bypasses environment proxies, and lets
the attach helper reject every websocket redirect. HTTP proxies and redirects are
also disabled. No stdio MCP configuration is read or changed.

`await launch(executable, isolated_profile_dir, headless=False)` starts a dedicated
Linux/POSIX Chromium profile with sync disabled, an ephemeral loopback port, and
an exclusive profile lock. The directory must be 0700 and initially empty;
later launches recognize its `.serena-cdp-profile` marker. Existing unmarked
browser data is refused to protect daily profiles. It verifies the real
listener's address and process group before atomically publishing `cdp.json` as
0600. Retain the returned handle and call `handle.close()` in `finally`; only its
recorded process group and discovery files are cleaned up. Existing/stale
discovery is refused, never silently attached to or overwritten. This helper is
an explicit operator lifecycle API, not a model tool or new authority grant.

```python
from core.computer_browser import BrowserChecks, Check

async with BrowserChecks.attach("/absolute/isolated-profile/cdp.json", target_id="PAGE_ID") as browser:
    result = await browser.check(Check("text", selector="#invoice-total", expected="$42"))
    # {"kind": "text", "value": "$42", "match": True, "elapsed_ms": ...}
```

Omit `target_id` only when exactly one page exists; ambiguous tab selection fails.
Checks support `url`, `title`, `text`, `selector-count`, and `wait-for-marker`.
`expected` compares exactly; without it, a non-null observation matches.
Marker waits check CSS presence with a five-second default (configurable up to
ten), and return false on expiry. A disconnected browser is an error, not a
missing marker. Attach has one attempt with a 1.5-second deadline; each CDP
request has a 750ms deadline. Fixed evaluation expressions reject side effects
and have a 500ms execution budget. Strings over 16,000 characters fail explicitly
instead of producing misleading matches from truncated values.
URL/title also support `match_mode="prefix"` or `"regex"`; regex is ECMAScript,
executed inside Chromium's deadline, never against page text in Python.

Pass this optional task data to `computer_start(..., browser_checks=plan)` or
the authenticated computer service's `run` request:

```json
{
  "port_file": "/absolute/isolated-profile/cdp.json",
  "target_id": "PAGE_ID",
  "pre": [{"kind": "wait-for-marker", "selector": "#invoice-total", "timeout": 5}],
  "post": [{"kind": "selector-count", "selector": "#invoice-total", "expected": 1}],
  "seal": {
    "slug": "invoices",
    "enrolled_at": "2026-09-17T12:00:00Z",
    "url_prefix": "https://invoices.example/account/",
    "title_regex": "^Invoices"
  }
}
```

Only background watch accepts the plan. Each phase allows at most eight checks.
Failed preconditions recheck even without a new screenshot and prevent model
turns. Failed postconditions hold the reply, including all streaming previews,
until they pass or the frame/session becomes obsolete. Results enter the next
prompt in escaped, explicitly untrusted task-data framing; events contain only
condition metadata. No plan means the existing watch behavior remains intact.

`verify_seal(browser, seal)` checks URL prefix and title regex without screenshots
and returns only slug/enrollment/verified metadata. Watch verifies a supplied seal
before every phase; mismatches raise `ReenrollRequired`. The newer profile
enrollment/store/CLI/authority implementation is preserved. Its `_cdp_marker`
seam now calls `check_session`, which publishes private discovery from Chromium's
`DevToolsActivePort` and runs the same bounded checks. Discovery is trusted only
after `_verify_profile_listener` proves the port has a live 127.0.0.1 listener whose
process names this profile's `--user-data-dir`, so a stale, dead, or recycled port
is polled past instead of verifying somebody else's browser; the same poll absorbs a
slow browser start. `attach` passes `LAUNCH_STARTUP_TIMEOUT` because it verifies
straight after launching, while every other caller keeps the 0.5s fast-fail budget.
Profile launches enable ephemeral loopback CDP; verification never falls back to the
desktop when CDP is unavailable, and errors remain metadata-only. A supplied watch
seal is task configuration, never permission to act.

Validation: `python -m pytest tests/test_computer_browser.py tests/test_browser_profiles.py tests/test_computer_browser_watch.py -q -rs`
(51 passed, 0 skipped on a host with a Chromium-family binary and loopback sockets).
The live browser test uses an isolated local fixture with DISPLAY removed, exercises
every check kind plus `verify_seal`, `check_session`, and profile-listener rejection,
and verifies listener/process cleanup; it skips only a missing executable or denied
loopback sockets. Test doubles cover transport errors, structured results,
untrusted framing, profile markers, lifecycle metadata, MCP plumbing, and watch
gating/recovery, including capture failure during held postconditions.
