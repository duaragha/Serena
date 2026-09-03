# Serena Operations, Machine Layout & Bootstrap


---

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
- Clean committed reconstruction: 508 Python tests passed
- Mobile clean install, 9 tests, and production build passed
- Electron dot-field smoke passed from a clean install
- Mobile and Electron dependency audits report zero known vulnerabilities
- Docker source-only build passed with private identity and memory paths excluded
- Always-on user services remained active after canonical relinking
- The 24-hour soak was not run

This map is updated when a path changes ownership. New code must fit one of the four storage classes above.

---

# Serena Backup and Bootstrap

The canonical machine layout is `config/runtime-manifest.json`. The commands below consume that file instead of maintaining separate path lists.

## Check the current machine

```bash
cd ~/Documents/Projects/serena
.venv/bin/python -m scripts.bootstrap doctor
```

Use `--json` for durable or automated evidence. Use `--source-only` when validating a clean source archive that does not yet have private state, models, or installed services.

## Install user services

Preview first:

```bash
.venv/bin/python -m scripts.bootstrap install-services
```

Apply:

```bash
.venv/bin/python -m scripts.bootstrap install-services --apply
```

The installer backs up regular unit files, replaces them with links to `systemd/`, reloads the user manager, and enables only the manifest's always-on units. It never starts or enables `serena-brain-soak.service`.

## Create a private snapshot

Safe default:

```bash
.venv/bin/python -m scripts.backup
```

This includes private repository content and ordinary runtime state. It excludes credential files, Claude and Codex histories, and large model caches. Live SQLite databases use SQLite's online backup operation.

Preview a full migration snapshot:

```bash
.venv/bin/python -m scripts.backup \
  --include-secrets \
  --include-auth \
  --include-sessions \
  --include-models \
  --dry-run
```

Remove `--dry-run` only when the destination is private and has enough disk space. Snapshots default to `~/.local/share/serena/backups`, with directories mode `0700` and files mode `0600`.

## Verify or restore a snapshot

Dry-run verification:

```bash
.venv/bin/python -m scripts.restore \
  ~/.local/share/serena/backups/serena-TIMESTAMP \
  --repo ~/Documents/Projects/serena \
  --home ~
```

Apply without overwriting existing files:

```bash
.venv/bin/python -m scripts.restore \
  ~/.local/share/serena/backups/serena-TIMESTAMP \
  --repo ~/Documents/Projects/serena \
  --home ~ \
  --apply
```

Every archived file is checksum-verified before its restore plan is accepted. Existing files are skipped unless `--overwrite` is explicit.

## Prove reconstruction from Git

```bash
.venv/bin/python -m scripts.verify_reconstruction
```

The verifier exports the committed revision into a temporary directory, creates a new Python environment, installs Serena, runs the source doctor and Python suite, installs and tests the mobile client, builds the mobile client, and runs the dot-field smoke check. It deletes the temporary tree afterward and writes durable evidence to `~/.config/serena/acceptance/reconstruction.json`.

This verification never imports live private data, changes installed services, starts voice, or runs the 24-hour brain soak.

## Fresh-machine order

1. Clone the repository.
2. Copy `Persona.example.md` to `Persona.md`, or restore the private snapshot.
3. Create `.venv` and install `.[desktop,dev]` plus the documented Linux GTK/VTE packages.
4. Install the separate Pocket TTS and wake environments from `voice/call/requirements-pocket.txt` and `voice/call/requirements-desk.txt`.
5. Restore or download the model assets named by the runtime manifest and voice documentation.
6. Run the bootstrap doctor.
7. Install the user services.
8. Authenticate Claude and Codex through their first-party subscription login flows.
9. Build the mobile client with `npm ci && npm run build` inside `mobile/`.
10. Run the normal tests and the real-hardware acceptance procedures.

Source reconstruction is automated. Subscription login, iPhone permissions, wake-word acceptance, and the physical call gate remain deliberate real-world actions.

---

# Windows Setup

## Prerequisites
- Python 3.10+ installed
- Claude Code installed (so `~/.claude/projects/` exists with session files)
- Syncthing syncing `~/.claude/` between machines

## Install

```powershell
cd C:\Users\ragha\Documents\Projects\serena\chats
python -m venv .venv
.venv\Scripts\pip install -e .
```

## Add to PATH

Either add `.venv\Scripts\` to your PATH, or create an alias:

```powershell
# Option 1: Add to PATH permanently
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\Users\ragha\Documents\Projects\serena\chats\.venv\Scripts", "User")

# Option 2: Just run directly
C:\Users\ragha\Documents\Projects\serena\chats\.venv\Scripts\chats.exe
```

## First Run

```powershell
chats reindex -f
```

This scans all session `.jsonl` files and builds the local index. Takes a minute or two.

## TUI

```powershell
chats
```

## Web UI

```powershell
chats web
```

Then open `http://localhost:8080` in your browser.

## Important Notes

- The SQLite index (`~/.local/share/chats/index.db`) is LOCAL to each machine — it doesn't sync
- Each machine builds its own index from the synced `.jsonl` files
- After syncing new sessions from another machine, run `chats reindex` or just open the TUI (it auto-indexes on startup)
- The `.chats-meta.json` file (stars, tags, custom titles) DOES sync between machines via Syncthing
- On Windows the DB path is: `C:\Users\ragha\.local\share\chats\index.db`

## Syncthing Setup

Make sure these directories sync between machines:
- `~/.claude/` (session files, metadata)
- `~/Documents/Projects/serena/` (the chats tool itself, knowledge base, persona)

The `.stignore` file in `~/.claude/` should already exclude device-specific files like `history.jsonl` and `settings.json`.

## Troubleshooting

**No chats showing**: Run `chats reindex -f` to rebuild the index.

**"Chat session" titles**: Run `chats reindex -f` — the title generator needs to re-parse first messages.

**Web UI empty**: The web server needs the index built first. Run `chats reindex -f` then `chats web`.

**Syncthing conflicts**: Delete any `.sync-conflict-*` files in the chats directory. They're duplicates from concurrent edits.
