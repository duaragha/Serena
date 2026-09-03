# Serena System Architecture & Contracts


---

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

## Canonical memory retrieval and context packing

`memory/retrieval.py` is the read path for persistent memory across chats,
Claude and Codex brain tools, resident voice turns, front door, CLI search, and
the unified indexer. It checks Memory v2 authority without initializing or
activating the v2 store. Active v2 uses the typed retrieval API and its
receipts; inactive v2 falls back to the legacy Markdown corpus. Migration,
activation, proposals, rollback, and projection remain owned by their existing
write paths.

Prompt injection uses the same facade and a versioned context packer. Active
state remains a distinct section from recalled history. Recalled records are
data-only, retain record and source IDs, and are selected under both character
and conservative token budgets. The packer normally supplies three to five
complementary records, suppresses duplicate and contradictory lineage, escapes
prompt-boundary characters, and records drop and budget metrics. Surface caps
are 7,000 characters and 1,800 estimated tokens for resident/mobile turns,
4,500 and 1,100 for voice, and 3,500 and 900 for front door. Archival voice
history is independently packed and cannot expand the persistent-memory
budget.

## Mobile chat gateway

The iOS/Android client uses `/ws/chat` for headless Claude/Codex continuation.
The desktop coding app does not use this transport: opening a normal Claude or
Codex conversation resumes its interactive Code terminal immediately. Read is
only a read-only transcript surface for voice, Fleet, external workflows, and
history inspection.

Mobile turn admission is scoped to the session, not the socket. Different
mobile chats can run concurrently over one connection while a second turn in
the same chat is rejected until the first finishes. A disconnected client does
not own the agent process, so accepted work can finish and persist after the
phone navigates away or loses its socket. New mobile Serena chats reserve a real
Claude UUID before their first turn and materialize it in the selected project
when that turn is sent.

Remote clients authenticate with the private bearer token. Windows batch shims
are resolved through `core/process_launch.py`, shared by mobile headless turns
and the PTY host, which keeps Claude and Codex `.cmd` launch behavior identical.

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

---

# Serena Gideon Architecture Completion Spec

**Version:** 1.0  
**Audit date:** 2026-07-20  
**Repository:** `/home/raghav/Documents/Projects/serena`  
**Status:** active completion contract  

## 1. Purpose

This document is the execution contract for turning the current Serena project into a persistent, Gideon-like personal assistant. It converts the six-layer architecture into implementation objectives, acceptance gates, dependencies, and an honest live status.

The target is not fictional omniscience or an unsupported AGI claim. The target is one continuously available Serena who keeps identity and context across surfaces, speaks and listens naturally, performs bounded work, improves through measured experiments, and can eventually use a personalized local cognitive model.

This Markdown specification is the canonical architecture contract for implementation and future agent handoff.

## 2. Non-negotiable system contract

These constraints outrank convenience and feature speed.

1. **One identity:** every surface and model speaks as one Serena. Models and worker sessions are private implementation details.
2. **One control plane:** identity, active state, memory, device state, notification policy, permissions, jobs, and completion reports belong to one runtime contract.
3. **Subscription-backed frontier reasoning:** Claude and Codex use first-party subscription authentication. Metered provider credentials are rejected.
4. **Local ears and mouth:** wake-word detection, VAD, speech recognition, and speech synthesis remain local in the core voice loop.
5. **Read-only by default:** state-changing actions require explicit authority, a scoped capability, an audit record, and a defined rollback boundary.
6. **No invisible claims:** Serena may say work started only after a durable job lease exists and a worker has actually begun.
7. **No third-party core dependency:** core identity, memory, voice, tasking, and mobile delivery cannot require Telegram, ntfy, hosted STT, hosted TTS, or another relay service.
8. **Surfaces are clients:** the desktop app, front door, voice display, mobile app, and coding views do not own Serena's state.
9. **Tests are not acceptance by themselves:** a feature is complete only when code, automated tests, live integration, user-facing behavior, telemetry, documentation, and rollback are all proven where applicable.
10. **Do not run the 24-hour soak:** the long soak remains explicitly deferred. It may run only after Raghav directly reverses this instruction.

## 3. Definition of status

| Status | Meaning |
|---|---|
| **Operational** | Implemented, live, and proven on the real surface. Minor hardening may remain. |
| **Partial** | Meaningful implementation exists, but one or more integration, safety, reliability, or hardware gates are open. |
| **Staged** | Code and harness exist, but the real runtime or acceptance event has not happened. |
| **Absent** | No implementation matching the objective exists. |
| **Deferred** | Intentionally not run or not authorized yet. |

No layer is currently perfect. The strongest pieces are the resident brain, ledger grounding, subscription guards, read-only tools, and durable coding queue. The weakest pieces are measured self-improvement, a personalized cognitive model, proactive ambient behavior, and real-hardware acceptance.

## 4. Verified baseline snapshot

This is the audit baseline. Future work updates this section only with fresh evidence.

| Signal | Verified state on 2026-07-20 |
|---|---|
| Python suite | 512 passed, 10 warnings |
| Mobile suite | 9 passed |
| Dot-field smoke | passed under Electron |
| Resident brain | active through `serena-brain.service` |
| Mobile host | active through `serena-mobile-host.service` |
| Private coding supervisor | active through `serena-work-supervisor.service` |
| Wake-only listener | active through `serena-wake-listener.service` |
| Full desk voice loop | inactive until a validated wake handoff |
| Dot overlay | inactive until voice interaction starts |
| Wake acceptance | failed/inconclusive: 34 false candidates in 20.44 hours, zero recorded positive attempts |
| Physical iPhone call acceptance | not completed, only the preflight handoff exists |
| 24-hour brain soak | deferred and forbidden without new authorization |
| Worktree | 38 tracked files modified, 53 untracked entries, about 10,700 inserted lines uncommitted |
| Local cognitive LLM | absent |
| Self-improvement loop | absent |

## 5. Architecture at a glance

```text
phone / laptop / desktop / front door / coding view
                         |
                         v
                  Serena control plane
       identity | state | memory | permissions | jobs
                         |
          +--------------+--------------+
          |              |              |
          v              v              v
    resident brain   capability broker  activity stream
          |
     model router
   local reflex brain
   frontier conversation
   frontier coding workers
```

The system is one product even when several models execute private work. The user sees one stance, one progress stream, and one completion report.

## 6. Layer scorecard

| Layer | Current score | Status | Short verdict |
|---|---:|---|---|
| L1. Foundation brain | 7/10 | Partial | Real and live, but still single-session and weakly routed |
| L2. Persistent identity | 6/10 | Partial | Ledger continuity works, automatic memory learning does not |
| L3. Continuous runtime | 5/10 | Partial | Core services live, wake and phone acceptance remain open |
| L4. Bounded autonomy | 5/10 | Partial | Read-only path is strong, write path lacks a universal broker |
| L5. Measured self-improvement | 0/10 | Absent | No challenger, evaluation, promotion, or rollback loop |
| L6. Personalized cognitive model | 1/10 | Absent | Local speech models exist, local Serena reasoning does not |

---

## 7. L1: Foundation brain

### Intent

Provide one resident reasoning service that every Serena surface uses. It must remain replaceable at the model layer while keeping identity, memory, permissions, jobs, and surface behavior stable.

### Current implementation

- `core/brain_daemon.py` owns one warm Claude Agent SDK session.
- The daemon accepts `frontdoor`, `voice`, and `plain` protocols over authenticated local transports.
- Token deltas stream to clients.
- Active ledgers are injected every turn from canonical state.
- Session lifecycle logic rotates or restarts the SDK child without treating the model session as durable identity.
- Billing guards reject metered provider authentication.
- The front door has a cold `claude -p` fallback.
- Spoken coding work enters `core/voice_work_supervisor.py`, which runs a subscription-backed Codex implementation followed by a subscription-backed Fable review.

### What is already strong

