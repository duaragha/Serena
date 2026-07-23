# Serena call pipeline v2a

This package is the server half of the pinned v2a call path. It stays
import-safe: importing `voice.call` does not import or load torch,
faster-whisper, CTranslate2, ONNX Runtime, Silero, NumPy, or Kokoro.

`voice.call.handle_websocket(ws)` is the synchronous Flask-Sock boundary.
The `/ws/call` route authenticates first, then delegates the connected socket.
`warm_default_runtime_background()` starts one idempotent daemon warmup thread.
Warm failures appear in `call.ready.models` and `error` controls instead of
bringing down the web server.

The server also exposes authenticated `/ws/desk`. It shares the already-warmed
STT, brain, and TTS workers with the call runtime while keeping a separate
Silero endpoint pool tuned to 650 ms trailing silence by default. The local
desk client and its privacy boundary are documented in `voice/desk/README.md`.

`GET /desk/greeting` returns one short, varied, already-synthesized PCM16
greeting from a bounded local pool. The pool refills through the same resident
brain and local TTS backends. It never calls a cloud speech service. A 503 with
`Retry-After: 1` means the pool is still warming; thin clients then use their
last-good local assistant greeting or an honest local tone.

## Wire contract

Every binary frame starts with this versioned 24-byte, big-endian header:

| Field | Width | Value |
| --- | ---: | --- |
| magic | u32 | `0x53524341`, ASCII `SRCA` |
| version | u8 | `1` |
| kind | u8 | `1` mic PCM16, `2` TTS PCM16 |
| flags | u16 | bit 0 is final, every other bit is rejected |
| sequence | u32 | starts at zero for each JSON generation |
| sample rate | u32 | Hz |
| timestamp | u64 | sender-local monotonic microseconds |

PCM payloads are signed little-endian PCM16. Client mic frames are exactly
3,200 samples, 6,400 bytes, mono, 16 kHz, or 200 ms. The server rejects bad
magic, versions, kinds, flags, rates, partial samples, and frame sizes. Sequence
gaps are reported in both telemetry and a `sequence.gap` control.

JSON controls from Android are `call.start`, `ptt.begin`, `ptt.end`, `cancel`,
`hangup`, `ping`, `pong`, `rtt.report`, `playback.started`,
`playback.segment_started`, `playback.underrun`, and `sequence.gap`.
Generations live in JSON, not the binary header. Output sequence resets to zero
after each generation. The server sends `audio.start` before PCM and
`audio.end` after the generation's last chunk. The final flag remains
available for bounded streams, while call TTS uses `audio.end` so it never
holds a chunk waiting for the next sentence. A generation check happens again
immediately before every audio send, so canceled or reconnected work cannot
leak stale audio.

`ptt.end` carries the Android monotonic timestamp captured when the user
released push-to-talk. `playback.started` is sent only after Android observes
the `AudioTrack` playback head advance, but it may describe the short local
acknowledgement. `playback.segment_started` marks the first real content frame
after its playback-head boundary advances. Its same-device monotonic
EOU-to-content duration is the only app-level acceptance metric. Generic first
output, acknowledgement, and play-call-return timings remain diagnostics.
Server estimates use its own monotonic stages plus the full rolling RTT for
the inbound and outbound network legs. They remain diagnostic estimates, not
substitutes for the phone measurement or an acoustic loopback test.

The first content playback-head acknowledgement in a call also carries a
one-shot `call_hello` marker, Android process uptime, and whether the call came
from a cold-launch deep link. The server records that as `call.hello`, so the
under-five-second gate survives after the UI closes instead of existing only
as an on-screen number. `call_hello` requires `app_uptime_ms`, and the shared
call runtime accepts it once for a call ID across websocket reconnects.

