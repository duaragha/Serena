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