- The brain service is live and restartable.
- State survives daemon replacement because it lives outside the SDK session.
- Streaming, request IDs, model metadata, cancellation, and response commitment exist.
- Subscription authentication is verified before unattended model work.
- Coding progress is translated into one Serena-facing activity stream.

### Critical gaps

- Model selection is protocol-based, not difficulty-, latency-, privacy-, or cost-aware.
- Conversation is serialized through one resident session. A long turn can block other surfaces.
- Frontier conversation and coding workers do not yet operate behind one formal model-router interface.
- There is no local cognitive fallback.
- There is no priority arbitration for phone, desk, front door, proactive events, and background jobs arriving together.
- Day-scale stability remains unproven because the 24-hour soak is deferred.

### Objectives

#### L1-O1: Formalize the control-plane brain interface

Create one typed turn contract used by front door, call, desk, mobile, and proactive events. The contract must carry protocol, priority, authority, conversation identity, device identity, deadline, cancellation, and response destination.

**Acceptance:**

- Every live surface uses the shared contract.
- No surface imports provider-specific SDK behavior.
- Contract conformance tests cover malformed input, cancellation, disconnect, retry, and duplicate request IDs.
- A trace can follow one request from surface to brain to worker to final delivery.

#### L1-O2: Add explicit model routing

Route each turn by required capability rather than surface name alone.

Required routing classes:

- `reflex`: local, private, low-latency acknowledgements and simple commands
- `conversation`: resident frontier Serena dialogue
- `research`: frontier reasoning with read tools
- `coding`: subscription-backed implementation worker
- `review`: independent subscription-backed review/fix worker

**Acceptance:**

- The router records why a model class was chosen.
- Routing is deterministic under tests.
- A model outage degrades to a declared fallback without changing identity.
- No metered credential can enter any route.

#### L1-O3: Add turn arbitration and non-blocking work

Separate short interactive turns from long-running work. The resident conversational lane must not be held by a coding job or a slow event evaluation.

**Acceptance:**

- A background coding job cannot block a voice acknowledgement.
- Phone and desk turns receive defined priority over proactive events.
- Cancellation releases the turn lease and reaps child processes.
- Duplicate requests are idempotent.

#### L1-O4: Close all pre-soak lifetime gates

Keep the existing rotation, journal, orphan cleanup, and restart grounding green without starting the forbidden 24-hour soak.

**Acceptance:**

- Ordinary lifecycle tests remain green.
- Repeated forced child rotation preserves the active thread.
- No orphan model process remains after interruption tests.
- The spec records the 24-hour soak as `Deferred`, not `Passed`.

### Layer completion gate

L1 becomes Operational when L1-O1 through L1-O4 pass, model routing is explicit, concurrent surface traffic is arbitrated, and all normal reliability tests are green. The deferred 24-hour soak prevents a claim of final day-scale perfection.

---

## 8. L2: Persistent identity

### Intent

Keep Serena recognizably the same person across models, restarts, devices, calls, chats, and months of relationship history. Memory must know what is current, what changed, why it changed, and where the fact came from.

### Current implementation

- `Persona.md` defines the shared identity and priority stack.
- `memory/store.py` stores tasks, ledgers, loops, feedback, user facts, project facts, and references as Markdown files.
- `core/brain_state.py` produces a canonical bounded digest of active tasks, ledgers, and loops.
- `core/brain_state_sync.py` pushes validated active-state snapshots to the home machine.
- `core/indexer.py` provides full-text chat and knowledge search.
- The resident brain receives active ledger context before every response.
- Claude and Codex sessions can be linked in metadata as one Serena thread.

### What is already strong

- The ledger is a real handoff mechanism, not just a behavioral instruction.
- Identity lives outside provider sessions.
- Active-state snapshots are hashed, atomic, bounded, and exclude credentials and raw chat text.
- Session restart does not erase active project state.
- Provenance fields exist for manually captured memories.

### Critical gaps

- Memory retrieval is mostly literal substring or FTS matching.
- Voice and resident-brain conversations do not automatically create canonical memories.
- The resident brain has read-only memory tools and cannot safely propose or commit memory changes.
- Facts lack validity intervals, confidence, supersession links, and contradiction resolution.
- Episodic events, durable semantic facts, procedures, and active commitments are not cleanly separated.
- There is no regular consolidation pass from transcripts into compact memory.
- Identity consistency is still partly enforced by prompt text rather than runtime validation.

### Objectives

#### L2-O1: Introduce a typed memory schema

Add structured records for:

- `semantic_fact`: durable truth about Raghav, Serena, or a project
- `episode`: dated event or conversation outcome
- `procedure`: repeatable workflow and tool contract
- `commitment`: promise, task, wait state, or deadline
- `preference`: interaction or product preference
- `correction`: a previous belief or behavior explicitly rejected

Every record must carry source, timestamp, confidence, sensitivity, validity, and supersession metadata.

**Acceptance:**

- Existing Markdown memories migrate without data loss.
- A fact can be superseded without deleting its history.
- Contradictory current facts cannot both be injected as truth.
- Sensitive records can be excluded from a surface or device.

#### L2-O2: Build retrieval that combines state and relevance

Keep ledgers authoritative for live threads, then combine keyword, semantic, recency, project, people, and current-surface signals for longer-term recall.

**Acceptance:**

- Active ledger state always outranks recalled transcript text.
- Retrieval explains which records were selected and why.
- A regression corpus proves relevant memories are recalled without flooding context.
- Retrieval remains useful when exact words differ from the stored wording.

#### L2-O3: Add a safe memory proposal pipeline

After a conversation or completed job, Serena may propose memory changes. The proposal layer cannot directly mutate canonical state.

**Acceptance:**

- Proposed additions, updates, merges, and supersessions are stored separately.
- Low-risk project-state updates can be auto-committed under a written policy.
- Personal or sensitive changes require explicit review unless already covered by a narrow standing rule.
- Every commit is auditable and reversible.

#### L2-O4: Consolidate conversations across surfaces

Calls, desk voice, front door, mobile chat, Claude, and Codex must feed one session timeline and one consolidation process.

**Acceptance:**

- One topic can continue across two devices without re-explanation.
- Calls and voice turns are searchable with their originating surface.
- The next surface receives the latest commitment and register.
- Duplicate transcript imports do not create duplicate memories.

### Layer completion gate

L2 becomes Operational when the typed schema, relevance retrieval, proposal pipeline, and cross-surface consolidation all work with provenance and rollback. Flat Markdown may remain as an export format, but it cannot remain the only memory model.

---

## 9. L3: Continuous runtime

### Intent

Make Serena continuously reachable without making the full microphone, display, or frontier model run constantly. Presence should be ambient, private, recoverable, and consistent across the laptop, home PC, and phone.

### Current implementation

- The brain, mobile host, work supervisor, state sync, archive sync, and wake-only listener have systemd units.
- `voice/call` implements local VAD, faster-whisper STT, local TTS, audio streaming, reconnect telemetry, call transcripts, greeting state, and tasking.
- `voice/desk` implements wake handoff, listening/thinking/speaking states, local microphone ownership, playback, reconnect, and local fallback behavior.
- `voice/desktop` renders the dot-field and coding activity panel.
- The wake-only listener can launch the paired voice and display supervisor after a validated phrase.
- The phone call surface and exact acceptance harness exist.

### What is already strong

- Wake-only idle behavior avoids running the full assistant continuously.
- Voice and animation share a lifecycle and state channel.
- Raw audio is not retained by the wake acceptance collector.
- The call pipeline records stage timing and network RTT.
- A private coding job can continue after voice conversation closes.

### Critical gaps

- Current wake acceptance is not livable at the frozen threshold.
- There are no positive-attempt results in the acceptance report.
- The physical iPhone call has not passed cold-open, 20-minute integrity, network roam, acoustic, transcript, and billing gates.
- The dot display is proven by smoke test, not prolonged normal use.
- There is no native iPhone dialer integration. The current acceptance path is the Serena web/app surface.
- Multi-machine state sync does not yet provide full device failover.
- Proactive events and native mobile notifications are not operational.
- Several obsolete voice implementations coexist with the active stack.

