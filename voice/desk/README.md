# Serena desk loop

This is the thin v2.5 desk face. It keeps the microphone and wake-word model on
the local machine, then uses the same authenticated STT, resident brain, and
local TTS runtime as `/ws/call` after a valid `hey serena` trigger.

## Privacy and failure boundary

- The openWakeWord model consumes local 16 kHz mono PCM in 80 ms frames.
- Before a wake, the client opens no call or greeting connection. Raw
  microphone audio is never persisted and never leaves the machine.
- After a wake, the client starts the authenticated `/ws/desk` connection and
  fetches one already-synthesized greeting from `/desk/greeting` in parallel.
- A last-good assistant greeting is stored locally. If both the brain host and
  that cache are unavailable, the immediate fallback is an honest local
  two-tone cue. The cue is not reported as Serena's voice.
- A dropped remote session reconnects twice with short bounded backoff. A local
  cue marks a successful reconnect so Raghav knows to repeat an interrupted
  turn.
- If the remote voice host stays down, the already-open microphone enters a
  local cold lane: faster-whisper transcribes in memory, a tool-free and
  non-persistent Claude CLI turn answers, and local Kokoro speaks the reply.
  This lane can continue for several turns. It stores neither PCM nor
  transcripts and cannot claim live tool state.
- A missing `hey_serena.onnx` model is a hard stop before the microphone opens.
  The client never substitutes another phrase or bundled model.
- The systemd service also requires a frozen acceptance manifest. Model and
  verifier hashes, openWakeWord version, gate settings, audio contract, and
  exact microphone identity are verified before the microphone opens.

The production wake-only listener adds a second local faster-whisper phrase
check after the frozen openWakeWord gate. It emits structured journal events
containing timestamps, score, accepted boolean, word count, and verifier hash.
It does not persist audio or transcripts.

The wake-to-first-write metric is a diagnostic PortAudio buffer-write time. It
is explicitly marked `acoustic_acceptance_claim: false`. The 1.5 second gate
still needs an audible or loopback measurement on the actual desk hardware.

## Data path

```text
local mic, 80 ms
  -> openWakeWord, local only
  -> wake gate
  -> hot varied greeting, local playback
  -> 80 ms to exact 200 ms PCM16 frame packer
  -> authenticated /ws/desk
  -> Silero endpoint, faster-whisper, resident brain, local TTS
  -> PCM16 playback with real per-chunk dot amplitude

remote host unavailable after bounded reconnects
  -> local faster-whisper, in-memory only
  -> cold tool-free Claude CLI turn, no session persistence
  -> local Kokoro PCM playback
```

The visual state stays on the existing state-file and local websocket bridge:

```text
~/.config/serena/voice_state
ws://127.0.0.1:8765
```

States are `idle`, `listening`, `thinking`, and `speaking`. Speaking amplitude
is calculated from every PCM chunk actually handed to PortAudio. If samples
stop for 260 ms, the renderer falls back to a synthetic breathing pulse.

## Install

Create the dedicated local runtime:

```bash
python3 -m venv voice/.venv-wake
voice/.venv-wake/bin/python -m voice.call.install_desk_runtime
```

Export the custom model with the official openWakeWord synthetic Piper Colab,
then promote it through Serena's importer. No local CUDA or real recordings
are required. The exact workflow is in `voice/call/WAKEWORD_TRAINING.md`:

```bash
voice/.venv-wake/bin/python -m voice.call.wakeword_model install \
  --model ~/Downloads/hey_serena.onnx \
  --source openwakeword-colab
```

Calibration then freezes the accepted runtime contract at:

```text
~/.config/serena/models/hey_serena.onnx
~/.config/serena/wakeword-acceptance.json
```

The production service reads the verifier, threshold, patience, cooldown,
device selector, and all other wake settings only from that manifest. Command
line wake flags remain available for development collection, but the service
will not substitute them for a missing or mismatched manifest.

Stage the local cold-lane Whisper model at:

```text
voice/models/faster-whisper-tiny.en/
```

The existing Kokoro assets remain at `voice/models/kokoro-v1.0.int8.onnx` and
`voice/models/voices-v1.0.bin`. The desk runtime installer includes
faster-whisper, Kokoro, websocket-client, sounddevice, and the ONNX-only
openWakeWord runtime. The local lane starts lazily only after a remote outage.

For a remote home brain, create `~/.config/serena/desk.env`:

```text
SERENA_DESK_URL=wss://YOUR-TAILNET-HOST:8445/ws/desk
SERENA_DESK_GREETING_URL=https://YOUR-TAILNET-HOST:8445/desk/greeting
```

The shared token stays in `~/.config/serena/chat_token` with mode 0600. It is
sent only in the Authorization header.

Install the units only after the real model exists and port 8765 is available:

```bash
install -Dm644 systemd/serena-brain-bridge.service ~/.config/systemd/user/serena-brain-bridge.service
install -Dm644 systemd/serena-desk.service ~/.config/systemd/user/serena-desk.service
install -Dm644 systemd/serena-dot-overlay.service ~/.config/systemd/user/serena-dot-overlay.service
systemctl --user daemon-reload
systemctl --user enable --now serena-brain-bridge.service serena-dot-overlay.service serena-desk.service
```

The service conditions keep `serena-desk.service` stopped while the production
model, frozen manifest, or token is missing. Do not remove those gates to test
with a different wake phrase.

## Verification

```bash
.venv/bin/python -m pytest -q voice/desk/tests
.venv/bin/python -m ruff check voice/desk voice/brain_bridge.py
npm --prefix voice/desktop run test:dot-field
journalctl --user -u serena-desk.service -f
```

Timing and state telemetry lands in
`~/.local/state/serena/desk_metrics.jsonl`. It contains no audio and no
transcripts. The frozen model can run the desk loop the same day. A week-long
false-wake verdict comes later from the separate passive observation collector,
not from a short desk-loop smoke test.

Mark intentional two-stage attempts immediately before saying the wake phrase,
then generate the structured livability report:

```bash
.venv/bin/python -m voice.desk.wake_acceptance attempt
.venv/bin/python -m voice.desk.wake_acceptance report
```

The report at
`~/.local/state/serena/wake-two-stage-report.json` requires seven observed
background hours across seven active days, twenty marked attempts, no more
than five percent misses, no unintended final accepts, and one unchanged local
phrase-verifier identity. Code or a short smoke run cannot set
`acceptance_claim` to true.

After a physical session with at least three turns and one deliberate
interruption, close it normally and run:

```bash
.venv/bin/python -m voice.desk.acceptance --heard-clean
```

That report independently requires complete endpoint, STT, brain, first-write,
and audio-end evidence for every accepted turn, a first-write p90 at or below
1.5 seconds, no runtime failure events, a physical barge-in, a clean hangup,
human heard-clean confirmation, and the accepted two-stage wake report.
