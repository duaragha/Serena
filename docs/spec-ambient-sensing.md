# Spec: Ambient Sensing (cheap always-on awareness)

## Overview

**What**: A small always-on sensor that records what Raghav is working on — active window, title, browser tab, idle, lock, power, clipboard shape — into a capped local store, plus a classifier that spots when he is stuck.
**Why**: Serena's only awareness today is a screenshot watch session he has to start, lasting 5–30 minutes (`core/computer_watch.py`), and `core/visual_context.py` states outright that there is no watcher or polling loop. The competitor's Ambient Mode is cheap and always on because it reads **accessibility metadata, not pixels** — foreground app, window title, browser URL, an activity class, a clipboard hash — and attaches the last ~20 minutes to every message. That is a better trade than screenshots: near-zero CPU, no image tokens, and far less to leak. His laptop is Cinnamon on **X11**, which makes this straightforward today.
**Scope**: IN — the X11 sensor, the browser-tab reporter, the store with retention and forget controls, the stuck classifier, a CLI and visible state. OUT — acting on it (spec-proactive-policy owns interrupts and check-ins), screenshots (already exist), Wayland (skip: he is on X11 and moving machines is not on the table).

## Requirements

- [x] WHEN the active window or its title changes THEN an event is recorded within 1 s, event-driven, with no polling loop burning CPU
- [x] WHEN he is idle THEN idle is recorded, and periods where Serena herself drove the mouse or keyboard are tagged as agent-driven rather than counted as him being active
- [x] WHEN the active app or URL is on the denylist THEN nothing about it is recorded at all — filtered before capture, not redacted after
- [x] WHEN the browser is focused THEN the active tab's URL and title are recorded, and incognito windows are skipped entirely
- [x] WHEN the clipboard changes THEN only a hash, size and owner are stored, and anything marked as a password-manager secret is skipped
- [x] WHEN `chats ambient forget --minutes 15` runs THEN those events are gone from memory and disk immediately
- [x] WHEN the store exceeds its caps THEN the oldest events are dropped (ring buffer of 2,000 events; 30-day and size ceiling on disk)
- [x] WHEN two or more struggle signals coincide within 10–15 minutes THEN the classifier marks "stuck" — and that is the only thing that may wake a model

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/ambient_sensor.py` — the X11 backend. `PropertyNotify` on the root window's `_NET_ACTIVE_WINDOW`, plus `_NET_WM_NAME` on the currently active window (tab switches change only the title), unsubscribing from the old window each switch and swallowing `BadWindow` on close. App identity from `WM_CLASS`; PID from XRes rather than trusting `_NET_WM_PID`. Idle from the XScreenSaver extension. Lock from logind `LockedHint` (and `PrepareForSleep`), power from UPower `OnBattery`. Clipboard via XFixes selection-notify, hashing only.
- `ADDED core/ambient_store.py` — SQLite plus an in-memory ring buffer (2,000 events), with ActivityWatch-style heartbeat merging: consecutive identical events collapse into one with a longer duration, which is what keeps the store small. Retention sweep on start and every 6 h.
- `ADDED core/ambient_classify.py` — the activity class (`focused | browsing | stuck | idle`) and the struggle rules: repeated rewordings of the same search or the same error page, bouncing between the same 2–3 windows, repeated failing test or build runs, undo bursts, long dwell with little input. Two or more within the window, or it is not stuck.
- `ADDED integrations/browser-extension/` — a small MV3 extension for Edge (his default, `core/laptop_actions.py:41`) reporting tab activation, tab updates and window focus over **native messaging** (host manifest pinned to the extension id via `allowed_origins`), plus the native host script. No remote-debugging port: Chrome 136+ ignores it on the default profile, and an open CDP port is an unauthenticated handle on every tab.
- `ADDED systemd/serena-ambient.service` — user unit alongside the existing resident services.
- `ADDED chats ambient` CLI — `status | pause | resume | forget [--minutes N | --all] | recent`.
- `MODIFIED core/visual_context.py` — it already defines `ScreenshotAdapter`, `OCRAdapter` and `AccessibilityAdapter` protocols (`:185-207`); the sensor becomes the accessibility-shaped source behind that interface rather than a parallel universe.

**Key decisions**:
- **Metadata, not pixels.** Cheap, private, and enough. Screenshots stay opt-in and on-demand.
- **Denylist before capture.** Recall's lesson is that filtering after the fact leaks; password managers, Tor, private windows and bank or login URLs are dropped at the sensor, so they never exist in the store.
- **Heartbeat merging** keeps a full day inside a few thousand rows.
- **Tag agent-driven input.** Serena's own XTest input resets the X idle timer; without tagging, her working looks like him working, and every "is he here?" decision downstream is wrong.
- **Small rules first, model second.** A local classifier runs constantly and a model is woken only after "stuck" fires — orders of magnitude cheaper and faster than asking a model every few minutes.
- **Visible state and a pause button.** Always-on sensing that he cannot see or stop is not something to ship.

## Tasks

> Execution: fleet, one run. Privacy-sensitive: the denylist and forget controls land in Phase 1, before any storage of real activity.

### Phase 1: Sensor + store + controls
- [x] `ambient_sensor.py`, `ambient_store.py`, denylist, retention, forget controls, CLI, systemd unit
  - accept: window and idle events recorded event-driven with negligible CPU over an hour; a denylisted app produces zero rows; `forget --minutes 15` provably removes rows from memory and disk
  - engine: fleet

### Phase 2: Browser tabs
- [x] MV3 extension + native messaging host + incognito skip
  - accept: switching tabs records URL and title within 1 s; an incognito window records nothing; the host refuses any origin but the extension
  - engine: fleet

### Phase 3: Classifier
- [x] `ambient_classify.py` with the activity classes and the two-signal stuck rule
  - accept: replaying a recorded "stuck" session fires once; a recorded focused session never fires; agent-driven periods never count as activity
  - engine: fleet

## Edge Cases / Gotchas

- Window titles are themselves sensitive: they carry document names, email subjects and meeting participants. The denylist must cover titles, not only app names.
- The idle timer resets on synthetic input — the single most important correctness detail in this spec.
- `LockedHint` is only as reliable as the screen locker; verify Cinnamon actually sets it, and fall back to the screensaver signal if not.
- Tab titles change without a tab switch (a background tab finishing a load); debounce so a noisy page does not flood the store.
- The store is a behavioural profile in a file. 0600, local only, never synced to the PC, and never included in a backup that leaves the machine.
- Do not let ambient events become a second memory system — this is a short-lived buffer, and anything worth keeping is written through the existing memory path.

## Testing

- [x] Sensor tests against a faked X connection (switch, title change, window close, BadWindow)
- [x] Denylist tests: denylisted app, denylisted URL pattern, incognito
- [x] Heartbeat-merge and retention tests
- [x] Forget tests (minutes and all), including the in-memory buffer
- [x] Classifier fixtures: stuck session fires once, focused session never fires
- [x] Agent-driven tagging test: XTest input does not register as user activity

---

## Progress Log

**Status**: Complete (Phases 1–3, fleet run 7b743f12)
**Branch**: serena/fleet/7b743f12-c5cd-42fb-a1fe-f09b1cfcf8d5/agent-a
**Current phase**: Done
**Last completed task**: Phase 3 classifier with two-signal stuck rule + latch
**Files modified**: core/ambient_denylist.py, core/ambient_store.py, core/ambient_sensor.py, core/ambient_classify.py, core/visual_context.py (AmbientAccessibilityAdapter), cli.py (chats ambient), systemd/serena-ambient.service, integrations/browser-extension/ (manifest, background, native host, installer, README), tests/test_ambient_*.py
**Blockers**: none. Ops-time verifications for enablement: Cinnamon LockedHint behavior (screensaver fallback wired), Edge extension id install step, git-hook install per repo (`scripts/ambient-git-hook.py --install --repo <path>`).
**Review**: denylist landed in Phase 1 before any storage, per spec; PID falls back to _NET_WM_PID (this python-xlib has no XRes binding)
**Fix (run 7b743f12)**: wired agent-driven tagging into computer_platform injection paths (fail-soft, 2 s re-arm); removed the tautological XFixes drain branch + stash replay for mid-fetch events; bounded the size-cap trim (one batch/sweep, 1000-row floor, post-commit VACUUM); long_dwell reads a 30-min slice with a per-window quiet gate; vcs stream ships as scripts/ambient-git-hook.py (operator installs the links); disk reads return newest-first; system sampler seeds without emitting; window PID kept in meta; background.js reports foreground tabs only; dwell clamped to the window. 258 focused tests green.
**Fix-2 (run 7b743f12)**: git hook tolerates git's own hook parameters (parse_known_args; hook-name mapping wins, so pre-push can no longer abort a push); background.js gates tab reports on the focused window, not just tab.active. Focused suites green.