Each application `ping` starts a nonblocking read of the server's local
Tailscale peer state. The matching `pong` carries the current direct or relay
route, a server-issued `sample_id`, and `server_processing_us`; Android
subtracts that server-side lookup time from the measured round trip. The
server accepts the subsequent `rtt.report` exactly once for that sample ID and
overwrites all client route claims with its own per-sample measurement, so
roaming samples cannot inherit a stale or spoofed route label. The RTT value
itself remains client-measured diagnostic telemetry, not a security signal.

## Pipeline

The receive path only validates and queues input. Silero sees exact 512-sample
windows and uses sample counts, not wall time, for endpointing. Its
configurable pre-roll is retained. Tests inject a fake probability model
without importing torch.

STT and TTS each live in a persistent local NDJSON subprocess. The models stay
warmed across normal turns. Canceling a generation terminates its active model
process, so an obsolete CTranslate2 or ONNX inference cannot hold the next PTT
turn behind an uncancelable Python thread. The next use restarts and warms only
the process that was terminated. Queued generations are namespaced by call and
can be canceled before they reach inference. Model readiness has a bounded
timeout, so a driver or model-load hang becomes an explicit `call.ready` error.

The faster-whisper process transcribes English with beam size 1,
`condition_on_previous_text=False`, and `vad_filter=False`. It only selects
CUDA when CTranslate2 reports an available CUDA device and supported compute
type. Otherwise it runs CPU int8. There is no cloud STT.

The streaming brain client sends this NDJSON shape to `brain.sock`:

```json
{"type":"turn","request_id":"...","protocol":"voice","text":"...","stream":true,"call_id":"...","turn_id":"..."}
```

It accepts only matching `response.start`, `response.delta`, `response.done`,
and `error` records. Done metadata preserves the fields actually received,
including `elapsed` and `turns`. Acceptance evidence requires `session_id`,
the exact call `turn_id`, and a positive `session_turns` count on every
completed voice turn. Closing an abandoned stream interrupts the daemon SDK
turn and releases its global turn lock. On Windows, the loopback TCP stream
requires the per-boot token from `brain.json`. On Unix, the stream is a
mode-0600 socket. The discovery file is mode 0600 on Unix and restricted to
the current user, SYSTEM, and Administrators on Windows.

If the stream cannot respond before a turn starts, the current `brain.json`
HTTP `/turn` path is used with its Bearer token and labeled
`brain.json-http-nonstream`. That fallback uses an async loopback transport.
Canceling it closes the connection, interrupts its daemon SDK turn, and
releases the shared turn lock. The deterministic stub is for tests and the
benchmark only.

Brain deltas feed an incremental sentence splitter. Kokoro synthesizes one
sentence at a time and returns small PCM chunks immediately after that
sentence is ready. The async backend boundary supports a future true-stream
Orpheus or CosyVoice adapter, but neither is claimed or installed here. The
pipeline never uses edge-tts, ElevenLabs, network TTS, or persisted raw audio.

### Laptop voice selection

`voice.call.voice_quality` measures the installed local voices on the serving
machine, exports one WAV per candidate, and applies hard first-PCM, realtime
factor, and clipping gates. Naturalness remains an explicit curated/listening
choice instead of a fake waveform score:

```bash
HF_HOME=~/.cache/serena/pocket-tts \
python -m voice.call.voice_quality \
  --pocket-python "$PWD/.venv-pocket/bin/python"
```

The report is written privately to
`~/.local/state/serena/voice-quality.json`; samples live beside it in
`voice-quality-samples/`. Pocket Alba is the laptop default because its casual
profile clears the realtime gate. Kokoro remains a local fallback, not the
interactive default.

## Install and models

Install the scoped requirements with the exact Python interpreter that runs
`chats serve`:

```bash
python -m pip install -r voice/call/requirements.txt
```

Here, `python` means the exact interpreter that runs the mobile host. The
separate `.venv-pocket` environment owns only the local Pocket TTS worker. It
does not provide the call server's STT, VAD, transport, or orchestration
dependencies.

Kokoro uses these existing local assets:

```text
voice/models/kokoro-v1.0.int8.onnx
voice/models/voices-v1.0.bin
```

