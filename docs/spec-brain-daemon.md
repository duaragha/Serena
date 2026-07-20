# Serena Brain Daemon — Spec

*Drafted 2026-07-14, out of the front-door build and the Gideon research passes.
This is the "durable nervous system" piece: one resident Serena process that is
instantly present, watches for events, and can speak first. Everything else
(front door, telegram, voice, panes) becomes a surface of this one brain.*

## Why now

- The front door works but every turn is a cold `claude -p`: ~4.5s floor after
  optimization (was 14-30s). Instant presence needs a resident process.
- All proactivity today is fake: nothing is awake when no chat is open. The
  Telegram bot, voice pipeline, and memory are all reactive surfaces waiting
  for Raghav to initiate.
- The research verdict (July 2026, in `knowledge/ai-desktop-assistant/`):
  don't wait for smarter models, the memory/permission/orchestration layer is
  the durable bet. This daemon is that layer's runtime.

## The runtime decision (researched, settled)

**Claude Agent SDK, Python, Streaming Input Mode.** This is the load-bearing
finding:

- `ClaudeSDKClient` with an async-generator input is explicitly designed as a
  long-lived process: one live session, new user messages fed in over time,
  interrupts supported, images supported. Not one-shot `query()` calls.
- Session persists as normal JSONL under `~/.claude/projects/`; resumable and
  forkable (`fork_session`) like any chat.
- `create_sdk_mcp_server` gives in-process tools (plain Python functions), no
  subprocess/IPC per tool call. Event-source adapters become cheap tools.
- **Billing: runs on the Max subscription.** Auth via `CLAUDE_CODE_OAUTH_TOKEN`
  (`claude setup-token`, prefix `sk-ant-oat01-`). CRITICAL: the daemon's env
  must ensure `ANTHROPIC_API_KEY` is UNSET, if both are present the API key
  silently wins and every turn becomes pay-per-token.

## Architecture

```
 events                       brain                        surfaces
──────────                ─────────────                ────────────────
fs watchers ──┐           ┌───────────┐    reply       front door (UI)
sched ticks ──┤  queue    │ SDK client │ ──────────►   telegram (chats text)
ledger ages ──┼─────────► │ (resident  │    speak-first desktop notification
mail/cal* ────┤  (judge)  │  session)  │ ──────────►   voice (later)
pane events ──┘           └───────────┘    spawn       coding panes
                            │      ▲
                            ▼      │
                     ledger + memory files (same stores as today, no new system)
```

- **One brain, many surfaces.** The front door's `/api/frontdoor` stops
  spawning `claude -p` and instead talks to the daemon over a local socket.
  Away-from-desk delivery goes through the Serena MOBILE APP over the
  tailnet, self-owned end to end. Raghav's explicit call (2026-07-15): no
  third-party service (Telegram included) in Serena's core loop. The daemon
  queues proactive messages; the mobile app drains the queue and raises
  native notifications. Telegram remains only as a legacy fallback until the
  mobile path ships, then it goes. Panes stay separate full Claude
  Code sessions, the daemon spawns and seeds them (existing spawn plumbing)
  and reads their ledgers; it does not replace them.
- **State lives where it already lives.** Ledger cards + memory files remain
  the source of truth (read-before-reply, write-after-act). The daemon session
  itself is disposable; kill it, restart it, re-ground from the ledger. Restart
  must never lose anything that matters. Vectors/transcripts are recall aids,
  not memory (per the July research: memory must know what changed, why, when
  it was true).
- **Letta lesson adopted:** the server owns state and context management, the
  surfaces just send messages. No surface ever manages a message array.
- **Reliability lessons adopted (ambient-agent literature):** event sources use
  watcher + periodic reconciliation, never watcher-only; failed actions go to
  a dead-letter file surfaced in the morning brief, never silently dropped.

## Interrupt policy (when she speaks first)

Grounded in CHI 2025 findings: timing beats content, identical suggestions
feel supportive at pause points and disruptive mid-task, and over-notifying
trains the user to tune the assistant out entirely.

1. Every event gets judged: time-sensitive? actionable by him today? would he
   act on it now? Score all three or stay quiet, log it for the brief instead.
2. Delivery timing: never while he's actively typing/working in a pane
   (suppress + retry after idle); prefer natural pauses.
