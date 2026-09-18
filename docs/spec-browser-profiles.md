# Spec: Saved Browser Auth Profiles

## Overview

**What**: Authenticate once per site into an isolated browser profile; computer-use and automations reuse the logged-in session without ever touching credentials.
**Why**: Every login today is re-typed through the screenshot loop, and agent credential entry is blocked by design (TIER_SECRET + private-app capture walls). Persistent profiles break the re-login loop without breaking the security model.
**Scope**: IN — per-site profile store, user-typed enrollment with agent verification, seal/use lifecycle, hygiene + audit. OUT — agent-typed passwords (stays forbidden); a Serena password vault (browser store + OS keyring instead); sharing the daily-use profile.

## Requirements

- [ ] WHEN `browser enroll <slug>` runs THEN it launches an isolated Chromium profile and a watch-mode session scoped to its window for verification
- [ ] WHEN enrollment completes THEN the profile seals with (enrolled_at, expected post-login marker: URL prefix + title regex) and the browser quits
- [ ] WHEN a sealed profile is used THEN the session is re-verified against its marker before any task proceeds
- [ ] WHEN a profile cookie expires THEN use fails with an explicit re-enroll signal, never a silent logged-out session
- [ ] WHEN any launch/status action runs THEN only doctored metadata is shown/logged (slug, timestamps, expiry signal — no cookies, no page content)

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/browser_profiles.py` — store + lifecycle: `enroll/verify/seal/attach/status`, `profiles/<slug>/` under `~/.config/serena/` (0700), one `--user-data-dir` per site/purpose
- `ADDED chats browser` CLI group — `enroll/use/status/remove` mirroring `chats computer`
- `MODIFIED core/action_authority.py` — enrolled-profile launch is tier-2 computer-adjacent; credential entry stays TIER_SECRET/user-only (no widening)
- `MODIFIED core/computer_use.py` — attach path: launch sealed profile, marker-check, hand scoped session to agent
- Secrets: browser's own encrypted cookie store via OS keyring session; any Serena-held tokens use the `mcp/secrets.py` keyring pattern (`serena.browser` service); launches audited as 0600 JSONL like laptop actions

**Key decisions**:
- User types, agent watches — enrollment keeps the credential blind: private-app capture block + TIER_SECRET stay exactly as-is
- Per-site isolated profiles, never the daily driver — blast-radius isolation; a compromised automation profile leaks one site
- No Serena password vault — browsers + OS keyrings already solve encrypted-at-rest cookies; don't rebuild it badly
- Seal markers over blind trust — every attach re-verifies logged-in state; expiry is detected, not assumed away
- CDP verification is import-guarded, not awaited — seal/attach verify via title through the desktop service today and upgrade to URL+title over CDP the moment track 3's `computer_browser` lands, with the method recorded per verification

## Tasks

> Execution: direct — security-sensitive, judgment-heavy, touches authority policy. No fleet.

### Phase 1: Store + enroll
- [ ] `core/browser_profiles.py` + CLI + enroll flow (launch + watch-verify + seal with markers)
  - accept: enroll on a fixture login flow seals with correct marker; agent-side logs contain zero credential-adjacent content
  - engine: direct

### Phase 2: Attach + hygiene
- [ ] Sealed attach with marker re-verify + expiry signal + metadata-only status + audit log + authority policy addition
  - accept: attach verifies marker before task; expired cookie yields re-enroll signal; audit log has launches with no page content
  - engine: direct

## Edge Cases / Gotchas

- First-run browser dialogs (sync offers, default-browser nags) can break marker checks — enrollment must dismiss or record them, not fail silently
- Concurrent attaches of one profile: Chromium locks user-data-dir — serialize or refuse with clear error, never corrupt
- Profile browsers must not sync to Raghav's Google account — launch flags disable sync/signin to keep the profile purpose-bound
- Removal must wipe the profile dir completely (cookies included) — partial delete is a credential leak

## Testing

- [ ] Enroll/seal lifecycle test on a local fixture login page
- [ ] Marker re-verify + expiry-signal test
- [ ] Concurrent-attach refusal test
- [ ] Metadata-only audit test (no cookies/content in logs)
- [ ] Authority test (launch allowed tier-2, credential entry still refused)

---

## Progress Log

**Status**: Implemented (phases 1-2), built direct by Serena 2026-09-17
**Branch**: working tree (direct build alongside fleet track 3)
**Current phase**: Done, pending review
**Last completed task**: Phase 2 — attach + hygiene + authority gate + `chats browser` CLI
**Files modified**: `core/browser_profiles.py` (new), `core/browser_cli.py` (new), `tests/test_browser_profiles.py` (new), `cli.py` (registration)
**Blockers**: —
**Review**: self-review vs requirements: all 5 MET (enroll launches isolated profile; seal records marker + quits; attach re-verifies pre-task; expiry yields explicit re-enroll signal; metadata-only status/audit). 10/10 tests, ruff clean. Live browser enroll not exercised (needs a display + real login) — first real enroll is the acceptance. Ready for Raghav review.
