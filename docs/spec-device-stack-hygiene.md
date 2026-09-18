# Spec: Device Stack Hygiene (the whole minor list)

## Overview

**What**: Every small fix across the phone, voice, sideload and messaging stack, batched into one pass so they stop being footnotes in bigger specs.
**Why**: Each item is individually too small to spec and collectively the difference between a demo and something that survives a week. They are the accumulated findings of the ConscioussAI teardown research: stale docs, a token in the wrong place, unpinned tooling, a logged password, a 90-second timer that will kill a 40-minute hold.
**Scope**: IN — behavioural and configuration fixes to the phone/voice/sideload/messaging paths listed below, plus the stale docs that describe them. OUT — structural refactors (`docs/spec-hygiene.md` owns splitting `ui/web.py`, deduping `fleet/`, the memory-v2 cutover and the uncommitted-backlog triage; that spec is explicitly "pure moves and deletions, no behaviour change"). These two never touch the same lines.

## Requirements

- [ ] WHEN any item below lands THEN it has a test or a recorded manual verification, and nothing else changes with it
- [ ] WHEN the pass is complete THEN no document in the repo describes a state that is no longer true
- [ ] WHEN a credential could reach a log THEN it does not, after this pass

## Architecture / Design

**Changes** (blast radius), grouped:

**Phone tooling**
- `MODIFIED integrations/shared/sideload/scripts/device_control.py` — pin pymobiledevice3 to the 11.15.x line rather than `>=11.3.0`; the uv cache currently holds everything from 11.3.1 to 11.15.4 and the display and HID APIs moved in that range.
- `ADDED` a developer-disk-image remount step after a phone reboot, and a keep-awake power assertion (`PowerAssertionService.create_power_assertion("PreventUserIdleSystemSleep", …, 300)` renewed every 120 s) so a mirror session does not die when the screen sleeps.
- `MODIFIED` the same script's preflight to say plainly when `usbmuxd` is inactive (it is, on the laptop, right now) instead of failing with "Unable to retrieve device list".

**Messaging**
- `MODIFIED core/bluebubbles_line.py` — the server password rides in the query string, so scrub URLs before logging, and assert the server is only reachable over the tailnet.
- `MODIFIED` webhook/poll paths — deduplicate by message GUID (`new-message` and `updated-message` both fire for one message) and read the chat GUID from `data.chats[0].guid`.
- `MODIFIED` typing indicator — re-send every 2–3 s while composing, since it expires on its own.

**Calls** (origin/master)
- `MODIFIED voice/phone/bridge.py` — the idle hang-up (90 s) becomes per-phase so hold does not trip it; echo cancellation is enabled for the phone-network leg; the fixed 4× caller gain becomes automatic gain control; block 911, 0, 900/976 and international patterns in the dialer, and record the E911 decision (without it, each 911 attempt costs $75).
- `MODIFIED` codec configuration — force PCMU on the trunk leg; verify the STIR/SHAKEN attestation level with the carrier.

**Voice**
- `MODIFIED voice/call/metrics` — log image-bearing turns separately (`had_image`, `prefill_ms`) so vision cost is visible in the p50/p90 tables.
- `MODIFIED voice/call/WAKEWORD.md` — it still says the model export is pending; the model has been installed since 2026-07-18 and the open item is field acceptance.

**Mobile app**
- `MODIFIED apps/mobile/src/settings.ts` — the auth token lives in WebView `localStorage`; move it to the iOS Keychain (`@capacitor/preferences` is already a registered dependency and unused).
- `MODIFIED apps/mobile/README` — it still describes an Android client with a mock daemon.

**Repo**
- `MODIFIED` branch state — `laptop-master` is behind `origin/master`, and `voice/phone/` plus `core/phone_call.py` exist only on origin. Rebase before any call work starts.

**Key decisions**:
- **One pass, one commit per group**, so a revert is surgical.
- **Docs count as code here.** A stale document cost this project real time twice during the teardown research; fixing them is part of the work, not a nicety.
- **No refactors.** If an item starts wanting a structural change, it stops being hygiene and goes to its own spec.

## Tasks

> Execution: direct, sequenced, alone — many small edits across files other specs are also touching. Run it when no other track is mid-flight.

### Phase 1: Credentials and logs
- [ ] BlueBubbles URL scrubbing, tailnet-only assertion, mobile token to Keychain
  - accept: no password appears in any log path; the app still authenticates after a cold start
  - engine: direct

### Phase 2: Phone tooling
- [ ] Version pin, DDI remount, power assertion, usbmuxd preflight message
  - accept: a fresh boot plus plug-in reaches a working session without hand-holding; the error message names `usbmuxd` when it is off
  - engine: direct

### Phase 3: Call-path fixes
- [ ] Per-phase idle timer, echo cancellation, automatic gain, dialer blocklist, PCMU, attestation check
  - accept: a 10-minute silent hold does not hang up; a test call has no echo and even levels; blocked patterns refuse
  - engine: direct (on origin/master)

### Phase 4: Messaging + metrics + docs
- [ ] GUID dedupe, typing keep-alive, image-turn metrics, WAKEWORD.md, mobile README
  - accept: one reply per message under webhook and poll; typing persists; metrics distinguish image turns; both documents match reality
  - engine: direct

## Edge Cases / Gotchas

- The Keychain move must not lock him out of an app that is mid-refresh: keep reading the old location once, migrate, then stop.
- Pinning pymobiledevice3 too tightly means missing the fixes landing weekly right now; pin the minor line, not the patch.
- Enabling echo cancellation on the *wrong* leg would undo the tuning that made his own calls work (250 ms buffer, 4× gain were deliberate fixes) — the trunk leg only.
- Blocking number patterns in the prompt is not blocking; it has to be in the dialer.
- Rebasing `laptop-master` while other sessions have uncommitted work in the tree is the one step here that can hurt; do it when the tree is quiet.

## Testing

- [ ] Log-scrubbing test with a password-bearing URL
- [ ] Token migration test (old location present, then absent)
- [ ] Dialer blocklist tests
- [ ] Idle-timer phase tests (talking, on hold, silent)
- [ ] GUID dedupe test
- [ ] A docs check: every file this spec touches is re-read after the change

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: Phase 3 needs origin/master; run the whole spec when no other track is mid-flight
**Review**: —
