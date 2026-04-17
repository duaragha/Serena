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
