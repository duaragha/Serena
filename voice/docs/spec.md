# Spec: Serena Desktop — Always-On Voice AI Assistant

## Overview

**What**: A JARVIS-style desktop AI assistant that listens for a wake word, has full voice conversations with the user, proactively initiates interactions (morning briefings, reminders, event prep), and displays a sleek neural-network overlay UI — all powered by Claude with the existing Serena personality, memory, and knowledge systems.

**Why**: Raghav has built the entire Serena brain (persona, memory, knowledge base, conversation history) but the only interface is typing into a terminal. This turns Serena into a true always-on companion — voice in, voice out, proactive, visual, persistent. The Friday and Jarvis demos proved the concept; this builds the real thing with actual tools and actual memory, not stubs and prompt hacks.

**Scope**:
- IN: Wake word detection, voice conversations, proactive daemon, desktop overlay with 3D animation, calendar/weather/news/email integration, system tray, notifications, TTS with a custom voice
- OUT: Mobile app (ntfy.sh covers mobile alerts), camera/vision analysis, computer use/screen control, phone calls (already have Twilio in reminder-system), smart home (no Home Assistant setup)

## Requirements

- [ ] Always-on wake word detection ("Hey Serena") with <1% CPU idle usage
- [ ] Voice-to-voice conversation loop: speak → transcribe → Claude responds → TTS playback, under 2 seconds end-to-end
- [ ] Proactive daemon that initiates conversations on schedules and events (morning briefing, pre-meeting prep, new email alerts)
- [ ] Desktop overlay with animated 3D neural network visualization (thinking state), transparent, always-on-top
- [ ] System tray icon with status indicator and quick controls
- [ ] Dashboard mode showing calendar, weather, tasks at a glance
- [ ] Full integration with existing chats memory system (`chats memory`, `chats search`, `chats knowledge`)
- [ ] Tool calling via MCP for real-world actions (calendar, weather, web search, system info)
- [ ] Interrupt budget: max 8 proactive messages/day, quiet hours 11PM-7AM, priority-based delivery
- [ ] Runs as a systemd user service on Ubuntu Linux (X11 and Wayland)
- [ ] Custom Serena TTS voice via ChatterBox voice cloning

## Hardware Constraints

- **CPU only** — AMD integrated GPU (no NVIDIA, no CUDA). 16GB RAM, 16 threads (Ryzen).
- STT adjusted: faster-whisper `base.en` on CPU (~1s) instead of `large-v3` on GPU
- TTS adjusted: Piper primary (CPU-native, 80ms). ChatterBox voice cloning deferred to Phase 5 (CPU feasibility TBD, may need cloud fallback).
- All other components (wake word, VAD, LLM via API) unaffected by CPU-only constraint.

## Architecture / Design

