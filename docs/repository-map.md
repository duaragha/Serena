# Serena Repository and Runtime Map

**Audited:** 2026-07-20
**Repository:** `/home/raghav/Documents/Projects/serena`
**Authority:** this file classifies where Serena source, state, credentials, generated assets, and installed wiring belong.

## Ownership rule

Serena has four storage classes. They must not be mixed.

| Class | Owner | Backed up by | May contain secrets |
|---|---|---|---|
| Portable source | Git repository | Git remote | No |
| Private identity and knowledge | Repository working tree, ignored by Git | Serena private backup | Yes |
| Mutable runtime state | `~/.config/serena` and `~/.local/share/chats` | Serena private backup | Yes |
| Rebuildable cache | virtual environments, model caches, Node dependencies, build output | Recreated by bootstrap | No |

## Portable source

These paths are required product source and belong in Git.

| Path | Role |
|---|---|
| `core/` | control plane, resident brain, state, tools, tasking, bridges, and runtime services |
| `chats/` | Claude and Codex session parsing, export, titles, watcher, and handoff code |
| `ui/` | Flask application, web front door, and browser terminal transport |
| `desktop/` | Linux GTK and cross-platform desktop shells |
| `mobile/src/` | Serena phone application source |
| `voice/call/` | phone and browser voice transport, local STT, local TTS, VAD, telemetry, and acceptance harnesses |
| `voice/desk/` | wake-only listener and desk conversation client |
| `voice/desktop/` | dot-field display and private coding activity surface |
| `voice/brain_bridge.py` | shared voice and display state bridge |
| `systemd/` | canonical user-service definitions |
| `tests/`, `desktop/tests/`, `voice/*/tests/`, `mobile/test/` | automated acceptance and regression coverage |
| `static/` | application icons and packaged static assets |
| `docs/` | current architecture, operations, and acceptance documentation |
| `memory/*.py`, `knowledge/*.py` | storage and retrieval implementation |
| `Dockerfile.serena`, `docker-compose.serena.yml` | current Windows PC container deployment path |

## Private source-adjacent content

These paths remain inside the working folder for daily use but stay ignored by Git.

| Path | Reason |
|---|---|
| `Persona.md` | live private identity and relationship specification |
| `memory/{user,feedback,project,general,task,loop,ledger,reference}/` | personal memory and active state |
| `knowledge/*/` | private knowledge documents and research |
| `reminders/`, `dream/` | personal generated state and local workflows |

They are not disposable. The private backup command covers them.

## Mutable runtime state outside the repository

| Path | Contents |
|---|---|
| `~/.config/serena/` | brain discovery, environment files, tokens, models, job databases, call metrics, wake calibration, acceptance evidence, logs, and service state |
| `~/.local/state/serena` | compatibility symlink to `~/.config/serena` on this laptop |
| `~/.local/share/chats/` | rebuilt chat index plus desktop WebView storage |
| `~/.claude/` | Claude subscription authentication and session history |
| `~/.codex/` | Codex subscription authentication and session history |
| `~/.config/systemd/user/` | installed service links used by the current Linux login |
| iPhone installation | compiled Serena client and its device-local permissions/state |

Runtime databases, credentials, logs, and tokens must never be committed.

## Rebuildable local cache

These paths may be removed once the owning process is stopped. Bootstrap recreates them.

| Path | Contents |
|---|---|
| `.venv/` | primary Python environment |
| `.venv-pocket/` | isolated Pocket TTS environment |
| `voice/.venv-wake/` | wake-word and desk audio environment |
| `voice/models/` | local Whisper and Kokoro model cache |
| `mobile/node_modules/` | mobile JavaScript dependencies |
| `mobile/dist/` | built mobile web client |
| `voice/desktop/node_modules/` | Electron display dependencies |
| `build/`, `*.egg-info/`, Python and test caches | generated packaging and test output |

## Installed runtime wiring

The active brain, mobile host, private work supervisor, and wake listener all execute code directly from this repository. Every supported installed unit is linked to its canonical definition in `systemd/`, so installed copies cannot drift.

The supported always-on services are:

- `serena-brain.service`
- `serena-mobile-host.service`
- `serena-work-supervisor.service`
- `serena-wake-listener.service`
- `serena-brain-state-sync.path`
- `serena-brain-state-sync.timer`
- `serena-archive-sync.timer`

The desk loop, bridge, and dot overlay are activation-time services. They do not remain active while Serena is only waiting for the wake phrase.

The brain soak unit is source only. The 24-hour soak must not run without new explicit authorization from Raghav.

## Removed obsolete and generated paths

The consolidation pass proved and removed the unsupported voice daemon generations, duplicate voice packages, old voice entrypoints, superseded systemd units, stale design material, empty nested repository metadata, old lockfiles, packaging output, and test caches. The active recognizer vocabulary now lives with its owner at `voice/call/vocabulary.txt`.

The supported voice surface is now only `voice/call/`, `voice/desk/`, `voice/desktop/`, and `voice/brain_bridge.py`. No legacy scheduler, speech daemon, or notification relay remains in the repository.

## Remote PC finding

The Windows PC currently runs the `serena-daemon:local` container from `C:\Users\ragha\Projects\serena`. Its Git metadata is still at commit `7b2d85e`, while its working files have been changed by synchronization. The laptop repository is the current source authority. The PC must not be hard-reset until its current folder is backed up and the new bootstrap path is proven.

## Consolidation evidence

- Safe private snapshot: 1,235 files and 238,356,228 bytes, all checksum-verified
- Clean committed reconstruction: 507 Python tests passed
- Mobile clean install, 9 tests, and production build passed
- Electron dot-field smoke passed from a clean install
- Always-on user services remained active after canonical relinking
- The 24-hour soak was not run

This map is updated when a path changes ownership. New code must fit one of the four storage classes above.