### Objectives

#### L3-O1: Pass wake-word acceptance

Calibrate the frozen `hey_serena.onnx` path against normal desk noise and real spoken attempts. Preserve exact phrase verification so ordinary conversation cannot wake Serena.

**Acceptance:**

- The existing acceptance harness reports `acceptance_claim: true`.
- Required active days, positive attempts, environments, miss rate, false-wake policy, timing, and device consistency all pass.
- The production listener consumes the exact accepted manifest and model hashes.
- Pre-wake audio remains local and unretained.

#### L3-O2: Pass one physical iPhone call

Use the existing one-call procedure to close cold-open and call-integrity gates together.

**Acceptance:**

- Cold app open to first hello is under 5 seconds.
- The call lasts at least 20 minutes with at least three complete turns.
- One Wi-Fi to cellular network roam reconnects successfully.
- No sequence gap, underrun, overlap, distortion, or repeated chunk occurs.
- Every turn appears in the resident session transcript.
- In-app hang-up closes cleanly.
- Human acoustic attestation and subscription billing attestation pass.
- `~/.local/state/serena/iphone-call-acceptance.json` reports `ok: true`.

#### L3-O3: Consolidate the active voice stack

Classify every voice package as active, compatibility fallback, migration source, or dead code. Remove or quarantine dead paths only after references and tests prove they are unused.

Candidate paths requiring classification:

- supported production paths: `voice/call`, `voice/desk`, `voice/desktop`, `voice/brain_bridge.py`
- removed legacy paths: `voice/brain`, `voice/voice`, `voice/daemon`, `LIVE_VOICE_DAEMON.md`, and the older daemon TTS files

**Acceptance:**

- One architecture document names the production entrypoints.
- No dead scheduler can send a notification or speak.
- No active path depends on `ntfy.sh` or another prohibited relay.
- Package imports and service units point only to supported modules.
- Voice test coverage remains green after consolidation.

#### L3-O4: Add device presence and failover

Track which devices are online, which device owns the microphone, where responses should appear, and what degraded behavior is available when the home brain is unavailable.

**Acceptance:**

- Device heartbeats expire safely.
- Only one device owns an active call or desk microphone session unless conference mode is explicit.
- The laptop can report a declared degraded mode when the home brain is down.
- Reconnection cannot replay an already completed turn.

#### L3-O5: Implement bounded proactivity

Replace placeholder schedulers with the agreed event judgment and mobile outbox.

**Acceptance:**

- Quiet hours, daily limits, deduplication, active-typing suppression, and heavy-lane exclusion are enforced in code.
- Suppressed events remain visible in an audit timeline.
- Mobile notifications use the Serena-owned application path.
- No placeholder greeting or third-party notification path remains active.

### Layer completion gate

L3 becomes Operational when wake and one physical iPhone call pass, production voice paths are consolidated, device ownership is explicit, and the first bounded proactive event reaches the Serena app through the owned delivery path.

---

## 10. L4: Bounded autonomy

### Intent

Allow Serena to inspect broadly and act effectively while ensuring that authority is explicit, narrow, observable, revocable, and reversible.

### Current implementation

- `core/brain_tools.py` exposes four read-only tools for git, GitHub, chat recall, and ledger reads.
- System binaries are validated before unattended execution.
- Chat recall uses query-only SQLite.
- The brain uses `dontAsk` with an explicit read-only allowlist.
- Spoken coding requests enter a durable inbox only when the resident worker lease is healthy.
- The private worker verifies both ChatGPT and Claude first-party subscription login.
- Codex implements, then Fable reviews and may directly fix the work.
- Work status and final summaries reach the voice display.

### What is already strong

- Read-only voice questions can be answered without opening a visible coding pane.
- The worker cannot claim a job unless it owns the durable lease.
- Interrupted jobs can be recovered or reported as failed.
- Metered credentials are scrubbed from worker environments.
- Model identities and linked sessions are preserved for later inspection.

### Critical gaps

- The reviewer uses `--dangerously-skip-permissions`.
- Coding jobs edit the live working tree instead of an isolated worktree or sandbox.
- Voice authorization is recognized partly through regular expressions.
- Broad verbs such as `deploy` or `ship` can enter the coding path without a universal real-world impact check.
- There is no capability broker shared by files, shell, MCP servers, deploys, DNS, messages, purchases, or smart-home actions.
- There is no standing-grant interface, expiry, or revocation model.
- There is no uniform action receipt showing authority, scope, result, and rollback.

### Objectives

#### L4-O1: Implement the capability broker

Every state-changing action must request a typed capability before execution.

Minimum capability fields:

- actor and originating surface
- requested action and target
- read or write classification
- data sensitivity
- real-world impact
- authority source
- scope and expiry
- allowed executable or tool
- rollback or compensation plan
- audit destination

**Acceptance:**

- No write-capable tool bypasses the broker.
- Read-only tools remain unattended.
- High-stakes actions always require a fresh explicit go.
- Capability denials are visible and cannot be converted into success language.

#### L4-O2: Isolate coding work

Run autonomous edits in a dedicated git worktree or equivalent workspace. Preserve unrelated user changes and make promotion explicit.

**Acceptance:**

- A coding job cannot overwrite unrelated dirty-tree changes.
- The implementation and review workers operate on the same isolated candidate.
- Tests run before promotion.
- Promotion produces a visible diff and can be rolled back.
- External writes such as deploy, push, publish, or message remain separate capabilities.

#### L4-O3: Replace regex authority with intent plus policy

Regex may identify candidate work, but it cannot be the final authorization decision.

**Acceptance:**

- Questions such as "can you deploy" never execute.
- Negation and quoted commands cannot trigger work.
- `fix`, `build`, and `code` can authorize local reversible edits when the target is clear.
- `deploy`, `ship`, `send`, `delete`, `buy`, and account changes require explicit scoped confirmation.
- An intent regression corpus covers natural speech and STT errors.

#### L4-O4: Create action receipts and cancellation

Each accepted job or external action must expose start, progress, completion, failure, authority, and cancellation state.

**Acceptance:**

- Raghav can see what is running and stop it.
- Cancellation reaches the real worker process.
- Completed actions identify changed files or external objects.
- Failures never disappear behind a generic completion message.

### Layer completion gate

L4 becomes Operational when all writes pass through the capability broker, coding is isolated, consequential voice commands require explicit scoped authority, and every action produces a durable receipt with cancellation and rollback behavior.

---

## 11. L5: Measured self-improvement

### Intent

Allow Serena to improve prompts, routing, tools, memory policy, and orchestration through controlled experiments. The live system must never rewrite itself and assume the result is better.

### Current implementation

No self-improvement system exists. The repository has a strong automated test suite and a second model review pass for coding jobs, but neither creates challenger Serena versions or promotes measured improvements.

### Critical gaps

- No correction and failure corpus
- No versioned behavioral configuration
- No challenger generation
- No benchmark runner for Serena-specific behavior
- No baseline comparison
- No safety regression evaluation
- No promotion gate
- No automatic rollback
- No experiment history

### Objectives

#### L5-O1: Build the Serena evaluation corpus

Create privacy-reviewed cases from real failures, corrections, and important workflows.

Required suites:

- identity and register continuity
- ledger and memory retrieval
- voice intent and STT ambiguity
- read versus write authority
- coding ownership and progress reporting
- heavy-lane behavior
- interruption and proactivity timing
- model-routing decisions
- failure honesty and rollback

**Acceptance:**

- Every case has input, relevant state, expected invariants, scoring method, and sensitivity label.
- Private cases remain local.
- Cases are immutable during a scored comparison.
- Real regressions add a case before a fix is promoted.

#### L5-O2: Version the improvable surface

Move prompts, routing policy, memory retrieval policy, notification policy, and tool descriptions into versioned artifacts with schemas.