### System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    SERENA DESKTOP                           │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐   │
│  │  Voice Loop  │◄──►│  Brain Core  │◄──►│  Proactive   │   │
│  │  (Python)    │    │  (Python)    │    │  Daemon      │   │
│  │              │    │              │    │  (Python)    │   │
│  │ - Wake Word  │    │ - Claude API │    │              │   │
│  │ - STT        │    │ - MCP Client │    │ - Scheduler  │   │
│  │ - VAD        │    │ - Memory     │    │ - Events     │   │
│  │ - TTS        │    │ - Context    │    │ - Triggers   │   │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘   │
│         │                   │                    │           │
│         └───────────┬───────┴────────────┬──────┘           │
│                     │                    │                   │
│              ┌──────▼───────┐    ┌───────▼──────┐           │
│              │  IPC Layer   │    │  Tool Layer  │           │
│              │  (WebSocket) │    │  (MCP)       │           │
│              └──────┬───────┘    └──────────────┘           │
│                     │                                       │
│              ┌──────▼───────┐                               │
│              │  Overlay UI  │                               │
│              │  (Electron)  │                               │
│              │              │                               │
│              │ - Three.js   │                               │
│              │ - Dashboard  │                               │
│              │ - Sys Tray   │                               │
│              └──────────────┘                               │
└─────────────────────────────────────────────────────────────┘
```

### Component Breakdown

**1. Voice Loop (`serena/voice/`)**
- **Wake word**: openwakeword with custom "Hey Serena" model (trained via synthetic audio from Piper TTS, ~4-8 hours on GPU)
- **VAD**: Silero VAD for end-of-speech detection
- **STT**: faster-whisper `large-v3` on GPU (~0.5-1s latency). No hybrid Vosk needed — on a desktop GPU, faster-whisper is fast enough and simpler.
- **TTS**: ChatterBox (primary, voice-cloned Serena voice, ~200ms on GPU) with Piper fallback for ultra-fast confirmations ("got it", "on it", "done")
- **Audio I/O**: PyAudio for mic capture, mpv subprocess for playback (respects PipeWire routing)
- **Echo cancellation**: Mute mic during TTS playback + energy-based gate after playback ends

**2. Brain Core (`serena/brain/`)**
- **LLM**: Claude API — Sonnet for quick responses (weather, time, simple questions), Opus for complex reasoning, research, multi-step tasks
- **System prompt**: Loaded from `~/Documents/Projects/chats/Persona.md` + injected memory context from `chats memory`
- **Conversation context**: Rolling window of last N turns, persisted to disk between sessions
- **Tool dispatch**: MCP client connecting to existing MCP servers in `~/.claude.json` + custom local tools
- **Memory integration**: Subprocess calls to `chats memory add` for saving, `chats search` for retrieval

**3. Proactive Daemon (`serena/daemon/`)**
- **Scheduler**: APScheduler `AsyncScheduler` with cron + interval triggers, running inside the main asyncio event loop
- **Scheduled events**:
  - Morning briefing (configurable, default 7:30 AM weekdays): weather + calendar + tasks
  - Pre-meeting prep (15 min before calendar events): attendees + context from memory
  - End-of-day summary (configurable, default 6:00 PM): what was accomplished, pending items
- **Event-driven triggers**:
  - Screen unlock (D-Bus `org.freedesktop.login1.Session` Lock/Unlock signals via `dbus-next`)
  - New email (IMAP IDLE via `aioimaplib`, renew every 9 min)
  - Calendar event approaching (poll Google Calendar API every 5 min)
- **Interrupt budget**:
  - Priority levels: CRITICAL (always deliver via TTS), HIGH (notification + sound), MEDIUM (silent notification), LOW (queue for next interaction)
  - Daily cap: 8 messages, hourly cap: 2, minimum 10-min cooldown
  - Quiet hours: 11 PM - 7 AM (only CRITICAL breaks through)
  - Focus mode: togglable via system tray, blocks MEDIUM and LOW

**4. Overlay UI (`serena/ui/`)**
- **Framework**: Electron with transparent BrowserWindow
- **3D visualization**: Three.js InstancedMesh particle system — idle state (slow orbit), listening state (pulse), thinking state (rapid morph/expansion), speaking state (waveform ripple)
- **Dashboard mode**: Togglable panel showing calendar (next 3 events), weather (current + forecast), recent notifications, task list
- **System tray**: Status icon (idle/listening/thinking/speaking), right-click menu (toggle overlay, focus mode, dashboard, quit)
- **Notifications**: Native Electron notifications for proactive messages, with click-to-respond
- **IPC**: WebSocket server in Python backend, Electron connects as client. Messages: state changes, transcription text, response text, dashboard data

**5. Tool Layer (`serena/tools/`)**
- **Google Calendar**: OAuth2 via `google-api-python-client`, events list/create/update
- **Weather**: Open-Meteo API (free, no key needed) — current conditions + 7-day forecast
- **News**: RSS feeds via `feedparser` (BBC, Reuters, CBC, AP)
- **Web search**: Tavily API or SerpAPI for real search results (not stubs)
- **System info**: `psutil` for CPU, memory, battery, temps, disk usage
- **App launcher**: `subprocess` for opening apps, URLs, files
- **Existing MCP servers**: Connect to any MCP server already configured in `~/.claude.json`

### Tech Stack Summary

| Component | Technology | Why |
|-----------|-----------|-----|
| Wake word | openwakeword | Open source, custom phrase training, <1% CPU |
| STT | faster-whisper large-v3 | Best accuracy, GPU-accelerated, ~0.5s |
| TTS (primary) | ChatterBox | Voice cloning, emotion control, MIT licensed |
| TTS (fast) | Piper | 80ms latency, good for short confirmations |
| VAD | Silero VAD | 87.7% TPR, best open source option |
| LLM | Claude API (Sonnet/Opus) | Already has Serena's brain, memory, tools |
| Overlay UI | Electron + Three.js | Transparency works X11+Wayland, 3D native |
| Daemon scheduler | APScheduler | Async, cron+interval, in-process |
| D-Bus | dbus-next | Async, screen lock/unlock, Bluetooth events |
| Email | aioimaplib | IMAP IDLE push, async |
| Calendar | google-api-python-client | OAuth2, events API |
| Weather | Open-Meteo | Free forever, no API key |
| Audio playback | mpv (subprocess) | Universal format support, PipeWire native |
| Audio capture | PyAudio | Low-level mic access, works everywhere |
| IPC | WebSocket | Bidirectional, real-time state sync |
| Package mgr | uv | Fast, modern Python package management |
| Process mgr | systemd user service | Auto-restart, boot start, journalctl logs |

### Data Model

```
serena/
├── config.yaml                 # All user-configurable settings
├── serena/
│   ├── __init__.py
│   ├── main.py                 # Entry point, orchestrates all components
│   ├── config.py               # Config loader (YAML → dataclass)
│   ├── voice/
│   │   ├── __init__.py
│   │   ├── wakeword.py         # openwakeword listener
│   │   ├── stt.py              # faster-whisper transcription
│   │   ├── tts.py              # ChatterBox + Piper output
│   │   ├── vad.py              # Silero VAD
│   │   └── audio.py            # PyAudio capture + mpv playback
│   ├── brain/
│   │   ├── __init__.py
│   │   ├── claude.py           # Claude API client (Sonnet/Opus routing)
│   │   ├── context.py          # Conversation context manager
│   │   ├── memory.py           # chats memory integration
│   │   └── tools.py            # Tool definitions + MCP client
│   ├── daemon/
│   │   ├── __init__.py
│   │   ├── scheduler.py        # APScheduler setup
│   │   ├── triggers.py         # Event-driven triggers (D-Bus, IMAP, etc.)
│   │   ├── briefings.py        # Morning/evening briefing generators
│   │   └── budget.py           # Interrupt budget manager
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── calendar_tool.py    # Google Calendar
│   │   ├── weather.py          # Open-Meteo
│   │   ├── news.py             # RSS feeds
│   │   ├── search.py           # Web search (Tavily)
│   │   ├── system.py           # System info + app launcher
│   │   └── mcp_bridge.py       # Bridge to existing MCP servers
│   └── ipc/
│       ├── __init__.py
│       └── server.py           # WebSocket server for UI communication
├── ui/
│   ├── package.json
│   ├── main.js                 # Electron main process
│   ├── preload.js              # Secure bridge
│   ├── renderer/
│   │   ├── index.html
│   │   ├── app.js              # Main renderer
│   │   ├── brain.js            # Three.js neural network animation
│   │   ├── dashboard.js        # Calendar/weather/news panel
│   │   ├── notifications.js    # Proactive message display
│   │   └── styles.css          # Dark theme
│   └── assets/
│       └── tray-icon.png
├── models/                     # Local model files
│   ├── hey_serena.onnx         # Custom wake word model
│   ├── silero_vad.onnx         # VAD model
│   └── serena_voice/           # ChatterBox voice clone files
├── systemd/
│   └── serena.service          # systemd user service file
├── scripts/
│   ├── train_wakeword.py       # openwakeword training script
│   ├── clone_voice.py          # ChatterBox voice cloning
│   └── setup_google_oauth.py   # Google Calendar OAuth setup
├── pyproject.toml              # Python project config (uv)
└── README.md
```

```yaml
# config.yaml
wake_word:
  model_path: "models/hey_serena.onnx"
  threshold: 0.7
  
