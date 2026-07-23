# Serena

Serena is Raghav's persistent personal assistant runtime. The repository contains one control plane for conversation, memory, voice, coding work, Claude and Codex sessions, the desktop shell, and the phone call surface.

The product has one identity at the surface. Claude and Codex are private execution runtimes behind it, authenticated through their first-party subscription logins. The core loop does not require an Anthropic or OpenAI API key.

## What is live

- A resident grounded brain with an NDJSON Unix socket and local HTTP fallback
- A Linux desktop app with a front door, linked Claude and Codex panes, memory, knowledge, and terminal views
- A private coding supervisor that can start subscription-backed work and stream visible progress
- A local voice pipeline with wake detection, VAD, faster-whisper STT, Pocket or Kokoro TTS, and call telemetry
- Capability-based resident model routing plus an audited, reversible local laptop-control broker for desk voice turns
- A phone web client served over the tailnet
- A desktop dot-field display for idle, listening, thinking, speaking, and working states
- A canonical runtime manifest, private backup and restore tooling, service installer, and clean reconstruction verifier

The current architecture and remaining physical acceptance gates are documented in [docs/serena-gideon-architecture-status.md](docs/serena-gideon-architecture-status.md).

## Architecture

```text
phone / desk mic / desktop front door
                 |
                 v
          local control plane
       core/brain_daemon.py
                 |
      +----------+-----------+
      |          |           |
      v          v           v
 memory state  voice loop  private work
      |          |           |
      +----------+-----------+
                 |
          Claude + Codex CLIs
       subscription authentication
```

The supported production paths are deliberately small:

| Path | Responsibility |
|---|---|
| `core/` | resident brain, state, read-only tools, tasking, bridges, and service processes |
| `chats/` | Claude and Codex session indexing, handoff, titles, and export |
| `ui/` | Flask front door, browser UI, and PTY transport |
| `desktop/` | native GTK shell and cross-platform fallback |
| `voice/call/` | call protocol, STT, TTS, VAD, wake acceptance, integrity, and telemetry |
| `voice/desk/` | passive wake listener and desk conversation client |
| `voice/desktop/` | Electron dot field and coding activity display |
| `mobile/` | phone call client and native wrappers |
| `memory/`, `knowledge/` | private persistent context and retrieval code |
| `systemd/` | canonical user service definitions |
| `config/` | secret-free templates and the runtime manifest |
| `scripts/` | backup, restore, bootstrap, and reconstruction tools |
| `docs/` | architecture, operations, setup, and acceptance documentation |

See [docs/repository-map.md](docs/repository-map.md) for the exact boundary between source, private data, runtime state, secrets, and rebuildable caches.

## Linux setup

Serena's daily-driver desktop uses GTK 3, WebKit2, and VTE.

```bash
sudo apt install \
  libgirepository1.0-dev libcairo2-dev \
  gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-vte-2.91 \
  python3-gi python3-gi-cairo

git clone <private-repository-url> ~/Documents/Projects/serena
cd ~/Documents/Projects/serena
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e '.[desktop,dev]'
.venv/bin/python -m scripts.bootstrap doctor --source-only
```

Restore the private snapshot or create `Persona.md` from `Persona.example.md`, then authenticate `claude` and `codex` with their subscription login flows. Live credentials and tokens belong under `~/.config/serena`, never in Git.

Install the canonical user services after the doctor is clean:

```bash
.venv/bin/python -m scripts.bootstrap install-services
.venv/bin/python -m scripts.bootstrap install-services --apply
```

The apply step links installed units back to `systemd/`, reloads the user manager, and enables only supported always-on services. It never starts the 24-hour soak.

## Run the desktop app

```bash
.venv/bin/chats desktop
```

The app opens on Serena's front door. She can continue an existing thread, create a private coding job, or open visible Claude and Codex panes when visibility is useful.

Useful commands:

```bash
.venv/bin/chats recall "search terms"
.venv/bin/chats memory active
.venv/bin/chats knowledge search "topic"
.venv/bin/chats web
```

## Voice and phone

Voice dependencies are intentionally separated from the main environment:

```bash
python3 -m venv voice/.venv-wake
voice/.venv-wake/bin/python -m pip install -r voice/call/requirements-desk.txt

python3 -m venv .venv-pocket
.venv-pocket/bin/python -m pip install -r voice/call/requirements-pocket.txt
```

Wake-word setup and calibration are in [voice/call/WAKEWORD.md](voice/call/WAKEWORD.md). The one-call iPhone acceptance procedure is in [voice/call/IPHONE_CALL_ACCEPTANCE.md](voice/call/IPHONE_CALL_ACCEPTANCE.md). The phone client is built with:

```bash
cd mobile
npm ci
npm test -- --run
npm run build
```

## Tests and reconstruction

Normal verification:

```bash
.venv/bin/python -m pytest -q
cd mobile && npm test -- --run && npm run build
cd ../voice/desktop && npm run test:dot-field
```

Clean committed reconstruction:

```bash
.venv/bin/python -m scripts.verify_reconstruction
```

The reconstruction verifier exports `HEAD` into a clean cache directory, creates a new Python environment, installs Serena, builds the mobile client, runs the complete Python and mobile suites, and exercises the Electron dot field. It does not import private data, modify services, start voice, or run the 24-hour soak.

## Backup and recovery

Create a private snapshot:

```bash
.venv/bin/python -m scripts.backup
```

Verify a snapshot before restoring it:

```bash
.venv/bin/python -m scripts.restore \
  ~/.local/share/serena/backups/serena-TIMESTAMP \
  --repo ~/Documents/Projects/serena \
  --home ~
```

Backups exclude credentials, session histories, and large model caches by default. See [docs/backup-and-bootstrap.md](docs/backup-and-bootstrap.md) before using any opt-in secret or authentication flags.

## Storage rules

| Data | Canonical location |
|---|---|
| portable source | this Git repository |
| private identity, memory, and knowledge | ignored paths inside this working tree |
| runtime state, tokens, models, and acceptance evidence | `~/.config/serena` |
| chat index and desktop WebView state | `~/.local/share/chats` |
| Claude and Codex sessions and authentication | their first-party home directories |
| rebuildable Python, Node, model, and build caches | ignored local directories |

Never commit a live token, credential, transcript, runtime database, or personal memory file.

## Current machine roles

The laptop repository is the source authority. The Windows PC currently runs the Serena container and must be migrated through a backup and the documented bootstrap path, not by hard-resetting its synchronized working folder.

## License

Private personal project. No redistribution license is granted.