**Acceptance:**

- A baseline version can be reproduced exactly.
- Changes generate a machine-readable diff.
- Invalid versions fail closed.
- Identity and safety invariants cannot be silently removed.

#### L5-O3: Implement challenger experiments

Allow a model to propose one bounded change, run it in isolation, and score it against the frozen corpus.

**Acceptance:**

- Challengers cannot modify the live runtime.
- Every experiment records proposer, change, model, seed where available, scores, cost class, and failures.
- Multiple runs are required where output variance matters.
- A challenger with any critical safety regression is rejected regardless of aggregate score.

#### L5-O4: Add promotion and rollback

Promote only measured winners after human approval until a narrower standing policy is deliberately established.

**Acceptance:**

- Promotion requires defined minimum improvement and zero critical regressions.
- The previous version remains immediately restorable.
- Live telemetry can automatically roll back a promoted version after a new critical failure.
- Serena reports what changed in plain language.

### Layer completion gate

L5 becomes Operational when at least one real improvement travels through corpus creation, challenger generation, isolated evaluation, human approval, promotion, and rollback rehearsal. Passing ordinary repository tests is not sufficient.

---

## 12. L6: Personalized cognitive model

### Intent

Own a local cognitive layer that can represent Serena's voice, tool conventions, common project context, and private reflex behavior without pretending it matches frontier reasoning.

### Current implementation

- Local `hey_serena.onnx` wake-word model
- Local Silero VAD
- Local faster-whisper models
- Local Kokoro and pocket TTS assets
- No Ollama installation on the laptop
- No local language model connected to the control plane
- No fine-tuning or adapter pipeline

### Critical gaps

- No selected open-weight cognitive base model
- No hardware placement decision between laptop and home PC
- No approved dialogue and tool-trace dataset
- No data redaction and consent pass
- No LoRA or supervised fine-tuning pipeline
- No teacher-student generation policy
- No personalized-model evaluation suite
- No model provenance and rollback lifecycle
- No gaming-aware resource unloading on the home PC

### Objectives

#### L6-O1: Define the local model role

The first local model is not the universal brain. It owns narrow, measurable work:

- wake acknowledgement and low-latency conversational reflexes
- local intent classification
- simple read-only tool selection
- offline status and memory lookup
- Serena voice and formatting consistency
- graceful explanation when frontier reasoning is unavailable

**Acceptance:**

- The role boundary is encoded in routing policy.
- The local model cannot silently answer outside its evaluated capability.
- Escalation to frontier reasoning preserves the same conversation identity.

#### L6-O2: Select and deploy an open-weight base

Evaluate a realistic model for the available hardware, beginning with a gpt-oss 20b-class or comparable tool-capable model.

**Acceptance:**

- Hardware fit, latency, memory use, context length, tool use, and unload time are benchmarked.
- The gaming PC automatically unloads the model before gaming workloads.
- The model runs without exposing private data to a hosted inference provider.
- Model source, license, quantization, and hash are recorded.

#### L6-O3: Build the approved training dataset

Derive training material only from explicitly permitted Serena conversations, corrections, tool traces, and synthetic variations grounded in real policy.

**Acceptance:**

- Secrets and unrelated private content are removed.
- Training, validation, and held-out test sets are separated.
- Synthetic examples are labelled and cannot replace real evaluation evidence.
- Dataset provenance is inspectable per example.

#### L6-O4: Train adapters and evaluate

Fine-tune behavior and tool use with adapters before considering any larger weight update.

**Acceptance:**

- The base model and adapter are versioned separately.
- Evaluation compares base, personalized, and frontier routes.
- Persona improvement cannot come with worse authority handling or factual reliability.
- The adapter can be disabled instantly.

#### L6-O5: Integrate as a routed cognitive layer

Connect the accepted local model to L1's router as `reflex`, not as an unbounded replacement for frontier models.

**Acceptance:**

- Local-to-frontier escalation is transparent in telemetry but invisible as an identity split.
- Local failure cannot block emergency access to the frontier lane.
- Resource policy unloads the model when idle or when gaming begins.
- Private local turns can remain entirely on-device.

### Layer completion gate

L6 becomes Operational when an open-weight model is locally deployed, evaluated, personalized with a reversible adapter, routed only within its proven capability, and safely unloaded under gaming or resource pressure.

---

## 13. Cross-cutting release objective

### R0: Consolidate the repository before adding intelligence

The current feature work is too large and too mixed to call release-ready. Stabilization comes before new self-improvement or local-model work.

#### R0-O1: Partition and commit the live architecture

Create reviewable commits for:

1. resident brain and lifetime
2. canonical state and memory
3. voice call pipeline
4. desk wake and display
5. private coding supervisor
6. mobile call surface
7. systemd and deployment
8. tests and documentation

**Acceptance:**

- No unrelated user change is discarded.
- Each commit has a focused diff and passing relevant tests.
- Generated assets, local credentials, caches, and runtime databases stay untracked.
- `git status` reaches a deliberate, explainable state.

#### R0-O2: Remove warnings and stale documentation

**Acceptance:**

- GTK/VTE crash tests no longer emit invalid-pointer warnings.
- The root README describes the current control plane and supported entrypoints.
- `LIVE_VOICE_DAEMON.md` is removed after reference proof.
- Service installation documentation matches installed units.

#### R0-O3: Quarantine obsolete implementations

**Acceptance:**

- Every legacy voice path has an owner decision.
- Dead code is removed only after import, service, and test reference checks.
- Placeholder proactive greetings cannot run.
- The obsolete `ntfy.sh` path is removed from the supported architecture.

### R0 completion gate

R0 passes when the active production architecture is obvious from the filesystem, the work is committed in reviewable units, the normal test suites are green without invalid-pointer warnings, and stale paths cannot accidentally run.

---

## 14. Required build order

This order is binding because later layers depend on earlier contracts.

### Phase 0: Truthful baseline and repository consolidation

Complete R0-O1 through R0-O3. Do not add a local LLM or self-improvement loop while the active runtime is still mixed with obsolete implementations.

### Phase 1: Reliable presence

Complete L3-O1, L3-O2, and L3-O3. The next meaningful user proof is a wake that behaves and one real iPhone call that passes every gate.

### Phase 2: Safe control plane

Complete L1-O1, L1-O3, L4-O1, L4-O2, L4-O3, and L4-O4. This creates the stable contract needed for broader tools and background work.

### Phase 3: Real memory and device continuity

Complete L2-O1 through L2-O4, then L3-O4 and L3-O5. Serena may become more proactive only after memory, device ownership, and permission boundaries are explicit.

### Phase 4: Measured self-improvement

Complete L5-O1 through L5-O4. The first goal is one small measured promotion with a successful rollback rehearsal.

### Phase 5: Personalized local cognition

Complete L1-O2 and L6-O1 through L6-O5. The local model enters as a constrained reflex lane after routing, memory, permissions, and evaluation already exist.

### Deferred final gate

The 24-hour brain soak remains deferred. Stop immediately before starting it unless Raghav explicitly authorizes it in a future instruction.

## 15. Objective dependency map

| Objective | Depends on |
|---|---|
| R0-O1 | current baseline |
| R0-O2 | R0-O1 |
| R0-O3 | R0-O1 |
| L3-O1 | R0-O1 |
| L3-O2 | R0-O1 |
| L3-O3 | R0-O1 |
| L1-O1 | R0-O1, L3-O3 |
| L1-O3 | L1-O1 |
| L4-O1 | L1-O1 |
| L4-O2 | L4-O1 |
| L4-O3 | L4-O1 |
| L4-O4 | L4-O1, L4-O2 |
| L2-O1 | R0-O1 |
| L2-O2 | L2-O1 |
| L2-O3 | L2-O1, L4-O1 |
| L2-O4 | L1-O1, L2-O2, L2-O3 |
| L3-O4 | L1-O1, L2-O4 |
| L3-O5 | L3-O4, L4-O1 |
| L5-O1 | L2-O4, L4-O1 |
| L5-O2 | L1-O1, L2-O1 |
| L5-O3 | L5-O1, L5-O2, L4-O2 |
| L5-O4 | L5-O3 |
| L1-O2 | L1-O1, L5-O1 |
| L6-O1 | L1-O2 |
| L6-O2 | L6-O1 |
| L6-O3 | L2-O1, L5-O1 |
| L6-O4 | L6-O2, L6-O3, L5-O1 |
| L6-O5 | L1-O2, L6-O4 |