stt:
  model: "large-v3"
  device: "cuda"
  language: "en"
  
tts:
  engine: "chatterbox"           # chatterbox | piper | openai
  voice_ref: "models/serena_voice/reference.wav"
  speed: 1.1
  fallback_engine: "piper"
  piper_model: "en_US-amy-medium"
  
llm:
  default_model: "claude-sonnet-4-6"
  complex_model: "claude-opus-4-6"
  max_context_turns: 20
  
daemon:
  morning_briefing: "07:30"
  evening_summary: "18:00"
  meeting_prep_minutes: 15
  quiet_hours_start: "23:00"
  quiet_hours_end: "07:00"
  daily_message_cap: 8
  hourly_message_cap: 2
  cooldown_minutes: 10
  
calendar:
  credentials_path: "~/.config/serena/google_credentials.json"
  poll_interval_minutes: 5
  
email:
  imap_server: ""               # e.g. imap.gmail.com
  username: ""
  app_password: ""              # Gmail app password
  
weather:
  latitude: 43.59               # Mississauga
  longitude: -79.65
  units: "celsius"
  
ui:
  overlay_opacity: 0.85
  animation_fps: 30
  dashboard_enabled: true
  
notifications:
  ntfy_topic: ""                # Optional: ntfy.sh topic for mobile alerts
