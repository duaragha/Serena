# Spec: Phone Mirror (Serena's eyes on the iPhone)

## Overview

**What**: A local daemon that holds one pymobiledevice3 tunnel to the iPhone, decodes its live display stream, and serves the current screen (and crisp full-res stills) to Serena over a local RPC socket.
**Why**: Raghav wants "video call your assistant and she sees your phone". The competitor does it with an on-device broadcast extension at ~1 low-res fps relayed through their cloud. We can do better from the laptop with **no app on the phone**: no app slot (all 3 free SideStore slots are taken, Serena runs as a LiveContainer guest and can never capture), no $99 developer account for a phone he replaces with Android in Nov–Dec 2026, and no weekly re-sign. pymobiledevice3 11.15.x ships an actively developed CoreDevice display stream, measured by its maintainer at ~53 fps with zero dropped frames on iOS 27.0.
**Scope**: IN — daemon process + tunnel lifecycle, HEVC display stream → frames, change detection, full-res stills, lock/privacy gating, local RPC, `chats phone-mirror` CLI, a desktop-shaped adapter so computer-use can drive it. OUT — any iOS app work (ScreenCaptureKit and ReplayKit are both dropped, see `knowledge/ai-desktop-assistant/conscioussai-gap-roadmap.md`), taps (spec-phone-control), attaching frames to turns (spec-vision-turns), Wi-Fi/away-from-home (USB-only in v1), audio, Android (its own spec after the switch).

## Requirements