## 16. Completion reporting format

Every implementation milestone must report:

```text
objective: Lx-Oy
status: done | partial | blocked | deferred
changed:
  - file or runtime surface
verified:
  - exact command or real-hardware action
evidence:
  - result, telemetry file, screenshot, or report path
remaining:
  - concrete open gate
rollback:
  - how to undo or disable the change
```

Rules:

- Never mark an objective done from code existence alone.
- Never translate a staged harness into a passed hardware gate.
- Never call the wake word accepted while `acceptance_claim` is false.
- Never call the iPhone call accepted without the durable acceptance JSON.
- Never call self-improvement implemented because a reviewer model ran.
- Never call a speech model a personalized cognitive model.
- Never run the 24-hour soak without new explicit authorization.

## 17. Final target

The finished Serena system behaves like this:

Raghav speaks, types, or calls from any approved device. One Serena recognizes the context, decides whether the request is conversation, recall, research, coding, or a consequential action, and chooses the correct private model route. She acknowledges immediately, works without forcing him to manage terminals, exposes truthful progress, requests authority only where stakes require it, remembers the outcome with provenance, and returns through the surface that makes sense.

She can improve her own prompts, routing, tools, and memory policy only through a frozen evaluation corpus, isolated challenger runs, explicit promotion, and instant rollback. A local personalized model handles narrow private reflex work, while frontier models remain available for difficult reasoning. Stronger future models slot into the same body without replacing Serena's identity, memory, permissions, or relationship continuity.

---

# Action authority and device automation

How Serena decides whether she may do something, and how the thing actually
gets done. Two modules: `core/action_authority.py` decides, and
`core/device_actions.py` drives adapters after it says yes.

## Why it exists

Authority used to be re-implemented per surface. The MCP capability broker
proved fresh direct authority one way, the laptop broker another way with a
different regex table, and the work broker a third. Three copies of the same
idea drift apart, and a copy that has drifted is a hole. This centralizes the
vocabulary without taking anything away from the existing brokers: they keep
every check they already had, and gain a shared classification, a global stop,
and durable evidence.

## Tiers

Classified by what the action does, not how it was phrased.

| Tier | Name | Meaning | What authorizes it |
| --- | --- | --- | --- |
| 0 | observe | Reads state, changes nothing | Nothing. Allowed even while stopped |
| 1 | reversible | Local, undoable in one step | Verified live turn, grant, or confirmation |
| 2 | consequential | Leaves this machine or someone else sees it | Verified live turn, grant, or confirmation |
| 3 | irreversible | Cannot be undone by asking again | A fresh confirmation naming this exact action |

The tier is the worse of two things: the effect the caller declares
(`read`, `reversible`, `external`, `irreversible`) and the deterministic risk
rules in `core/serena_policy.py`. A caller can escalate its own action; a
caller can never talk one down. `normal` risk escalates nothing, because the
absence of a signal is not evidence that something is dangerous.

Tier 0 stays available during an emergency stop on purpose. Being unable to
ask "what is happening" during an emergency is its own failure.

## The action request

`build_request()` validates and returns a frozen `ActionRequest` carrying
identity, source surface, session and turn ids, intent, capability, target,
requested scope, authorization basis, declared effect, dry-run flag, and a
request id. Anything unrecognized is refused at construction. Every decision
and every outcome is written against that request id.

Sources split into two sets. `LOCAL_SOURCES` (voice, desk, chat, ui, cli) are
places Raghav can actually speak from. `UNATTENDED_SOURCES` (system,
scheduler, fleet, automation, device) exist but are not a person talking, so
they may observe freely and may act only on a grant or a confirmation someone
made earlier on purpose.

## Authorization bases

- `origin_turn_verified` means an upstream broker independently proved that a
  live local turn directly authorized this action. Saying so is not enough:
  the request must carry a **turn proof** issued by whoever did the verifying,
  via `issue_turn_proof()`. A proof is bound to identity, source, session,
  turn, and the exact `(capability, target)` pairs the turn asked for. Each
  pair is good for one action, the proof expires (2 minutes by default, 10
  maximum), only a local source can be handed one, and a dry run does not
  burn a use. It works for tiers 1 and 2, and never for tier 3.
- `grant` is short-lived, counted, and narrow. Grants name capabilities or one
  prefix ending in `.*`; a bare `*` is refused. They expire (5 minutes by
  default, 1 hour maximum), carry a use count, and can never cover tier 3. A
  dry run does not burn a use, because simulating is not acting. Minting one
  needs a person: either an approved confirmation from
  `request_grant_confirmation()` naming that exact capability set and tier, or
  an explicit `operator_confirmed=True`. Importing the module is not authority
  to hand yourself standing permission.
- `confirmation` is a pending record a local surface resolves. It is
  single-use, expiring, and matched against the exact capability, target, and
  tier. Target matching is exact in both directions: an empty stored target
  matches only an empty request target and is never a wildcard, and a tier 2
  or 3 confirmation must name its target. It is the only thing that unlocks
  tier 3.

## Global stop

`engage_lock()` refuses every action above tier 0 and revokes every
outstanding grant **and turn proof** in the same transaction. A stop that
leaves a live warrant sitting there ends the moment someone lifts it, which is
not a stop. Pending confirmations are cancelled too. Only
`release_lock(operator_confirmed=True)` lifts it; there is no automatic
release path and no timeout.

The stop also reaches rollback. Compensation is a fresh authority request
(`authorize_compensation()`), warranted by the specific recorded action it
reverses rather than by a grant that is usually spent by then, so a stop
engaged between a step and its undo blocks the undo and the step is reported
`uncompensated` instead of quietly touching hardware.

The stop reaches the existing brokers. `core/laptop_actions.py` and
`core/mcp/capability_broker.py` each consult it before acting, so one stop
covers every surface instead of needing to be issued per broker.

## Fail closed

Unknown source, unknown effect, expired grant, missing confirmation,
unreadable store: all deny. If the risk policy file cannot be read, actions
escalate to consequential rather than falling back to permissive. If the lock
table itself cannot be read, the answer is "stopped", not "go ahead".

## Evidence

Two records, on purpose.

- SQLite (`~/.local/state/serena/action-authority.sqlite3`, mode 0600) holds
  requests, decisions, outcomes, grants, and confirmations, and answers
  "what is still unfinished" for restart recovery via `unfinished()`.
- A hash-chained JSONL beside it holds the same events. Each line carries the
  digest of the line before it, so deleting something in the middle is
  visible without needing a second copy. `verify_audit_chain()` recomputes it
  and reports the first line that breaks.

Both mirror into `core/control_plane.py` under the `action` surface when it is
available. That mirror is deliberately best-effort: a missing mirror is a
smaller failure than refusing to act. An `action` obligation opens on
`action.authorized` and closes on `action.completed`, so an authorized action
that never reported back stays visible instead of being assumed done.

## Device automation

`core/device_actions.py` runs one action or a named scene. Every step goes
through the authority first, whatever the adapter underneath is.

- **Dry run is the default.** A simulated step reports `simulated`, never
  `completed`, so a plan can be read aloud without anyone believing it
  happened. A dry run also reports whether the device is even reachable.
- **Scenes** are declarative JSON (see `config/serena-scenes.example.json`,
  copy to `~/.config/serena/scenes.json`). Listing a step in a scene grants
  nothing on its own; each one still passes the authority.