Stage a converted faster-whisper model locally at
`voice/models/faster-whisper-small.en`, or set
`SERENA_CALL_WHISPER_MODEL` to another local converted model directory. The
worker sets `local_files_only=True` and does not fetch a model at call time.

For the Windows RX 6800 XT host, DirectML applies to ONNX TTS only. Replace the
regular ONNX Runtime distribution installed through Kokoro, then install the
DirectML scope with the same serving interpreter:

```bash
python -m pip uninstall onnxruntime
python -m pip install -r voice/call/requirements-windows-directml.txt
```

Set this before launching the server:

```text
SERENA_CALL_ONNX_PROVIDER=DmlExecutionProvider
```

`ONNX_PROVIDER=DmlExecutionProvider` is also accepted. The Serena-specific
variable wins. The provider is checked against
`onnxruntime.get_available_providers()`. Missing or failed DirectML falls back
to `CPUExecutionProvider` only when no provider was explicitly required. When
`SERENA_CALL_ONNX_PROVIDER` or `ONNX_PROVIDER` is set, an unavailable or failed
provider makes warmup fail instead of silently missing the latency target.
`call.ready.model_details` and telemetry expose the selected provider.
CTranslate2 does not use DirectML, so faster-whisper runs CPU int8 on this AMD
host unless a separately supported CUDA device is actually reported.

## Android lifecycle and authentication

The native Android websocket sends the call token in the Authorization header,
not the URL. The unauthenticated `/app` static route injects only the same-origin
websocket URL. A fresh client must save its token in settings once, and the
server never publishes that credential in page source.

Playback uses a single bounded executor. Cancellation drains queued PCM before
placing cleanup directly behind the one active write. Buffer overflow closes
the call explicitly instead of growing heap and latency without limit. If the
app backgrounds, the plugin sends hangup and closes capture, playback, timers,
and the socket. v2 is deliberately an in-app call, not a background microphone
service.

## Telemetry and verification

Metrics append to `~/.config/serena/call_metrics.jsonl`. They contain timings,
counts, queue depth, generation, direct/relay/unknown path, and brain metadata.
They do not contain PCM or transcripts. Automatic endpoint EOU timestamps are
backdated by Silero's measured trailing-silence samples, so the derived
EOU-to-first-playable estimate includes endpoint delay. Every received 200 ms
batch records the current rolling websocket RTT over the Tailnet, measured
path, path source, RTT sample id and age, and the client monotonic capture
timestamp. Stage rows split endpoint close, STT,
brain first delta, sentence closure, TTS first PCM, server send, and playback
ack so percentile reports show which segment owns a miss. Playback-start acks
also carry the exact single-phone-clock app-level PTT measurement.

Run focused verification from the repository root:

```bash
python -m pytest -q voice/call/tests
python -m compileall -q voice/call
python -m voice.call.benchmark --iterations 50
python -m voice.call.benchmark --metrics ~/.config/serena/call_metrics.jsonl
```

After a physical 20-minute call, close it normally and run:

```bash
python -m voice.call.integrity --heard-clean
```

The analyzer selects the latest call unless `--call-id` is given. It requires
at least 20 minutes, three completed user turns, a clean final hangup, no
sequence gaps, underruns, queue overflows, or call errors, content playback for
every transcribed turn, and an exact call/turn marker plus assistant response
in Claude's real session JSONL. It reads transcript content only to verify the
markers and emits no transcript text. `--heard-clean` is deliberately required
for a full acceptance claim because telemetry cannot prove that a human heard
no distortion. Without it, a clean run can pass transport integrity but cannot
claim the acoustic gate.

## Read-only voice tool acceptance

With the resident brain live and local TTS assets installed, run:

```bash
python -m voice.call.tool_acceptance \
  --tts-python .venv-pocket/bin/python
```

