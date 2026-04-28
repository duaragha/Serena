# Serena — Chats / Memory / Knowledge AI Wrapper

A unified UI for managing Claude Code conversations, memories, and knowledge.

The headline feature: a sidebar of every Claude Code chat across every project, with full-text search, an inline terminal that resumes any chat in-place (no shell-out to `gnome-terminal`), persistent active terminals across chat switches, a git-tracked file pane, and a "Done" workflow to keep the active list curated.

Built primarily for **Linux** (native GTK shell with `Vte.Terminal` for the embedded terminal). Works on **Windows** and **macOS** via a pywebview + xterm.js fallback path.

## Quick start

```bash
git clone <this repo>
cd serena
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[desktop]"
chats desktop
```

The window opens. Your existing Claude Code chats from `~/.claude/projects/` populate the sidebar automatically.

## Platform notes

### Linux (best experience)

The desktop shell is a native GTK 3 app: `WebKit2.WebView` for the chat UI, `Vte.Terminal` for the embedded terminal (the same widget GNOME Terminal is built on). You get:

- Native drag-and-drop of files into the terminal — drop an image, the path types into the prompt
- Per-VTE keyboard shortcuts that fire even while the terminal has focus (`Alt+W` close terminal, `Alt+J/K` next/prev chat, etc.)
- `Ctrl+Backspace` → delete word, `Shift+Enter` → newline, both intercepted at the GTK layer
- `Ctrl+click` opens URLs in your default browser

System dependencies (Debian / Ubuntu / Mint):

```bash
sudo apt install \
    libgirepository1.0-dev libcairo2-dev \
    gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-vte-2.91 \
    python3-gi python3-gi-cairo
```

Then `pip install -e ".[desktop]"` (pulls in pywebview + GTK Python bindings).

### macOS (functional fallback)

The native GTK shell isn't available on macOS, so `chats desktop` falls back to a pywebview window (uses WKWebView) with an xterm.js-based terminal pane wired up over WebSockets to a backend `pty` process.

What this means in practice:

- Window + chat browsing work the same as Linux
- Inline terminal works (xterm.js + ptyprocess), just rendered in the browser instead of a native VTE
- File drag-drop uses an HTTP upload endpoint instead of native GTK drop. Works, slightly slower than a real native drop.
- Alt+key shortcuts (close terminal, next chat, etc.) won't fire while the terminal has focus — those are GTK-specific. Single-key shortcuts in the chat list still work.
- `Cmd+C / Cmd+V` for copy-paste (browser native).

Setup:

```bash
brew install python@3.11
cd serena
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[desktop]"
chats desktop
```

### Windows

Same fallback path as macOS (pywebview using Edge WebView2, which ships pre-installed on Windows 11).

```powershell
cd serena
py -3.11 -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[desktop]"
chats desktop
```

PTY backend uses `pywinpty` (ConPTY) automatically — see `ui/pty_terminal.py`. No additional setup.

## What you get out of the box

When you first launch:

- **Sidebar** populated from `~/.claude/projects/` (Claude Code's standard chat history location). Your existing chats just show up.
- **Project chips** down the left — single-click filters, double-click starts a new chat scoped to that project's cwd.
- **Time-grouped sessions** (Today / Yesterday / Last 7 Days / etc.).
- **Search** at the top of the chat list — full-text across all conversations.
- **Active Terminals** group at the top of the sidebar — shows whichever chats currently have a running `claude --resume` terminal, with a pulsing live dot. Cleared on app close.
- **Done** group at the bottom — collapsed by default. Press `D` on a focused chat (or `Alt+D`) to mark done. Auto-unmarks when the session sees new activity.
- **File pane** (right side, toggle with `Alt+B`) showing the git tree for the active chat's repo.
- **Memory** and **Knowledge** tabs at the top (also empty until you populate them via `chats memory add` etc.).

## Optional setup

### Custom persona / instructions

Drop a `Persona.md` in the repo root to give Claude an identity to act as inside chats. A starter template is in `Persona.example.md` — copy it and edit:

```bash
cp Persona.example.md Persona.md
$EDITOR Persona.md
```

`Persona.md` is gitignored — your customizations stay local.

There's also `~/.claude/CLAUDE.md` (Claude Code's global instructions file, lives in your home dir, not the repo) where you can add things like "always consult Codex for second opinions on judgment calls."