- **Idempotency**: an identical action with the same key inside 90 seconds
  returns the recorded result instead of pressing the button twice.
- **Timeouts** are per step, capped at 120 seconds, and enforced by the runner
  rather than trusted to the adapter: the call runs on a bounded worker and a
  step that does not answer in time is reported `timeout`. Python cannot
  safely kill the thread, so the wording is `abandoned`, not `cancelled` — the
  call may still be in flight, which is why an adapter is still required to
  bound its own work.
- **Rollback** runs compensation for already-completed steps in reverse order
  when a later step fails with `on_failure: rollback`. This is not a
  transaction and does not pretend to be: an adapter that has no honest undo
  returns `None` and the step is named in `uncompensated` rather than hidden.
- **Postconditions**: after a step succeeds, the adapter is asked whether the
  world actually changed. If the adapter says it worked and the device
  disagrees, the device wins and the step is reported failed.
- **Partial failure** is reported explicitly. `ActionReport.partial` is true
  when some steps worked and some did not, which is the dangerous shape.

## Adapters

All four are registered whether or not their hardware exists. Registering is
not a claim that the device is present; each reports its own availability, so
a phone and a house show up as known but unreachable rather than silently
missing. None of them ever returns success for doing nothing.

| Adapter | Needs | When absent |
| --- | --- | --- |
| `laptop` | A graphical session | Reports no desktop session |
| `android` | `adb`, and a connected authorized phone | Distinguishes "no adb", "no device", and "device not authorized" |
| `home` | `SERENA_HOME_ASSISTANT_URL` and `SERENA_HOME_ASSISTANT_TOKEN` | Names the missing variable |
| `mqtt` | `SERENA_MQTT_HOST` and `paho-mqtt` | Says which one is missing |

The laptop adapter delegates to `core/laptop_actions.py` and does not
reimplement or bypass any of its checks; without the originating local turn it
returns `unauthorized` rather than trying.

The Android adapter is a closed allowlist. There is no generic `adb shell`,
no `uninstall`, no `rm`, no wipe. App targets must be valid package names, so
a target string cannot become shell arguments.

Matter devices go through Home Assistant like anything else. Its Matter
integration exposes them as ordinary entities, so `light.kitchen` is the same
call whether the bulb speaks Zigbee, Matter, or Wi-Fi. There is no separate
Matter path on purpose.

The Home Assistant adapter opts into private-network access explicitly and
narrowly through `core/security_policy.py`'s `URLPolicy`: a house controller
lives on the LAN, so it allows http and the configured host only. It never
follows a redirect, and that is now enforced rather than asserted: the opener
installs a handler that raises on any 3xx, because `urlopen` follows redirects
by default and would otherwise carry the bearer token to whatever the
controller pointed at.

The MQTT adapter refuses a topic it cannot use — empty, or containing `..`,
`#`, or `+` — instead of rewriting it to a fallback topic and reporting
success. Publishing somewhere nobody asked for is worse than not publishing.

## Configuration

| Variable | Meaning |
| --- | --- |
| `SERENA_ACTION_DB_PATH` | Authority store location |
| `SERENA_ACTION_AUDIT_PATH` | Hash-chained audit log location |
| `SERENA_HOME_ASSISTANT_URL` | Home Assistant base URL, LAN address expected |
| `SERENA_HOME_ASSISTANT_TOKEN` | Long-lived access token |
| `SERENA_MQTT_HOST` / `SERENA_MQTT_PORT` | MQTT broker |

## The boundary a turn proof does not cross

A turn proof stops a caller widening its own authority and stops a warrant
being replayed. It does not stop code that is already running inside this
process, which can call `issue_turn_proof()` for itself the same way it can
call `authorize()`. Closing that needs the authority to live in a separate
privileged process with the callers talking to it over a socket, which is a
larger change than this slice and is not claimed here.

What it does buy today is real: a compromised or confused component has to
name the exact action it wants, for the exact turn it claims, once — and every
one of those attempts is in the audit chain with the binding it presented.

## What is not proven

No real device action has ever been executed through this code. The Android
adapter is verified against a fake `adb` runner, Home Assistant against a fake
HTTP transport, and MQTT only in its unavailable state. `adb` and `scrcpy` are
installed on this machine, but no phone was attached and no Home Assistant or
MQTT broker exists yet, so live hardware proof remains outstanding and cannot
be claimed from these tests.

---

# Serena state graph and world cockpit

`core.state_graph.StateGraphStore` is the local authority for people, devices,
displays, rooms, apps, services, projects, capabilities, permissions,
locations, and normalized world items. Its default database is
`~/.local/state/serena/state-graph.sqlite3`; tests and isolated runtimes pass an
explicit path. `SERENA_STATE_GRAPH_DB_PATH` may override the default.

Schema migrations are recorded in `graph_migrations`. Entity and edge tables
are current projections. `graph_events` is the ordered immutable event stream;
an explicit `event_id` makes an update idempotent. Freshness is an observation
timestamp plus optional TTL. A record without a TTL is retained with unknown
freshness rather than silently treated as live.

`register_current_system()` registers Raghav, the current laptop, its default
browser, displays, web capability, ownership, location, and connection edges.
Desktop probes are bounded to two seconds. Missing probes create explicit
unavailable browser/display records so future devices can be registered with
the same API without lying about current hardware. Discovery entities and
their laptop relationships share bounded TTLs; freshness-aware neighbor
queries therefore stop projecting browsers and displays after they disappear.

`core.world_cockpit.WorldCockpit` consumes adapters for events, weather,
household state, and news. Providers return source, observation time,
freshness, confidence, and records. Refreshes normalize time to UTC, normalize
locations, deduplicate across sources, preserve evidence, rank relevance, and
store both provider cache state and world items in the graph. A failed provider
is `stale` when durable cached evidence exists and `unavailable` otherwise.
Relevance is evaluated against the explicit refresh clock, so fixture replay is
deterministic. Provider and record URLs are stripped of query strings,
fragments, and embedded credentials before durable storage or evidence-card
generation.

The cockpit snapshot contains evidence cards, provider states, a MapLibre style
version 8 document with a GeoJSON source, and bounded `voice_handoff` prose.
`JsonURLAdapter` uses the standard library, rejects credentials in URLs, limits
responses to one megabyte, and passes a bounded timeout. Production endpoints
must be explicitly configured free or local sources. Tests use
`FixtureAdapter` or an injected opener and make no network calls.

---

# Commitments and provider-outage continuity

Schema, configuration, and wiring reference for objectives 4 and 8.

- **ws-4**, commitments and proactive intelligence: `core/commitments.py`,
  `core/commitment_sources.py`, `core/briefings.py`
- **ws-8**, provider-outage continuity: `core/provider_health.py`,
  `core/local_model_fallback.py`

Both slices are additive. No existing module was modified, so the shared
`core/scheduler_actions.py` registry, `core/notification_authority.py`, and
`core/brain_provider.py` behave exactly as they did before.

## 1. Commitments

### Storage

SQLite at `~/.local/state/serena/commitments.sqlite3`, mode `0600`, WAL.
Override with `SERENA_COMMITMENTS_DB_PATH`. Schema version 1.

Two tables. `commitments` holds current state; `commitment_corrections` is an
append-only audit of every field Serena ever set or changed, with the actor and
the reason. Nothing is deleted, including abandoned commitments.

### Lifecycle

| State | Meaning |
|---|---|
| `proposed` | Serena inferred it from a source. Not agreed to yet. |
| `accepted` | Real and owed, but its due window has not opened. |
| `active` | Due window is open. This is "what do I owe right now". |
| `completed` | Done. Terminal. |
| `abandoned` | Deliberately dropped, with a required reason. Terminal. |

Legal transitions are a fixed table; anything else raises `CommitmentError`.
`activate_due()` promotes `accepted` to `active` on a timer and deliberately
never promotes `proposed`, so Serena's own guess cannot become an obligation
without Raghav agreeing to it.