The v2.5c harness sends a real voice-protocol turn over `brain.sock`, requires
the resident session transcript to prove a completed allowlisted read-only
tool call, synthesizes the answer with the configured local TTS backend, and
fails if a Claude or Codex session, assistant CLI process, or Serena session
metadata file appears or changes during the turn. It persists no raw audio or
tool output.

## Zero-metered-cost audit

Capture a fail-closed baseline immediately before the physical acceptance
call:

```bash
python -m voice.call.cost_audit \
  --capture-baseline ~/.config/serena/call_cost_baseline.json
```

The baseline proves that Claude is using first-party Max subscription OAuth,
the one discovered resident brain is local and has no metered-provider auth
override, and the live health counter belongs to that exact daemon boot. Run
the call and close it normally. After the Anthropic billing dashboard has had
time to settle, verify that it shows no metered charge for the acceptance
window, then run:

```bash
python -m voice.call.cost_audit \
  --baseline ~/.config/serena/call_cost_baseline.json \
  --billing-dashboard-clear
```

The audit rejects stale call telemetry, daemon replacement, duplicate brain
processes, cloud speech provenance, direct brain API transport, unfinished
call task workers, and any metered-provider environment override in the audit,
call host, or resident daemon. The flag is a human attestation of the dashboard
check. Automation cannot inspect billing truth from local process telemetry.
The SDK `notional_cost_usd` counter is a model-price estimate and is never
reported as actual spend.

## Mid-call draft links

The narrow v2 task action is explicit and deterministic. A turn matching
`draft <request> and send me a link` is the go, with an optional `okay` or
`please` prefix. Brainstorming, hypotheticals, negation, a draft request without
link delivery, and other state-changing requests do not start a job.

The call persists one idempotent `draft_link` job for the exact call and turn
before starting a worker. The worker is a separate headless Claude print turn
with no tools, no MCP servers, and `ANTHROPIC_API_KEY` removed. It can return
Markdown but cannot edit a project, send a message, or publish anything. Jobs
and replayable events live in
`~/.local/state/serena/work_jobs.sqlite3`. Generated drafts live under
`~/.local/state/serena/artifacts/<job-id>/`.

The call websocket emits `job.accepted`, `job.progress`, `artifact.ready`, or
`job.failed`. A reconnect replays durable events and the app deduplicates them
by `event_seq`. `artifact.ready.url` is a same-host `/artifacts/<capability>`
path. The capability is HMAC signed, expires after 24 hours, resolves only to a
registered file under the artifact root, rejects symlinks and changed content,
and is served only to loopback or Tailnet source addresses. It never embeds the
chat bearer token and never exposes the general file reader.

The app shows the job below the live transcript and turns it into an `open
draft` action when ready. It fetches and renders the draft inside the call UI,
then sends a native phone acknowledgement over the call socket. The native
plugin will only send that acknowledgement for a receipt captured by its own
completed, same-host fetch. The server signs, records, and atomically consumes
each receipt once, so reconnect replay cannot manufacture another open. The
job keeps running if the call websocket drops, and submitting the same call
turn again returns the original job rather than starting a second worker.

During the physical call, open the draft in the phone app before hanging up,
then run the task gate beside the normal call integrity gate:

```bash
python -m voice.call.task_acceptance --call-id <call-id>
```

It verifies that every job in that call reached `artifact_ready`, the signed
artifact still resolves with the recorded hash, Android acknowledged the ready
event and opened the in-app preview before clean hangup, and both the resident
call session and headless worker session are linked. Clean hangup starts a hard
lifecycle boundary if a call id is ever reused. A server send, stale receipt,
or CLI attestation is not accepted as phone delivery proof.

The deterministic benchmark is a regression harness. Its JSON says
`hardware_path_measured: false` and `acceptance_claim: false`. It cannot prove
the under-two-second p90 objective. That gate requires the physical Android
capture and playback path, Tailnet direct and relay cases, the running brain,
the staged Whisper model, the selected ONNX provider, and the actual RX 6800
XT Windows host.
