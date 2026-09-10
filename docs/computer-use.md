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
identifies the focused application and window title from the latest captured
frame. In browsers this normally includes the selected tab's title. It refreshes
while the model is thinking, without waiting for another coaching reply. This
label describes focus within the selected capture scope; desktop mode still
captures the desktop. Missing or expired frame details show a waiting label.
The indicator
grows to fit. Long updates scroll within the popup while the stop button stays
visible. The screenshot mask follows the popup's size so advice is not fed back
into the next visual observation.

Watch replies stream into the popup as a labelled draft before completion. A
changed page clears the draft, and only completed advice enters conversation
history. The initial check asks for a useful step or visible blocker even when
the parent chat already contains related advice. Later `UNCHANGED` replies show
that the screen was checked rather than leaving a reading message. Active checks
display elapsed seconds; `inspection_completed` events expose model and first
token timings, including checks that produce no new advice.

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

Ask the connected chat to watch a selected screen or complete a specific GUI
task. The chat calls `computer_start` directly from that request; you do not
need to open a terminal session manually or send a second confirmation.

`computer_start` defaults to live desktop coaching with `background=true`,
using GPT-6 Astra at medium reasoning with fast processing. Select a window/display explicitly when
the user requests that narrower scope. Background sessions stream through
`computer_events` and the desktop indicator. Do not send input from the chat
alongside a background controller.

The background worker is a separate ephemeral model thread linked to the exact
launching Codex or Claude conversation. The caller resolves its full session ID;
the helper never guesses a parent by title, directory or recency. Status shows
`source_session_id`, `source_agent` and the worker's `context_message_count`.
The worker receives the parent's earlier user/assistant text, new messages and
completed coaching. After each eight-turn rotation it reloads this text history.
Images, hidden reasoning and tool transcripts are not copied into this history.

`chats computer install` (or `install-hooks`) registers a `UserPromptSubmit`
hook for Codex and Claude, preserving existing hooks. Codex requires reviewing
and trusting the installed command through `/hooks`; reopen an existing chat
if its hook configuration is cached. On the next question, the hook adds all
completed coaching from this exact chat to its native conversation context.
Thus 30 earlier chat messages plus 10 coaching updates plus a follow-up are
available together. Coaching persists after stop/restart. The transcript itself
is never rewritten. `computer_history` or `chats computer history SESSION_ID`
provides a read-only fallback. A standalone terminal without a chat identity
has local coaching history but cannot infer which conversation to link.

Worker context is passed verbatim up to a 700 KB per-request guard; exceeding it
produces a visible error instead of silently dropping earlier messages. Native
parent-chat compaction still applies to very long chats. Stored text remains
available for retrieval; this is not an unlimited model context window.

Use `background=false` only for deliberate interactive MCP sessions handled
by the connected chat's own model. This shares the screen but does not start
automatic coaching. The indicator calls this sharing and says automatic
coaching is off. Status reports the session's `driver` and `observation_state`
so clients can distinguish a shared screen from a running visual worker.

Already-open chats may have the older tool list cached. `computer_status`
returns current startup guidance from the helper; those chats can execute
the CLI fallback themselves after the user's request: `chats computer watch
--detach` for live guidance, or `chats computer run --detach` for a GUI task.
Legacy `begin --mode watch` also starts the watcher. `begin --interactive`
explicitly opens sharing only, and internal single-use captures never start
a watcher. Reloading the MCP connection makes `computer_start` available.
An interactive example:

```bash
chats computer begin "complete this specific task" --interactive --mode control --target window:12345
```

Register the MCP server with an absolute Python and CLI path so it works from
any repository:

```bash
codex mcp add serena-computer -- /path/to/serena/.venv/bin/python /path/to/serena/cli.py computer mcp
claude mcp add --scope user serena-computer -- /path/to/serena/.venv/bin/python /path/to/serena/cli.py computer mcp
```

The stdio server exposes `computer_start`, `computer_status`, `computer_observe`,
`computer_act`, `computer_events`, `computer_history`, and `computer_stop`. The local chat is an
operator surface: it starts only the task its user requested, and screen text
cannot supply authorization. A lease's task and owner are visible in status. An agent receives
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
runner uses `gpt-6-astra` with medium reasoning effort and `service_tier=fast`
through `codex app-server` (the accepted server tier is `priority`),
with shell, web search, ambient MCP servers and metered credentials disabled.
There is no API-key requirement. Each visual thread is ephemeral and rotates
after eight watch turns. Model choice does not silently fall back to another
model. Missing access or an unaccepted fast tier is returned as a visible error.
Fast mode is scoped to computer workers and does not change the parent chat's
model or effort. [Fast mode](https://learn.chatgpt.com/docs/agent-configuration/speed)
uses 2.5 times standard Codex credits where available. It does not remove
model inference latency.

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
it does not store screenshots or computer-input payloads. A private
`conversations.sqlite3` stores linked chat text and completed coaching for
follow-up questions; it is retained across sessions, with file mode 0600.
Chat text can include details the user typed in the conversation. Explicit
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

The original model timings above used low reasoning before the switch to
medium. They are historical samples, not a latency promise for the current model.
At 120 Hz a monitor refresh interval is 8.33 ms; capture and model latency remain
separate. Watch mode now checks for meaningful changes locally every 150 ms
(or as fast as capture permits), including while Astra is thinking. A change
clears stale guidance, waits 200 ms for page painting to settle, interrupts an
obsolete turn and sends the newest frame. Superseded replies cannot become
the current observation. The popup polls active state every 100 ms.

The detector excludes the popup and ignores small changes such as a caret or
JPEG noise. Detection is approximate: tiny updates can be missed and substantial
animation can repeatedly interrupt reasoning. Fresh medium-effort guidance
still takes model inference time. This is not continuous video or 120 Hz
perception. `screen_changed`, `superseded` and timestamped `observation` events
separate local detection from model latency.

To reduce restarts from animated badges and small desktop updates, pixel-only
changes must cover 0.5% of the sampled image (previously 0.15%). Window identity
and tab/title transitions still trigger a fresh check regardless of pixel area.
This does not remove inference latency or make continuously changing pages
instantaneous.

The updated loop was verified with real X11 fixture screenshots and Astra at
medium on an isolated 1600×1000 display: three page changes were detected in
57–123 ms, and two fresh replies arrived 3.06 and 7.31 seconds after the page
painted. The latter included interrupting an obsolete turn. No answer for that
obsolete page was published, and popup updates did not trigger another turn.
These samples describe that fixture, not a guaranteed desktop latency.

Fast/medium acceptance on the same isolated fixture detected three changes in
56–58 ms. Fresh guidance took 4.71 and 7.06 seconds, the latter including a
cancelled obsolete turn. This small sample does not establish a speedup.
A real native Codex test created 30 chat messages and 10 stored coaching updates,
then answered the 41st message using a random detail from the first chat message
and another from the last coaching update, injected through the trusted native
hook. Both Codex and Claude transcript tests also verify restart and isolation.

Run reproducible local verification:

```bash
python -m pytest -q tests/test_computer_use.py tests/test_computer_conversation.py tests/test_computer_x11.py tests/test_codex_brain.py tests/test_codex_brain_tools.py tests/test_gideon_api_wiring.py tests/test_visual_context.py tests/test_action_authority.py tests/test_session_identity.py
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