```

### Key Decisions

- **Claude API over local LLM**: Serena's entire personality, memory system, and knowledge base are built around Claude. Running a local 8B model would mean rebuilding everything and losing quality. The API cost is worth it for a personal assistant — estimated $5-15/month depending on usage.
- **Electron over Tauri/GTK**: Transparency works on both X11 and Wayland, Three.js runs natively, massive ecosystem. RAM overhead (~150MB) is acceptable on a desktop. No Wayland compositor restrictions.
- **ChatterBox over ElevenLabs**: Free (MIT license), runs locally on GPU, voice cloning from short samples, emotion control. No per-character cost. ElevenLabs quality is marginally better but the cost adds up for an always-on assistant.
- **faster-whisper over Vosk**: On a desktop GPU, faster-whisper large-v3 is fast enough (~0.5s) with much better accuracy. Vosk streaming would add complexity for marginal latency improvement.
- **APScheduler over systemd timers**: All scheduling in-process means shared state, no cold starts, simpler architecture. The daemon itself is one systemd service.
- **WebSocket IPC over D-Bus/Unix socket**: Electron's WebSocket support is native and reliable. D-Bus would require native bindings. Simple JSON messages over WS is the cleanest approach.
- **mpv for audio playback over PulseAudio API**: Zero Python audio output dependencies, handles all formats, respects PipeWire routing. Subprocess overhead is negligible for TTS playback.

## Tasks

### Phase 1: Voice Pipeline MVP
> Goal: Talk to Serena in the terminal, get a voice response. No UI, no daemon.

- [ ] **1.1** Project scaffolding — `pyproject.toml` with uv, directory structure, config.yaml loader
- [ ] **1.2** Audio capture module — PyAudio mic stream, configurable device selection, chunk-based reading
- [ ] **1.3** Wake word detection — openwakeword integration, load model, continuous listening on audio stream, fire callback on detection
- [ ] **1.4** VAD integration — Silero VAD on audio stream, detect speech start/end, return complete utterance audio
- [ ] **1.5** STT module — faster-whisper `large-v3` transcription, GPU inference, return text from audio numpy array
- [ ] **1.6** Claude brain — Anthropic SDK client, system prompt from Persona.md, conversation context manager (rolling window), Sonnet/Opus routing based on query complexity heuristic
- [ ] **1.7** TTS module — Piper integration first (simpler, faster for MVP), generate WAV from text, mpv playback
- [ ] **1.8** Voice loop orchestration — Wire everything: wake word → VAD capture → STT → Claude → TTS → playback. Terminal output showing states.
- [ ] **1.9** End-to-end testing — Full conversation loop, measure latency, fix audio issues, test with background noise

### Phase 2: Tool Integration
> Goal: Serena can actually do things — check calendar, weather, search the web.

- [ ] **2.1** Tool framework — Base tool class, tool registry, Claude tool_use integration (function calling)
- [ ] **2.2** Weather tool — Open-Meteo API integration, current conditions + forecast, format for voice output
- [ ] **2.3** Google Calendar tool — OAuth2 setup script, events list (today/tomorrow/week), create event, upcoming meetings
- [ ] **2.4** News tool — RSS feed parser (configurable sources), summarize top stories via Claude
- [ ] **2.5** System info tool — CPU, memory, battery, disk, running processes via psutil
- [ ] **2.6** Web search tool — Tavily or SerpAPI integration, return summarized results
- [ ] **2.7** App/URL launcher — Open applications, URLs, files via subprocess
- [ ] **2.8** Memory integration — `chats memory` read/write, `chats search` for context retrieval, `chats knowledge` for research lookups
- [ ] **2.9** Tool testing — Test each tool individually, test Claude tool selection, test multi-tool chains

### Phase 3: Proactive Daemon
> Goal: Serena initiates conversations — morning briefings, meeting prep, alerts.

- [ ] **3.1** Daemon skeleton — asyncio main loop, APScheduler setup, graceful shutdown, signal handling
- [ ] **3.2** Interrupt budget system — Priority enum, daily/hourly caps, cooldown tracking, quiet hours, focus mode toggle
- [ ] **3.3** Morning briefing — Scheduled job: pull weather + calendar + tasks, generate briefing via Claude, deliver via TTS
- [ ] **3.4** Pre-meeting prep — Poll calendar every 5 min, detect approaching meetings (15 min out), pull context, brief user
- [ ] **3.5** Screen unlock greeting — D-Bus listener for login1 Lock/Unlock, contextual greeting (time-aware, not repetitive)
- [ ] **3.6** Email monitoring — IMAP IDLE connection, detect important new emails (sender/subject filtering), alert user
- [ ] **3.7** Evening summary — Scheduled job: summarize day's interactions, pending items, tomorrow's calendar
- [ ] **3.8** systemd service — Unit file, install script, auto-restart on crash, journalctl logging, `systemctl --user` commands
- [ ] **3.9** Daemon testing — Test all triggers, verify budget limits, test quiet hours, test crash recovery

### Phase 4: Desktop Overlay UI
> Goal: The visual brain — 3D animation, dashboard, system tray, notifications.

- [ ] **4.1** Electron scaffolding — package.json, main process, transparent BrowserWindow, Wayland + X11 support
- [ ] **4.2** WebSocket IPC client — Connect to Python backend, handle state messages, bidirectional communication
- [ ] **4.3** Three.js neural network — Particle system (InstancedMesh), connection lines (BufferGeometry), glow shader, 4 states: idle/listening/thinking/speaking with smooth transitions
- [ ] **4.4** Transcription display — Show user's speech as it's transcribed, show Serena's response text with typewriter effect
- [ ] **4.5** System tray — Icon with state colors, right-click menu (toggle overlay, focus mode, dashboard, settings, quit)
- [ ] **4.6** Dashboard panel — Slide-in panel: next 3 calendar events, current weather, recent notifications, quick task list
- [ ] **4.7** Notification system — Proactive message popups, click to respond, dismiss, snooze
- [ ] **4.8** Dark theme styling — Consistent dark theme, glassmorphism panels, status indicators, clean typography
- [ ] **4.9** UI testing — Test all states, test on X11 and Wayland, test transparency, test system tray on GNOME

### Phase 5: Voice & Polish
> Goal: Custom Serena voice, production hardening, quality of life.

- [ ] **5.1** Wake word training — Generate synthetic "Hey Serena" samples via Piper, train openwakeword model, test false positive/negative rates
- [ ] **5.2** ChatterBox voice cloning — Record or find reference audio for Serena's voice, clone via ChatterBox, integrate as primary TTS
- [ ] **5.3** Conversation persistence — Save/load conversation context between daemon restarts, prune old context
- [ ] **5.4** ntfy.sh mobile alerts — Forward CRITICAL proactive messages to ntfy.sh topic for mobile notifications
- [ ] **5.5** Error recovery — Handle Claude API failures (retry with backoff), handle mic disconnection, handle network loss gracefully
- [ ] **5.6** Performance optimization — Profile memory usage, optimize GPU VRAM sharing between models, reduce Electron RAM
- [ ] **5.7** Configuration UI — Settings accessible from system tray: wake word sensitivity, quiet hours, briefing times, voice speed
- [ ] **5.8** First-run setup — Interactive setup script: Google OAuth, email config, wake word training, voice cloning, test run

## Edge Cases / Gotchas

- **Echo cancellation**: When Serena speaks via TTS, the mic picks it up and re-triggers. Solution: mute mic during playback + 500ms gate after playback ends. The Jarvis project by isair uses energy-based echo detection which is more robust but complex — start simple, upgrade if needed.
- **Wake word false positives**: TV, YouTube, other people talking. openwakeword threshold is tunable (0.5-0.9). Start at 0.7, adjust based on real usage. Log all activations for review.
- **GPU VRAM sharing**: faster-whisper, ChatterBox, and Silero all want GPU memory. faster-whisper large-v3 needs ~3GB, ChatterBox ~1.5GB, Silero ~50MB. Total ~5GB — fine for a 12GB+ GPU. Load/unload models if VRAM is tight (< 8GB).
- **Claude API rate limits**: Opus has lower rate limits than Sonnet. Route simple queries to Sonnet, only escalate to Opus for complex reasoning. Implement retry with exponential backoff.
- **IMAP IDLE renewal**: RFC 2177 says servers may drop connections after 10 min. Renew every 9 min. Handle reconnection gracefully.
- **Wayland transparency**: Electron 41+ supports native Wayland. Verify transparency works on GNOME Wayland specifically — some compositors handle it differently. Fallback to X11 via `--ozone-platform=x11` if needed.
- **Multiple audio devices**: User might have speakers + headphones + monitor audio. PyAudio device selection needs to be configurable and handle device hot-plugging (Bluetooth headphones connecting/disconnecting).
- **Long-running conversations**: Context window fills up. Implement automatic summarization of older turns to keep context fresh without losing important details.
- **Network dependency**: Claude API, weather, calendar all need internet. Cache last-known data for offline mode. Wake word + STT + Piper TTS all work offline.

## Testing

### Phase 1 Tests
- [ ] Wake word detects "Hey Serena" within 1 second, <5% false positive rate in quiet room
- [ ] STT accurately transcribes spoken English with <5% WER
- [ ] Full voice loop completes in under 2.5 seconds (end of speech → start of TTS playback)
- [ ] Works with default mic device on Ubuntu

### Phase 2 Tests
- [ ] Weather tool returns current conditions for Mississauga
- [ ] Calendar tool lists today's events correctly
- [ ] Claude correctly selects and calls tools based on natural language queries
- [ ] Multi-tool queries work (e.g., "What's the weather and do I have any meetings today?")

### Phase 3 Tests
- [ ] Morning briefing fires at configured time and delivers via TTS
- [ ] Pre-meeting prep triggers 15 min before a real calendar event
- [ ] Budget system blocks messages after daily cap reached
- [ ] Quiet hours suppress non-critical messages
- [ ] Daemon survives and recovers from Claude API timeout

### Phase 4 Tests
- [ ] Overlay renders on both X11 and GNOME Wayland
- [ ] 3D animation transitions smoothly between all 4 states
- [ ] System tray icon appears and context menu works on GNOME
- [ ] Dashboard shows real calendar and weather data
- [ ] Notifications appear for proactive messages

### Phase 5 Tests
- [ ] Custom "Hey Serena" wake word works reliably (>95% detection, <2% false positive)
- [ ] ChatterBox voice clone sounds consistent and natural
- [ ] System recovers gracefully from network loss (queues messages, resumes)
- [ ] Full system runs for 24 hours without memory leaks or crashes

### Phase 6: Claude Code Integration
> Goal: Serena becomes a voice-first coding interface. She asks what you're working on, launches Claude Code in the right project, narrates progress, and shows full output in the overlay.

- [ ] **6.1** Startup greeting flow — on boot, Serena speaks a greeting ("hey, what are we working on today?") and waits for the user to name a project. Match spoken project name to `~/Documents/Projects/` directories (fuzzy match). Store active project in state.
- [ ] **6.2** `serena/tools/code.py` — CodeTool implementing the Tool protocol:
  - Spawns `claude --print --output-format stream-json --dangerously-skip-permissions` in the active project directory
  - Streams stdout line-by-line, parses JSON events (tool_use, text, result)
  - Accumulates output, tracks which files are being edited, what tools are being called
  - Method `async run(prompt: str, project_dir: str) -> AsyncIterator[CodeEvent]` yielding parsed events
  - Supports cancellation (kill the subprocess) if user says "stop" or "wait"
- [ ] **6.3** Narration engine — as Claude Code works, Serena summarizes progress via TTS:
  - On file read: "reading the auth middleware..."
  - On file edit: "editing the login handler, fixing the token validation..."
  - On bash command: "running the tests..."
  - On completion: "done. fixed three files, all tests passing."
  - Summaries generated by passing accumulated events to Claude API with "summarize this action in one short sentence for spoken output"
  - Batch updates: don't narrate every single event, group by ~3-5 second windows
- [ ] **6.4** Code output panel in Electron overlay — new scrollable panel showing full Claude Code output:
  - File edits shown as diffs (green/red lines)
  - Tool calls shown with name + arguments
  - Text responses shown as-is
  - Auto-scrolls, dark theme, monospace font
  - Toggle visibility from system tray or voice command ("show me what you're doing" / "hide the code")
- [ ] **6.5** IPC messages for code events — new message types:
  - `{ type: "code_start", project: "konpeki" }` — show coding mode indicator
  - `{ type: "code_event", event: { kind: "file_edit"|"bash"|"text", summary: "...", detail: "..." } }`
  - `{ type: "code_done", summary: "..." }` — final summary
- [ ] **6.6** Voice commands during coding — handle mid-session interrupts:
  - "stop" / "cancel" → kill the Claude Code subprocess
  - "what are you doing?" → speak current status without interrupting work
  - "show me" / "hide" → toggle the code output panel
  - "switch to [project]" → change active project directory
- [ ] **6.7** Register CodeTool in the tool registry so Claude brain can decide when to use it:
  - Tool description tells Claude: "use this when the user asks you to write code, fix bugs, refactor, or do any programming task"
  - Claude brain routes coding requests to CodeTool automatically based on intent
- [ ] **6.8** Integration testing — full flow: wake word → "we're working on serena" → "add a health check endpoint" → Claude Code runs → narration → completion summary

### Phase 6.5: Custom "Hey Serena" Wake Word
> Goal: Train and deploy a custom wake word model so the user says "Hey Serena" instead of "Hey Jarvis."

- [ ] **6.5.1** Record positive samples — script to record 20+ clips of the user saying "Hey Serena" in varied tones, distances, and background noise levels. Save to `models/wakeword_training/positive/`.
- [ ] **6.5.2** Generate synthetic positives — use Piper TTS with 10+ different voice models to generate ~500 synthetic "Hey Serena" clips with varied speed and noise augmentation.
- [ ] **6.5.3** Generate negative samples — synthetic clips of confusable phrases ("Hey Siri", "Hey Sarah", "Hey Sierra", "serene", "arena") plus general speech clips. ~500 samples to `models/wakeword_training/negative/`.
- [ ] **6.5.4** Train the model — use openwakeword's training pipeline (custom verifier on top of the embedding model, or full ONNX training if deps available). Export to `models/hey_serena.onnx`.
- [ ] **6.5.5** Test and tune — measure false positive/negative rates, adjust threshold, test in real conditions (TV on, music, other people talking). Target: >95% detection, <2% false positive.
- [ ] **6.5.6** Deploy — update `config.yaml` to point to the custom model, remove "hey_jarvis" fallback.

### Phase 6 Tests
- [ ] Startup greeting plays and project selection works via voice
- [ ] CodeTool spawns Claude Code and streams output correctly
- [ ] Narration summarizes progress without overwhelming the user (max 1 narration per 5 seconds)
- [ ] Code output panel in Electron shows diffs, tool calls, and text
- [ ] "stop" command kills Claude Code subprocess within 1 second
- [ ] "Hey Serena" wake word detects reliably in quiet and moderate noise environments

### Phase 7: The Life Operations Layer
> Goal: Serena stops being a voice Q&A and starts actually running shit. NOT "autonomous AI making judgment calls" — it's a glorified cron system with memory, voice, and an approval queue. I watch patterns, fire reminders on triggers, draft actions for approval, write to narrow scoped APIs.
>
> **Design principle**: autonomous for deterministic triggers and narrow-scope writes. Human-in-the-loop for anything with tone, judgment, or social context.
>
> **Not building yet** — voice interaction has reliability problems that must be solved first. Spec is here for when we're ready.

**Philosophy — what IS vs ISN'T autonomous:**

| Autonomous (just do it) | Human-in-the-loop (draft → approve → send) | Never |
|---|---|---|
| Time-based reminders | Scheduled messages to people | Judgment calls on tone |
| Calendar block creation | Week planning drafts | Open-ended "plan my life" |
| Task write to Google Tasks | Proactive nudges based on patterns | Autonomous messaging to people |
| Workspace launch (open apps) | Meal suggestions | Guessing at preferences with low confidence |
| Pattern detection + notification | Gym schedule drafts | Irreversible actions |

### Concrete examples from conversation

#### 7.1 Time-based triggers
- **5:30am wake-up call**: TTS "raghav, get up. gym in thirty." If no response in 10 min, escalate to ntfy.sh high-priority on phone.
- **10pm wind-down**: "phone down, you're not sleeping enough already."
- **Meal time reminders**: "it's 7pm, eat actual food not protein shakes."

#### 7.2 Event-based triggers
- **Calendar event approaching (15 min out)**: pull attendees, pull context from memory/past conversations, brief via TTS.
- **New high-priority email**: surface subject + sender, don't auto-reply.
- **Screen unlock after 2+ hours**: contextual greeting, mention anything urgent from the queue.

#### 7.3 Week planning (collaborative, Sunday night)
1. I pull calendar, memories (goals, deadlines), tasks, gym plan
2. I draft a week: gym blocks at 5:30am, deep work on konpeki tuesday, grocery run thursday, deadline X friday
3. You review the draft in the overlay, edit what's wrong, approve
4. I write confirmed blocks to Google Calendar, tasks to Google Tasks, reminders to ntfy.sh

#### 7.4 Context-aware workspace launch
- "hey serena, let's work on konpeki" → I open: VS Code at `~/Documents/Projects/konpeki/`, terminal in that dir, Claude Code ready, relevant browser tabs (Supabase dashboard, Cal.com, whatever the project uses)
- "let's plan the week" → Google Calendar opens, my draft plan appears in the overlay
- "gym time" → fitness tracker app opens, playlist starts

#### 7.5 Narrow scoped writes (autonomous, no approval needed)
- "add milk to my grocery list" → Google Tasks API write, done
- "block 2pm tomorrow for a dentist appointment" → Google Calendar API write
- "remind me at 4pm to call mom" → ntfy.sh scheduled notification
- "log that I benched 135 for 8 reps" → workout tracker write

#### 7.6 Pattern detection + nudges (notification only, no action)
- "you've opened the fridge three times in the last hour. eat something real."
- "you haven't moved from your desk in 4 hours. stretch."
- "you said you'd ship konpeki by friday and it's wednesday. where are we?"
- "you've been scrolling instagram for 45 minutes."

#### 7.7 Approval queue (drafts that need sign-off)
- Proactive messages ("serena wants to text X the following: [...]. approve/edit/cancel?")
- Email drafts (compose, not send)
- Calendar events involving others
- Anything I'm <80% confident about

### Tasks

- [ ] **7.1** Google Tasks integration — OAuth, `create_task`, `list_tasks`, `complete_task`. Same auth flow as Calendar.
- [ ] **7.2** Trigger engine — extend APScheduler setup with user-configurable rules. Each rule: `{trigger: time|event|pattern, condition: ..., action: tts|notify|draft}`.
- [ ] **7.3** Pattern daemon — background process watching: time since last app activity, screen lock/unlock frequency, calendar gaps, task completion rate. Fires events when thresholds hit.
- [ ] **7.4** Approval queue — new Electron panel showing pending drafts. Each item: title, body, approve/edit/cancel buttons. WebSocket IPC message type `approval_request`.
- [ ] **7.5** Workspace launcher tool — `launch_workspace(project: str)` that runs project-specific setup (open VS Code, terminal in dir, relevant browser tabs). Config in `config.yaml` per project.
- [ ] **7.6** Week planner — tool that pulls calendar + memories + tasks + goals, drafts a week via Claude, renders in overlay for approval, commits to calendar/tasks on approve.
- [ ] **7.7** Reminder system bridge — integrate with existing `~/Documents/Projects/reminder-system/` (Twilio calls + ntfy.sh). CodeTool or direct API calls to create reminders.
- [ ] **7.8** Time-based rules UI — edit `config.yaml` via overlay (wake-up time, meal reminders, wind-down time).
- [ ] **7.9** Opt-in pattern nudges — user toggles which patterns to watch (fridge frequency, desk time, social media, etc.). Respects focus mode + quiet hours.

### Phase 7 Tests
- [ ] Create a Google Task via voice command, verify it appears in Google Tasks
- [ ] Schedule a reminder for 2 minutes from now, verify TTS fires at the right time
- [ ] Week planner drafts a reasonable schedule, approval writes to calendar
- [ ] Workspace launcher opens the right apps for a named project
- [ ] Pattern nudge fires when threshold hit (e.g., 4h desk time) and respects quiet hours
- [ ] Approval queue renders pending items, approve button writes, cancel button discards

## Dependencies & Costs

### Python packages
```
anthropic               # Claude API
faster-whisper          # STT
openwakeword            # Wake word
silero-vad              # Voice activity detection
chatterbox-tts          # Primary TTS
piper-tts               # Fast fallback TTS
pyaudio                 # Mic capture
apscheduler             # Task scheduling
dbus-next               # D-Bus monitoring (async)
aioimaplib              # IMAP IDLE (async)
google-api-python-client # Google Calendar
google-auth-oauthlib    # Google OAuth
feedparser              # RSS news
psutil                  # System monitoring
httpx                   # HTTP client (async)
websockets              # IPC with Electron
pyyaml                  # Config
```

### System dependencies
```
mpv                     # Audio playback
portaudio19-dev         # PyAudio build dep
ffmpeg                  # Audio processing
```

### Monthly costs (estimated)
- Claude API: $5-15/month (depending on conversation volume, Sonnet-heavy routing)
- Tavily API: Free tier (1000 searches/month) or $5/month for more
- Everything else: Free (Open-Meteo, RSS, Google Calendar API, open source models)
- **Total: ~$10-20/month**

---

## Progress Log

**Status**: In progress
**Current phase**: Phase 6 — Claude Code Integration + Phase 6.5 — Custom Wake Word
**Last completed task**: 5.8 — Phases 1-5 complete (39 tasks). Post-phase fixes: Kokoro TTS replacing Piper, follow-up conversation mode, Electron registerBrain fix, single-process launch (Electron as child of Python).
**Blockers**: System deps (portaudio19-dev, mpv) need sudo install for full testing. Using sounddevice (bundled portaudio) and ffplay (installed) as workarounds. Python 3.11 required (tflite-runtime no cp312 wheels). No GPU — using CPU-optimized models (base.en whisper, piper TTS).

### Phase 1 completion notes:
- [x] 1.1 Project scaffolding (pyproject.toml, config.py, directory structure)
- [x] 1.2 Audio capture (sounddevice, 16kHz/int16/mono, async stream + callbacks)
- [x] 1.3 Wake word (openwakeword, "hey_jarvis" placeholder, ONNX inference)
- [x] 1.4 VAD (Silero VAD, 800ms silence threshold, pre-speech padding)
- [x] 1.5 STT (faster-whisper base.en, int8 on CPU, silence detection)
- [x] 1.6 Claude brain (Anthropic SDK, Persona.md + voice mode, rolling context)
- [x] 1.7 TTS (Piper CLI, en_US-amy-medium, speed scaling, sentence splitting)
- [x] 1.8 Voice loop (state machine: idle→listening→thinking→speaking, echo mute)
- [ ] 1.9 End-to-end testing (deferred until system deps available)