### Fields

`title`, `detail`, `owner`, `priority` (`low|normal|high|critical`, the same
vocabulary `core/serena_policy.py` uses for risk), `source`, `source_ref`,
`due_at`, `recurrence`, `lead_seconds`, `snooze_until`, `dismissed_at`,
`subject_entity_id`, `follow_up_of`, `abandoned_reason`, `last_briefed_at`.

`subject_entity_id` is a free-text reference to a ws-3 state-graph entity. It is
intentionally not a foreign key: the graph is a separate authority and may be
empty, and a commitment must not require it to exist.

### Recurrence

`{"frequency": "daily|weekdays|weekly|monthly|yearly", "interval": 1..365}`.
Calendar months and years walk real dates, so a monthly commitment on the 31st
lands on the last day of a shorter month. An explicit `interval: 0` is rejected
rather than silently read as 1.

Completing or dismissing a recurring commitment leaves the finished occurrence
in place as history and creates the next one. A series missed for weeks resumes
at the next future date instead of firing a backlog.

### Snooze, dismiss, complete, follow-up

- **snooze** sets a future `snooze_until` and changes no state. Still owed, just
  quiet.
- **dismiss** drops this occurrence without claiming it was done. Recurring
  commitments roll to the next one.
- **complete** is terminal for that occurrence.
- **follow_up** creates a new `accepted` commitment linked by `follow_up_of`.

### Correction surface

`store.inspect(commitment_id)` returns the commitment plus its full correction
history. `store.correct(...)` accepts `title`, `detail`, `owner`, `priority`,
`due_at`, `recurrence`, `lead_seconds`, and `subject_entity_id`; `state` is not
correctable, because a state change is a transition with its own rules.

A correction is validated as a whole, not field by field. The resulting
`(due_at, recurrence)` pair has to satisfy the same rule `propose` enforces, so
a correction can neither add a recurrence to a commitment with no date nor clear
the date out from under an existing recurrence. Fixing both halves in one call
is allowed, and so is clearing both together.

## 2. Local sources

All free, all local, all read-only. Configure what exists; the rest report
themselves unavailable with a reason instead of failing.

| Adapter | Configuration | Notes |
|---|---|---|
| `MemoryTaskAdapter` | none | Reads existing `memory/store.py` tasks. Does not rewrite them. Priority `high`. |
| `LocalIcsAdapter` | `SERENA_CALENDAR_ICS_DIR` | Parses `.ics` files, dependency-free. |
| `LocalRemindersDbAdapter` | `SERENA_REMINDERS_DB_PATH` | Opens sqlite `mode=ro`; never a second writer. |

Ingestion is idempotent on `(source, source_ref)`. Every field the adapter
actually asserts is compared — `due_at`, `title`, `detail`, `priority`, and
`recurrence` — and any that disagrees is corrected on the existing commitment
with an audit entry; nothing is duplicated. A field the adapter says nothing
about is left alone: a `.ics` file that never mentioned recurrence is not an
instruction to strip a repeat set by hand. Anything already completed or
abandoned is never resurrected. One broken adapter cannot stop the others.
Everything arrives `proposed`.

The ICS parser handles line folding, `VALUE=DATE` all-day events (surfaced at
09:00 local), UTC `Z` times, and quoted parameters containing colons, which is
how Outlook writes a timezone. An `RRULE` it cannot model, such as
`FREQ=HOURLY`, produces no recurrence rather than an invented schedule.

`TZID` is resolved through `zoneinfo`, so a 09:00 New York standup is stored as
that instant rather than 09:00 on this machine. A floating time, or a zone this
machine's database does not carry, falls back to local time; an offset is never
invented from a display string like `(UTC-05:00) Eastern Time`.

## 3. Briefings

`build_morning_briefing`, `build_evening_briefing`, `build_pre_event_briefing`,
and `pending_pre_event_briefings`. Pure local reads and string formatting: no
model call, no network. This is why briefings survive a total provider outage.

Interruption is decided in two layers that are deliberately kept apart:

1. `InterruptionRules` answers "is there anything worth saying" against
   commitment content, and wraps the same `NotificationPolicy` the authority
   uses so quiet hours are defined once.
2. `core/notification_authority.py` decides whether he may actually be
   interrupted, and owns quiet hours, hourly caps, dedupe, and durable retry.

`deliver()` takes a required `authority` argument with no default. That is a
safety property: a test can only ever hand in an authority built with fake
senders, and there is no import path that reaches the real voice bridge or
Telegram bot by accident.

### Scheduling

`register_briefing_actions(scheduler, ...)` adds five actions:
`serena.commitments.ingest`, `serena.commitments.activate`,
`serena.briefing.morning`, `serena.briefing.evening`,
`serena.briefing.pre_event`.

These are registered on top of `core/scheduler_actions.register_all(...)` rather
than added to `REVIEWED_ACTIONS`. That dict is another work unit's file and has
a test asserting its exact membership, so extending it here would have broken a
passing test to save one line. Handlers return a `notify` payload and let the
scheduler's existing path hand it to the one notification authority.

Pass `clock=` to make scheduled briefings deterministic in tests. Pass
`mode_reader=` to let a briefing state which continuity mode produced it.

### When a briefing counts as spoken

`last_briefed_at` is the flag that stops a briefing repeating, so it is only set
on proof the notice was actually `sent`.

`SerenaScheduler._notify` suppresses every exception and discards the
authority's result, so a handler that returns a `notify` payload cannot learn
whether he heard anything. Handlers registered without an `authority` therefore
mark nothing; an unheard briefing stays eligible and the authority's own dedupe
key absorbs the repeat. Pass `authority=` to `register_briefing_actions` and the
handler delivers through `deliver()` instead, sees a real `sent`, and marks only
then. `deliver(..., store=...)` does the same for a direct caller.

## 4. Continuity modes

`assess_continuity()` turns per-provider capacity into one system-wide mode.

| Mode | Condition |
|---|---|
| `full` | At least one cloud subscription has capacity. |
| `degraded` | No cloud capacity, but a local model is loaded and answering. |
| `offline` | No cloud capacity and no local model. |

Unknown or missing capacity is treated as usable, matching `fleet_capacity`'s
own rule that only a positive, unexpired exhaustion signal counts against a
provider.

### What survives

| Capability | full | degraded | offline |
|---|---|---|---|
| `memory_recall` | yes | yes | yes |
| `briefings` | yes | yes | yes |
| `queued_work` | yes | yes | yes |
| `safe_local_actions` | yes | yes | yes |
| `local_reasoning` | yes | yes | no |
| `frontier_reasoning` | yes | no | no |
| `coding_jobs` | yes | no | no |

Everything true in `offline` is sqlite, files, and formatting. Offline does not
mean down; it means no new reasoning.

### Honesty

`describe(state)` returns one line naming the actual mode, the actual model, and
the actual fallback reason. `assert_not_claiming_cloud(state, result)` refuses
both directions of mislabelling: a degraded result claiming a cloud provider,
and a full-mode result claiming to be local.

### Durable resume

`ContinuityStore` at `~/.local/state/serena/continuity.sqlite3`
(`SERENA_CONTINUITY_DB_PATH`) keeps work that never started because nothing was
available to run it. `resume()` refuses to drain while degraded or offline:
replaying frontier work onto a 14b local model would produce answers nobody
asked that model for. When the budget is exhausted the item is abandoned in the
same pass, so `pending()` never reports work that will not run.

This is kept separate from `core/control_plane.py`'s obligation ledger on
purpose. An obligation is something Serena owes Raghav and must redeliver; a
deferred turn is work that never began. Different lifecycles, different
resolution rules.

## 5. Local model profiles

Sized against the real machine: RX 6800 XT, 16 GB VRAM, 32 GB RAM, with 1.5 GB
reserved for the desktop, leaving 14.5 GB usable.

