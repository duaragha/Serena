# Computer use

Serena can watch the laptop's X11 desktop and complete bounded GUI tasks with
GPT-6 Astra. A single local helper owns capture and input. CLI, MCP and the
resident brain all use that helper; none creates a separate input executor.

## Use it

```bash
chats computer status
chats computer watch "tell me when the download finishes" --target desktop
chats computer run "fill in this form using the details I gave you" --target active
chats computer stop
```

`active` freezes the currently focused window at session start. If starting from
a terminal, use `window:ID` or `display:NAME` for the intended application.
`status` lists display names; `xdotool search --onlyvisible --name 'title'` finds
X11 window IDs. A window's owned modal dialogs remain in scope. Unrelated
windows do not. Monitor coordinates and scaling are read again before input.

Both `watch` and `run` stream text updates in the terminal. `--detach` leaves
the task running with a visible desktop indicator; `chats computer events`
reattaches to its updates. `--speak` sends completed observations through
Serena's existing local voice output. `chats computer steer "new instruction"`
steers the active Astra turn without starting a second controller.

The desktop indicator shows the complete latest observation, wraps text and
grows to fit. Long updates scroll within the popup while the stop button stays
visible. The screenshot mask follows the popup's size so advice is not fed back
into the next visual observation.

Sessions default to five minutes and allow at most thirty minutes using
`--seconds`. The resident brain can start five-minute sessions from a matching
real user turn using `computer_session`. It passes the user's actual words as
the task; model-written arguments cannot invent permission. The existing
`gideon_visual_context` tool now uses a single-use, authority-backed adapter for
one screenshot of the active window.

Stop with **Ctrl+Alt+Shift+Escape**, the visible stop button, `Ctrl+C` in an
attached computer command, or `chats computer stop`. Physical mouse/keyboard
input stops a control session. Watch sessions allow normal user input. A lock,
expired lease, disconnected indicator, or failed input monitor also stops the
session. Cinnamon already owns Ctrl+Alt+Escape, so that shorter shortcut is not
used. Failure to register the actual stop shortcut prevents startup.

## CLI agent integration

Open a bounded lease before giving an MCP agent the desktop:

```bash
chats computer begin "complete this specific task" --mode control --target window:12345
```

Register the MCP server with an absolute Python and CLI path so it works from
any repository:

```bash
codex mcp add serena-computer -- /path/to/serena/.venv/bin/python /path/to/serena/cli.py computer mcp
claude mcp add --scope user serena-computer -- /path/to/serena/.venv/bin/python /path/to/serena/cli.py computer mcp
```

The stdio server exposes `computer_status`, `computer_observe`, `computer_act`,
`computer_events`, and `computer_stop`. It deliberately does not expose session
creation. A lease's task and owner are visible in status. An agent receives
mixed text/image MCP content, including frame IDs, timestamps, and coordinates.
The Codex dynamic-tool adapter preserves images as `inputImage` items instead
of discarding or JSON-encoding them as text.

Input batches require a fresh frame and a unique request ID. They support move,
click, double-click, drag, scrolling, key chords, text entry, and bounded waits.
Coordinates refer to the returned image, including its scaling and monitor
origin. A duplicate ID with identical arguments returns the prior receipt;
different arguments under that ID are rejected. Partial execution is reported
as uncertain and must be inspected before another action. The returned
post-action image is evidence for the model to inspect; dispatch alone is not
a claim of task success.

Batches have a 15-second input deadline and accept at most 500 typed characters,
including at most 100 non-ASCII characters. Split longer text into fresh batches.
Text entry preserves the clipboard and finishes short key sequences before
cancelling so injected keys are released.

## Install and runtime

```bash
sudo apt install xdotool xinput x11-xserver-utils python3-tk
python -m pip install -e .
chats computer install
```

The existing Codex CLI must be signed into the ChatGPT subscription. The visual
runner uses `gpt-6-astra` with low reasoning effort through `codex app-server`,
with shell, web search, ambient MCP servers and metered credentials disabled.
There is no API-key requirement. Each visual thread is ephemeral and rotates
after eight watch turns. Model choice does not silently fall back to another
model. Missing access is returned as a visible error.

