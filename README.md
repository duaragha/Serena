# Serena

One repo, one install, one brain. Each feature owns a top-level folder with its
code (and where it applies, its data) colocated.

## Layout

```
serena/
├── cli.py              # root CLI entry (the `chats` command)
├── __main__.py         # `python -m serena` shim
├── core/               # shared foundation — used by every feature
│   ├── config.py       # paths, env vars
│   ├── parser.py       # Message, SessionMeta data models
│   ├── scanner.py      # scan ~/.claude/projects/
│   ├── metadata.py     # synced metadata JSON
│   └── indexer.py      # SQLite index layer (sessions + knowledge)
├── chats/              # session-browsing feature
│   ├── titles.py
│   ├── llm_titles.py
│   ├── watcher.py
│   ├── formatter.py
│   └── exporter.py
├── memory/             # feature code + data
│   ├── store.py        # [code, tracked]
│   └── {user,feedback,project,general}/  # [data, gitignored]
├── knowledge/          # feature code + data
│   ├── reader.py       # [code, tracked]
│   └── <topic>/        # [data, gitignored]
├── ui/
│   ├── web.py          # Flask app
│   └── tui.py          # Textual app
├── voice/              # voice assistant daemon (optional deps)
├── reminders/          # Android Tasker reminder system
├── dream/              # memory consolidation hook
└── docs/
```

## Data vs code

- **Code lives in the repo and is tracked by git.** `.py` files, README, Persona.md, docs.
- **User data (memory notes, knowledge markdown) lives inside the same feature folder but is gitignored.** That way you open `memory/` and see both code and data, but `git status` never shows your notes.

## Install

```bash
uv sync                     # base install (chats, memory, knowledge, UI)
uv sync --extra voice       # add voice assistant deps
uv sync --extra dev         # add ruff + pyright + pytest
```

Editable by default — edits to `.py` files take effect on next invocation.

## Why this shape

- Every feature is self-contained. Memory code next to memory data. Knowledge code next to knowledge data.
- Cross-cutting foundation (`core/`) is explicit, small, and doesn't know about features.
- One `chats` CLI dispatches to all features. No per-feature installs or per-feature repos.
