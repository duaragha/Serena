# Serena Live Voice Daemon

A resident process that speaks each assistant **sentence** the instant it is
written, replacing the per-turn Stop-hook cold start. The old hook
(`voice/kokoro_speak.py`) stays in place as a fallback — the daemon is an
optimization, not a requirement.

## Why

The Stop-hook path (`kokoro_speak.py`) fires only when a turn *ends*, then
cold-starts Python + (optionally) the Kokoro model every turn, adding a ~1s gap
before the first word. The daemon stays resident: it loads Kokoro once, tails
the active transcript, and starts speaking sentence one while the rest of the
reply is still being written.

## Architecture

```
  Claude writes JSONL ──► TranscriptTailer ──► IncrementalSplitter ──► TTSQueue
  (~/.claude/projects)     (0.4s polling)       (sentence boundaries)    (edge-tts / Kokoro)
```

| File | Role |
|------|------|
| `voice/voice_common.py`       | Shared config, paths, `clean()`/`chunk()`, brain-bridge state, global `SpeakerLock`, mute + daemon-liveness helpers. Single source of truth for both the daemon and the Stop-hook. |
| `voice/transcript_tailer.py`  | Polls the active transcript (Stop-hook hint first, freshest-JSONL fallback), surfaces the growing current-turn assistant text, signals turn end on idle. |
| `voice/sentence_splitter.py`  | Incremental segmentation: emits only the NEW complete sentences on each feed, holds back unterminated fragments until `flush()` (turn end). |
| `voice/daemon_tts.py`         | One worker thread owns all audio. edge-tts (Ava) primary, Kokoro fallback per sentence. Bounded queue (backpressure), mute mid-clip barge-in, Kokoro warmed once at startup. |
| `voice/live_daemon.py`        | Orchestrator: wires tailer → splitter → queue, holds the speaker lock while talking, manages the pidfile, handles SIGTERM/SIGINT. |
| `voice/daemon_cli.py`         | `status` / `mute` / `unmute` / `stop-playback` / `logs`. |
| `voice/run_voice_daemon.sh`   | Foreground runner using the voice venv. |
| `voice/systemd/serena-voice-daemon.service` | systemd `--user` unit (NOT auto-installed). |

## Run it

Foreground (Ctrl-C to stop):

```bash
voice/run_voice_daemon.sh
```

As a `--user` service (auto-restart, starts with the GUI session). **Not
auto-installed** — copy it in yourself when you want it:

```bash
cp voice/systemd/serena-voice-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now serena-voice-daemon.service
journalctl --user -u serena-voice-daemon -f      # logs
```

Stop / disable:

```bash
systemctl --user disable --now serena-voice-daemon.service
```

## Controls

```bash
voice/.venv/bin/python voice/daemon_cli.py status         # alive? state? muted?
voice/.venv/bin/python voice/daemon_cli.py mute           # silence + cut current audio
voice/.venv/bin/python voice/daemon_cli.py unmute
voice/.venv/bin/python voice/daemon_cli.py stop-playback  # barge-in now
voice/.venv/bin/python voice/daemon_cli.py logs -n 60
```

These wrap the same well-known files the Stop-hook honors, so the existing
`serena-voice {stop|off|on}` script and the `voice_off` mute file keep working
whether or not the daemon is running.

## How the daemon and Stop-hook coexist

`kokoro_speak.py` (the Stop hook) now:

1. Writes the active transcript path to `/tmp/serena_active_transcript` so the
   daemon knows exactly which JSONL to tail (no guessing among open chats).
2. Checks the daemon pidfile (`~/.config/serena/voice_daemon.pid`). If the
   daemon is **live**, the hook *yields* — it does not speak — so the two never
   double-speak. If the daemon is **down**, the hook runs its original
   detached-speak path exactly as before.

Both acquire the global `~/.config/serena/voice.lock` before any TTS, so even a
transient race can't produce overlapping voices.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `SERENA_TTS_ENGINE`        | `auto`            | `auto` (edge→kokoro), `edge`, or `kokoro`. |
| `SERENA_EDGE_VOICE`        | `en-US-AvaNeural` | edge-tts neural voice. |
| `SERENA_VOICE_RATE`        | `+50%`            | edge-tts speech rate. |
| `SERENA_VOICE` / `SERENA_VOICE_SPEED` | `af_heart` / `1.5` | Kokoro voice + speed. |
| `SERENA_TTS_QUEUE_MAX`     | `50`              | Bounded playback queue (backpressure cap). |
| `SERENA_TAIL_INTERVAL`     | `0.4`             | JSONL poll interval (s). |
| `SERENA_TRANSCRIPT_HINT_AGE` | `0` (off)       | Ignore Stop-hook hint older than N s; fall back to mtime glob. |

## Design decisions

The research plan flagged several open questions; resolved as:

1. **Incomplete sentences** — held until a sentence boundary `[.!?…]`; the
   trailing fragment is spoken only at turn end (`flush()`). No "The…" stutters.
   Because Claude appends each assistant block as one finalized line, a tail that
   already ends in a terminator is treated as complete immediately (no
   whitespace wait).
2. **edge-tts failure** — degrades to Kokoro for *that sentence* (not dropped).
3. **Mute** — the daemon stays resident but silent; it does not exit. Mute is
   re-checked between every chunk and cuts the current clip immediately.
4. **Startup** — `WantedBy=graphical-session.target` (GUI-only; audio + brain
   overlay need a session). Not auto-enabled.
5. **Transcript discovery** — Stop-hook stdin passthrough (fastest, exact) with
   freshest-JSONL mtime glob as the robust fallback.

## Troubleshooting

- **No audio at all** — check `paplay`/`pw-play` exist and `PULSE_SERVER` /
  `XDG_RUNTIME_DIR` are set in the service env; tail `voice/daemon_cli.py logs`.
- **Double voices** — a stale pidfile or a Stop-hook that didn't see the daemon.
  Run `daemon_cli.py status`; the global lock should prevent overlap regardless.
- **Wrong chat spoken** — the daemon follows `/tmp/serena_active_transcript`; if
  stale, set `SERENA_TRANSCRIPT_HINT_AGE` so it falls back to the freshest file.
- **Daemon won't start** — `journalctl --user -u serena-voice-daemon`; a second
  instance refuses to start while a live pidfile exists.

## Tests

```bash
voice/.venv/bin/python voice/tests/test_live_daemon.py
```

Covers transcript discovery, current-turn extraction (skips thinking/tool_use),
incremental splitting + fragment hold-back, no-double-speak across feeds, turn
resync, mute gating, queue backpressure, daemon liveness, and the speaker lock.