3. Hard rate limit: max 3 proactive pings/day to the phone (via the Serena
   mobile app), 1/hour desktop. One ping per event, no repeats (nagging
   isn't dominant, it's annoying).
4. Quiet hours: no proactive contact 23:00-08:00 unless the house is on fire.
5. Heavy-lane topics are NEVER proactive pings. Work steers only.
6. Everything she chose NOT to say gets one line in a daily log, auditable,
   reviewable in the morning brief (Home Assistant pattern: judge → notify →
   persistent timeline).

## Safety rails (lethal-trifecta, from the July research)

- Daemon tools are read-only by default. Anything state-changing (send, buy,
  delete, deploy, DNS, smart-home writes) requires his explicit go, same
  wait-for-go protocol as front-door pane seeds.
- Untrusted content (mail bodies, web pages, calendar invites from others)
  never reaches a turn that holds privileged tools. Read-untrusted and
  act-privileged are separate turns, always.
- `SERENA_FRONTDOOR`-style hook guards apply: the daemon builds its own
  compact context (persona + ledger + active memory), it does not inherit the
  interactive-session hook stack.

## Build order

**v1 (one session of work): resident brain + front door client.**
- `core/brain_daemon.py`: ClaudeSDKClient in streaming-input mode; async
  generator fed from an internal asyncio queue; surfaces POST to a localhost
  HTTP endpoint or unix socket at `~/.config/serena/brain.sock`.