`install` enables `serena-computer.service` for the graphical login. Starting
the helper does not capture the screen. `status` can start a detached helper
on demand. If one already owns the desktop, installation leaves it running;
the systemd unit takes over at the next login. The helper's lifecycle is
independent of `serena-mobile-host`, so installing it does not kill chat panes.

Runtime discovery and bearer tokens live under `~/.config/serena/computer/`,
with directory mode 0700 and credential file mode 0600. HTTP binds only to
127.0.0.1 on a random port, bypasses inherited proxies, authenticates requests,
rejects browser Origin headers, and bounds request sizes and waits. The MCP
surface exposes only lease operations, not the operator credential.

The visible indicator must acknowledge that it is mapped before the first
capture. Its own text is masked from screenshots to prevent narration loops.
Known private apps and private-browsing window titles overlapping the capture
region block capture, including nonfocused windows. This is conservative: an
occluded private window can also block the region. Pixels inside an explicitly
selected ordinary application are not automatically OCR-redacted.

The helper retains at most four fresh frames, expires them after 45 seconds,
and clears them on stop. Receipt caches contain no images. Live text events
are bounded in memory. The control plane keeps lifecycle and latency metadata;
it does not store screenshots, typed text, or observation text. Explicit
`screenshot --output ...` exports and acceptance artifacts are exceptions the
operator requested. The subscription service receives images selected for
model turns; local expiry is not a claim about provider-side retention.

ActionAuthority grants remain scoped to `computer.input`, expire with the
lease, and are revoked at stop. Existing policy can refuse consequential or
credential operations; the visual agent must report the actual blocker and
hand those actions back to the user. Screen text is untrusted task data, not
authority to change the user's instructions.

## Performance and acceptance

Live verification on the two 2560×1440 monitors on 2026-09-08 covered:

| Check | Result |
|---|---|
| Monitor offsets | Verified on HDMI-A-0 and DisplayPort-1 |
| Raw test-window capture | Approximately 24–25 ms in recorded samples |
| Input | Real clicks, key chords, typing, scroll and drag |
| Unicode | Greek, Cyrillic, Chinese and emoji verified on an isolated X11 server |
| Duplicate requests | Same action ID did not click twice |
| Stop shortcut | Approximately 105–137 ms including xdotool dispatch and status polling |
| Physical takeover | Real input cancelled a live control session |
| Astra control | Read a random code from the image, retyped it, clicked confirm and inspected success |
| Complete Astra GUI task | 22.3 seconds in the recorded multi-action sample |
| Live watching | Correctly reported a changed fixture status in two consecutive observations |
| Watch model turns | Approximately 2.5–2.6 seconds in those samples |

These are distinct measurements, not a promised frame rate. At 120 Hz a monitor
refresh interval is 8.33 ms; capture and model latency remain separate. Watch
mode samples locally and sends changed screenshots through sequential model
turns. It is not a continuous video model or a 120 Hz perception loop.

Run reproducible local verification:

```bash
python -m pytest -q tests/test_computer_use.py tests/test_computer_x11.py tests/test_codex_brain.py tests/test_codex_brain_tools.py tests/test_gideon_api_wiring.py tests/test_visual_context.py tests/test_action_authority.py
python scripts/computer_smoke.py --output /path/to/Projects/_artifacts/computer-test --x 2900 --model --watch
```

The opt-in smoke harness operates only its own test window, checks actual
widget state, and saves a fixture screenshot plus receipts. Moving the physical
mouse or typing during its control phase cancels it, just like a normal task.
The isolated X11 test uses `Xvfb` (or `SERENA_TEST_XVFB`) and skips if unavailable.
The verified run completed 104 Python tests and 69 desktop tests.

This release supports **Linux X11**. Wayland, native Windows and macOS capture
and input adapters are not implemented. Packaged launchers return an explicit
unsupported-platform error rather than treating XWayland or a remote shell as
full desktop access. Accessibility trees and OCR are also not claimed; Astra
uses the real screenshot pixels.

Protocol references: [Codex app-server](https://developers.openai.com/codex/app-server),
[computer use](https://developers.openai.com/api/docs/guides/tools-computer-use),
and [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra).