- [ ] WHEN `chats phone-mirror start` runs with the phone on USB THEN a userspace tunnel opens with no root and the first decoded frame arrives in under 5 s
- [ ] WHEN the stream is live THEN `frame` returns a JPEG no older than its declared expiry, with width/height/captured_at/signature, in the same shape `core/computer_use.observe()` returns
- [ ] WHEN the screen does not change THEN no new frame is emitted (change-gated, not clock-gated)
- [ ] WHEN Serena needs to read small text THEN `still` returns a full-resolution PNG from `ScreenCaptureService`, not an upscaled video frame
- [ ] WHEN the phone is locked (`DeviceInfoService.get_lockstate()`) THEN frames are refused with an explicit reason, never served
- [ ] WHEN a foreground app holds the camera or microphone (error 9022) THEN the daemon reports "quit that app on the device" and exits cleanly instead of retrying
- [ ] WHEN the USB cable is pulled THEN the daemon marks itself disconnected, stops serving stale frames, and re-establishes when the phone returns
- [ ] WHEN any frame is served THEN it exists only in memory; nothing is written to disk unless `--debug-frames` is passed

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/phone_mirror.py` — the daemon. Owns `UserspaceRsdTunnel(serial=udid)` (`pymobiledevice3/remote/userspace_tunnel.py:752`) as an async context manager, then `DisplayService(rsd)` (`remote/core_device/display_service.py:48`). Starts video with `start_video_stream(receiver_ip, receiver_port, sender_ip, display_id=1)` (`:74`) **after** binding the receiver via `open_media_receiver(svc, (4MB, 1MB), bind_port=0)` (`remote/core_device/screen_stream.py:557`) — the device pushes RTP the instant it answers.
- `ADDED core/phone_frames.py` — RTP → pixels. `depacketize_hevc(payload, fu_buffer, nal_out)` (`screen_stream.py:161`) → Annex-B NALs → `HevcToBgraTranscoder(vps, sps, pps, on_frame=…)` (`remote/core_device/hevc_av.py:59`, PyAV, BGRA on a worker thread, `.width`/`.height` from SPS). Then downscale + JPEG (quality 90, `subsampling=0`) and compute the same 320×200 greyscale sha256 signature `computer_use.observe()` uses, so both sources look identical downstream.
- `ADDED core/phone_rpc.py` — `ThreadingHTTPServer`, single `POST /rpc`, bearer token, `Origin` header must be absent, `fcntl` exclusive lock on a state file. This is a deliberate copy of `core/computer_service.py:231-268`, **not** Flask: this process is not the web app.
- `ADDED systemd/serena-phone-mirror.service` — user unit, **not** `WantedBy=default.target`; started on demand by the CLI because it needs the cable.
- `ADDED chats phone-mirror` CLI group — `start|stop|status|frame|still|watch`.
- `MODIFIED core/computer_platform.py` — new `PhoneDesktop` implementing exactly the duck-type `ComputerController` consumes (`core/computer_use.py`): `name`, `locked()`, `active_window()`, `release()`, `monitors()`, `window_info()`, `visible_windows()`, `context()`, `capture(rect)`, `close()`, and the input methods (stubs here; spec-phone-control fills them). `create_desktop()` (`:470`) gains a `kind` argument instead of hard-refusing anything but X11.
- `MODIFIED core/computer_use.py` — `observe()` re-checks the focused window after capture and raises `ComputerTransientError` (`:359-363`); a phone has one surface and no window ids, so gate that behind a `supports_focus_recheck` flag on the desktop object.

**Key decisions**:
- **Its own process, not a thread in the mobile host.** The userspace tunnel is a PyTCP global singleton: one per process, not thread-safe (`userspace_tunnel.py:779`). Everything else follows from that.
- **Consume the Python API, don't scrape `serve-web`.** `ScreenStreamServer` (`screen_stream.py:646`) has no public frame hook — `_subscribers` is private — so wrapping it would mean parsing our own `/stream.bin` chunks. `serve-web` stays as the human debugging mirror (it also gives clipboard, bezel buttons and rotation).
- **Video for awareness, stills for reading.** There is *no* fps/resolution/quality negotiation in the API; the encoder caps around 6 Mbps and collapses resolution to a corner under sustained motion. So text gets read from `ScreenCaptureService.capture_screenshot()` (`remote/core_device/screen_capture_service.py:17`, PNG bytes) instead of chasing video quality.
- **One RSD serves everything.** Display, HID, screenshot, pasteboard and power assertion all ride the same `RemoteServiceDiscoveryService`, which is why the daemon owns it and hands out capabilities over RPC.
- **Reuse the existing frame budget** (4 frames, 45 s expiry) and change thresholds (0.005 trigger, 0.05 supersede) from `computer_use`/`computer_watch` rather than inventing new ones, so `WatchFrames` works unchanged against a phone session.

## Tasks

> Execution: fleet, one run, phases in order. Phase 1 is a measurement gate — nothing else starts until the numbers are real.

### Phase 1: Tunnel + stream proof
- [ ] Minimal script: userspace tunnel → display stream → decoded frame; record fps, first-frame latency, CPU, and behaviour on cable pull
  - accept: on Raghav's iPhone 16 Pro (iOS 27.0), ≥15 fps sustained for 60 s and first frame under 5 s, numbers written into this spec's Progress Log; `usbmuxd` must be running (it is currently inactive on the laptop)
  - engine: direct (needs the physical phone)

### Phase 2: Frame store + RPC
- [ ] `core/phone_frames.py` + `core/phone_mirror.py` + `core/phone_rpc.py` + systemd unit + CLI
  - accept: `chats phone-mirror frame` returns a fresh JPEG with the `observe()` field shape; `still` returns a full-res PNG; no frames on disk; idle screen emits no new frames
  - engine: fleet

### Phase 3: Desktop adapter + computer-use integration
- [ ] `PhoneDesktop` in `computer_platform.py`, `create_desktop(kind=…)`, `supports_focus_recheck` gate, and a watch session against the phone
  - accept: `WatchFrames` runs unmodified against a phone session and fires `screen_changed` when the phone screen changes
  - engine: fleet

### Phase 4: Gating + audit
- [ ] Lock-state refusal, 9022 camera/mic message, disconnect handling, per-session audit line (0600 JSONL, metadata only)
  - accept: locking the phone stops frames with a clear reason; opening the camera app produces the "quit that app" message, not a retry storm; unplugging marks disconnected within 2 s
  - engine: fleet

## Edge Cases / Gotchas

- `stop_media_stream` must be the only reply-bearing request on its connection, and `stop_all_streams` needs a *fresh* connection, or `dtremotedisplayd` fatally asserts (`display_service.py:230`). Tear down in that exact shape.
- Media sessions are device-global: `stop_all_streams` kills every session, including a `serve-web` window Raghav has open. Check before stopping.
- PLI barrages corrupt or stall the encoder on iOS 26/27 (`screen_stream.py:2650`) — never request keyframes in a loop.
- The built-in reconnect loop (`:2888`) is **tunneld-based**; a userspace tunnel gets none, so the daemon must re-establish itself, and PyTCP's one-per-process rule means a full `aclose()` before reopening.
- `--userspace` is USB-only on Linux; Wi-Fi needs `sudo pymobiledevice3 remote tunneld`. Out of scope for v1, but keep the transport behind one function so Wi-Fi is a later swap.
- Developer Mode must stay on and the developer disk image is remounted after each phone reboot (see spec-device-stack-hygiene).
- Frames are screen content: they can contain 2FA codes and banking screens. Metadata-only logs, memory-only frames, and the lock gate are the whole privacy story for v1.

## Testing

- [ ] Depacketize + decode unit test against a recorded RTP capture (`capture_rtp_to_file`, `screen_stream.py:401`) — no phone needed in CI
- [ ] Change-detection test: identical frames emit nothing; a 1% change emits once
- [ ] Frame expiry/budget test mirroring the `computer_use` frame tests
- [ ] Lock-state and 9022 refusal tests with a faked service
- [ ] RPC auth test: missing bearer refused, `Origin` present refused, second instance blocked by the lock file

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: phone must be on USB and `usbmuxd` active for Phase 1
**Review**: —