- systemd --user unit `serena-brain.service`, auto-restart, env scrubbed of
  ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`.
- Context at session start: persona + compact ledger/task digest (reuse
  core/frontdoor._compact_active + read_agent_context); re-ground by reading
  the ledger before each reply (read-before-reply is code, not convention).
- Front door `/api/frontdoor` routes to the daemon when alive, falls back to
  the current `claude -p` path when not.
- OBJECTIVES / acceptance: (a) front-door reply starts rendering < 1s after
  send with the daemon warm; (b) kill -9 the daemon mid-conversation, restart,
  next reply still knows the active ledgers (state survives, session doesn't
  have to); (c) 24h soak: memory footprint stable, no orphaned sessions;
  (d) zero API-key spend (verify billing dashboard after soak).

**The call, full vision (Raghav, 2026-07-16), what v2 builds toward:**
Serena is a contact in his phone. He dials her like a person, auto-pickup.
"hey raghav, first time you're calling today, how are you." He can put her
on speaker with someone in the room: "I'm here with Kamakshi, she's telling
me about this idea", "oh hello Kamakshi", and she participates like a
person. Mid-call tasking: "okay draft something up and send me a link", she
spawns the work on the PC and the artifact/preview link lands in his app by
the time the call ends. Reference point: The Accountant, the assistant on
the phone, except she's the AI.
Notes: Android ConnectionService lets an app be a real dial-able calling
service (she appears as a contact, real call UI). Greeting state (first call
of day etc.) is free once the daemon holds state. Guest awareness works
conversationally once introduced; true speaker diarization (knowing who's
talking by voice, unprompted) is a later layer. Auto-pickup is an explicit
call, not ambient listening, consensual by design, no always-on mic.

**v2: THE CALL. (Reordered 2026-07-15, this is the point for Raghav:
"i wanna be able to call her, talk to her, get her help with things we
worked on, brainstorm.")**
- Call surface, staged: v2.0 is an in-app call screen (big answer/hang-up,
  call-style UX) reached from a "Serena" contact card that deep-links into
  the app; v2.1 registers a self-managed Android ConnectionService so she is
  genuinely dial-able from the native dialer with a real in-call UI. Same
  daemon path behind both.
- Audio transport: one websocket per call, phone -> PC over the tailnet
  (the mobile app already reaches `chats serve`; the call socket lives on
  the daemon). Mic audio streams up in ~200ms opus/pcm chunks; her voice
  streams back as synthesized sentence chunks, playback starts on the first
  chunk (never wait for the full reply).
- STT on the PC, LOCAL: faster-whisper on the gaming PC GPU (no groq, the
  no-third-party rule applies to ears too). Utterance end via Silero VAD,
  per the realtime-voice-orchestration research.
- TTS on the PC, LOCAL-FIRST (decision 2026-07-16): upgrade from CPU kokoro
  to a GPU-class natural model (Sesame CSM / Orpheus / Chatterbox tier,
  bench against the tts-cpu-models-comparison follow-up) streamed sentence-
  by-sentence. TTS is ONE swappable interface; ElevenLabs stays a paid A/B
  experiment (~$99/mo at our volume per the KB research, third-party, cloud
  latency), only if local loses the ear test.
- Conversation mode: v2.0 is push-to-talk (hold or tap-to-toggle mic). v2.2
  is hands-free with barge-in (Silero VAD + TTS duck/cancel on his speech).
- Call state: daemon keeps per-day call memory ("first call today" greeting
  varies), and the call transcript lands in the normal session store so a
  call IS a chat like any other: searchable, ledger-updating.
- Guests: no diarization in v2. She handles "I'm here with Kamakshi" the
  human way, conversational awareness from what's said. Voice-ID is v5+.
- Mid-call tasking: a call turn can invoke the same spawn plumbing as the
  front door (spawn pane/headless job with seed). Artifacts (preview links,
  files) get pushed to the mobile app; she says it's ready and where.
  Wait-for-go applies, except explicit on-call instructions ("draft it and
  send me a link") which ARE the go.
- OBJECTIVES / acceptance: (a) end-of-utterance -> her first audio < 2s p90
  over tailnet away from home; (b) cold app start -> her hello < 5s;
  (c) 20-minute call with no drift or audio artifacts, transcript complete
  in the session store; (d) "draft X and send me a link" produces a working
  link in the app before call end on a small task; (e) $0 marginal cost
  per call.

**v2.5: THE DESK, same brain, ambient at the PC (Raghav, 2026-07-16).**
- Wake word "hey serena" via openWakeWord, fully local, mic audio never
  leaves the machine pre-wake. Then the SAME pipeline as the call: local
  faster-whisper + Silero VAD -> daemon -> local GPU TTS, all on this box,
  so latency beats the phone path.
- Varied greeting (persona, never canned). Conversational recall works
  because calls/chats/desk sessions all land in the one session store.
- Read-only asks ("check github for the repo's latest changes") are daemon
  tools, answered by voice, NO pane spawned. Panes spawn only when work
  needs doing, wait-for-go as usual.
- Visual anchor: the dot-pulse. REUSE the abandoned voice/desktop overlay's
  architecture, brain_bridge.py's state file + websocket
  (~/.config/serena/voice_state: idle|listening|thinking|speaking) already
  does state broadcast; replace the abandoned 3D brain renderer with a
  dot-field that breathes on idle, ripples on listening, swirls on thinking,
  pulses with amplitude on speaking. Same state language as the front-door
  orb so she reads as one being everywhere.
- Multi-machine: one brain, many faces. The daemon + STT + GPU TTS stay on
  the always-on home box; the LAPTOP runs the same desk face as a thin
  client, wake word + mic capture + dots overlay local to the laptop
  (nothing streams pre-wake), audio to the brain over the tailnet, works
  anywhere Tailscale reaches. If the brain is unreachable, fall back to a
  local cold-start reply (same pattern as the front door's claude -p
  fallback), degraded but never dead.
- OBJECTIVES / acceptance: (a) wake-to-greeting < 1.5s; (b) false-wake rate
  tolerable in a week of normal desk noise (tune threshold); (c) a read-only
  ask answered by voice with zero panes spawned; (d) dot-field state visibly
  tracks listen/think/speak in real time.

**v3: proactivity.**
- Mobile-app notification path: daemon-side outbox + app-side drain/ack over
  the tailnet, native Android notifications (no third-party services).
- Event sources: ledger staleness, task deadlines, scheduled morning brief.
- Interrupt policy enforced from day one, rate limits are code, not vibes.

**v4: senses.**
- Filesystem watchers (PhoneShots, Syncthing conflicts), calendar/mail via
  the existing self-hostable MCP servers on the tailnet, PC-side events.

## Open questions

- Context growth in a long-lived session: when to compact/fork. Letta-style
  server-side summarization vs periodic `fork_session` from a clean ledger
  re-ground. Decide in v1 by measuring.
- Model choice per turn (cheap triage model for event judgment, full model
  for conversation) — the SDK supports per-session model; per-turn routing
  may need two sessions (judge + voice).
- Multi-device: daemon runs on the PC (always on); laptop/phone reach it over
  the tailnet like the MCP fleet. Laptop-local fallback is out of scope.
