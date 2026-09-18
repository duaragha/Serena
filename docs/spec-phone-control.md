# Spec: Phone Control (taps in any app, with permission)

## Overview

**What**: Serena taps, swipes, types and presses hardware buttons on the iPhone through the same daemon that streams its screen, gated by action authority.
**Why**: This is the capability the competitor cannot match. On mobile their agent can only act **inside its own built-in browser**; Serena drives real apps — Uber, the banking app, Messages — because the input path is CoreDevice HID from the laptop, not an in-app webview. The pieces exist but are crude: `integrations/shared/sideload/scripts/device_control.py` opens a fresh tunnel per command, takes a screenshot per action, and has no typing at all.
**Scope**: IN — a `DeviceAdapter` for phone input, HID session management, coordinate mapping, sensitive-surface escalation, Astra tool exposure, migrating `device_control.py` onto the daemon. OUT — the frame source (spec-phone-mirror), approval delivery (spec-approval-routes), Android (later).

## Requirements

- [ ] WHEN a tap is requested THEN it is delivered through the daemon's existing HID session, with no new tunnel and no per-action screenshot
- [ ] WHEN coordinates are given in frame pixels THEN they are scaled to the 0–65535 HID space using the live display size, and a mismatch between frame size and display size refuses the action
- [ ] WHEN the target app is on the sensitive list (wallet, banking, purchases, Messages) THEN the action is tier-2 and requires confirmation before it lands
- [ ] WHEN any input is attempted without an active display stream THEN it is refused with a clear reason, not silently dropped by the device
- [ ] WHEN Raghav touches the phone himself THEN the current control session ends, matching how physical input stops a desktop control session
- [ ] WHEN `/sideload` runs its existing device-control flows THEN they still work, now routed through the daemon

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/adapters/phone_ios.py` — a `DeviceAdapter` (`core/adapters/base.py:60`) named `phone`, modelled on `core/adapters/android_adb.py:24-37`:
  ```python
  _CAPABILITIES = {
      "phone.screen": "read", "phone.lockstate": "read", "phone.apps": "read",
      "phone.tap": "reversible", "phone.swipe": "reversible", "phone.type": "reversible",
      "phone.button": "reversible", "phone.launch_app": "reversible",
      "phone.tap_sensitive": "external",
  }
  ```
  with the same defensive target validation the Android adapter uses for package/serial strings.
- `MODIFIED core/device_actions.py` — register the adapter in the default runner alongside Laptop/Android/HomeAssistant/MQTT (`:725-732`).
- `MODIFIED core/phone_mirror.py` (from spec-phone-mirror) — hold the HID handle. Input is gated by the device: without an active media stream backboardd drops every report (`hid_service.py:97-110`), so use `touch_session(rsd, display_id=1)` (`:547`), which opens the stream, drains RTP, waits 0.3 s for surface re-matching, and yields `UniversalHIDServiceService`. The mirror's stream and the HID gate are therefore the same stream — a second one must never be opened.
- `ADDED core/phone_input.py` — the verbs over that handle: tap (`send_touchscreen(TOUCHSCREEN_STATE_CONTACT/RELEASE, x, y)`), swipe/drag (stepped contacts), buttons via `IndigoHIDService.send_button` with the named codes (HOME `0x0C,0x40`; LOCK `0x0C,0x30,0.5`; VOL_UP `0xE9`; VOL_DOWN `0xEA`; MUTE `0xE2`; SIRI `0xCF,1.0`), and typing via `create_keyboard_service()` then `send_keyboard(usage_codes)` (pressed-set, not deltas).
- `MODIFIED core/computer_tools.py` — `act` is registered only for control-mode sessions (`:44`); add phone verbs to that tool so Astra can drive a phone session exactly as it drives the desktop.
- `MODIFIED integrations/shared/sideload/scripts/device_control.py` — detect a running daemon and delegate; keep the standalone per-command path as the fallback so the skill never breaks.

**Key decisions**:
- **Input rides the mirror's stream.** It is not an optimisation, it is the device's authentication gate. This is why control and eyes are one daemon rather than two.
- **Sensitive surfaces escalate, and we detect them from what is on screen**, because iOS gives no API for the foreground app (Apple DTS is explicit about this). OCR plus the launched-app record is the evidence; when in doubt, escalate rather than tap.
- **Reversible by default, confirmation for consequences.** A tap is usually harmless and gating every one would make her useless; the escalation list is where the safety lives.
- **Physical input wins.** Same rule as the desktop: he touches it, she stops.
- **Frame-to-display size check before every action.** The existing script already refuses screenshots older than 180 s or of changed dimensions; keep that instinct — a tap computed against a stale frame lands somewhere random.

## Tasks

> Execution: fleet, one run, after spec-phone-mirror Phase 3. Phase 3 here needs spec-approval-routes.

### Phase 1: Input verbs
- [ ] `core/phone_input.py` + HID session ownership in the daemon + coordinate scaling from `get_display_info()`
  - accept: tap, swipe, type and each named button land on the real phone; a size mismatch refuses; typing "hello serena" into Notes is exact
  - engine: direct (physical phone)

### Phase 2: Adapter + authority
- [ ] `core/adapters/phone_ios.py`, registry wiring, effects/tiers, audit receipts
  - accept: capability map declares effects; a tier-2 action without confirmation is denied and audited; adapter status reports cable/daemon state
  - engine: fleet

### Phase 3: Sensitive escalation + agent tools
- [ ] Sensitive-surface detection, escalation to confirmation, phone verbs in `computer_tools.visual_tools`, physical-input abort
  - accept: a tap on a banking or purchase screen requires confirmation; Astra completes a two-step task ("open Notes, type X") in a phone control session; touching the screen ends the session
  - engine: fleet

### Phase 4: Migrate device_control.py
- [ ] Delegate to the daemon with fallback; keep the sideload skill green
  - accept: `/sideload` screenshot-gated flows work unchanged and faster (no per-action tunnel)
  - engine: fleet

## Edge Cases / Gotchas

- Gesture-surface coordinates (`send_digitizer`) are signed Int32 pointer coords, **not** the normalised 0–65535 touchscreen space. Mixing them silently taps the wrong place.
- `send_keyboard` takes the full pressed set each call, so modifiers and key-release are explicit; a dropped release leaves a key stuck down on the device.
- Text fields with autocorrect will rewrite typed text. For anything exact (codes, passwords — which she must never type anyway), verify by reading the field back.
- Launching an app via `AppServiceService.launch_application(bundle_id, kill_existing=True)` kills the running instance by default; for "go back to what he had open" flows pass `kill_existing=False`.
- Tapping while the phone is locked does nothing useful and may reveal a lock screen in frames: refuse when `get_lockstate()` says locked.
- The 5-minute default and 30-minute ceiling on desktop watch sessions is a good model here; a phone control session should never be open-ended.

## Testing

- [ ] Coordinate scaling unit tests (edges, rounding, mismatch refusal)
- [ ] Adapter contract tests mirroring `tests/test_adapters_*` for capabilities/status/describe/execute/compensate
- [ ] Authority tests: reversible allowed, sensitive requires confirmation, denial audited
- [ ] Stuck-key regression: every keyboard send is followed by a release
- [ ] Sideload skill smoke test against the daemon path and the fallback path

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: spec-phone-mirror Phase 3; spec-approval-routes for Phase 3 here
**Review**: —