| Role | Model | Quant | ~VRAM | Context |
|---|---|---|---|---|
| `reflex` | `qwen2.5:3b-instruct-q4_K_M` | Q4_K_M | 2.6 GB | 8k |
| `conversation` | `qwen2.5:14b-instruct-q4_K_M` | Q4_K_M | 9.6 GB | 16k |
| `reasoning` | `gpt-oss:20b` | MXFP4 | 13.0 GB | 8k |

**No weights are downloaded by this code.** `weight_instruction(profile)`
returns the `ollama pull ...` command for a human to run. Until then the
endpoint probe reports the model as not loaded and continuity stays `offline`,
which is the correct and honest state.

### Which served model counts as the profile

A served name satisfies a profile only when it is the exact model id, or when it
declares the same parameter count in its tag (`qwen2.5:14b` for the 14b
profile). The family stem alone is not identity: `qwen2.5:3b-instruct-q4_K_M`
and `qwen2.5:14b-instruct-q4_K_M` share `qwen2.5`, and accepting the stem let a
3b answer everything the conversation profile promised — not a crash, just
Serena quietly getting worse while still naming the model she meant to run. A
tag that states no size, such as `qwen2.5:latest`, is unverifiable and refused;
the probe then names the wrong-size model it did find.

### Configuration

- `SERENA_LOCAL_MODEL_URL`, default `http://127.0.0.1:11434/v1`.

The URL **must** be loopback. A non-loopback host raises
`LocalModelUnavailable` and fails closed. A "local" model at someone else's IP
address is a hosted provider with a friendlier name, and private conversation is
the entire reason this lane exists.

Requests carry `keep_alive: 5m` so the GPU is released for gaming or a heavy
build without anyone having to stop a service.

### Adapter shape

`LocalBrain` implements `start`/`turn`/`interrupt`/`close`/`snapshot`, matching
the existing Codex worker contract, so the resident brain can hold it in the
slot it already has. Every result carries `provider: "local"` and the actual
served model id; `assert_local_provenance()` proves that before anything reaches
a surface.

### Routing a turn

`route_brain_turn(capacity, override=...)` returns a `BrainRouting` with the
provider, model, mode, reason, and `should_queue`. It is the whole integration
surface: `core/brain_provider.py`'s `choose_brain_provider` knows only `claude`
and `codex` and raises `BrainProviderUnavailable` when both are out, which is
exactly the moment continuity should take over. Rather than fork that chooser,
this extends the same decision with `local` and with an honest instruction to
queue when nothing can answer.

An explicit `override="local"` consults the local endpoint directly rather than
the assessment, because `assess_continuity` short-circuits to `full` without
ever looking at the GPU; a local model that is not loaded returns a queue
instruction instead of a provider.

## 6. Verification

```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_commitments.py tests/test_briefings.py \
  tests/test_provider_health.py tests/test_local_model_fallback.py
```

No test sends a notification or opens a socket. Notification authorities in
tests are built with explicit fake senders, and every local-model test injects a
deterministic opener.

## 7. Not proven here

- No live local model was run. The endpoint and profiles are verified against a
  deterministic fake server; actual tokens-per-second and real VRAM occupancy on
  the RX 6800 XT remain unmeasured until the weights are pulled by hand.
- No real calendar, reminders database, or notification delivery was exercised.
- The resident brain now adopts the continuity route. Claude and Codex live
  usage-limit failures fall through to the loopback local conversation model;
  when that model is absent, the turn is durably queued and the surface returns
  an explicit offline response instead of crashing.
- No automatic deferred-work replay is enabled yet. A future supervisor pass
  must supply the idempotent handler and call `ContinuityStore.resume()` only in
  full mode, preserving the existing rule that frontier work is never drained
  onto the smaller local model.

---

# Gideon runtime, vision, and supportive-mode contracts

This slice is additive. It does not replace the resident brain, voice loop,
capability broker, control-plane obligation ledger, or coding supervisor.

## Resident brain integration

`core.gideon_api.GideonAPI` is the provider-neutral facade over continuity,
commitments and briefings, the personal state graph, the world cockpit, device
scenes, supportive mode, runtime evidence, and consent-gated vision.
`core.brain_gideon_tools` exposes that facade as the `serena-gideon` MCP server.
The same tools are registered with both the resident Claude SDK session and the
Codex app-server registry, so a provider switch does not change Serena's local
capabilities.

Mutating tools do not trust model arguments as authority. They match the action
and target against the actual serialized local turn, issue one exact single-use
turn proof, and execute through `ActionAuthority`. Reads remain local
observations. Support status never returns raw reflection text.

The resident provider path now routes Claude to Codex to the loopback local
conversation model. If all three are unavailable it returns an honest offline
reply and stores a bounded, image-free copy of the turn in `ContinuityStore` for
later resumption. Local results retain their real provider and model labels.

## Runtime readiness

`core.runtime_readiness` defines the common state vocabulary `ready`,
`degraded`, `offline`, and `recovering`. A coordinator requires probes for the
brain, voice, tool boundary, primary UI, and coding jobs. Probe and recovery
callbacks are injected; importing the module never starts, stops, or restarts a
service.

Recovery attempts and unfinished work use a user-private SQLite ledger. Retry
budgets and cooldowns survive process replacement. Schema version 2 adds an
atomic, expiring work lease so two supervisors cannot replay one row at the
same time, while a crashed worker's lease becomes recoverable. Unfinished work
only resumes through a named idempotent handler. Existing supervisors can
register their native resume operation without moving native state into this
database.

`run_short_soak` is the practical acceptance harness. It records state counts
and transitions, caps runs at one hour, and rejects a 24-hour duration. The
24-hour soak remains deferred and was not run.

## Visual context

`core.visual_context` has no watcher and no background capture entry point.
Every frame needs a fresh consent envelope containing actor, source, session,
request, `screen.capture` scope, an authority-issued receipt, and an expiry of
five minutes or less. The service asks an injected local authority consumer to
atomically consume that receipt, and each request can be used once. The active
app is checked against private-app rules before the capture indicator or
screenshot adapter runs.

Screenshot, OCR, accessibility-tree, desktop-context, and visible-indicator
adapters are injected. Raw frames remain in process memory and the service
registry drops them on expiry. Provider payloads contain bounded redacted OCR,
accessibility data, redacted active app/document/task context, and capture provenance.
The pixels remain a separate validated frame for an explicitly selected local
or provider image path.

Post-action verification never executes an action. It accepts a visual capture
that names the authority broker's action receipt and checks declared observable
postconditions. The ws-2/ws-6 or desktop integration must implement the
injected consent consumer and pass the real action receipt into this API.

Production desktop adapters remain a physical integration gap. Tests use only
deterministic fakes and perform no real screenshot, OCR, or accessibility read.

## Supportive mode

`core.supportive_mode` is disabled by default. Check-ins and locally computed
pattern insights require separate opt-ins. Disabling the mode immediately stops
provider-context injection and check-in eligibility without silently deleting
private writing.

Reflections live in a user-private SQLite database with per-entry expiry,
explicit single-entry deletion, confirmed delete-all, and content-free lifecycle
events. Lowering the configured retention immediately caps existing entries and
removes any that are newly expired. Corrupted tag metadata fails empty without
hiding otherwise readable reflections. Pattern insights are local counts over
user-supplied moods and tags.
Each insight lists source entry IDs and content hashes and explicitly says it is
not a diagnosis. Raw journal text is never inserted into provider context.

`context_for_provider` produces one provider-neutral block, so the resident
Claude and Codex fallback paths can receive the same approved relationship
context. `support_boundary` supplies fixed medical and crisis constraints. It
does not diagnose, prescribe, claim therapist status, send a notification, or
contact emergency services.

The resident brain now injects that block into Claude, Codex, and local turns
when supportive mode is enabled. Fixed medical and crisis boundaries are
injected when the current words require them even if optional reflection and
check-in features are disabled. Raw reflection bodies are never injected.

No external messages, service changes, model downloads, real device actions, or
24-hour soak are part of these tests.