### Codex as a peer model

If you have ChatGPT Plus and the [Codex CLI](https://github.com/openai/codex) installed, you can register it as an MCP server so Claude can consult it for second opinions:

```bash
codex login   # authenticates via your ChatGPT account, no API key needed
```

Then add to `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "codex": {
      "type": "stdio",
      "command": "codex",
      "args": ["mcp-server"]
    }
  }
}
```

Restart Claude Code. The `mcp__codex__codex` tool will be available, and any CLAUDE.md instructions about consulting Codex will fire automatically.

### Knowledge base

The `knowledge/` directory is a long-term store of distilled research notes. Add Markdown files in topic folders, run `chats knowledge` to list them, `chats knowledge search "<query>"` for FTS. The Knowledge tab in the desktop UI browses them visually.

### Personal memories

`chats memory add "fact" --type user|feedback|project|reference` saves cross-session memory. Surfaced on every session start so Claude has context it would otherwise forget.

## Keyboard shortcuts (Linux)

In the chat list:

| Key | Action |
|---|---|
| `↑ / ↓` | Navigate |
| `Enter` | Open conversation (live terminal) |
| `/` | Focus search |
| `N` | New chat (inline) |
| `Alt+N` | New chat (external `gnome-terminal`) |
| `O` | Resume in external terminal |
| `S` | Star toggle |
| `R` | Rename |
| `T` | AI-generated title |
| `D` | Toggle done |
| `Alt+Del` | Delete (Alt prefix prevents accidents) |
| `Ctrl+A` | Select all |

While terminal has focus (Linux only):

| Key | Action |
|---|---|
| `Alt+W` | Close current terminal |
| `Alt+J / Alt+K` | Next / previous chat |
| `Alt+1..4` | Switch to Chats / Memory / Knowledge / Usage tab |
| `Alt+B` | Toggle file pane |
| `Ctrl+Shift+C / V` | Copy / paste |
| `Ctrl+Backspace` | Delete word |
| `Shift+Enter` | Insert newline (multi-line claude prompt) |
| `Ctrl+click` | Open URL |

Customize keybindings at `~/.config/serena/keybindings.json` (auto-created on first launch with defaults).

## Repo layout

```
serena/
├── cli.py                # `chats` CLI entry
├── core/                 # indexer, parser, scanner, metadata sync
├── chats/                # session formatter, exporter, watcher, AI titles
├── ui/
│   ├── web.py            # Flask app + entire HTML/CSS/JS frontend (single-file by choice)
│   └── pty_terminal.py   # cross-platform PTY (ptyprocess on Linux/macOS, pywinpty on Windows)
├── desktop/
│   ├── app_gtk.py        # Linux-native GTK shell with VTE
│   └── app.py            # pywebview fallback for macOS/Windows
├── memory/               # code only; user notes gitignored
└── knowledge/            # code only; user notes gitignored
```

## Storage

| What | Where |
|---|---|
| Index DB | `~/.claude/projects/.chats-index.db` (SQLite, rebuilt by indexer) |
| Synced metadata (stars, dones, tags, custom titles) | `~/.claude/projects/.chats-meta.json` |
| Image upload staging (drag-drop) | `/tmp/serena-chats-uploads/<uuid>.<ext>` |
| Keybindings overrides | `~/.config/serena/keybindings.json` |

The metadata JSON is intentionally small and key-by-UUID so it's safe to sync via Syncthing / iCloud across multiple devices without conflicts.

## Known limitations

- macOS / Windows have a less polished terminal experience than Linux (xterm.js vs. native VTE). The Linux path is the daily-driver target.
- Chats from other devices (synced via the metadata file) appear in the sidebar but `chats desktop` can only resume chats whose `.jsonl` files exist locally.
- The auto-poll refreshes the chat list every 5 seconds while any terminal is running, off when nothing's active.

## License

Internal / personal project. Not licensed for redistribution without permission.
