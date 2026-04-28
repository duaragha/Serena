"""Terminal-style web UI for browsing Claude Code conversations, memories, and knowledge."""

import functools
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, request
from flask_sock import Sock

from ui import pty_terminal

from core.indexer import (
    get_session,
    list_sessions,
    list_projects,
    search_fts,
    build_fts,
    toggle_star,
    update_index,
    update_knowledge_index,
    list_knowledge_topics,
    search_knowledge_fts,
    unified_search,
    get_session_topics,
    get_topic_sessions,
    link_session_topic,
    unlink_session_topic,
    set_title,
    delete_session,
    get_usage_stats,
)
from core.config import ensure_session_visible, resolve_session_cwd
from knowledge.reader import get_topic_content, get_file_content, get_topic_files
from core.parser import parse_full
from chats.llm_titles import generate_titles_batch

app = Flask(__name__)
sock = Sock(app)

# ---------------------------------------------------------------------------
# Memory filesystem helpers
# ---------------------------------------------------------------------------

def _find_memory_dir() -> Path:
    """Locate the filesystem-based memory directory."""
    candidates = [
        Path.home() / "Projects" / "serena" / "memory",
        Path.home() / "Documents" / "Projects" / "serena" / "memory",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


MEMORY_DIR = _find_memory_dir()
MEMORY_TYPES = ["feedback", "user", "project", "general", "reference"]


def _parse_memory_file(fpath: Path) -> dict | None:
    """Parse a single memory .md file with YAML frontmatter."""
    try:
        text = fpath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Extract YAML frontmatter between --- lines
    if not text.startswith("---"):
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        return None

    frontmatter = parts[1].strip()
    body = parts[2].strip()

    meta: dict = {}
    for line in frontmatter.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()

    return {
        "id": int(meta.get("id", 0)),
        "type": meta.get("type", "general"),
        "created": meta.get("created", ""),
        "updated": meta.get("updated", ""),
        "content": body,
        "filename": fpath.name,
    }


def _list_all_memories() -> list[dict]:
    """Read all memory files from the filesystem."""
    memories = []
    for mtype in MEMORY_TYPES:
        type_dir = MEMORY_DIR / mtype
        if not type_dir.exists():
            continue
        for f in sorted(type_dir.glob("*.md")):
            mem = _parse_memory_file(f)
            if mem:
                memories.append(mem)
    memories.sort(key=lambda m: (m["type"], m["id"]))
    return memories


def _next_memory_id() -> int:
    """Find the next available memory ID across all types."""
    max_id = 0
    for mtype in MEMORY_TYPES:
        type_dir = MEMORY_DIR / mtype
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            mem = _parse_memory_file(f)
            if mem and mem["id"] > max_id:
                max_id = mem["id"]
    return max_id + 1


def _slugify(text: str, max_len: int = 50) -> str:
    """Create a filename-safe slug from text."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug)
    slug = slug[:max_len].rstrip("-")
    return slug


def _write_memory_file(mem_id: int, mem_type: str, content: str, created: str = "", updated: str = ""):
    """Write a memory to the filesystem."""
    from datetime import datetime
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not created:
        created = now_str
    if not updated:
        updated = now_str

    type_dir = MEMORY_DIR / mem_type
    type_dir.mkdir(parents=True, exist_ok=True)

    slug = _slugify(content)
    filename = f"{mem_id:03d}-{slug}.md"
    fpath = type_dir / filename

    text = f"---\nid: {mem_id}\ntype: {mem_type}\ncreated: {created}\nupdated: {updated}\n---\n\n{content}\n"
    fpath.write_text(text, encoding="utf-8")
    return fpath


def _find_memory_path(mem_id: int) -> Path | None:
    """Find the file path for a memory by its ID."""
    for mtype in MEMORY_TYPES:
        type_dir = MEMORY_DIR / mtype
        if not type_dir.exists():
            continue
        for f in type_dir.glob("*.md"):
            if f.name.startswith(f"{mem_id:03d}-"):
                return f
    return None


def _update_memory_index():
    """Regenerate the INDEX.md file in the memory directory."""
    memories = _list_all_memories()
    by_type: dict[str, list[dict]] = {}
    for m in memories:
        by_type.setdefault(m["type"], []).append(m)

    lines = ["# Memory", "", "Persistent memories grouped by type. Each file is one memory.", ""]

    type_order = ["feedback", "user", "project", "general", "reference"]
    for t in type_order:
        mems = by_type.get(t, [])
        if not mems:
            continue
        lines.append(f"## {t.title()} ({len(mems)})")
        lines.append("")
        for m in mems:
            summary = m["content"].split("\n")[0][:80]
            lines.append(f"- [#{m['id']}](./{m['type']}/{m['filename']}) \u2014 {summary}")
        lines.append("")

    index_path = MEMORY_DIR / "INDEX.md"
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Project path helpers
# ---------------------------------------------------------------------------

def _shorten_project(project: str, cwd: str | None = None) -> str:
    """Convert a slugified project dir (or a cwd) to a short readable name.

    Returns just the leaf folder — 'full_tracker', 'hydrogen', 'admin-dashboard'.
    Home dir collapses to '~'. Organizational wrappers like
    ``personal_projects/`` or ``Documents/Projects/`` are stripped implicitly by
    taking only the last path segment.
    """
    if cwd:
        path = cwd
    else:
        # Decode from slugified dir name
        if project.startswith("C--"):
            path = project.replace("C--", "C:\\", 1).replace("-", "\\")
        elif project.startswith("-home-") or project.startswith("-root-") or project.startswith("-Users-"):
            path = "/" + project[1:].replace("-", "/")
        else:
            path = project

    norm = path.replace("\\", "/").rstrip("/")

    # Home dir exact match (cross-platform: Linux /home/X, macOS /Users/X, Windows C:/Users/X)
    home_norm = str(Path.home()).replace("\\", "/").rstrip("/")
    if norm == home_norm:
        return "~"

    parts = [p for p in norm.split("/") if p]
    return parts[-1] if parts else norm or "~"


def _get_session_cwd(session: dict) -> str:
    """Get the best working directory for a session."""
    return session.get("last_cwd") or session.get("cwd") or ""


# ---------------------------------------------------------------------------
# Browser process tracking
# ---------------------------------------------------------------------------

_browser_pid: int | None = None


# ---------------------------------------------------------------------------
# HTML template (raw string to preserve JS backslash escapes)
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chats</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-web-links@0.11.0/lib/addon-web-links.min.js"></script>
<style>
/* ── Reset & Vars ── */
:root {
  --bg: #0a0a0a;
  --surface: #111111;
  --surface2: #161616;
  --border: #222222;
  --border-bright: #333333;
  --text: #c9d1d9;
  --text-dim: #555555;
  --text-bright: #e6edf3;
  --green: #3fb950;
  --green-dim: rgba(63,185,80,0.12);
  --amber: #d29922;
  --amber-dim: rgba(210,153,34,0.12);
  --red: #f85149;
  --red-dim: rgba(248,81,73,0.12);
  --blue: #58a6ff;
  --blue-dim: rgba(88,166,255,0.12);
  --mono: 'JetBrains Mono', ui-monospace, 'Cascadia Code', 'Fira Code', monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body {
  height: 100%;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.5;
}

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #444; }

/* ── Layout ── */
#app { display: flex; flex-direction: column; height: 100%; }

/* ── Tab Bar ── */
.tab-bar {
  display: flex;
  align-items: center;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  padding: 0 12px;
  height: 38px;
  gap: 0;
}
.tab {
  padding: 8px 16px;
  cursor: pointer;
  color: var(--text-dim);
  font-size: 12px;
  font-family: var(--mono);
  text-transform: uppercase;
  letter-spacing: 1px;
  border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
  user-select: none;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--green); border-bottom-color: var(--green); }
.tab .count {
  font-size: 10px;
  color: var(--text-dim);
  margin-left: 4px;
}
.tab.active .count { color: var(--green); }
.tab-spacer { flex: 1; }
.tab-action {
  padding: 4px 10px;
  cursor: pointer;
  color: var(--text-dim);
  font-size: 11px;
  font-family: var(--mono);
  border: 1px solid var(--border);
  background: transparent;
  border-radius: 3px;
  transition: all 0.15s;
}
.tab-action:hover { color: var(--green); border-color: var(--green); }

/* ── Main Content Area ── */
.main { flex: 1; display: flex; overflow: hidden; }

/* ── Panels ── */
.panel-left {
  width: 30%;
  min-width: 220px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
  overflow: hidden;
}
.chat-list-col {
  width: 30%;
  min-width: 220px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
  overflow: hidden;
}
.panel-right {
  width: 50%;
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.panel-right-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-dim);
  font-size: 12px;
}

/* ── Files pane (toggle: Alt+B) ── */
.panel-files {
  width: 9%;
  min-width: 100px;
  max-width: 240px;
  flex-shrink: 0;
  border-left: 1px solid var(--border);
  background: var(--surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.panel-files.hidden { display: none; }
.files-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--surface2);
  flex-shrink: 0;
}
.files-root {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-bright);
  text-transform: uppercase;
  letter-spacing: 0.6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.files-close {
  background: transparent;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 13px;
  padding: 0 4px;
}
.files-close:hover { color: var(--text); }
.files-tree {
  flex: 1;
  overflow-y: auto;
  font-size: 12px;
  padding: 4px 0;
}
.fnode {
  padding: 2px 8px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text);
  user-select: none;
}
.fnode:hover { background: rgba(255,255,255,0.04); }
.fnode.file { color: var(--text-dim); }
.fnode.file:hover { color: var(--text-bright); }
.fnode.folder { color: var(--text); font-weight: 500; }
.fnode[draggable="true"]:active { opacity: 0.6; }

/* ── Search ── */
.search-bar {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.search-bar input {
  width: 100%;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 6px 10px;
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
  outline: none;
}
.search-bar input:focus { border-color: var(--green); }
.search-bar input::placeholder { color: var(--text-dim); }

/* ── Project Sidebar ── */
.project-sidebar {
  width: 8%;
  min-width: 88px;
  flex-shrink: 0;
  overflow-y: auto;
  overflow-x: hidden;
  border-right: 1px solid var(--border);
  background: var(--surface);
  padding: 4px 0;
}
.project-item {
  padding: 6px 10px;
  cursor: pointer;
  border-left: 3px solid transparent;
  font-size: 11px;
  font-family: var(--mono);
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: background 0.1s, color 0.1s;
  user-select: none;
}
.project-item:hover { color: var(--text); background: rgba(255,255,255,0.04); }
.project-item.active {
  color: var(--green);
  border-left-color: var(--green);
  background: rgba(63,185,80,0.08);
}

/* ── Session List ── */
.session-list {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
}
.group-header {
  padding: 6px 12px 4px;
  font-size: 10px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 1px;
  background: var(--bg);
  position: sticky;
  top: 0;
  z-index: 2;
  border-bottom: 1px solid var(--border);
}
.group-header.starred-header { color: var(--amber); }
.group-header.active-header { color: var(--green); }

/* Agent badges (Claude / Codex) — inline SVG, color via currentColor */
.agent-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  margin-right: 6px;
  vertical-align: middle;
  flex-shrink: 0;
}
.agent-icon svg { width: 14px; height: 14px; display: block; }
.agent-icon.claude { color: #C15F3C; }   /* Anthropic crail orange */
.agent-icon.codex  { color: #b07cff; }   /* purple to differentiate cleanly from claude orange */
.group-header.done-header {
  color: var(--text-dim);
  cursor: pointer;
  user-select: none;
  margin-top: 8px;
  transition: color 0.12s;
}
.group-header.done-header:hover { color: var(--text); }
.done-section.collapsed { display: none; }
.session-row.done .session-title { color: var(--text-dim); }
.session-row.done .session-project,
.session-row.done .session-tokens,
.session-row.done .session-date,
.session-row.done .session-date-created { opacity: 0.7; }
.live-indicator {
  position: relative;
  display: inline-block;
  width: 13px;
  height: 13px;
  margin-right: 6px;
  vertical-align: middle;
}
.live-indicator .live-dot {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  margin: 0;
  width: 7px;
  height: 7px;
  transition: opacity 0.12s;
}
.live-indicator .term-close {
  position: absolute;
  inset: 0;
  margin: 0;
  transition: opacity 0.12s;
}
.live-indicator .term-close {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  line-height: 1;
  color: var(--text-dim);
  border-radius: 3px;
  cursor: pointer;
  opacity: 0;
}
.session-row:hover .live-indicator .live-dot { opacity: 0; }
.session-row:hover .live-indicator .term-close { opacity: 1; }
.live-indicator .term-close:hover {
  color: #f85149;
  background: rgba(248,81,73,0.15);
}
.session-row.active-terminal .session-title { color: var(--text-bright); }
.live-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--green);
  margin-right: 6px;
  vertical-align: middle;
  box-shadow: 0 0 6px rgba(63,185,80,0.8);
  animation: livePulse 1.6s ease-in-out infinite;
}
@keyframes livePulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}
.session-list-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  font-size: 9px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--text-dim);
  flex-shrink: 0;
  user-select: none;
}
.session-list-header .col-star { width: 16px; flex-shrink: 0; }
.session-list-header .col-title { flex: 1; overflow: hidden; }
.session-list-header .col-tokens { width: 48px; text-align: right; flex-shrink: 0; }
.session-list-header .col-date { width: 68px; text-align: right; flex-shrink: 0; }

.session-row {
  display: flex;
  align-items: center;
  padding: 7px 12px;
  cursor: pointer;
  border-left: 3px solid transparent;
  transition: background 0.1s;
  user-select: none;
  gap: 8px;
  min-height: 36px;
}
.session-row:nth-child(even) { background: rgba(255,255,255,0.015); }
.session-row:hover { background: rgba(255,255,255,0.04); }
.session-row.focused {
  border-left-color: var(--green);
  background: rgba(63,185,80,0.06);
}
.session-row.selected {
  background: rgba(63,185,80,0.08);
}
.session-row.focused.selected {
  border-left-color: var(--green);
  background: rgba(63,185,80,0.12);
}

.session-star {
  flex-shrink: 0;
  width: 16px;
  text-align: center;
  cursor: pointer;
  font-size: 12px;
  color: var(--border-bright);
}
.session-star.starred { color: var(--amber); }
.session-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--text);
}
.session-project {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-dim);
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.session-tokens {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-dim);
  width: 48px;
  text-align: right;
}
.session-date {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-dim);
  width: 68px;
  text-align: right;
}
.session-date-created {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-dim);
  width: 68px;
  text-align: right;
  opacity: 0.6;
}

/* ── Conversation View ── */
.conv-header {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  flex-shrink: 0;
}
.conv-header-top {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.conv-header-text {
  min-width: 0;
  flex: 1;
}
.conv-header h2 {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-bright);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv-header .meta {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 2px;
}
.conv-view-toggle {
  display: inline-flex;
  border: 1px solid var(--border-bright);
  border-radius: 4px;
  overflow: hidden;
  flex-shrink: 0;
}
.view-tab {
  background: transparent;
  color: var(--text-dim);
  border: none;
  padding: 4px 10px;
  font: inherit;
  font-size: 11px;
  letter-spacing: 0.4px;
  cursor: pointer;
  transition: color 0.1s, background 0.1s;
}
.view-tab:hover { color: var(--text); }
.view-tab.active {
  background: var(--green-dim);
  color: var(--green);
}
.view-tab + .view-tab { border-left: 1px solid var(--border-bright); }
.conv-terminal {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #000;
  overflow: hidden;
}
.conv-terminal.hidden { display: none; }
.term-status {
  padding: 6px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  font-size: 11px;
  color: var(--text-dim);
  flex-shrink: 0;
}
.term-status.error { color: #f85149; }
.term-status.live { color: var(--green); }
.term-mounts {
  flex: 1;
  min-height: 0;
  padding: 6px 8px 2px 8px;
  overflow: hidden;
  position: relative;
}
.term-pane {
  position: absolute;
  inset: 6px 8px 2px 8px;
  overflow: hidden;
}
.term-pane.hidden { display: none; }
.term-pane .xterm,
.term-pane .xterm-viewport,
.term-pane .xterm-screen {
  height: 100% !important;
  width: 100% !important;
}
.term-pane .xterm-viewport { background-color: #000 !important; }
.term-pane.drop-active {
  outline: 2px dashed var(--green);
  outline-offset: -6px;
  background: rgba(63,185,80,0.08);
}

/* ── Confirm modal ── */
#modalBackdrop {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.55);
  z-index: 9998;
  display: none;
  align-items: center;
  justify-content: center;
  animation: modalFade 0.12s ease;
}
#modalBackdrop.visible { display: flex; }
.modal-card {
  background: var(--surface);
  border: 1px solid var(--border-bright);
  border-radius: 8px;
  padding: 18px 20px 16px;
  min-width: 340px;
  max-width: 460px;
  box-shadow: 0 18px 48px rgba(0,0,0,0.55);
  color: var(--text);
  font-size: 13px;
  animation: modalPop 0.14s ease;
}
.modal-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-bright);
  margin-bottom: 6px;
}
.modal-body {
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.5;
  margin-bottom: 16px;
}
.modal-input {
  width: 100%;
  background: var(--bg);
  color: var(--text-bright);
  border: 1px solid var(--border-bright);
  border-radius: 4px;
  padding: 8px 10px;
  font: inherit;
  font-size: 13px;
  margin-bottom: 16px;
  outline: none;
  transition: border-color 0.1s;
}
.modal-input:focus { border-color: var(--green); }
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.modal-btn {
  background: transparent;
  color: var(--text);
  border: 1px solid var(--border-bright);
  border-radius: 4px;
  padding: 6px 14px;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.1s, border-color 0.1s, color 0.1s;
}
.modal-btn:hover { background: rgba(255,255,255,0.05); }
.modal-btn:focus-visible { outline: none; border-color: var(--text-bright); }
.modal-btn.primary {
  background: var(--green-dim);
  color: var(--green);
  border-color: var(--green);
}
.modal-btn.primary:hover { background: rgba(63,185,80,0.18); }
.modal-btn.danger {
  background: rgba(248,81,73,0.12);
  color: #f85149;
  border-color: #f85149;
}
.modal-btn.danger:hover { background: rgba(248,81,73,0.22); }
@keyframes modalFade { from { opacity: 0; } to { opacity: 1; } }
@keyframes modalPop  { from { transform: translateY(4px) scale(0.98); opacity: 0; } to { transform: none; opacity: 1; } }

/* ── Toast ──
   Positioned bottom-LEFT so it lands inside the sidebar column. In the GTK
   desktop shell, the VTE is a native widget overlaid on top of the WebView,
   which means DOM z-index can't stack above it — the only reliable trick is
   to render toasts outside the terminal's rect. */
#toastStack {
  position: fixed;
  bottom: 18px;
  left: 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 9999;
  pointer-events: none;
}
.toast {
  pointer-events: auto;
  min-width: 240px;
  max-width: 360px;
  padding: 10px 14px;
  font-size: 12px;
  border-radius: 6px;
  background: var(--surface2);
  color: var(--text-bright);
  border: 1px solid var(--border-bright);
  box-shadow: 0 6px 18px rgba(0,0,0,0.35);
  display: flex;
  align-items: center;
  gap: 10px;
  opacity: 0;
  transform: translateY(8px);
  transition: opacity 0.14s ease, transform 0.14s ease;
}
.toast.visible { opacity: 1; transform: translateY(0); }
.toast.success { border-color: var(--green); color: var(--green); }
.toast.error   { border-color: #f85149; color: #f85149; }
.toast-spinner {
  width: 12px; height: 12px;
  border: 2px solid var(--border-bright);
  border-top-color: var(--text-bright);
  border-radius: 50%;
  animation: toast-spin 0.8s linear infinite;
  flex-shrink: 0;
}
@keyframes toast-spin { to { transform: rotate(360deg); } }
#convContent {
  display: flex;
  flex-direction: column;
  flex: 1;
  overflow: hidden;
}
.conv-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
.msg { margin-bottom: 16px; }
.msg-role {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 3px;
}
.msg-role.user { color: var(--amber); }
.msg-role.assistant { color: var(--green); }
.msg-role.tool { color: var(--blue); }
.msg-body {
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-wrap: break-word;
  color: var(--text);
}
.msg-tool {
  font-size: 11px;
  color: var(--blue);
  padding: 4px 8px;
  background: var(--blue-dim);
  border-radius: 3px;
  margin-bottom: 4px;
  display: inline-block;
}
.msg-tool-output {
  font-size: 11px;
  color: var(--text-dim);
  padding: 4px 8px;
  background: rgba(255,255,255,0.03);
  border-left: 2px solid var(--border);
  margin-bottom: 4px;
  white-space: pre-wrap;
  word-wrap: break-word;
  max-height: 200px;
  overflow-y: auto;
}

/* ── Memory View ── */
.memory-list {
  flex: 1;
  overflow-y: auto;
}
.memory-group-header {
  padding: 8px 12px;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--green);
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 2;
}
.memory-row {
  display: flex;
  align-items: flex-start;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  border-left: 3px solid transparent;
  cursor: pointer;
  transition: background 0.1s;
  gap: 8px;
  min-height: 36px;
}
.memory-row:nth-child(even) { background: rgba(255,255,255,0.015); }
.memory-row:hover { background: rgba(255,255,255,0.04); }
.memory-row.focused {
  border-left-color: var(--green);
  background: rgba(63,185,80,0.06);
}
.memory-id {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--text-dim);
  width: 30px;
}
.memory-content {
  flex: 1;
  font-size: 12px;
  color: var(--text);
  line-height: 1.5;
}
.memory-actions {
  flex-shrink: 0;
  display: none;
  gap: 4px;
}
.memory-row:hover .memory-actions,
.memory-row.focused .memory-actions { display: flex; }
.mem-btn {
  padding: 2px 8px;
  font-size: 10px;
  font-family: var(--mono);
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  border-radius: 2px;
  transition: all 0.15s;
}
.mem-btn:hover { color: var(--text); border-color: var(--text-dim); }
.mem-btn.danger:hover { color: var(--red); border-color: var(--red); }

/* ── Knowledge View ── */
.knowledge-left {
  width: 280px;
  min-width: 200px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
  overflow: hidden;
}
.knowledge-right {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.topic-list {
  flex: 1;
  overflow-y: auto;
}
.topic-row {
  padding: 8px 12px;
  cursor: pointer;
  border-left: 3px solid transparent;
  transition: background 0.1s;
}
.topic-row:nth-child(even) { background: rgba(255,255,255,0.015); }
.topic-row:hover { background: rgba(255,255,255,0.04); }
.topic-row.focused {
  border-left-color: var(--green);
  background: rgba(63,185,80,0.06);
}
.topic-title {
  font-size: 12px;
  color: var(--text);
}
.topic-desc {
  font-size: 10px;
  color: var(--text-dim);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.topic-meta {
  font-size: 10px;
  color: var(--text-dim);
  margin-top: 2px;
}
.file-list {
  flex: 1;
  overflow-y: auto;
}
.file-row {
  padding: 6px 12px;
  cursor: pointer;
  border-left: 3px solid transparent;
  transition: background 0.1s;
  font-size: 12px;
  display: flex;
  justify-content: space-between;
}
.file-row:nth-child(even) { background: rgba(255,255,255,0.015); }
.file-row:hover { background: rgba(255,255,255,0.04); }
.file-row.focused {
  border-left-color: var(--green);
  background: rgba(63,185,80,0.06);
}
.file-name { color: var(--text); }
.file-size { color: var(--text-dim); font-size: 10px; }
.file-content-view {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-wrap: break-word;
}
.file-header {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  flex-shrink: 0;
  font-size: 12px;
  color: var(--text-bright);
  display: flex;
  align-items: center;
  gap: 8px;
}
.file-back {
  cursor: pointer;
  color: var(--green);
  font-size: 12px;
}

/* ── Shortcut Bar ── */
.shortcut-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 12px;
  background: var(--surface);
  border-top: 1px solid var(--border);
  flex-shrink: 0;
  height: 28px;
  overflow-x: auto;
}
.shortcut {
  font-size: 10px;
  color: var(--text-dim);
  white-space: nowrap;
  flex-shrink: 0;
}
.shortcut kbd {
  display: inline-block;
  padding: 0 4px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 2px;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--green);
  margin-right: 3px;
}

/* ── Usage Dashboard ── */
.usage-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 28px 20px 40px;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.usage-wrap {
  width: 100%;
  max-width: 1120px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
}
.usage-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 8px;
  align-items: start;
}
.usage-row > * { margin-top: 0 !important; }
@media (max-width: 820px) {
  .usage-row { grid-template-columns: 1fr; }
}
.usage-topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
  gap: 12px;
  flex-wrap: wrap;
}
.usage-subtabs, .usage-range {
  display: flex;
  gap: 2px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 2px;
}
.usage-subtab, .usage-range-btn {
  padding: 5px 12px;
  background: transparent;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.3px;
  border-radius: 4px;
  transition: color 0.15s, background 0.15s;
}
.usage-subtab:hover, .usage-range-btn:hover { color: var(--text); }
.usage-subtab.active, .usage-range-btn.active {
  background: var(--bg);
  color: var(--text-bright);
}
.stat-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 6px;
  margin-bottom: 6px;
}
@media (max-width: 980px) { .stat-grid { grid-template-columns: repeat(4, 1fr); } }
@media (max-width: 620px) { .stat-grid { grid-template-columns: repeat(2, 1fr); } }
.stat-card {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
  min-width: 0;
}
.stat-card-label {
  font-size: 10px;
  color: var(--text-dim);
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.stat-card-value {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-bright);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.heatmap-wrap {
  margin-top: 10px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px 12px;
  overflow-x: auto;
}
.heatmap-grid {
  display: inline-grid;
  grid-template-rows: repeat(7, 12px);
  grid-auto-flow: column;
  grid-auto-columns: 12px;
  gap: 3px;
}
.heatmap-cell {
  width: 12px;
  height: 12px;
  border-radius: 2px;
  background: #1a1a1a;
}
.heatmap-cell.empty { background: transparent; }
.heatmap-cell[data-level="1"] { background: rgba(63,185,80,0.22); }
.heatmap-cell[data-level="2"] { background: rgba(63,185,80,0.45); }
.heatmap-cell[data-level="3"] { background: rgba(63,185,80,0.70); }
.heatmap-cell[data-level="4"] { background: rgba(63,185,80,0.95); }
.usage-flavor {
  margin-top: 10px;
  padding: 8px 12px;
  font-size: 11px;
  color: var(--text-dim);
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-style: italic;
}
.model-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.model-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
}
.model-row-name { font-size: 12px; color: var(--text-bright); font-weight: 600; }
.model-row-meta { font-size: 10px; color: var(--text-dim); }
.model-row-tokens { font-size: 12px; color: var(--green); font-variant-numeric: tabular-nums; }
.model-bar-wrap {
  grid-column: 1 / -1;
  height: 4px;
  background: var(--bg);
  border-radius: 2px;
  overflow: hidden;
  display: flex;
}
.model-bar-seg {
  height: 100%;
}
.model-bar-seg.input { background: rgba(88,166,255,0.85); }
.model-bar-seg.output { background: rgba(63,185,80,0.85); }
.model-bar-seg.cache-read { background: rgba(210,153,34,0.70); }
.model-bar-seg.cache-create { background: rgba(210,153,34,0.35); }
.model-legend {
  display: flex;
  gap: 14px;
  font-size: 10px;
  color: var(--text-dim);
  padding: 4px 2px 6px;
  flex-wrap: wrap;
}
.model-legend .dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  margin-right: 5px;
  vertical-align: middle;
}
.model-legend .dot.input { background: rgba(88,166,255,0.85); }
.model-legend .dot.output { background: rgba(63,185,80,0.85); }
.model-legend .dot.cache-read { background: rgba(210,153,34,0.70); }
.model-legend .dot.cache-create { background: rgba(210,153,34,0.35); }

/* Hour-of-day bar chart */
.hour-chart-wrap {
  margin-top: 10px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px 12px 8px;
}
.hour-chart-title {
  font-size: 10px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}
.hour-chart {
  display: grid;
  grid-template-columns: repeat(24, 1fr);
  gap: 3px;
  align-items: end;
  height: 60px;
}
.hour-bar {
  background: rgba(63,185,80,0.55);
  border-radius: 2px 2px 0 0;
  min-height: 2px;
  position: relative;
  transition: background 0.15s;
}
.hour-bar.peak { background: var(--green); }
.hour-bar:hover { background: var(--green); }
.hour-labels {
  display: grid;
  grid-template-columns: repeat(24, 1fr);
  gap: 3px;
  margin-top: 4px;
  font-size: 9px;
  color: var(--text-dim);
  text-align: center;
}

/* Top projects list */
.section-title {
  font-size: 10px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 14px 2px 6px;
}
.top-projects-card {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
  display: flex;
  flex-direction: column;
}
.top-projects-card .hour-chart-title { margin-bottom: 8px; }
.top-projects-card .project-list-usage {
  border: none;
  background: transparent;
  flex: 1;
}
.project-list-usage {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}
.project-row-usage {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  position: relative;
}
.project-row-usage:last-child { border-bottom: none; }
.project-row-usage .bar-bg {
  position: absolute;
  left: 0; top: 0; bottom: 0;
  background: rgba(63,185,80,0.08);
  z-index: 0;
}
.project-row-usage > * { position: relative; z-index: 1; }
.project-name-usage { color: var(--text-bright); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.project-count-usage { color: var(--text-dim); font-size: 10px; font-variant-numeric: tabular-nums; }
.project-pct-usage { color: var(--green); font-weight: 600; font-size: 11px; font-variant-numeric: tabular-nums; }

/* Achievements */
.achievements-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 6px;
}
.ach {
  padding: 8px 10px;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  opacity: 0.4;
  transition: opacity 0.2s, border-color 0.2s;
}
.ach.unlocked {
  opacity: 1;
  border-color: rgba(63,185,80,0.4);
  background: rgba(63,185,80,0.06);
}
.ach-label { font-size: 11px; font-weight: 600; color: var(--text-bright); }
.ach.unlocked .ach-label { color: var(--green); }
.ach-desc { font-size: 10px; color: var(--text-dim); }

/* Brain strip (memory + knowledge) */
.brain-strip {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
}
@media (max-width: 980px) {
  .brain-strip { grid-template-columns: 1fr; }
}
.brain-card {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
}
.brain-card-label { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
.brain-card-value { font-size: 15px; font-weight: 600; color: var(--text-bright); margin-top: 2px; }
.brain-card-sub { font-size: 10px; color: var(--text-dim); margin-top: 2px; }

/* ── Utility ── */
.hidden { display: none !important; }
.loading-text { color: var(--text-dim); padding: 20px; text-align: center; font-size: 12px; }
.empty-text { color: var(--text-dim); padding: 40px 20px; text-align: center; font-size: 12px; }
.selection-info {
  padding: 4px 12px;
  background: var(--green-dim);
  border-bottom: 1px solid var(--border);
  font-size: 11px;
  color: var(--green);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.selection-actions { display: flex; gap: 6px; }
.sel-btn {
  padding: 2px 8px;
  font-size: 10px;
  font-family: var(--mono);
  border: 1px solid var(--green);
  background: transparent;
  color: var(--green);
  cursor: pointer;
  border-radius: 2px;
}
.sel-btn:hover { background: var(--green-dim); }
.sel-btn.danger { border-color: var(--red); color: var(--red); }
.sel-btn.danger:hover { background: var(--red-dim); }
</style>
</head>
<body>
<div id="app">
  <!-- Tab Bar -->
  <div class="tab-bar">
    <div class="tab active" data-tab="chats" onclick="switchTab('chats')">Chats <span class="count" id="chatCount"></span></div>
    <div class="tab" data-tab="memory" onclick="switchTab('memory')">Memory <span class="count" id="memoryCount"></span></div>
    <div class="tab" data-tab="knowledge" onclick="switchTab('knowledge')">Knowledge <span class="count" id="knowledgeCount"></span></div>
    <div class="tab" data-tab="usage" onclick="switchTab('usage')">Usage</div>
    <div class="tab-spacer"></div>
    <button class="tab-action" onclick="shutdownServer()" title="Shutdown server">Quit</button>
  </div>

  <!-- ═══ CHATS VIEW ═══ -->
  <div class="main" id="viewChats">
    <div class="project-sidebar" id="projectSidebar"></div>
    <div class="chat-list-col">
      <div class="search-bar">
        <input type="text" id="searchInput" placeholder="Search conversations... ( / )" autocomplete="off">
      </div>
      <div class="selection-info hidden" id="selectionInfo">
        <span id="selectionText">0 selected</span>
        <div class="selection-actions">
          <button class="sel-btn" onclick="bulkRetitle()">AI Title</button>
          <button class="sel-btn danger" onclick="bulkDelete()">Delete</button>
        </div>
      </div>
      <div class="session-list-header">
        <span class="col-star"></span>
        <span class="col-title">Title</span>
        <span class="col-tokens">Tokens</span>
        <span class="col-date">C.Date</span>
        <span class="col-date">M.Date</span>
      </div>
      <div class="session-list" id="sessionList"></div>
    </div>
    <div class="panel-right" id="convPanel">
      <div class="panel-right-empty" id="convEmpty">Select a conversation</div>
      <div class="hidden" id="convContent">
        <div class="conv-header">
          <div class="conv-header-top">
            <div class="conv-header-text">
              <h2 id="convTitle"></h2>
              <div class="meta" id="convMeta"></div>
            </div>
            <div class="conv-view-toggle" role="tablist" aria-label="View mode">
              <button class="view-tab" id="viewReadBtn" onclick="setConvMode('read')" title="Transcript (read-only)">Read</button>
              <button class="view-tab active" id="viewLiveBtn" onclick="setConvMode('live')" title="Resume inline (live Claude session)">Code</button>
            </div>
          </div>
        </div>
        <div class="conv-body" id="convBody"></div>
        <div class="conv-terminal hidden" id="convTerminal">
          <div class="term-status" id="termStatus">Ready to resume.</div>
          <div class="term-mounts" id="termMounts"></div>
        </div>
      </div>
    </div>
    <div class="panel-files" id="filesPane">
      <div class="files-header">
        <span class="files-root" id="filesRootName">—</span>
        <button class="files-close" onclick="toggleFilesPane()" title="Close (Alt+B)">✕</button>
      </div>
      <div class="files-tree" id="filesTree"><div class="empty-text">Open a chat to view files</div></div>
    </div>
  </div>

  <!-- ═══ MEMORY VIEW ═══ -->
  <div class="main hidden" id="viewMemory">
    <div class="panel-left" style="width:100%;border:none;">
      <div style="padding:8px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;">
        <span style="font-size:12px;color:var(--text-dim);">All Memories</span>
        <button class="tab-action" onclick="addMemory()">+ Add</button>
      </div>
      <div class="memory-list" id="memoryList"></div>
    </div>
  </div>

  <!-- ═══ KNOWLEDGE VIEW ═══ -->
  <div class="main hidden" id="viewKnowledge">
    <div class="knowledge-left">
      <div style="padding:8px 12px;border-bottom:1px solid var(--border);font-size:12px;color:var(--text-dim);flex-shrink:0;">Topics</div>
      <div class="topic-list" id="topicList"></div>
    </div>
    <div class="knowledge-right" id="knowledgeRight">
      <div class="panel-right-empty" id="knowledgeEmpty">Select a topic</div>
      <div class="hidden" id="knowledgeContent">
        <div class="file-header" id="knowledgeHeader"></div>
        <div class="file-list hidden" id="fileList"></div>
        <div class="file-content-view hidden" id="fileContentView"></div>
      </div>
    </div>
  </div>

  <!-- ═══ USAGE VIEW ═══ -->
  <div class="main hidden" id="viewUsage">
    <div class="usage-scroll">
      <div class="usage-wrap">
        <div class="usage-topbar">
          <div class="usage-subtabs" id="usageSubtabs">
            <button class="usage-subtab active" data-subtab="overview" onclick="setUsageSubtab('overview')">Overview</button>
            <button class="usage-subtab" data-subtab="models" onclick="setUsageSubtab('models')">Models</button>
          </div>
          <div class="usage-range" id="usageRangeGroup">
            <button class="usage-range-btn" data-range="all" onclick="setUsageRange('all')">All</button>
            <button class="usage-range-btn active" data-range="30" onclick="setUsageRange('30')">30d</button>
            <button class="usage-range-btn" data-range="7" onclick="setUsageRange('7')">7d</button>
          </div>
        </div>
        <div id="usageOverview">
          <div class="stat-grid" id="statGrid"></div>
          <div class="usage-row">
            <div class="hour-chart-wrap">
              <div class="hour-chart-title">Sessions by hour</div>
              <div class="hour-chart" id="hourChart"></div>
              <div class="hour-labels"><span>12a</span><span></span><span></span><span>3a</span><span></span><span></span><span>6a</span><span></span><span></span><span>9a</span><span></span><span></span><span>12p</span><span></span><span></span><span>3p</span><span></span><span></span><span>6p</span><span></span><span></span><span>9p</span><span></span><span></span></div>
            </div>
            <div class="top-projects-card">
              <div class="hour-chart-title">Top projects</div>
              <div class="project-list-usage" id="topProjectList"></div>
            </div>
          </div>
          <div class="heatmap-wrap" id="heatmapWrap"></div>
          <div class="usage-row">
            <div>
              <div class="section-title" style="margin-top:0">Achievements</div>
              <div class="achievements-grid" id="achievementsGrid"></div>
            </div>
            <div>
              <div class="section-title" style="margin-top:0">Your second brain</div>
              <div class="brain-strip" id="brainStrip"></div>
            </div>
          </div>
          <div class="usage-flavor" id="usageFlavor"></div>
        </div>
        <div id="usageModels" class="hidden">
          <div class="model-legend">
            <span><span class="dot input"></span>Input</span>
            <span><span class="dot output"></span>Output</span>
            <span><span class="dot cache-read"></span>Cache read</span>
            <span><span class="dot cache-create"></span>Cache create</span>
          </div>
          <div class="model-list" id="modelList"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Shortcut Bar -->
  <div class="shortcut-bar" id="shortcutBar"></div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════
let currentTab = 'chats';
let sessions = [];
let allSessions = [];
let currentProject = null;
let currentSessionId = null;
let focusedIndex = -1;
let focusedSid = null;  // authoritative: which chat is focused, survives re-renders
let selectedIds = new Set();
let searchTimeout = null;

// Memory state
let memories = [];
let memFocusedIndex = -1;

// Knowledge state
let topics = [];
let topicFocusedIndex = -1;
let currentTopicSlug = null;
let topicFiles = [];
let fileFocusedIndex = -1;
let viewingFile = false;

// Usage state
let usageRange = '30';
let usageSubtab = 'overview';
let usageData = null;

// ═══════════════════════════════════════════════════════════════
// Tab Switching
// ═══════════════════════════════════════════════════════════════
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.getElementById('viewChats').classList.toggle('hidden', tab !== 'chats');
  document.getElementById('viewMemory').classList.toggle('hidden', tab !== 'memory');
  document.getElementById('viewKnowledge').classList.toggle('hidden', tab !== 'knowledge');
  document.getElementById('viewUsage').classList.toggle('hidden', tab !== 'usage');
  updateShortcutBar();
  if (tab === 'memory') { if (memories.length) renderMemoryList(); else loadMemories(); }
  if (tab === 'knowledge') { if (topics.length) renderTopicList(); else loadTopics(); }
  if (tab === 'usage') { loadUsage(); }
}

// ═══════════════════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════════════════
function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function formatTokens(n) {
  if (!n) return '';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return Math.round(n / 1e3) + 'K';
  return String(n);
}

function formatSize(n) {
  if (!n) return '';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + 'MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + 'KB';
  return n + 'B';
}

function formatDate(ts) {
  if (!ts) return '';
  return ts.slice(0, 10);
}

function timeGroup(ts) {
  if (!ts) return 'Unknown';
  const d = new Date(ts);
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diffMs = todayStart - new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.floor(diffMs / 86400000);
  if (days < 0 || days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days <= 7) return 'This Week';
  if (days <= 14) return 'Last Week';
  if (days <= 30) return 'This Month';
  if (days <= 60) return 'Last Month';
  return d.toLocaleString('default', { month: 'long', year: 'numeric' });
}

function totalTokens(s) {
  // Billable tokens only (excludes cache reads) — mirrors the usage dashboard.
  return (s.input_tokens || 0) + (s.output_tokens || 0) + (s.cache_create_tokens || 0);
}

// ═══════════════════════════════════════════════════════════════
// CHATS: Data Loading
// ═══════════════════════════════════════════════════════════════
async function loadSessions(projectOrDirs, opts) {
  // Accepts either a single project_dir string (legacy) or an array of dirs
  // for a merged chip that covers multiple OSes. opts.refresh forces a disk
  // rescan server-side (picks up brand-new jsonl files from /clear, etc).
  let dirs = null;
  if (Array.isArray(projectOrDirs)) {
    dirs = projectOrDirs;
  } else if (projectOrDirs) {
    dirs = [projectOrDirs];
  }
  currentProject = dirs;
  const params = new URLSearchParams();
  if (dirs && dirs.length) params.set('projects', dirs.join(','));
  if (opts && opts.refresh) params.set('refresh', '1');
  try {
    const r = await fetch('/api/sessions?' + params);
    allSessions = await r.json();
    // Note: /clear auto-migration was removed — it was merging unrelated chats.
    // New session files that appear during a live terminal just show up as their
    // own rows now; user can click them to continue, or mark the old one done.
    // Keep any in-flight pseudo sessions pinned at the top after a reload
    sessions = _pseudoSessions.length ? [..._pseudoSessions, ...allSessions] : allSessions;
    renderSessionList();
    updateChatCount();
  } catch(e) {
    console.error('loadSessions:', e);
  }
}

async function searchSessions(q) {
  if (!q) { loadSessions(currentProject); return; }
  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(q));
    const results = await r.json();
    sessions = results;
    renderSessionList();
  } catch(e) {
    console.error('searchSessions:', e);
  }
}

let projectChips = [];
let currentProjectCwd = null;

async function loadProjects() {
  try {
    const r = await fetch('/api/projects');
    projectChips = await r.json();
    projectChips.sort((a, b) => (a.short || '').localeCompare(b.short || '', undefined, { sensitivity: 'base' }));
    const bar = document.getElementById('projectSidebar');
    let html = '<div class="project-item active" onclick="filterProject(-1, this)" title="Show all chats">All</div>';
    projectChips.forEach((p, idx) => {
      html += '<div class="project-item" onclick="filterProject(' + idx + ', this)" '
        + 'ondblclick="newChatInProject(' + idx + ')" '
        + 'title="' + esc(p.short) + ' — double-click for new chat here">'
        + esc(p.short) + '</div>';
    });
    bar.innerHTML = html;
  } catch(e) {
    console.error('loadProjects:', e);
  }
}

async function newChatInProject(idx) {
  const p = projectChips[idx];
  if (!p) return;
  // Filter first so the sidebar reflects where we're working
  const el = document.querySelectorAll('.project-sidebar .project-item')[idx + 1];
  filterProject(idx, el);
  // Snap the cwd for this project then kick off inline new chat
  currentProjectCwd = p.cwd || null;
  newChatInline();
}

function filterProject(idx, el) {
  document.querySelectorAll('.project-sidebar .project-item').forEach(c => c.classList.remove('active'));
  if (el) el.classList.add('active');
  else document.querySelector('.project-sidebar .project-item').classList.add('active');
  if (idx === -1 || idx == null) {
    currentProjectCwd = null;
    loadSessions(null);
  } else {
    const p = projectChips[idx];
    currentProjectCwd = p.cwd || null;
    loadSessions(p.project_dirs || [p.project_dir]);
  }
}

function updateChatCount() {
  document.getElementById('chatCount').textContent = '(' + allSessions.length + ')';
}

// ═══════════════════════════════════════════════════════════════
// CHATS: Rendering
// ═══════════════════════════════════════════════════════════════
function renderSessionList() {
  const el = document.getElementById('sessionList');
  if (!sessions.length) {
    el.innerHTML = '<div class="empty-text">No conversations found</div>';
    focusedIndex = -1;
    focusedSid = null;
    return;
  }

  const active = _activeTerms.size
    ? sessions.filter(s => _activeTerms.has(s.session_id))
    : [];
  const activeSet = new Set(active.map(s => s.session_id));

  // Done chats — hidden from Active/Starred/time groups, rendered at bottom.
  const doneList = sessions.filter(s => s.is_done && !activeSet.has(s.session_id));
  const doneSet = new Set(doneList.map(s => s.session_id));

  const remaining = sessions.filter(s => !activeSet.has(s.session_id) && !doneSet.has(s.session_id));
  const starred = remaining.filter(s => s.starred);
  const unstarred = remaining.filter(s => !s.starred);
  const rendered = [];

  let html = '';

  if (active.length) {
    html += '<div class="group-header active-header">\u25CF Active Terminals</div>';
    for (const s of active) {
      html += renderSessionRow(s, rendered.length);
      rendered.push(s);
    }
  }

  if (starred.length) {
    html += '<div class="group-header starred-header">\u2605 Starred</div>';
    for (const s of starred) {
      html += renderSessionRow(s, rendered.length);
      rendered.push(s);
    }
  }

  let group = null;
  for (const s of unstarred) {
    const g = timeGroup(s.last_timestamp || s.first_timestamp);
    if (g !== group) {
      group = g;
      html += '<div class="group-header">' + esc(g) + '</div>';
    }
    html += renderSessionRow(s, rendered.length);
    rendered.push(s);
  }

  if (doneList.length) {
    const chev = _doneCollapsed ? '▸' : '▾';
    html += '<div class="group-header done-header" onclick="toggleDoneCollapsed()">'
      + chev + ' ✓ Done (' + doneList.length + ')</div>';
    // Always render the rows so they stay in `sessions`; hide via CSS when collapsed.
    html += '<div class="done-section' + (_doneCollapsed ? ' collapsed' : '') + '">';
    for (const s of doneList) {
      html += renderSessionRow(s, rendered.length);
      rendered.push(s);
    }
    html += '</div>';
  }

  sessions = rendered;
  el.innerHTML = html;

  // Re-attach focus highlight by sid — not by numeric index. Auto-poll
  // reshuffles the list and index N would otherwise point at a random chat.
  if (focusedSid) {
    focusedIndex = sessions.findIndex(s => s.session_id === focusedSid);
    if (focusedIndex < 0) focusedSid = null;
  } else {
    focusedIndex = -1;
  }

  if (focusedIndex >= 0) {
    const rows = el.querySelectorAll('.session-row');
    if (rows[focusedIndex]) rows[focusedIndex].classList.add('focused');
  }
}

let _doneCollapsed = true;
function toggleDoneCollapsed() {
  _doneCollapsed = !_doneCollapsed;
  renderSessionList();
}

// Bootstrap Icons (MIT) — inline SVG marks for Claude (Anthropic sparkle)
// and Codex (OpenAI flower). Currentcolor lets CSS pick the brand tint.
const _CLAUDE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor"><path d="m3.127 10.604 3.135-1.76.053-.153-.053-.085H6.11l-.525-.032-1.791-.048-1.554-.065-1.505-.08-.38-.081L0 7.832l.036-.234.32-.214.455.04 1.009.069 1.513.105 1.097.064 1.626.17h.259l.036-.105-.089-.065-.068-.064-1.566-1.062-1.695-1.121-.887-.646-.48-.327-.243-.306-.104-.67.435-.48.585.04.15.04.593.456 1.267.981 1.654 1.218.242.202.097-.068.012-.049-.109-.181-.9-1.626-.96-1.655-.428-.686-.113-.411a2 2 0 0 1-.068-.484l.496-.674L4.446 0l.662.089.279.242.411.94.666 1.48 1.033 2.014.302.597.162.553.06.17h.105v-.097l.085-1.134.157-1.392.154-1.792.052-.504.25-.605.497-.327.387.186.319.456-.045.294-.19 1.23-.37 1.93-.243 1.29h.142l.161-.16.654-.868 1.097-1.372.484-.545.565-.601.363-.287h.686l.505.751-.226.775-.707.895-.585.759-.839 1.13-.524.904.048.072.125-.012 1.897-.403 1.024-.186 1.223-.21.553.258.06.263-.218.536-1.307.323-1.533.307-2.284.54-.028.02.032.04 1.029.098.44.024h1.077l2.005.15.525.346.315.424-.053.323-.807.411-3.631-.863-.872-.218h-.12v.073l.726.71 1.331 1.202 1.667 1.55.084.383-.214.302-.226-.032-1.464-1.101-.565-.497-1.28-1.077h-.084v.113l.295.432 1.557 2.34.08.718-.112.234-.404.141-.444-.08-.911-1.28-.94-1.44-.759-1.291-.093.053-.448 4.821-.21.246-.484.186-.403-.307-.214-.496.214-.98.258-1.28.21-1.016.19-1.263.112-.42-.008-.028-.092.012-.953 1.307-1.448 1.957-1.146 1.227-.274.109-.477-.247.045-.44.266-.39 1.586-2.018.956-1.25.617-.723-.004-.105h-.036l-4.212 2.736-.75.096-.324-.302.04-.496.154-.162 1.267-.871z"/></svg>';
const _CODEX_SVG  = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor"><path d="M14.949 6.547a3.94 3.94 0 0 0-.348-3.273 4.11 4.11 0 0 0-4.4-1.934 A4.1 4.1 0 0 0 8.423.2 4.15 4.15 0 0 0 6.305.086a4.1 4.1 0 0 0-1.891.948 4.04 4.04 0 0 0-1.158 1.753 4.1 4.1 0 0 0-1.563.679A4 4 0 0 0 .554 4.72a3.99 3.99 0 0 0 .502 4.731 3.94 3.94 0 0 0 .346 3.274 4.11 4.11 0 0 0 4.402 1.933c.382.425.852.764 1.377.995.526.231 1.095.35 1.67.346 1.78.002 3.358-1.132 3.901-2.804a4.1 4.1 0 0 0 1.563-.68 4 4 0 0 0 1.14-1.253 3.99 3.99 0 0 0-.506-4.716m-6.097 8.406a3.05 3.05 0 0 1-1.945-.694l.096-.054 3.23-1.838a.53.53 0 0 0 .265-.455v-4.49l1.366.778q.02.011.025.035v3.722c-.003 1.653-1.361 2.992-3.037 2.996m-6.53-2.75a2.95 2.95 0 0 1-.36-2.01l.095.057L5.29 12.09a.53.53 0 0 0 .527 0l3.949-2.246v1.555a.05.05 0 0 1-.022.041L6.473 13.3c-1.454.826-3.311.335-4.15-1.098m-.85-6.94A3.02 3.02 0 0 1 3.07 3.949v3.785a.51.51 0 0 0 .262.451l3.93 2.237-1.366.779a.05.05 0 0 1-.048 0L2.585 9.342a2.98 2.98 0 0 1-1.113-4.094zm11.216 2.571L8.747 5.576l1.362-.776a.05.05 0 0 1 .048 0l3.265 1.86a3 3 0 0 1 1.173 1.207 2.96 2.96 0 0 1-.27 3.2 3.05 3.05 0 0 1-1.36.997V8.279a.52.52 0 0 0-.276-.445m1.36-2.015-.097-.057-3.226-1.855a.53.53 0 0 0-.53 0L6.249 6.153V4.598a.04.04 0 0 1 .019-.04L9.533 2.7a3.07 3.07 0 0 1 3.257.139c.474.325.843.778 1.066 1.303.223.526.289 1.103.191 1.664zM5.503 8.575 4.139 7.8a.05.05 0 0 1-.026-.037V4.049c0-.57.166-1.127.476-1.607s.752-.864 1.275-1.105a3.08 3.08 0 0 1 3.234.41l-.096.054-3.23 1.838a.53.53 0 0 0-.265.455zm.742-1.577 1.758-1 1.762 1v2l-1.755 1-1.762-1z"/></svg>';

function _agentBadge(agent) {
  if (agent === 'codex') return '<span class="agent-icon codex" title="Codex">' + _CODEX_SVG + '</span>';
  return '<span class="agent-icon claude" title="Claude">' + _CLAUDE_SVG + '</span>';
}

function renderSessionRow(s, idx) {
  const isFocused = idx === focusedIndex;
  const isSelected = selectedIds.has(s.session_id);
  const isActive = _activeTerms.has(s.session_id);
  const isDone = !!s.is_done;
  let cls = 'session-row';
  if (isFocused) cls += ' focused';
  if (isSelected) cls += ' selected';
  if (isActive) cls += ' active-terminal';
  if (isDone && !isActive) cls += ' done';

  const tokens = totalTokens(s);
  const starCls = s.starred ? 'session-star starred' : 'session-star';
  const starChar = s.starred ? '\u2605' : '\u2606';
  const liveIndicator = isActive
    ? '<span class="live-indicator">'
      + '<span class="live-dot" title="Terminal running"></span>'
      + '<span class="term-close" title="Close terminal (Alt+W)" '
      + 'onclick="event.stopPropagation();closeActiveTerminal(\'' + s.session_id + '\')">\u2715</span>'
      + '</span>'
    : '';

  return '<div class="' + cls + '" data-idx="' + idx + '" data-sid="' + s.session_id + '" '
    + 'onclick="onRowClick(event,' + idx + ')" ondblclick="openConv(\'' + s.session_id + '\')">'
    + '<span class="' + starCls + '" onclick="event.stopPropagation();toggleStar(\'' + s.session_id + '\')">' + starChar + '</span>'
    + '<span class="session-title">' + liveIndicator + _agentBadge(s.agent) + esc(s.display_title || 'Untitled') + '</span>'
    + '<span class="session-project">' + esc(s.project_short || '') + '</span>'
    + '<span class="session-tokens">' + formatTokens(tokens) + '</span>'
    + '<span class="session-date-created" title="Created">' + formatDate(s.first_timestamp) + '</span>'
    + '<span class="session-date" title="Last activity">' + formatDate(s.last_timestamp || s.first_timestamp) + '</span>'
    + '</div>';
}

// ═══════════════════════════════════════════════════════════════
// CHATS: Focus & Selection
// ═══════════════════════════════════════════════════════════════
function setFocus(idx, scroll) {
  if (idx < 0 || idx >= sessions.length) return;
  focusedIndex = idx;
  focusedSid = sessions[idx] ? sessions[idx].session_id : null;
  const rows = document.querySelectorAll('#sessionList .session-row');
  rows.forEach(r => r.classList.remove('focused'));
  if (rows[idx]) {
    rows[idx].classList.add('focused');
    if (scroll !== false) rows[idx].scrollIntoView({ block: 'nearest' });
  }
}

function onRowClick(e, idx) {
  if (e.ctrlKey || e.metaKey) {
    // Toggle selection
    const sid = sessions[idx].session_id;
    if (selectedIds.has(sid)) selectedIds.delete(sid);
    else selectedIds.add(sid);
    setFocus(idx, false);
    renderSessionList();
  } else if (e.shiftKey && focusedIndex >= 0) {
    // Range select
    const lo = Math.min(focusedIndex, idx);
    const hi = Math.max(focusedIndex, idx);
    for (let i = lo; i <= hi; i++) {
      selectedIds.add(sessions[i].session_id);
    }
    setFocus(idx, false);
    renderSessionList();
  } else {
    // Single click: focus + open
    selectedIds.clear();
    setFocus(idx, false);
    openConv(sessions[idx].session_id);
  }
  updateSelectionInfo();
}

function updateSelectionInfo() {
  const el = document.getElementById('selectionInfo');
  if (selectedIds.size > 0) {
    el.classList.remove('hidden');
    document.getElementById('selectionText').textContent = selectedIds.size + ' selected';
  } else {
    el.classList.add('hidden');
  }
}

// ═══════════════════════════════════════════════════════════════
// CHATS: Conversation
// ═══════════════════════════════════════════════════════════════
async function openConv(sid) {
  const switching = currentSessionId !== sid;
  if (switching) {
    // GTK: Python keeps every VTE alive in a stack — code-on swaps the visible child.
    // Web: termSessions Map keeps every xterm + WebSocket alive — switch panes only.
    convMode = 'live';
    document.getElementById('viewReadBtn').classList.remove('active');
    document.getElementById('viewLiveBtn').classList.add('active');
    document.getElementById('convBody').classList.add('hidden');
    document.getElementById('convTerminal').classList.remove('hidden');
  }
  currentSessionId = sid;
  _convLoaded.delete(sid);  // invalidate so Read view re-fires if user switches to it
  document.getElementById('convEmpty').classList.add('hidden');
  document.getElementById('convContent').classList.remove('hidden');

  // Fire Code immediately — don't wait for any fetch
  if (switching && convMode === 'live') {
    if (window.__gtkBridge) startGtkCode(sid);
    else startLiveTerminal(sid);
  }

  // Pull session metadata from the already-loaded sessions array; no network hop.
  const local = sessions.find(s => s.session_id === sid);
  if (local) {
    document.getElementById('convTitle').textContent = local.display_title || 'Untitled';
    const tokens = (local.input_tokens || 0) + (local.output_tokens || 0) +
                   (local.cache_read_tokens || 0) + (local.cache_create_tokens || 0);
    document.getElementById('convMeta').textContent =
      (local.created_at || local.last_timestamp || '') + '  \u00b7  ' +
      formatTokens(tokens) + ' tokens' +
      (local.cwd ? '  \u00b7  ' + local.cwd : '');
  }
  document.getElementById('convBody').innerHTML = '';  // placeholder, loaded on Read

  // Focus the row
  const idx = sessions.findIndex(s => s.session_id === sid);
  if (idx >= 0) setFocus(idx, true);

  // If user is in Read mode on this open, lazily fetch the transcript now.
  if (convMode === 'read') loadReadTranscript(sid);
  if (_filesVisible) loadFiles(sid);
}

function closeConv() {
  // Hide the panel; keep terminals alive so re-opening is instant.
  // Use Alt+w (close-terminal) or app-close to actually kill a session.
  if (window.__gtkBridge) stopGtkCode();
  convMode = 'live';
  document.getElementById('viewReadBtn').classList.remove('active');
  document.getElementById('viewLiveBtn').classList.add('active');
  document.getElementById('convBody').classList.add('hidden');
  document.getElementById('convTerminal').classList.remove('hidden');
  currentSessionId = null;
  _hideAllTermPanes();
  document.getElementById('convEmpty').classList.remove('hidden');
  document.getElementById('convContent').classList.add('hidden');
}

// ═══════════════════════════════════════════════════════════════
// CHATS: Actions
// ═══════════════════════════════════════════════════════════════
async function toggleStar(sid) {
  try {
    await fetch('/api/star/' + sid, { method: 'POST' });
    await loadSessions(currentProject);
  } catch(e) {}
}

async function deleteSession(sid) {
  const ok = await showConfirm({
    title: 'Delete conversation?',
    body: 'This cannot be undone.',
    confirm: 'Delete',
    danger: true,
  });
  if (!ok) return;
  try {
    await fetch('/api/session/' + sid, { method: 'DELETE' });
    if (currentSessionId === sid) closeConv();
    await loadSessions(currentProject);
  } catch(e) {}
}

async function bulkDelete() {
  if (selectedIds.size === 0) return;
  const n = selectedIds.size;
  const ok = await showConfirm({
    title: 'Delete ' + n + ' conversation' + (n === 1 ? '' : 's') + '?',
    body: 'This cannot be undone.',
    confirm: 'Delete',
    danger: true,
  });
  if (!ok) return;
  try {
    await fetch('/api/sessions/bulk-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: Array.from(selectedIds) }),
    });
    if (selectedIds.has(currentSessionId)) closeConv();
    selectedIds.clear();
    updateSelectionInfo();
    await loadSessions(currentProject);
  } catch(e) {}
}

async function renameSession(sid) {
  const s = sessions.find(s => s.session_id === sid);
  const current = s ? (s.display_title || '') : '';
  const result = await showPrompt({
    title: 'Rename conversation',
    body: '',
    placeholder: 'New title',
    defaultValue: current,
    confirm: 'Rename',
  });
  if (result === null) return;
  const title = result.trim();
  if (!title) return;

  // Pseudo sessions have no DB row yet — persist the rename locally and
  // apply it once claude has written the real session file.
  if (_isPseudoSid(sid)) {
    const pseudo = _pseudoSessions.find(p => p.session_id === sid);
    if (pseudo) {
      pseudo.display_title = title;
      pseudo.pending_rename_title = title;
      if (s) s.display_title = title;
      if (currentSessionId === sid) {
        document.getElementById('convTitle').textContent = title;
      }
      renderSessionList();
      showToast('Renamed (will save when chat is written to disk)', { variant: 'success' });
      _startPseudoReconciler();
    }
    return;
  }
  const toast = showToast('Renaming chat…', { spinner: true, sticky: true });
  try {
    const r = await fetch('/api/rename/' + sid, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title.trim() }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await loadSessions(currentProject);
    if (currentSessionId === sid) {
      document.getElementById('convTitle').textContent = title.trim();
    }
    toast.update('Renamed', 'success');
  } catch(e) {
    toast.update('Rename failed', 'error');
  }
}

async function retitleSession(sid) {
  const toast = showToast('Generating AI title…', { spinner: true, sticky: true });
  try {
    const r = await fetch('/api/retitle/' + sid, { method: 'POST' });
    const data = await r.json();
    if (!r.ok || !data.title) throw new Error(data.error || 'No title returned');
    await loadSessions(currentProject);
    if (currentSessionId === sid) {
      document.getElementById('convTitle').textContent = data.title;
    }
    toast.update('Retitled: ' + data.title, 'success');
  } catch(e) {
    toast.update('Retitle failed', 'error');
  }
}

async function bulkRetitle() {
  const count = selectedIds.size;
  if (count === 0) return;
  const noun = count === 1 ? 'chat' : 'chats';
  const toast = showToast('Retitling ' + count + ' ' + noun + '…', { spinner: true, sticky: true });
  try {
    const r = await fetch('/api/retitle-bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: Array.from(selectedIds) }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const done = (data && typeof data.count === 'number') ? data.count : count;
    selectedIds.clear();
    updateSelectionInfo();
    await loadSessions(currentProject);
    toast.update('Retitled ' + done + ' ' + (done === 1 ? 'chat' : 'chats'), 'success');
  } catch(e) {
    toast.update('Bulk retitle failed', 'error');
  }
}

async function resumeSession(sid) {
  try {
    const r = await fetch('/api/resume/' + sid, { method: 'POST' });
    const data = await r.json();
  } catch(e) {}
}


async function toggleDone(sid) {
  try {
    const r = await fetch('/api/done/' + sid, { method: 'POST' });
    const data = await r.json();
    const s = sessions.find(x => x.session_id === sid);
    if (s) s.is_done = data.done ? 1 : 0;
    // Marking done = wrapped up. If there's a running terminal, close it so the
    // row actually leaves the Active section. Also auto-expand Done so you see
    // where it went.
    if (data.done) {
      if (_activeTerms.has(sid)) closeActiveTerminal(sid);
      _doneCollapsed = false;
    }
    renderSessionList();
    showToast(data.done ? 'Marked as done' : 'Back to active', { variant: 'success' });
  } catch(e) {
    showToast('Failed to toggle done', { variant: 'error' });
  }
}

async function bulkToggleDone() {
  const ids = Array.from(selectedIds);
  if (!ids.length) return;
  // Majority-mark: if most are not-done, mark all done; else unmark all.
  const notDoneCount = ids.filter(id => {
    const s = sessions.find(x => x.session_id === id);
    return s && !s.is_done;
  }).length;
  const markDone = notDoneCount >= ids.length / 2;
  const toast = showToast(
    (markDone ? 'Marking ' : 'Reactivating ') + ids.length + ' chats…',
    { spinner: true, sticky: true }
  );
  try {
    const r = await fetch('/api/bulk-done', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, done: markDone }),
    });
    const data = await r.json();
    if (markDone) {
      // Close any active terminals among the marked set
      for (const id of ids) if (_activeTerms.has(id)) closeActiveTerminal(id);
      _doneCollapsed = false;
    }
    selectedIds.clear();
    updateSelectionInfo();
    await loadSessions(currentProject);
    toast.update((markDone ? 'Marked ' : 'Reactivated ') + data.count + ' chats', 'success');
  } catch(e) {
    toast.update('Bulk update failed', 'error');
  }
}

// ═══════════════════════════════════════════════════════════════
// FILES PANE (git-tracked tree, Alt+B to toggle)
// ═══════════════════════════════════════════════════════════════
let _filesVisible = true;
let _filesData = null;
let _filesSid = null;
const _filesExpanded = new Set();

function escAttr(s) { return String(s).replace(/'/g, '&#39;').replace(/"/g, '&quot;'); }

async function loadFiles(sid) {
  const local = sessions.find(s => s.session_id === sid);
  const cwd = (local && local.cwd) || '';
  if (!cwd) {
    document.getElementById('filesTree').innerHTML = '<div class="empty-text">No cwd for this chat</div>';
    return;
  }
  _filesSid = sid;
  document.getElementById('filesTree').innerHTML = '<div class="empty-text">Loading…</div>';
  try {
    const r = await fetch('/api/files?cwd=' + encodeURIComponent(cwd));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _filesData = await r.json();
    renderFilesTree();
  } catch(e) {
    document.getElementById('filesTree').innerHTML = '<div class="empty-text">Failed to load</div>';
  }
}

function renderFilesTree() {
  if (!_filesData) return;
  document.getElementById('filesRootName').textContent = _filesData.root_name || '';
  const tree = _filesData.tree;
  let html = '';
  for (const c of (tree.children || [])) html += renderFNode(c, 0);
  document.getElementById('filesTree').innerHTML = html || '<div class="empty-text">No tracked files</div>';
}

function renderFNode(node, depth) {
  const pad = depth * 12 + 8;
  if (node.type === 'file') {
    const abs = _filesData.root_path + '/' + node.path;
    return '<div class="fnode file" draggable="true" '
      + 'data-abs="' + escAttr(abs) + '" '
      + 'onclick="insertFilePath(\'' + escAttr(abs) + '\')" '
      + 'style="padding-left:' + pad + 'px" '
      + 'title="Click to insert · Drag into terminal">'
      + esc(node.name) + '</div>';
  }
  const expanded = _filesExpanded.has(node.path);
  let html = '<div class="fnode folder" '
    + 'onclick="toggleFolder(\'' + escAttr(node.path) + '\')" '
    + 'style="padding-left:' + pad + 'px">'
    + (expanded ? '▾ ' : '▸ ') + esc(node.name) + '</div>';
  if (expanded && node.children) {
    for (const c of node.children) html += renderFNode(c, depth + 1);
  }
  return html;
}

function toggleFolder(path) {
  if (_filesExpanded.has(path)) _filesExpanded.delete(path);
  else _filesExpanded.add(path);
  renderFilesTree();
}

function toggleFilesPane() {
  _filesVisible = !_filesVisible;
  document.getElementById('filesPane').classList.toggle('hidden', !_filesVisible);
  if (_filesVisible) {
    if (currentSessionId && _filesSid !== currentSessionId) loadFiles(currentSessionId);
  }
  // Terminal rect changed — nudge Python to re-lay-out the VTE overlay
  if (window.__gtkBridge && convMode === 'live') {
    requestAnimationFrame(() => {
      const rect = _gtkGetRect();
      if (rect) window.gtkSend({ type: 'code-rect', rect });
    });
  }
}

function insertFilePath(absPath) {
  // Click-to-insert — quoted path fed into the active VTE via the bridge.
  if (!window.__gtkBridge || !currentSessionId) return;
  window.gtkSend({ type: 'feed-text', sid: currentSessionId,
                   text: "'" + String(absPath).replace(/'/g, "'\\''") + "' " });
}

// Drag from files → VTE drop handler eats the URI list
document.addEventListener('dragstart', (e) => {
  const t = e.target && e.target.closest && e.target.closest('.fnode.file');
  if (!t || !e.dataTransfer) return;
  const abs = t.dataset.abs;
  if (!abs) return;
  const uri = 'file://' + abs.split('/').map(p => p ? encodeURIComponent(p) : '').join('/');
  e.dataTransfer.setData('text/uri-list', uri);
  e.dataTransfer.effectAllowed = 'copy';
});

// ═══════════════════════════════════════════════════════════════
// INLINE TERMINAL (Live view — PTY over WebSocket)
// ═══════════════════════════════════════════════════════════════

let convMode = 'live';            // 'read' | 'live' (Code tab)
const termSessions = new Map();   // sid -> { term, fit, ws, tid, mount }
let activeTermSid = null;         // sid of the currently visible terminal pane (or null)
let _termResizeObs = null;        // single observer on #termMounts container
const _convLoaded = new Set();    // sids whose transcript is already in the DOM
const _activeTerms = new Set();   // sids with a running terminal — cleared on app close
const _activeMeta = new Map();    // sid -> { cwd, activatedAt } for /clear migration
const _pseudoSessions = [];       // synthetic rows for brand-new chats (temp ids)

function _isPseudoSid(sid) { return typeof sid === 'string' && sid.startsWith('new-'); }

// Periodic poll that watches for real session files to appear for any open
// pseudo. When a match lands, we apply any pending rename, move active-terminal
// tracking onto the real sid, and drop the pseudo row.
let _pseudoTimer = null;
function _startPseudoReconciler() {
  if (_pseudoTimer) return;
  _pseudoTimer = setInterval(async () => {
    if (_pseudoSessions.length === 0) {
      clearInterval(_pseudoTimer);
      _pseudoTimer = null;
      return;
    }
    try {
      const params = new URLSearchParams();
      if (currentProject && currentProject.length) params.set('projects', currentProject.join(','));
      const r = await fetch('/api/sessions?' + params);
      const fresh = await r.json();
      await _reconcilePseudos(fresh);
    } catch(e) { /* ignore, will retry */ }
  }, 3000);
}

// Periodic session-list refresh while any terminal is live. Picks up new
// sessions claude writes after /clear, compact, or starting fresh within a PTY.
let _activeRefreshTimer = null;
function _ensureActiveRefresh() {
  if (_activeRefreshTimer) return;
  _activeRefreshTimer = setInterval(async () => {
    if (_activeTerms.size === 0) {
      clearInterval(_activeRefreshTimer);
      _activeRefreshTimer = null;
      return;
    }
    if (currentTab !== 'chats') return;
    try { await loadSessions(currentProject, { refresh: true }); } catch(e) {}
  }, 5000);
}

function _migrateActiveOnClear(freshSessions) {
  // Pseudos get first dibs on any new session in their cwd — otherwise the
  // /clear-migration logic here steals the pseudo's real session and merges it
  // into a pre-existing active terminal. Compute which sessions the pseudos
  // will claim and exclude them from the migration pool.
  const claimedByPseudo = new Set();
  for (const pseudo of _pseudoSessions) {
    if (!pseudo.cwd) continue;
    const pCands = freshSessions.filter(s =>
      s.cwd === pseudo.cwd &&
      s.first_timestamp &&
      s.first_timestamp >= pseudo.first_timestamp
    );
    if (!pCands.length) continue;
    pCands.sort((a, b) => (a.first_timestamp < b.first_timestamp ? -1 : 1));
    claimedByPseudo.add(pCands[0].session_id);
  }

  for (const [oldSid, meta] of [..._activeMeta]) {
    if (_isPseudoSid(oldSid)) continue;
    if (!meta.cwd) continue;
    const candidates = freshSessions.filter(s =>
      !claimedByPseudo.has(s.session_id) &&
      s.session_id !== oldSid &&
      s.cwd === meta.cwd &&
      s.first_timestamp &&
      s.first_timestamp > meta.activatedAt
    );
    if (!candidates.length) continue;
    candidates.sort((a, b) => (a.first_timestamp < b.first_timestamp ? -1 : 1));
    const target = candidates[0];

    // Migrate client-side tracking
    _activeTerms.delete(oldSid);
    _activeMeta.delete(oldSid);
    _activeTerms.add(target.session_id);
    _activeMeta.set(target.session_id, { cwd: meta.cwd, activatedAt: target.first_timestamp });

    // Push the rename to Python so the VTE stack child gets re-keyed
    if (window.__gtkBridge) {
      window.gtkSend({ type: 'code-migrate-sid', old: oldSid, new: target.session_id });
    }

    // If the user was viewing the old sid, point them at the new one
    if (currentSessionId === oldSid) {
      currentSessionId = target.session_id;
      const titleEl = document.getElementById('convTitle');
      if (titleEl) titleEl.textContent = target.display_title || 'Untitled';
    }
    if (focusedSid === oldSid) {
      focusedSid = target.session_id;
    }
  }
}

async function _reconcilePseudos(fresh) {
  let changed = false;
  for (const pseudo of [..._pseudoSessions]) {
    // Match heuristic: same cwd + real session started at or after the pseudo was
    // created. Pick the newest candidate to avoid stealing an older session's id.
    const candidates = fresh.filter(s =>
      s.cwd === pseudo.cwd &&
      s.first_timestamp &&
      s.first_timestamp >= pseudo.first_timestamp
    );
    if (!candidates.length) continue;
    candidates.sort((a, b) => (a.first_timestamp < b.first_timestamp ? -1 : 1));
    const match = candidates[0];

    // Apply pending rename to the real session
    if (pseudo.pending_rename_title) {
      try {
        await fetch('/api/rename/' + match.session_id, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: pseudo.pending_rename_title }),
        });
        match.display_title = pseudo.pending_rename_title;
        match.custom_title = pseudo.pending_rename_title;
      } catch(e) {}
    }

    // Transfer active-terminal tracking from pseudo sid to real sid, AND tell
    // Python to rename the VTE's stack key so subsequent clicks on the real sid
    // find the existing terminal instead of spawning a second claude on the
    // same session file (which was the "merged with existing chat" bug).
    if (_activeTerms.has(pseudo.session_id)) {
      _activeTerms.delete(pseudo.session_id);
      _activeTerms.add(match.session_id);
      const old_meta = _activeMeta.get(pseudo.session_id);
      if (old_meta) {
        _activeMeta.delete(pseudo.session_id);
        _activeMeta.set(match.session_id, old_meta);
      }
      if (window.__gtkBridge) {
        window.gtkSend({
          type: 'code-migrate-sid',
          old: pseudo.session_id,
          new: match.session_id,
        });
      }
    }
    if (currentSessionId === pseudo.session_id) {
      currentSessionId = match.session_id;
    }
    if (focusedSid === pseudo.session_id) {
      focusedSid = match.session_id;
    }

    // Drop the pseudo
    const pi = _pseudoSessions.findIndex(p => p.session_id === pseudo.session_id);
    if (pi >= 0) _pseudoSessions.splice(pi, 1);
    changed = true;
  }
  if (changed) {
    await loadSessions(currentProject);
  }
}

function _markActive(sid) {
  if (!sid) return;
  if (_activeTerms.has(sid)) return;
  _activeTerms.add(sid);
  const local = sessions.find(s => s.session_id === sid);
  _activeMeta.set(sid, {
    cwd: (local && local.cwd) || '',
    activatedAt: new Date().toISOString(),
  });
  renderSessionList();
  _ensureActiveRefresh();
}

function _unmarkActive(sid) {
  if (!sid) return;
  _activeMeta.delete(sid);
  const had = _activeTerms.delete(sid);
  // Prune any pseudo row that matches — temp sessions don't survive a close
  if (_isPseudoSid(sid)) {
    const pi = _pseudoSessions.findIndex(p => p.session_id === sid);
    if (pi >= 0) _pseudoSessions.splice(pi, 1);
    const si = sessions.findIndex(s => s.session_id === sid);
    if (si >= 0) sessions.splice(si, 1);
  }
  if (had || _isPseudoSid(sid)) renderSessionList();
}

async function closeActiveTerminal(sid) {
  if (!sid || !_activeTerms.has(sid)) return;
  if (window.__gtkBridge) {
    // Python's _kill_session SIGTERMs claude and fires onGtkCodeExit(sid),
    // which calls _unmarkActive and re-renders the sidebar.
    window.gtkSend({ type: 'code-close', sid });
  } else {
    // Browser mode — termSessions tracks every live terminal, kill the one matching sid.
    if (termSessions.has(sid)) {
      teardownLiveTerminal(sid);
    } else {
      _unmarkActive(sid);
    }
  }
  if (sid === currentSessionId && convMode === 'live') {
    setTermStatus('Session closed.', 'error');
  }
}

async function loadReadTranscript(sid) {
  if (_convLoaded.has(sid)) return;
  if (_isPseudoSid(sid)) {
    document.getElementById('convBody').innerHTML =
      '<div class="empty-text">New chat — no transcript yet.</div>';
    return;
  }
  document.getElementById('convBody').innerHTML = '<div class="loading-text">Loading...</div>';
  try {
    const r = await fetch('/api/conversation/' + sid);
    const data = await r.json();
    if (currentSessionId !== sid) return;  // user switched away while we loaded

    document.getElementById('convTitle').textContent = data.title || 'Untitled';
    const tokens = (data.input_tokens || 0) + (data.output_tokens || 0) +
                   (data.cache_read_tokens || 0) + (data.cache_create_tokens || 0);
    document.getElementById('convMeta').textContent =
      (data.date || '') + '  \u00b7  ' + formatTokens(tokens) + ' tokens' +
      (data.cwd ? '  \u00b7  ' + data.cwd : '');

    let html = '';
    for (const m of data.messages) {
      if (m.tool_name) {
        html += '<div class="msg"><div class="msg-tool">\u2699 ' + esc(m.tool_name);
        if (m.tool_input) html += ': ' + esc(m.tool_input.substring(0, 200));
        html += '</div></div>';
      } else if (m.role === 'tool_result') {
        html += '<div class="msg"><div class="msg-tool-output">' + esc(m.text) + '</div></div>';
      } else {
        const roleLabel = m.role === 'user' ? 'You' : 'Claude';
        html += '<div class="msg">'
          + '<div class="msg-role ' + m.role + '">' + roleLabel + '</div>'
          + '<div class="msg-body">' + esc(m.text) + '</div>'
          + '</div>';
      }
    }
    document.getElementById('convBody').innerHTML = html || '<div class="empty-text">No messages</div>';
    _convLoaded.add(sid);
  } catch(e) {
    document.getElementById('convBody').innerHTML = '<div class="empty-text">Error loading conversation</div>';
  }
}

function setConvMode(mode) {
  if (mode === convMode) return;
  convMode = mode;
  document.getElementById('viewReadBtn').classList.toggle('active', mode === 'read');
  document.getElementById('viewLiveBtn').classList.toggle('active', mode === 'live');
  document.getElementById('convBody').classList.toggle('hidden', mode !== 'read');
  document.getElementById('convTerminal').classList.toggle('hidden', mode !== 'live');
  if (mode === 'live') {
    if (!currentSessionId) {
      setTermStatus('Select a conversation first.', 'error');
      return;
    }
    if (window.__gtkBridge) {
      startGtkCode(currentSessionId);
    } else if (termSessions.has(currentSessionId)) {
      _activateTermPane(currentSessionId);
    } else {
      startLiveTerminal(currentSessionId);
    }
  } else {
    if (window.__gtkBridge) stopGtkCode();
    else _hideAllTermPanes();
    if (currentSessionId) loadReadTranscript(currentSessionId);
  }
}

function setTermStatus(text, cls) {
  const el = document.getElementById('termStatus');
  el.textContent = text;
  el.classList.remove('error', 'live');
  if (cls) el.classList.add(cls);
}

function _sendResizeForSid(sid) {
  const s = termSessions.get(sid);
  if (!s || !s.ws || s.ws.readyState !== 1) return;
  try { s.ws.send(JSON.stringify({ resize: { rows: s.term.rows, cols: s.term.cols } })); } catch(e) {}
}

function sendResize() {
  if (activeTermSid) _sendResizeForSid(activeTermSid);
}

function _hideAllTermPanes() {
  const container = document.getElementById('termMounts');
  if (!container) return;
  for (const child of container.children) child.classList.add('hidden');
  activeTermSid = null;
}

function _activateTermPane(sid) {
  const container = document.getElementById('termMounts');
  if (!container) return;
  for (const child of container.children) {
    child.classList.toggle('hidden', child.dataset.sid !== sid);
  }
  activeTermSid = sid;
  const s = termSessions.get(sid);
  if (!s) return;
  // Status reflects the now-visible session
  if (s.ws && s.ws.readyState === 1) {
    setTermStatus('● live · cwd: ' + (s.cwd || '(unknown)'), 'live');
  } else if (s.ws && s.ws.readyState === 0) {
    setTermStatus('Connecting…');
  } else {
    setTermStatus('Disconnected.', 'error');
  }
  requestAnimationFrame(() => {
    try { s.fit.fit(); _sendResizeForSid(sid); s.term.focus(); } catch(e) {}
  });
}

function _ensureTermResizeObserver() {
  if (_termResizeObs) return;
  const container = document.getElementById('termMounts');
  if (!container) return;
  _termResizeObs = new ResizeObserver(() => {
    if (!activeTermSid) return;
    const s = termSessions.get(activeTermSid);
    if (!s) return;
    try { s.fit.fit(); _sendResizeForSid(activeTermSid); } catch(e) {}
  });
  _termResizeObs.observe(container);
}

async function startLiveTerminal(sid) {
  // Already alive? Just bring its pane to front.
  if (termSessions.has(sid)) {
    _activateTermPane(sid);
    return;
  }

  setTermStatus('Starting claude --resume ' + sid.slice(0, 8) + '…');

  const container = document.getElementById('termMounts');
  const mount = document.createElement('div');
  mount.className = 'term-pane';
  mount.dataset.sid = sid;
  container.appendChild(mount);
  // Hide the others, show this fresh one immediately
  for (const child of container.children) {
    if (child !== mount) child.classList.add('hidden');
  }
  activeTermSid = sid;

  const term = new Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: 13,
    lineHeight: 1.15,
    theme: {
      background: '#000000',
      foreground: '#c9d1d9',
      cursor: '#3fb950',
      cursorAccent: '#000000',
      selectionBackground: 'rgba(63,185,80,0.35)',
    },
    scrollback: 5000,
    allowProposedApi: true,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  try { term.loadAddon(new WebLinksAddon.WebLinksAddon()); } catch(e) {}

  term.open(mount);
  fit.fit();

  let spawnResp;
  try {
    const r = await fetch('/api/spawn-terminal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid, rows: term.rows, cols: term.cols }),
    });
    spawnResp = await r.json();
  } catch(e) {
    setTermStatus('Failed to spawn terminal: ' + e.message, 'error');
    mount.remove();
    if (activeTermSid === sid) activeTermSid = null;
    return;
  }
  if (!spawnResp.ok) {
    setTermStatus(spawnResp.error || 'Failed to spawn terminal', 'error');
    mount.remove();
    if (activeTermSid === sid) activeTermSid = null;
    return;
  }

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(proto + '//' + location.host + '/ws/terminal/' + spawnResp.terminal_id);
  ws.binaryType = 'arraybuffer';

  const state = {
    term, fit, ws, mount,
    tid: spawnResp.terminal_id,
    cwd: spawnResp.cwd || '',
  };
  termSessions.set(sid, state);

  ws.onopen = () => {
    if (activeTermSid === sid) {
      setTermStatus('● live · cwd: ' + (state.cwd || '(unknown)'), 'live');
      term.focus();
    }
    _markActive(sid);
    _sendResizeForSid(sid);
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      if (ev.data.startsWith('{')) {
        try {
          const msg = JSON.parse(ev.data);
          if (msg && msg.exit) {
            if (activeTermSid === sid) setTermStatus('Session ended.', 'error');
            // Auto-clean on natural exit
            teardownLiveTerminal(sid);
            return;
          }
          if (msg && msg.error) {
            if (activeTermSid === sid) setTermStatus('Error: ' + msg.error, 'error');
            return;
          }
        } catch(e) { /* not JSON — fall through to write */ }
      }
      term.write(ev.data);
    } else {
      term.write(new Uint8Array(ev.data));
    }
  };
  ws.onerror = () => {
    if (activeTermSid === sid) setTermStatus('WebSocket error', 'error');
  };
  ws.onclose = () => {
    if (activeTermSid === sid) setTermStatus('Disconnected.', 'error');
  };

  term.onData((data) => {
    if (ws.readyState === 1) ws.send(data);
  });

  _ensureTermResizeObserver();
  setupTerminalDrop(mount, ws);
}

function setupTerminalDrop(mount, ws) {
  // In pywebview (desktop), the Python side handles the drop — skip the JS path entirely
  // so we don't stopPropagation the event before pywebview's handler sees it.
  if (window.pywebview) return;

  const stop = (ev) => { ev.preventDefault(); ev.stopPropagation(); };

  ['dragenter', 'dragover'].forEach(evt => {
    mount.addEventListener(evt, (ev) => {
      stop(ev);
      if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'copy';
      mount.classList.add('drop-active');
    });
  });
  ['dragleave', 'dragend'].forEach(evt => {
    mount.addEventListener(evt, (ev) => {
      if (!mount.contains(ev.relatedTarget)) mount.classList.remove('drop-active');
    });
  });

  mount.addEventListener('drop', async (ev) => {
    stop(ev);
    mount.classList.remove('drop-active');
    const files = ev.dataTransfer && ev.dataTransfer.files;
    if (!files || files.length === 0) return;

    const images = Array.from(files).filter(f => !f.type || f.type.startsWith('image/'));
    if (images.length === 0) return;

    const toast = showToast(
      images.length === 1 ? 'Uploading image…' : ('Uploading ' + images.length + ' images…'),
      { spinner: true, sticky: true }
    );

    const paths = [];
    for (const file of images) {
      try {
        const form = new FormData();
        form.append('file', file, file.name || 'image.png');
        const r = await fetch('/api/upload-image', { method: 'POST', body: form });
        const data = await r.json();
        if (!r.ok || !data.path) throw new Error(data.error || 'upload failed');
        paths.push(data.path);
      } catch(e) {
        toast.update('Upload failed: ' + e.message, 'error');
        return;
      }
    }

    // Type each path into the PTY as a quoted literal (matches gnome-terminal drag behavior,
    // which is what claude parses into [Image #N]).
    if (ws && ws.readyState === 1) {
      for (const p of paths) ws.send("'" + p.replace(/'/g, "'\\''") + "' ");
    }
    toast.update(
      images.length === 1 ? 'Image attached' : (images.length + ' images attached'),
      'success'
    );
  });
}

function teardownLiveTerminal(sid) {
  // No arg → tear down all live terminals (used on page unload).
  if (sid == null) {
    for (const id of Array.from(termSessions.keys())) teardownLiveTerminal(id);
    if (_termResizeObs) { try { _termResizeObs.disconnect(); } catch(e) {} _termResizeObs = null; }
    return;
  }
  const s = termSessions.get(sid);
  if (!s) return;
  termSessions.delete(sid);
  _unmarkActive(sid);
  try { if (s.ws && s.ws.readyState <= 1) s.ws.close(); } catch(e) {}
  try { if (s.term) s.term.dispose(); } catch(e) {}
  if (s.tid) {
    fetch('/api/kill-terminal/' + s.tid, { method: 'POST' }).catch(() => {});
  }
  if (s.mount && s.mount.parentNode) s.mount.parentNode.removeChild(s.mount);
  if (activeTermSid === sid) {
    activeTermSid = null;
    setTermStatus('Ready to resume.');
  }
}

window.addEventListener('beforeunload', () => teardownLiveTerminal());

// ═══════════════════════════════════════════════════════════════
// GTK BRIDGE (native Linux shell — VTE instead of xterm.js)
// ═══════════════════════════════════════════════════════════════

let _gtkCodeSid = null;
let _gtkRectObs = null;

function _gtkGetRect() {
  const mount = document.getElementById('termMounts');
  if (!mount) return null;
  const r = mount.getBoundingClientRect();
  return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) };
}

function startGtkCode(sid) {
  setTermStatus('Starting claude --resume ' + sid.slice(0, 8) + '…', 'live');
  _gtkCodeSid = sid;
  const rect = _gtkGetRect();
  // No cwd fetch — Python resolves it from the session DB server-side.
  // Pull cwd from the already-loaded sessions list as a hint, no network hop.
  const local = sessions.find(s => s.session_id === sid);
  const cwd = (local && local.cwd) || '';
  window.gtkSend({ type: 'code-on', sid, cwd, rect });

  if (_gtkRectObs) _gtkRectObs.disconnect();
  _gtkRectObs = new ResizeObserver(() => {
    const r = _gtkGetRect();
    if (r) window.gtkSend({ type: 'code-rect', rect: r });
  });
  const mount = document.getElementById('termMounts');
  if (mount) _gtkRectObs.observe(mount);
  window.addEventListener('resize', _gtkOnResize);
}

function _gtkOnResize() {
  const r = _gtkGetRect();
  if (r && _gtkCodeSid) window.gtkSend({ type: 'code-rect', rect: r });
}

function stopGtkCode() {
  _gtkCodeSid = null;
  if (_gtkRectObs) { _gtkRectObs.disconnect(); _gtkRectObs = null; }
  window.removeEventListener('resize', _gtkOnResize);
  window.gtkSend && window.gtkSend({ type: 'code-off' });
  setTermStatus('Ready to resume.');
}

window.onGtkCodeStart = function(sid) {
  _markActive(sid);
};

window.onGtkCodeExit = function(sid) {
  _unmarkActive(sid);
  // Only update status if the session that died is the one the user is looking at
  if (sid && sid === currentSessionId) {
    setTermStatus('Session ended.', 'error');
    _gtkCodeSid = null;
  }
};

// Invoked from Python on Alt+<key>. Lets app shortcuts work even when VTE has focus.
window.__gtkShortcut = function(action) {
  const focusedSid = () =>
    currentSessionId ||
    (typeof focusedIndex !== 'undefined' && focusedIndex >= 0 && sessions[focusedIndex]
      ? sessions[focusedIndex].session_id : null);

  switch (action) {
    case 'next': {
      if (typeof focusedIndex === 'undefined') return;
      const n = sessions.length;
      if (n === 0) return;
      setFocus(Math.min(n - 1, (focusedIndex < 0 ? 0 : focusedIndex + 1)), true);
      return;
    }
    case 'prev': {
      if (typeof focusedIndex === 'undefined') return;
      if (focusedIndex > 0) setFocus(focusedIndex - 1, true);
      return;
    }
    case 'delete': {
      if (typeof selectedIds !== 'undefined' && selectedIds.size > 0) { bulkDelete(); return; }
      const sid = focusedSid();
      if (sid) deleteSession(sid);
      return;
    }
    case 'toggle-done': {
      if (typeof selectedIds !== 'undefined' && selectedIds.size > 0) { bulkToggleDone(); return; }
      const sid = focusedSid();
      if (sid) toggleDone(sid);
      return;
    }
    case 'rename':      { const sid = focusedSid(); if (sid) renameSession(sid); return; }
    case 'retitle':     { const sid = focusedSid(); if (sid) retitleSession(sid); return; }
    case 'star':        { const sid = focusedSid(); if (sid) toggleStar(sid); return; }
    case 'resume-ext':  { const sid = focusedSid(); if (sid) resumeSession(sid); return; }
    case 'new-chat-external': newChat(); return;
    case 'focus-search': {
      const el = document.getElementById('searchInput');
      if (el) el.focus();
      return;
    }
    case 'close-terminal': {
      const sid = currentSessionId || focusedSid();
      if (sid && _activeTerms.has(sid)) closeActiveTerminal(sid);
      return;
    }
    case 'toggle-files': toggleFilesPane(); return;
    case 'view-chats':     switchTab('chats'); return;
    case 'view-memory':    switchTab('memory'); return;
    case 'view-knowledge': switchTab('knowledge'); return;
    case 'view-usage':     switchTab('usage'); return;
  }
};

// Called from the pywebview Python side after a native drop — paths are already on disk.
window.onFileDropped = function(paths) {
  const s = activeTermSid ? termSessions.get(activeTermSid) : null;
  if (!s || !s.ws || s.ws.readyState !== 1) {
    showToast('Open a Code tab first to drop files.', { variant: 'error' });
    return;
  }
  if (!Array.isArray(paths)) paths = [paths];
  for (const p of paths) {
    const q = "'" + String(p).replace(/'/g, "'\\''") + "' ";
    s.ws.send(q);
  }
  showToast(
    paths.length === 1 ? 'Image attached' : (paths.length + ' images attached'),
    { variant: 'success' }
  );
};

async function newChat() {
  // External terminal — spawn gnome-terminal with claude. Used for Alt+N.
  let cwd = currentProjectCwd;
  if (!cwd) {
    const res = await showPrompt({
      title: 'New chat (external terminal)',
      body: 'Working directory (leave empty for home):',
      placeholder: '~/Documents/Projects/...',
    });
    if (res === null) return;
    cwd = res.trim();
  }
  try {
    const body = cwd ? { cwd } : {};
    await fetch('/api/new-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch(e) {}
}

async function newChatInline() {
  // In-app new chat — spawns a VTE in the Code pane (GTK only).
  if (!window.__gtkBridge) return newChat();

  const res = await showPrompt({
    title: 'New chat',
    body: '',
    placeholder: 'Name this chat (e.g. MCP Install)',
    confirm: 'Create',
  });
  if (res === null) return;
  const typedTitle = res.trim();

  // cwd comes from the active project chip (or stays empty → Python uses $HOME).
  // No more typing paths.
  const cwd = currentProjectCwd || '';
  const shortProj = cwd ? (cwd.split('/').filter(Boolean).pop() || '~') : '~';
  const label = typedTitle || 'New chat';

  const tempId = 'new-' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  const iso = new Date().toISOString();
  const pseudo = {
    session_id: tempId,
    display_title: label,
    project_short: shortProj,
    cwd,
    first_timestamp: iso,
    last_timestamp: iso,
    starred: false,
    input_tokens: 0, output_tokens: 0,
    cache_read_tokens: 0, cache_create_tokens: 0,
    isPseudo: true,
    // If the user typed a name, remember it so the REAL session gets the same
    // title once the reconciler migrates the pseudo onto it.
    pending_rename_title: typedTitle || null,
  };
  _pseudoSessions.unshift(pseudo);
  sessions.unshift(pseudo);
  _markActive(tempId);

  currentSessionId = tempId;
  focusedSid = tempId;
  convMode = 'live';
  document.getElementById('viewReadBtn').classList.remove('active');
  document.getElementById('viewLiveBtn').classList.add('active');
  document.getElementById('convBody').classList.add('hidden');
  document.getElementById('convTerminal').classList.remove('hidden');
  document.getElementById('convEmpty').classList.add('hidden');
  document.getElementById('convContent').classList.remove('hidden');
  document.getElementById('convTitle').textContent = label;
  document.getElementById('convMeta').textContent = cwd || '~';

  setTermStatus('Starting claude…', 'live');
  const rect = _gtkGetRect();
  window.gtkSend({ type: 'code-on', sid: tempId, cwd, rect, isNew: true });
  _gtkCodeSid = tempId;
  _startPseudoReconciler();
}

// Python calls this right after spawning claude with the RESOLVED cwd.
// We fix up the pseudo so the reconciler's cwd match actually works when the
// real session file appears on disk.
window.onGtkCodeStarted = function(sid, resolvedCwd) {
  if (!sid || !resolvedCwd) return;
  const pseudo = _pseudoSessions.find(p => p.session_id === sid);
  if (!pseudo) return;
  pseudo.cwd = resolvedCwd;
  pseudo.project_short = resolvedCwd.split('/').filter(Boolean).pop() || '~';
  renderSessionList();
};

async function shutdownServer() {
  try { await fetch('/api/shutdown', { method: 'POST' }); } catch(e) {}
  setTimeout(() => window.close(), 300);
}

// ═══════════════════════════════════════════════════════════════
// MEMORY: Data Loading
// ═══════════════════════════════════════════════════════════════
async function loadMemories() {
  try {
    const r = await fetch('/api/memory');
    memories = await r.json();
    renderMemoryList();
    document.getElementById('memoryCount').textContent = '(' + memories.length + ')';
  } catch(e) {
    console.error('loadMemories:', e);
  }
}

function renderMemoryList() {
  const el = document.getElementById('memoryList');
  if (!memories.length) {
    el.innerHTML = '<div class="empty-text">No memories found</div>';
    return;
  }

  // Group by type
  const byType = {};
  for (const m of memories) {
    const t = (m.type || 'general').toUpperCase();
    if (!byType[t]) byType[t] = [];
    byType[t].push(m);
  }

  let html = '';
  let globalIdx = 0;
  const typeOrder = ['FEEDBACK', 'USER', 'PROJECT', 'GENERAL', 'REFERENCE'];
  for (const t of typeOrder) {
    const mems = byType[t];
    if (!mems || !mems.length) continue;
    html += '<div class="memory-group-header">' + t + ' (' + mems.length + ')</div>';
    for (const m of mems) {
      const focused = globalIdx === memFocusedIndex ? ' focused' : '';
      html += '<div class="memory-row' + focused + '" data-midx="' + globalIdx + '" onclick="setMemFocus(' + globalIdx + ')">'
        + '<span class="memory-id">#' + m.id + '</span>'
        + '<span class="memory-content">' + esc(m.content) + '</span>'
        + '<span class="memory-actions">'
        + '<button class="mem-btn" onclick="event.stopPropagation();editMemory(' + m.id + ')">edit</button>'
        + '<button class="mem-btn danger" onclick="event.stopPropagation();deleteMemory(' + m.id + ')">del</button>'
        + '</span>'
        + '</div>';
      globalIdx++;
    }
  }
  el.innerHTML = html;
}

function setMemFocus(idx) {
  memFocusedIndex = idx;
  const rows = document.querySelectorAll('#memoryList .memory-row');
  rows.forEach(r => r.classList.remove('focused'));
  if (rows[idx]) {
    rows[idx].classList.add('focused');
    rows[idx].scrollIntoView({ block: 'nearest' });
  }
}

function getFlatMemory(idx) {
  // Flatten memories in display order
  const typeOrder = ['feedback', 'user', 'project', 'general', 'reference'];
  const flat = [];
  for (const t of typeOrder) {
    for (const m of memories) {
      if (m.type === t) flat.push(m);
    }
  }
  return flat[idx] || null;
}

// ═══════════════════════════════════════════════════════════════
// MEMORY: Actions
// ═══════════════════════════════════════════════════════════════
async function addMemory() {
  const content = prompt('Memory content:');
  if (!content || !content.trim()) return;
  const type = prompt('Type (feedback/user/project/general/reference):', 'general');
  if (!type) return;
  try {
    await fetch('/api/memory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: content.trim(), type: type.trim().toLowerCase() }),
    });
    await loadMemories();
  } catch(e) {}
}

async function editMemory(id) {
  const mem = memories.find(m => m.id === id);
  if (!mem) return;
  const content = prompt('Edit memory:', mem.content);
  if (content === null) return;
  const type = prompt('Type:', mem.type);
  if (!type) return;
  try {
    await fetch('/api/memory/' + id, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: content.trim(), type: type.trim().toLowerCase() }),
    });
    await loadMemories();
  } catch(e) {}
}

async function deleteMemory(id) {
  const ok = await showConfirm({
    title: 'Delete memory #' + id + '?',
    body: 'This cannot be undone.',
    confirm: 'Delete',
    danger: true,
  });
  if (!ok) return;
  try {
    await fetch('/api/memory/' + id, { method: 'DELETE' });
    await loadMemories();
  } catch(e) {}
}

// ═══════════════════════════════════════════════════════════════
// KNOWLEDGE: Data Loading
// ═══════════════════════════════════════════════════════════════
async function loadTopics() {
  try {
    const r = await fetch('/api/knowledge');
    topics = await r.json();
    renderTopicList();
    document.getElementById('knowledgeCount').textContent = '(' + topics.length + ')';
  } catch(e) {
    console.error('loadTopics:', e);
  }
}

function renderTopicList() {
  const el = document.getElementById('topicList');
  if (!topics.length) {
    el.innerHTML = '<div class="empty-text">No topics found</div>';
    return;
  }

  let html = '';
  for (let i = 0; i < topics.length; i++) {
    const t = topics[i];
    const focused = i === topicFocusedIndex ? ' focused' : '';
    html += '<div class="topic-row' + focused + '" data-tidx="' + i + '" onclick="selectTopic(' + i + ')">'
      + '<div class="topic-title">' + esc(t.title || t.slug) + '</div>'
      + '<div class="topic-desc">' + esc(t.description || '') + '</div>'
      + '<div class="topic-meta">' + t.file_count + ' files \u00b7 ' + formatSize(t.total_size) + '</div>'
      + '</div>';
  }
  el.innerHTML = html;
}

async function selectTopic(idx) {
  topicFocusedIndex = idx;
  const t = topics[idx];
  if (!t) return;
  currentTopicSlug = t.slug;
  viewingFile = false;
  fileFocusedIndex = -1;

  // Update focus visuals
  const rows = document.querySelectorAll('#topicList .topic-row');
  rows.forEach(r => r.classList.remove('focused'));
  if (rows[idx]) rows[idx].classList.add('focused');

  // Load files
  try {
    const r = await fetch('/api/knowledge/' + t.slug);
    const data = await r.json();
    topicFiles = data.files || [];
    const linked = data.sessions || [];

    document.getElementById('knowledgeEmpty').classList.add('hidden');
    document.getElementById('knowledgeContent').classList.remove('hidden');
    document.getElementById('fileList').classList.remove('hidden');
    document.getElementById('fileContentView').classList.add('hidden');

    document.getElementById('knowledgeHeader').innerHTML = esc(t.title || t.slug);

    let html = '';
    for (let i = 0; i < topicFiles.length; i++) {
      const f = topicFiles[i];
      html += '<div class="file-row" data-fidx="' + i + '" onclick="openFile(' + i + ')">'
        + '<span class="file-name">' + esc(f.name) + '</span>'
        + '<span class="file-size">' + formatSize(f.size) + '</span>'
        + '</div>';
    }
    if (linked.length) {
      html += '<div style="padding:8px 12px;font-size:10px;color:var(--text-dim);border-top:1px solid var(--border);text-transform:uppercase;letter-spacing:1px;">Linked Sessions</div>';
      for (const ls of linked) {
        html += '<div class="file-row" onclick="switchTab(\'chats\');openConv(\'' + ls.session_id + '\')">'
          + '<span class="file-name" style="color:var(--green)">' + esc(ls.display_title || 'Untitled') + '</span>'
          + '<span class="file-size">' + formatDate(ls.first_timestamp) + '</span>'
          + '</div>';
      }
    }
    document.getElementById('fileList').innerHTML = html;
  } catch(e) {}
}

async function openFile(idx) {
  if (!currentTopicSlug || !topicFiles[idx]) return;
  fileFocusedIndex = idx;
  viewingFile = true;
  const f = topicFiles[idx];

  // Update file row focus
  const rows = document.querySelectorAll('#fileList .file-row');
  rows.forEach(r => r.classList.remove('focused'));
  if (rows[idx]) rows[idx].classList.add('focused');

  try {
    const r = await fetch('/api/knowledge/' + currentTopicSlug + '/' + f.name);
    const data = await r.json();

    document.getElementById('fileList').classList.add('hidden');
    document.getElementById('fileContentView').classList.remove('hidden');
    document.getElementById('knowledgeHeader').innerHTML =
      '<span class="file-back" onclick="backToFileList()">\u2190</span> ' + esc(f.name);
    document.getElementById('fileContentView').textContent = data.content || '';
  } catch(e) {}
}

function backToFileList() {
  viewingFile = false;
  document.getElementById('fileList').classList.remove('hidden');
  document.getElementById('fileContentView').classList.add('hidden');
  const t = topics[topicFocusedIndex];
  if (t) {
    document.getElementById('knowledgeHeader').innerHTML = esc(t.title || t.slug);
  }
}

// ═══════════════════════════════════════════════════════════════
// Shortcut Bar
// ═══════════════════════════════════════════════════════════════
// ═══════════════════════════════════════════════════════════════
// USAGE DASHBOARD
// ═══════════════════════════════════════════════════════════════
const USAGE_COMPARISONS = [
  { name: 'The Great Gatsby', tokens: 70000 },
  { name: 'Moby-Dick', tokens: 280000 },
  { name: 'The Lord of the Rings', tokens: 580000 },
  { name: 'War and Peace', tokens: 750000 },
  { name: 'the Bible', tokens: 780000 },
  { name: 'the Harry Potter series', tokens: 1500000 },
  { name: 'every Pixar screenplay combined', tokens: 400000 },
  { name: 'the US tax code', tokens: 3400000 },
  { name: 'the Oxford English Dictionary', tokens: 80000000 },
];

function setUsageRange(r) {
  usageRange = r;
  document.querySelectorAll('#usageRangeGroup .usage-range-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.range === r));
  loadUsage();
}

function setUsageSubtab(s) {
  usageSubtab = s;
  document.querySelectorAll('#usageSubtabs .usage-subtab').forEach(b =>
    b.classList.toggle('active', b.dataset.subtab === s));
  document.getElementById('usageOverview').classList.toggle('hidden', s !== 'overview');
  document.getElementById('usageModels').classList.toggle('hidden', s !== 'models');
  if (usageData) renderUsage(usageData);
}

async function loadUsage() {
  try {
    const r = await fetch('/api/usage?range=' + usageRange);
    usageData = await r.json();
    renderUsage(usageData);
  } catch(e) { console.error('loadUsage:', e); }
}

function renderUsage(d) {
  if (usageSubtab === 'overview') renderUsageOverview(d);
  else renderUsageModels(d);
}

function renderUsageOverview(d) {
  const peak = d.peak_hour != null ? formatHour(d.peak_hour) : '\u2014';
  const fav = d.favorite_model ? prettyModel(d.favorite_model) : '\u2014';
  const cards = [
    ['Sessions', fmtInt(d.sessions)],
    ['Messages', fmtInt(d.messages)],
    ['Billable tokens', d.total_tokens ? formatTokens(d.total_tokens) : '0'],
    ['Est. cost', formatUSD(d.cost_usd)],
    ['Active days', fmtInt(d.active_days)],
    ['Current streak', d.current_streak + 'd'],
    ['Longest streak', d.longest_streak + 'd'],
    ['Peak hour', peak],
    ['Favorite model', fav],
    ['Cache hit rate', formatPct(d.cache_hit_rate)],
    ['Typical session', formatDuration(d.median_session_seconds)],
    ['Projects touched', fmtInt(d.distinct_projects)],
  ];
  document.getElementById('statGrid').innerHTML = cards.map(([l, v]) =>
    '<div class="stat-card"><div class="stat-card-label">' + esc(l) + '</div>'
    + '<div class="stat-card-value" title="' + esc(String(v)) + '">' + esc(String(v)) + '</div></div>'
  ).join('');
  renderHourChart(d.hourly || [], d.peak_hour);
  renderHeatmap(d.heatmap || [], d.heatmap_days || 365);
  renderTopProjects(d.top_projects || [], d.sessions || 0);
  renderAchievements(d.achievements || []);
  renderBrainStrip(d);
  document.getElementById('usageFlavor').textContent = flavorLine(d.total_tokens);
}

function renderHourChart(hourly, peakHour) {
  const max = Math.max(1, ...hourly);
  const html = hourly.map((v, i) => {
    const h = Math.max(2, Math.round((v / max) * 100));
    const isPeak = i === peakHour;
    const title = formatHour(i) + ' \u2014 ' + v + ' session' + (v === 1 ? '' : 's');
    return '<div class="hour-bar' + (isPeak ? ' peak' : '') + '" style="height:' + h + '%" title="' + esc(title) + '"></div>';
  }).join('');
  document.getElementById('hourChart').innerHTML = html;
}

function renderTopProjects(projects, totalSessions) {
  if (!projects.length) {
    document.getElementById('topProjectList').innerHTML =
      '<div class="empty-text" style="padding:16px">No project data in this range</div>';
    return;
  }
  const max = projects[0].sessions || 1;
  const html = projects.map(p => {
    const barWidth = (p.sessions / max) * 100;
    return '<div class="project-row-usage">'
      + '<div class="bar-bg" style="width:' + barWidth + '%"></div>'
      + '<div class="project-name-usage" title="' + esc(p.project_dir) + '">' + esc(prettyProject(p.project_dir)) + '</div>'
      + '<div class="project-count-usage">' + fmtInt(p.sessions) + ' \u00b7 ' + formatTokens(p.tokens) + '</div>'
      + '<div class="project-pct-usage">' + p.pct.toFixed(1) + '%</div>'
      + '</div>';
  }).join('');
  document.getElementById('topProjectList').innerHTML = html;
}

function renderAchievements(achs) {
  const html = achs.map(a =>
    '<div class="ach' + (a.unlocked ? ' unlocked' : '') + '" title="' + esc(a.desc) + '">'
    + '<div class="ach-label">' + (a.unlocked ? '\u2713 ' : '\u2022 ') + esc(a.label) + '</div>'
    + '<div class="ach-desc">' + esc(a.desc) + '</div>'
    + '</div>'
  ).join('');
  document.getElementById('achievementsGrid').innerHTML = html;
}

function renderBrainStrip(d) {
  const bytes = d.knowledge_bytes || 0;
  const kbLabel = bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB'
                : bytes >= 1024 ? (bytes / 1024).toFixed(0) + ' KB'
                : bytes + ' B';
  const cards = [
    { label: 'Memories', value: fmtInt(d.memory_count), sub: 'filesystem notes' },
    { label: 'Knowledge', value: fmtInt(d.knowledge_topics) + ' topics', sub: fmtInt(d.knowledge_files) + ' files \u00b7 ' + kbLabel },
    { label: 'Starred chats', value: fmtInt(d.starred_count), sub: 'across all time' },
  ];
  document.getElementById('brainStrip').innerHTML = cards.map(c =>
    '<div class="brain-card">'
    + '<div class="brain-card-label">' + esc(c.label) + '</div>'
    + '<div class="brain-card-value">' + esc(c.value) + '</div>'
    + '<div class="brain-card-sub">' + esc(c.sub) + '</div>'
    + '</div>'
  ).join('');
}

function prettyProject(slug) {
  if (!slug) return '\u2014';
  // SERENA.homeSlug is injected at /index render time. Linux: "-home-alice",
  // macOS: "-Users-alice", Windows: "C--Users-alice".
  const env = window.SERENA || {};
  const home = env.homeSlug;
  if (home) {
    if (slug.startsWith(home + '-Documents-Projects-')) return slug.slice((home + '-Documents-Projects-').length).replace(/-/g, '/');
    if (slug.startsWith(home + '-Projects-')) return slug.slice((home + '-Projects-').length).replace(/-/g, '/');
    if (slug.startsWith(home + '-')) return '~/' + slug.slice((home + '-').length).replace(/-/g, '/');
    if (slug === home) return '~';
  }
  return slug;
}

function formatUSD(n) {
  if (n == null || !isFinite(n)) return '\u2014';
  if (n < 1) return '$' + n.toFixed(2);
  if (n < 100) return '$' + n.toFixed(2);
  if (n < 10000) return '$' + Math.round(n).toLocaleString('en-US');
  return '$' + (n / 1000).toFixed(1) + 'k';
}

function formatPct(r) {
  if (r == null || !isFinite(r)) return '\u2014';
  return (r * 100).toFixed(1) + '%';
}

function formatDuration(secs) {
  if (!secs || secs < 1) return '\u2014';
  if (secs < 60) return Math.round(secs) + 's';
  if (secs < 3600) return Math.round(secs / 60) + 'm';
  if (secs < 86400) return (secs / 3600).toFixed(1) + 'h';
  return Math.round(secs / 86400) + 'd';
}

function renderUsageModels(d) {
  const models = d.models || [];
  if (!models.length) {
    document.getElementById('modelList').innerHTML = '<div class="empty-text">No model data in this range</div>';
    return;
  }
  const max = Math.max(1, ...models.map(m =>
    (m.input_tokens || 0) + (m.output_tokens || 0) + (m.cache_read_tokens || 0) + (m.cache_create_tokens || 0)
  ));
  const html = models.map(m => {
    const tot = (m.input_tokens || 0) + (m.output_tokens || 0) + (m.cache_read_tokens || 0) + (m.cache_create_tokens || 0);
    const pct = (x) => tot ? (x / tot * 100) : 0;
    const widthPct = (tot / max) * 100;
    return '<div class="model-row">'
      + '<div><div class="model-row-name">' + esc(prettyModel(m.model)) + '</div>'
      + '<div class="model-row-meta">' + fmtInt(m.sessions) + ' session' + (m.sessions === 1 ? '' : 's') + '</div></div>'
      + '<div class="model-row-meta">'
        + 'in ' + formatTokens(m.input_tokens) + ' \u00b7 out ' + formatTokens(m.output_tokens)
        + ' \u00b7 cr ' + formatTokens(m.cache_read_tokens) + ' \u00b7 cc ' + formatTokens(m.cache_create_tokens)
      + '</div>'
      + '<div class="model-row-tokens">' + formatTokens(tot) + '</div>'
      + '<div class="model-bar-wrap" style="width:' + widthPct + '%">'
        + '<div class="model-bar-seg input" style="width:' + pct(m.input_tokens) + '%"></div>'
        + '<div class="model-bar-seg output" style="width:' + pct(m.output_tokens) + '%"></div>'
        + '<div class="model-bar-seg cache-read" style="width:' + pct(m.cache_read_tokens) + '%"></div>'
        + '<div class="model-bar-seg cache-create" style="width:' + pct(m.cache_create_tokens) + '%"></div>'
      + '</div>'
    + '</div>';
  }).join('');
  document.getElementById('modelList').innerHTML = html;
}

function renderHeatmap(days, totalDays) {
  const byDay = new Map((days || []).map(d => [d.day, d]));
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  let max = 0;
  for (const d of days) { if ((d.sessions || 0) > max) max = d.sessions; }
  function level(n) {
    if (!n || !max) return 0;
    const r = n / max;
    if (r >= 0.75) return 4;
    if (r >= 0.5) return 3;
    if (r >= 0.25) return 2;
    return 1;
  }
  const cells = [];
  for (let i = totalDays - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    const key = localISODate(d);
    const rec = byDay.get(key);
    cells.push({
      date: key,
      sessions: rec ? rec.sessions : 0,
      messages: rec ? rec.messages : 0,
      tokens: rec ? rec.tokens : 0,
      dow: d.getDay(),
    });
  }
  const pad = [];
  for (let i = 0; i < cells[0].dow; i++) pad.push(null);
  const grid = pad.concat(cells);
  let html = '<div class="heatmap-grid">';
  for (const c of grid) {
    if (!c) { html += '<div class="heatmap-cell empty"></div>'; continue; }
    const lv = level(c.sessions);
    const title = c.date + ' \u2014 ' + c.sessions + ' session' + (c.sessions === 1 ? '' : 's')
      + (c.tokens ? ', ' + formatTokens(c.tokens) + ' tokens' : '');
    html += '<div class="heatmap-cell" data-level="' + lv + '" title="' + esc(title) + '"></div>';
  }
  html += '</div>';
  document.getElementById('heatmapWrap').innerHTML = html;
}

function localISODate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + day;
}

function formatHour(h) {
  if (h == null) return '\u2014';
  if (h === 0) return '12 AM';
  if (h === 12) return '12 PM';
  if (h < 12) return h + ' AM';
  return (h - 12) + ' PM';
}

function fmtInt(n) {
  if (n == null) return '0';
  return Number(n).toLocaleString('en-US');
}

function prettyModel(m) {
  if (!m) return '\u2014';
  const clean = m.toLowerCase().replace(/^claude-/, '').replace(/-\d{8}.*$/, '');
  let tier = '';
  if (clean.includes('opus')) tier = 'Opus';
  else if (clean.includes('sonnet')) tier = 'Sonnet';
  else if (clean.includes('haiku')) tier = 'Haiku';
  else return m;
  const nums = clean.match(/\d+/g) || [];
  if (!nums.length) return tier;
  if (nums.length === 1) return tier + ' ' + nums[0];
  return tier + ' ' + nums[0] + '.' + nums[1];
}

function flavorLine(tokens) {
  if (!tokens) return '';
  const pick = USAGE_COMPARISONS[Math.floor(Math.random() * USAGE_COMPARISONS.length)];
  const ratio = tokens / pick.tokens;
  if (ratio >= 1) {
    const r = ratio >= 100 ? Math.round(ratio).toLocaleString('en-US')
            : ratio >= 10 ? ratio.toFixed(0) : ratio.toFixed(1);
    return "You've used ~" + r + "\u00d7 more tokens than " + pick.name + ".";
  }
  return "You've used about " + Math.round(ratio * 100) + "% as many tokens as " + pick.name + ".";
}

function updateShortcutBar() {
  const bar = document.getElementById('shortcutBar');
  let shortcuts = [];

  if (currentTab === 'chats') {
    shortcuts = [
      ['\u2191\u2193', 'navigate'], ['Enter', 'open'], ['/', 'search'],
      ['n', 'new chat'], ['Alt+n', 'new in ext term'],
      ['o', 'resume ext'], ['s', 'star'], ['r', 'rename'], ['t', 'AI title'],
      ['d', 'done / undone'], ['Alt+Del', 'delete'], ['Ctrl+A', 'select all'],
      ['Shift+\u2191\u2193', 'extend sel'], ['Esc', 'deselect'],
    ];
    if (window.__gtkBridge) {
      shortcuts.push(
        ['Alt+w', 'close term'],
        ['Alt+j/k', 'next/prev'],
        ['Alt+d', 'done / undone'],
        ['Alt+r/t/s', 'rename / title / star'],
        ['Alt+1-4', 'switch tab'],
        ['Alt+b', 'toggle files'],
        ['Ctrl+C', 'copy (if selection)'],
        ['Ctrl+V', 'paste'],
        ['Ctrl+click', 'open link'],
        ['Ctrl+⌫', 'delete word'],
        ['Shift+Enter', 'newline'],
      );
    }
  } else if (currentTab === 'memory') {
    shortcuts = [
      ['\u2191\u2193', 'navigate'], ['a', 'add'], ['e', 'edit'],
      ['Del', 'delete'], ['Esc', 'close'],
    ];
  } else if (currentTab === 'knowledge') {
    shortcuts = [
      ['\u2191\u2193', 'navigate'], ['Enter', 'open'], ['Esc', 'back'],
    ];
  } else if (currentTab === 'usage') {
    shortcuts = [
      ['r', 'cycle range'], ['t', 'toggle tab'],
    ];
  }

  bar.innerHTML = shortcuts.map(([key, desc]) =>
    '<span class="shortcut"><kbd>' + key + '</kbd>' + desc + '</span>'
  ).join('');
}

// ═══════════════════════════════════════════════════════════════
// Keyboard Handler
// ═══════════════════════════════════════════════════════════════
document.addEventListener('keydown', function(e) {
  // Don't capture when typing in input
  const tag = (e.target.tagName || '').toLowerCase();
  const isInput = tag === 'input' || tag === 'textarea';

  // Escape always works
  if (e.key === 'Escape') {
    e.preventDefault();
    if (isInput) {
      e.target.blur();
      return;
    }
    if (currentTab === 'chats') {
      if (selectedIds.size > 0) {
        selectedIds.clear();
        updateSelectionInfo();
        renderSessionList();
      } else if (currentSessionId) {
        closeConv();
      }
    } else if (currentTab === 'knowledge') {
      if (viewingFile) {
        backToFileList();
      } else if (currentTopicSlug) {
        currentTopicSlug = null;
        document.getElementById('knowledgeEmpty').classList.remove('hidden');
        document.getElementById('knowledgeContent').classList.add('hidden');
      }
    }
    return;
  }

  if (isInput) return;

  // === CHATS TAB ===
  if (currentTab === 'chats') {
    if (e.key === '/') {
      e.preventDefault();
      document.getElementById('searchInput').focus();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      const next = Math.min(focusedIndex + 1, sessions.length - 1);
      if (e.shiftKey) {
        selectedIds.add(sessions[next].session_id);
        if (focusedIndex >= 0) selectedIds.add(sessions[focusedIndex].session_id);
        updateSelectionInfo();
      }
      setFocus(next);
      if (e.shiftKey) renderSessionList();
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      const next = Math.max(focusedIndex - 1, 0);
      if (e.shiftKey) {
        selectedIds.add(sessions[next].session_id);
        if (focusedIndex >= 0) selectedIds.add(sessions[focusedIndex].session_id);
        updateSelectionInfo();
      }
      setFocus(next);
      if (e.shiftKey) renderSessionList();
      return;
    }
    if (e.key === 'Enter' && focusedIndex >= 0) {
      e.preventDefault();
      openConv(sessions[focusedIndex].session_id);
      return;
    }
    if (e.key === 'a' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      for (const s of sessions) selectedIds.add(s.session_id);
      updateSelectionInfo();
      renderSessionList();
      return;
    }

    // Single-key shortcuts
    if (focusedIndex >= 0) {
      const sid = sessions[focusedIndex].session_id;
      if (e.key === 's') { e.preventDefault(); toggleStar(sid); return; }
      if (e.key === 'o') { e.preventDefault(); resumeSession(sid); return; }
      if (e.key === 'r') { e.preventDefault(); renameSession(sid); return; }
      if (e.key === 't') { e.preventDefault(); retitleSession(sid); return; }
      if (e.key === 'd') {
        e.preventDefault();
        if (selectedIds.size > 1) bulkToggleDone();
        else toggleDone(sid);
        return;
      }
      // Alt+Del / Alt+Backspace → delete (plain Del no longer deletes, too easy to hit)
      if ((e.key === 'Delete' || e.key === 'Backspace') && e.altKey) {
        e.preventDefault();
        if (selectedIds.size > 1) bulkDelete();
        else deleteSession(sid);
        return;
      }
    }
    if (e.key === 'n') { e.preventDefault(); newChatInline(); return; }
  }

  // === MEMORY TAB ===
  if (currentTab === 'memory') {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setMemFocus(Math.min(memFocusedIndex + 1, memories.length - 1));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setMemFocus(Math.max(memFocusedIndex - 1, 0));
      return;
    }
    if (e.key === 'a') { e.preventDefault(); addMemory(); return; }
    if (e.key === 'e' && memFocusedIndex >= 0) {
      e.preventDefault();
      const m = getFlatMemory(memFocusedIndex);
      if (m) editMemory(m.id);
      return;
    }
    if ((e.key === 'Delete' || e.key === 'Backspace') && memFocusedIndex >= 0) {
      e.preventDefault();
      const m = getFlatMemory(memFocusedIndex);
      if (m) deleteMemory(m.id);
      return;
    }
  }

  // === KNOWLEDGE TAB ===
  if (currentTab === 'knowledge') {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (viewingFile) return;
      if (currentTopicSlug && topicFiles.length) {
        const next = Math.min(fileFocusedIndex + 1, topicFiles.length - 1);
        fileFocusedIndex = next;
        const rows = document.querySelectorAll('#fileList .file-row');
        rows.forEach(r => r.classList.remove('focused'));
        if (rows[next]) { rows[next].classList.add('focused'); rows[next].scrollIntoView({ block: 'nearest' }); }
      } else {
        selectTopic(Math.min(topicFocusedIndex + 1, topics.length - 1));
      }
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (viewingFile) return;
      if (currentTopicSlug && topicFiles.length && fileFocusedIndex > 0) {
        fileFocusedIndex--;
        const rows = document.querySelectorAll('#fileList .file-row');
        rows.forEach(r => r.classList.remove('focused'));
        if (rows[fileFocusedIndex]) { rows[fileFocusedIndex].classList.add('focused'); rows[fileFocusedIndex].scrollIntoView({ block: 'nearest' }); }
      } else if (!currentTopicSlug || fileFocusedIndex <= 0) {
        selectTopic(Math.max(topicFocusedIndex - 1, 0));
      }
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      if (!currentTopicSlug && topicFocusedIndex >= 0) {
        selectTopic(topicFocusedIndex);
      } else if (currentTopicSlug && fileFocusedIndex >= 0) {
        openFile(fileFocusedIndex);
      }
      return;
    }
  }

  // === USAGE TAB ===
  if (currentTab === 'usage') {
    if (e.key === 'r') {
      e.preventDefault();
      const order = ['all', '30', '7'];
      const next = order[(order.indexOf(usageRange) + 1) % order.length];
      setUsageRange(next);
      return;
    }
    if (e.key === 't') {
      e.preventDefault();
      setUsageSubtab(usageSubtab === 'overview' ? 'models' : 'overview');
      return;
    }
  }
});

// ═══════════════════════════════════════════════════════════════
// Search Debounce
// ═══════════════════════════════════════════════════════════════
document.getElementById('searchInput').addEventListener('input', function(e) {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => searchSessions(e.target.value.trim()), 250);
});

// ═══════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════
updateShortcutBar();
loadSessions();
loadProjects();
// Pre-fetch counts for tabs
fetch('/api/memory').then(r => r.json()).then(m => {
  memories = m;
  document.getElementById('memoryCount').textContent = '(' + m.length + ')';
}).catch(() => {});
fetch('/api/knowledge').then(r => r.json()).then(t => {
  topics = t;
  document.getElementById('knowledgeCount').textContent = '(' + t.length + ')';
}).catch(() => {});

// ═══════════════════════════════════════════════════════════════
// THEMED MODALS — confirm + prompt. Replace native confirm()/prompt().
// In GTK mode, the VTE overlay covers this area, so we temporarily hide
// the terminal stack while a modal is up (O(1), zero visual cost).
// ═══════════════════════════════════════════════════════════════
(function setupModal() {
  if (document.getElementById('modalBackdrop')) return;
  const b = document.createElement('div');
  b.id = 'modalBackdrop';
  b.innerHTML = `
    <div class="modal-card" role="dialog" aria-modal="true">
      <div class="modal-title" id="modalTitle"></div>
      <div class="modal-body"  id="modalBody"></div>
      <input class="modal-input" id="modalInput" type="text" style="display:none;" />
      <div class="modal-actions">
        <button class="modal-btn" id="modalCancelBtn">Cancel</button>
        <button class="modal-btn primary" id="modalConfirmBtn">OK</button>
      </div>
    </div>`;
  document.body.appendChild(b);
})();

let _modalOpenCount = 0;
function _modalHideTerminal() {
  _modalOpenCount++;
  if (_modalOpenCount === 1 && window.__gtkBridge) {
    window.gtkSend && window.gtkSend({ type: 'code-off' });
  }
}
function _modalRestoreTerminal() {
  _modalOpenCount = Math.max(0, _modalOpenCount - 1);
  if (_modalOpenCount === 0 && window.__gtkBridge && convMode === 'live' && currentSessionId) {
    const rect = _gtkGetRect();
    const local = sessions.find(s => s.session_id === currentSessionId);
    const cwd = (local && local.cwd) || '';
    window.gtkSend({ type: 'code-on', sid: currentSessionId, cwd, rect });
  }
}

function showConfirm({ title = 'Are you sure?', body = '', confirm = 'OK', cancel = 'Cancel', danger = false } = {}) {
  return new Promise((resolve) => {
    const bd = document.getElementById('modalBackdrop');
    const input = document.getElementById('modalInput');
    input.style.display = 'none';
    document.getElementById('modalTitle').textContent = title;
    document.getElementById('modalBody').textContent = body;
    const okBtn = document.getElementById('modalConfirmBtn');
    const cancelBtn = document.getElementById('modalCancelBtn');
    okBtn.textContent = confirm;
    cancelBtn.textContent = cancel;
    okBtn.className = 'modal-btn ' + (danger ? 'danger' : 'primary');

    const close = (result) => {
      bd.classList.remove('visible');
      bd.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey, true);
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      _modalRestoreTerminal();
      resolve(result);
    };
    const onBackdrop = (e) => { if (e.target === bd) close(false); };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(false); }
      else if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); close(true); }
    };
    const onOk = () => close(true);
    const onCancel = () => close(false);

    bd.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey, true);
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);

    _modalHideTerminal();
    bd.classList.add('visible');
    setTimeout(() => okBtn.focus(), 10);
  });
}

function showPrompt({ title = 'Enter value', body = '', placeholder = '', defaultValue = '', confirm = 'OK', cancel = 'Cancel' } = {}) {
  return new Promise((resolve) => {
    const bd = document.getElementById('modalBackdrop');
    const input = document.getElementById('modalInput');
    document.getElementById('modalTitle').textContent = title;
    document.getElementById('modalBody').textContent = body;
    input.style.display = '';
    input.placeholder = placeholder;
    input.value = defaultValue;
    const okBtn = document.getElementById('modalConfirmBtn');
    const cancelBtn = document.getElementById('modalCancelBtn');
    okBtn.textContent = confirm;
    cancelBtn.textContent = cancel;
    okBtn.className = 'modal-btn primary';

    const close = (result) => {
      bd.classList.remove('visible');
      bd.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey, true);
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      _modalRestoreTerminal();
      resolve(result);
    };
    const onBackdrop = (e) => { if (e.target === bd) close(null); };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(null); }
      else if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); close(input.value); }
    };
    const onOk = () => close(input.value);
    const onCancel = () => close(null);

    bd.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey, true);
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);

    _modalHideTerminal();
    bd.classList.add('visible');
    setTimeout(() => { input.focus(); input.select(); }, 10);
  });
}

// ═══════════════════════════════════════════════════════════════
// TOAST NOTIFICATIONS
// ═══════════════════════════════════════════════════════════════
(function setupToasts() {
  if (document.getElementById('toastStack')) return;
  const stack = document.createElement('div');
  stack.id = 'toastStack';
  document.body.appendChild(stack);
})();

function showToast(message, opts) {
  opts = opts || {};
  const stack = document.getElementById('toastStack');
  const el = document.createElement('div');
  el.className = 'toast' + (opts.variant ? ' ' + opts.variant : '');
  el.setAttribute('role', 'status');
  if (opts.spinner) {
    const sp = document.createElement('div');
    sp.className = 'toast-spinner';
    el.appendChild(sp);
  }
  const text = document.createElement('span');
  text.textContent = message;
  el.appendChild(text);
  stack.appendChild(el);
  requestAnimationFrame(() => el.classList.add('visible'));

  const api = {
    el,
    update(msg, variant) {
      text.textContent = msg;
      el.classList.remove('success', 'error');
      if (variant) el.classList.add(variant);
      const sp = el.querySelector('.toast-spinner');
      if (sp) sp.remove();
      if (api._autoDismiss) clearTimeout(api._autoDismiss);
      api._autoDismiss = setTimeout(api.dismiss, 2200);
    },
    dismiss() {
      el.classList.remove('visible');
      setTimeout(() => el.remove(), 180);
    },
  };
  if (!opts.sticky) {
    api._autoDismiss = setTimeout(api.dismiss, opts.duration || 2200);
  }
  return api;
}

</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # Inject the user's home dir + slug pattern at runtime so JS slug-decoders
    # work for any user/OS, not just whoever the dev was when the JS was written.
    home = str(Path.home()).replace("\\", "/")
    if home.lower().startswith("c:/"):
        home_slug = "C--" + home[3:].replace("/", "-")  # Windows: C--Users-bob
    else:
        home_slug = "-" + home.lstrip("/").replace("/", "-")  # Linux/macOS: -home-bob, -Users-bob
    boot = (
        '<script>window.SERENA = '
        + json.dumps({"home": home, "homeSlug": home_slug, "platform": sys.platform})
        + ';</script>\n'
    )
    return boot + HTML


def _ambiguous_shorts() -> set[str]:
    """Return the set of shortened-project names that map to more than one project_dir.

    Used to decide when a session row needs a [lin]/[win] tag to disambiguate."""
    shorts: dict[str, set[str]] = {}
    for p in list_projects():
        s = _chip_short(p["project_dir"], p.get("cwd"))
        shorts.setdefault(s, set()).add(p["project_dir"])
    return {s for s, dirs in shorts.items() if len(dirs) > 1}


def _decorate_sessions(sessions: list[dict]) -> list[dict]:
    ambiguous = _ambiguous_shorts()
    for s in sessions:
        project_dir = s.get("project_dir", "")
        stored_cwd = _get_session_cwd(s)
        real_cwd = _resolve_project_cwd(project_dir, stored_cwd)
        short = _shorten_project(project_dir, real_cwd)
        if short in ambiguous:
            tag = _device_tag(s.get("device"))
            if tag:
                short = f"{short} {tag}"
        s["project_short"] = short
        s["input_tokens"] = s.get("input_tokens") or 0
        s["output_tokens"] = s.get("output_tokens") or 0
        s["cache_read_tokens"] = s.get("cache_read_tokens") or 0
        s["cache_create_tokens"] = s.get("cache_create_tokens") or 0
    return sessions


@app.route("/api/sessions")
def api_sessions():
    # Opt-in disk rescan so the auto-poll can pick up new jsonl files claude
    # writes mid-session (e.g. after /clear). Cheap when nothing changed
    # (mtime/size diff only).
    if request.args.get("refresh"):
        try:
            update_index()
        except Exception as e:
            print(f"[api_sessions] refresh failed: {e}", flush=True)

    projects_param = request.args.get("projects")
    project = request.args.get("project")
    dirs: list[str] = []
    if projects_param:
        dirs = [d for d in projects_param.split(",") if d]
    elif project:
        dirs = [project]

    if dirs:
        seen: set[str] = set()
        merged: list[dict] = []
        for d in dirs:
            for s in list_sessions(project=d, limit=500):
                if s["session_id"] in seen:
                    continue
                seen.add(s["session_id"])
                merged.append(s)
        merged.sort(key=lambda s: s.get("last_timestamp") or "", reverse=True)
        sessions = merged[:500]
    else:
        sessions = list_sessions(limit=500)

    return jsonify(_decorate_sessions(sessions))


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    if not q:
        return jsonify([])
    results = search_fts(q, limit=80)
    if not results:
        build_fts()
        results = search_fts(q, limit=80)
    seen = set()
    sessions = []
    for r in results:
        sid = r["session_id"]
        if sid in seen:
            continue
        seen.add(sid)
        s = get_session(sid)
        if s:
            sessions.append(s)
    return jsonify(_decorate_sessions(sessions))


def _current_device() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def _device_tag(device: str | None) -> str:
    if device == "windows":
        return "[win]"
    if device == "linux":
        return "[lin]"
    if device == "darwin" or device == "macos":
        return "[mac]"
    return ""


def _walk_slug_match(current: Path, remaining: list[str]) -> str | None:
    """Walk the filesystem to match a slug's components to real directories.

    Claude replaces both ``/`` and ``_`` with ``-`` when slugifying a path,
    so decoding ``-home-raghav-Documents-Projects-personal-projects-konpeki``
    is ambiguous between ``personal/projects/konpeki`` and ``personal_projects/konpeki``.
    We resolve the ambiguity by walking the real filesystem: at each level,
    match a real child dir whose slugified name equals a prefix of the
    remaining slug parts.
    """
    if not remaining:
        return str(current)
    try:
        children = [c for c in current.iterdir() if c.is_dir()]
    except (OSError, PermissionError):
        return None
    # Try longer matches first so nested names like ``konpeki-konpeki-admin``
    # win over greedily taking a single component.
    candidates = []
    for child in children:
        slug_parts = child.name.replace("_", "-").split("-")
        n = len(slug_parts)
        if remaining[:n] == slug_parts:
            candidates.append((n, child))
    candidates.sort(key=lambda c: -c[0])
    for n, child in candidates:
        result = _walk_slug_match(child, remaining[n:])
        if result:
            return result
    return None


@functools.lru_cache(maxsize=512)
def _slug_to_real_path(slug: str) -> str | None:
    """Best-effort mapping of a claude project slug to an existing directory.

    Returns ``None`` for Windows slugs (``C--…``) when running on Linux and
    for slugs whose components no longer exist on disk."""
    if not slug.startswith("-"):
        return None
    parts = slug[1:].split("-")
    if not parts:
        return None
    root = Path("/" + parts[0])
    if not root.is_dir():
        return None
    return _walk_slug_match(root, parts[1:])


def _resolve_project_cwd(project_dir: str, stored_cwd: str | None) -> str | None:
    """Pick the best current-machine cwd for a project.

    Prefer a slug→real-path resolution (accurate after folder moves), then
    fall back to the stored cwd if it still exists, else just the stored cwd."""
    real = _slug_to_real_path(project_dir)
    if real:
        return real
    if stored_cwd and os.path.isdir(stored_cwd):
        return stored_cwd
    return stored_cwd


def _chip_short(project_dir: str, fallback_cwd: str | None) -> str:
    slug_short = _shorten_project(project_dir)
    if slug_short in ("~", "~[lin]", "~[win]", "~[mac]"):
        return slug_short
    real = _slug_to_real_path(project_dir)
    if real:
        return _shorten_project(project_dir, real)
    if fallback_cwd:
        return _shorten_project(project_dir, fallback_cwd)
    return slug_short


@app.route("/api/projects")
def api_projects():
    raw = list_projects()
    this_dev = _current_device()
    groups: dict[str, dict] = {}
    for p in raw:
        short = _chip_short(p["project_dir"], p.get("cwd"))
        g = groups.get(short)
        if g is None:
            g = {
                "short": short,
                "project_dirs": [],
                "devices": [],
                "cwd": None,
                "project_dir": p["project_dir"],
                "chat_count": 0,
                "latest": p.get("latest"),
            }
            groups[short] = g
        g["project_dirs"].append(p["project_dir"])
        dev = p.get("device")
        if dev and dev not in g["devices"]:
            g["devices"].append(dev)
        g["chat_count"] += p.get("chat_count") or 0
        if (p.get("latest") or "") > (g.get("latest") or ""):
            g["latest"] = p.get("latest")
        # Prefer a cwd/project_dir from the current device so "new chat" lands locally.
        if dev == this_dev or g["cwd"] is None:
            resolved = _resolve_project_cwd(p["project_dir"], p.get("cwd"))
            if resolved:
                g["cwd"] = resolved
            if dev == this_dev:
                g["project_dir"] = p["project_dir"]
    out = sorted(groups.values(), key=lambda g: g.get("latest") or "", reverse=True)
    return jsonify(out)


@app.route("/api/conversation/<session_id>")
def api_conversation(session_id):
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "Not found"}), 404

    file_path = Path(session["file_path"])
    messages = []
    if file_path.exists():
        for msg in parse_full(file_path):
            entry = {"role": msg.role, "text": msg.text}
            if msg.tool_name:
                entry["tool_name"] = msg.tool_name
                entry["tool_input"] = msg.tool_input or ""
            messages.append(entry)

    cwd = _get_session_cwd(session)

    return jsonify({
        "session_id": session["session_id"],
        "title": session.get("display_title", "Untitled"),
        "date": (session.get("first_timestamp") or "")[:16].replace("T", " "),
        "cwd": cwd,
        "input_tokens": session.get("input_tokens") or 0,
        "output_tokens": session.get("output_tokens") or 0,
        "cache_read_tokens": session.get("cache_read_tokens") or 0,
        "cache_create_tokens": session.get("cache_create_tokens") or 0,
        "messages": messages,
    })


@app.route("/api/star/<session_id>", methods=["POST"])
def api_star(session_id):
    try:
        starred = toggle_star(session_id)
        return jsonify({"starred": starred})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/done/<session_id>", methods=["POST"])
def api_done(session_id):
    from core.indexer import toggle_done
    try:
        done = toggle_done(session_id)
        return jsonify({"ok": True, "done": done})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/bulk-done", methods=["POST"])
def api_bulk_done():
    from core.indexer import toggle_done
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    mark = data.get("done")  # true/false/None (None = toggle each based on current state)
    count = 0
    for sid in ids:
        try:
            cur = get_session(sid)
            if cur is None:
                continue
            desired = mark if mark is not None else (not cur.get("is_done"))
            if bool(cur.get("is_done")) == bool(desired):
                continue
            toggle_done(sid)
            count += 1
        except Exception:
            pass
    return jsonify({"ok": True, "count": count})


@app.route("/api/session/<session_id>", methods=["DELETE"])
def api_delete_session(session_id):
    try:
        path = delete_session(session_id)
        return jsonify({"ok": True, "path": path})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/sessions/bulk-delete", methods=["POST"])
def api_bulk_delete():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    deleted = []
    errors = []
    for sid in ids:
        try:
            delete_session(sid)
            deleted.append(sid)
        except Exception as e:
            errors.append({"id": sid, "error": str(e)})
    return jsonify({"deleted": deleted, "errors": errors})


@app.route("/api/rename/<session_id>", methods=["POST"])
def api_rename(session_id):
    data = request.get_json(silent=True) or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "Title required"}), 400
    try:
        set_title(session_id, title)
        return jsonify({"ok": True, "title": title})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/retitle/<session_id>", methods=["POST"])
def api_retitle(session_id):
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "Not found"}), 404

    items = [{
        "id": session["session_id"],
        "first_message": session.get("first_message", ""),
    }]
    titles = generate_titles_batch(items)
    title = titles.get(session["session_id"])
    if title:
        set_title(session["session_id"], title)
        return jsonify({"ok": True, "title": title})
    return jsonify({"ok": False, "error": "Title generation failed"}), 500


@app.route("/api/retitle-bulk", methods=["POST"])
def api_retitle_bulk():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "No IDs provided"}), 400

    items = []
    for sid in ids:
        s = get_session(sid)
        if s:
            items.append({
                "id": s["session_id"],
                "first_message": s.get("first_message", ""),
            })

    if not items:
        return jsonify({"error": "No valid sessions"}), 404

    titles = generate_titles_batch(items)
    results = {}
    for sid, title in titles.items():
        try:
            set_title(sid, title)
            results[sid] = title
        except Exception:
            pass

    return jsonify({"ok": True, "titles": results, "count": len(results)})


@app.route("/api/resume/<session_id>", methods=["POST"])
def api_resume(session_id):
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "Not found"}), 404

    cwd = resolve_session_cwd(_get_session_cwd(session))
    sid = session["session_id"]
    ensure_session_visible(sid, session.get("project_dir", ""), cwd)

    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "cmd", "/k",
                 f'cd /d "{cwd}" && claude --dangerously-skip-permissions -r {sid}'],
                shell=False,
            )
        else:
            subprocess.Popen(
                ["x-terminal-emulator", "-e", "bash", "-c",
                 f'cd "{cwd}" && claude --dangerously-skip-permissions -r {sid}; bash'],
                start_new_session=True,
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Server stays up — auto-shutdown disabled while we debug resume
    return jsonify({"ok": True, "cwd": cwd})


# ---------------------------------------------------------------------------
# Inline terminal (PTY over WebSocket) — "resume in-place" without an external term
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# File tree — git-tracked (or fallback to filesystem walk)
# ---------------------------------------------------------------------------

_FS_IGNORE = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", ".next", "target", ".cache", ".pytest_cache",
              ".mypy_cache", "egg-info", ".turbo"}


def _fallback_walk(root: str, max_files: int = 5000) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _FS_IGNORE and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/")
            out.append(rel)
            if len(out) >= max_files:
                return out
    return out


def _build_tree(cwd: str) -> dict:
    """Return the file tree rooted at cwd's git repo (or cwd itself if not a repo)."""
    is_git = False
    repo_root = cwd
    try:
        p = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
        if p.returncode == 0 and p.stdout.strip():
            repo_root = p.stdout.strip()
            is_git = True
    except Exception:
        pass

    files: list[str] = []
    if is_git:
        try:
            p = subprocess.run(
                ["git", "-C", repo_root, "ls-files",
                 "--cached", "--others", "--exclude-standard"],
                capture_output=True, text=True, timeout=10,
            )
            if p.returncode == 0:
                files = [line for line in p.stdout.splitlines() if line.strip()]
        except Exception:
            pass
    if not files:
        files = _fallback_walk(repo_root)

    root_node: dict = {"name": Path(repo_root).name or repo_root,
                       "path": "", "type": "dir", "children": {}}
    for rel in files:
        parts = rel.split("/")
        node = root_node
        for i, p in enumerate(parts):
            is_last = i == len(parts) - 1
            if p not in node["children"]:
                if is_last:
                    node["children"][p] = {
                        "name": p, "path": "/".join(parts[: i + 1]), "type": "file",
                    }
                else:
                    node["children"][p] = {
                        "name": p, "path": "/".join(parts[: i + 1]),
                        "type": "dir", "children": {},
                    }
            if not is_last:
                node = node["children"][p]

    def finalize(n: dict):
        if "children" in n:
            kids = list(n["children"].values())
            kids.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
            for k in kids:
                finalize(k)
            n["children"] = kids
        return n

    finalize(root_node)
    return {
        "root_path": repo_root,
        "root_name": Path(repo_root).name or repo_root,
        "is_git": is_git,
        "tree": root_node,
    }


@app.route("/api/files")
def api_files():
    raw_cwd = (request.args.get("cwd") or "").strip()
    cwd = resolve_session_cwd(raw_cwd)
    if not os.path.isdir(cwd):
        return jsonify({"error": "cwd not found"}), 404
    return jsonify(_build_tree(cwd))


# ---------------------------------------------------------------------------
# Inline terminal spawn
# ---------------------------------------------------------------------------

@app.route("/api/spawn-terminal", methods=["POST"])
def api_spawn_terminal():
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    cols = int(data.get("cols") or 100)
    rows = int(data.get("rows") or 30)

    if session_id:
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        cwd = resolve_session_cwd(_get_session_cwd(session))
        sid = session["session_id"]
        ensure_session_visible(sid, session.get("project_dir", ""), cwd)
        argv = ["claude", "--dangerously-skip-permissions", "-r", sid]
    else:
        raw_cwd = (data.get("cwd") or "").strip()
        cwd = resolve_session_cwd(raw_cwd)
        argv = ["claude", "--dangerously-skip-permissions"]

    try:
        tid = pty_terminal.spawn(argv, cwd=cwd, cols=cols, rows=rows)
    except FileNotFoundError:
        return jsonify({"error": "claude CLI not found on PATH"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "terminal_id": tid, "cwd": cwd})


@app.route("/api/kill-terminal/<tid>", methods=["POST"])
def api_kill_terminal(tid):
    pty_terminal.kill(tid)
    return jsonify({"ok": True})


_UPLOAD_DIR = Path("/tmp/serena-chats-uploads")
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MB

_SAFE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic", ".heif", ".tiff"}


@app.route("/api/upload-image", methods=["POST"])
def api_upload_image():
    """Save a dropped image to a temp path so the CLI in the PTY can read it."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file"}), 400

    raw = f.read(_UPLOAD_MAX_BYTES + 1)
    if len(raw) > _UPLOAD_MAX_BYTES:
        return jsonify({"error": f"File exceeds {_UPLOAD_MAX_BYTES // (1024 * 1024)} MB"}), 413

    ext = Path(f.filename).suffix.lower()
    if ext not in _SAFE_EXT:
        ext = ".png"

    dest = _UPLOAD_DIR / f"{uuid4().hex}{ext}"
    dest.write_bytes(raw)
    try:
        os.chmod(dest, 0o644)
    except OSError:
        pass

    return jsonify({"ok": True, "path": str(dest)})


@sock.route("/ws/terminal/<tid>")
def ws_terminal(ws, tid):
    """Bidirectional PTY <-> browser stream.

    Frontend sends either a raw string (user keystrokes) or a JSON object
    ``{"resize": {"rows": R, "cols": C}}``. Backend pushes raw output strings.
    """
    if not pty_terminal.get(tid):
        ws.send(json.dumps({"error": "Terminal not found"}))
        ws.close()
        return

    stop_reader = threading.Event()

    def reader():
        while not stop_reader.is_set():
            chunk = pty_terminal.read_available(tid, max_bytes=8192, timeout=0.05)
            if chunk is None:
                try:
                    ws.send(json.dumps({"exit": True}))
                except Exception:
                    pass
                break
            if chunk:
                try:
                    ws.send(chunk.decode("utf-8", errors="replace"))
                except Exception:
                    break

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            if isinstance(msg, bytes):
                pty_terminal.write(tid, msg)
                continue
            # Text frame — attempt JSON first (resize control), fallback to raw input
            if msg.startswith("{"):
                try:
                    payload = json.loads(msg)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and "resize" in payload:
                    r = payload["resize"]
                    pty_terminal.resize(tid, int(r.get("rows", 30)), int(r.get("cols", 100)))
                    continue
                if isinstance(payload, dict) and "input" in payload:
                    pty_terminal.write(tid, payload["input"].encode("utf-8"))
                    continue
            pty_terminal.write(tid, msg.encode("utf-8"))
    finally:
        stop_reader.set()
        pty_terminal.kill(tid)


@app.route("/api/new-chat", methods=["POST"])
def api_new_chat():
    data = request.get_json(silent=True) or {}
    cwd = resolve_session_cwd(data.get("cwd", "").strip())

    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/c", "start", "", "cmd", "/k",
                 f'cd /d "{cwd}" && claude --dangerously-skip-permissions'],
                shell=False,
            )
        else:
            subprocess.Popen(
                ["x-terminal-emulator", "-e", "bash", "-c",
                 f'cd "{cwd}" && claude --dangerously-skip-permissions; bash'],
                start_new_session=True,
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Server stays up — auto-shutdown disabled while we debug resume
    return jsonify({"ok": True, "cwd": cwd})


# ---------------------------------------------------------------------------
# Memory API (filesystem-based)
# ---------------------------------------------------------------------------

@app.route("/api/memory")
def api_memory():
    return jsonify(_list_all_memories())


@app.route("/api/memory", methods=["POST"])
def api_memory_add():
    data = request.get_json(silent=True) or {}
    content = data.get("content", "").strip()
    mem_type = data.get("type", "general").strip().lower()
    if not content:
        return jsonify({"error": "Content required"}), 400
    if mem_type not in MEMORY_TYPES:
        return jsonify({"error": f"Invalid type. Must be one of: {', '.join(MEMORY_TYPES)}"}), 400

    mem_id = _next_memory_id()
    _write_memory_file(mem_id, mem_type, content)
    _update_memory_index()
    return jsonify({"ok": True, "id": mem_id})


@app.route("/api/memory/<int:mem_id>", methods=["PUT"])
def api_memory_update(mem_id):
    data = request.get_json(silent=True) or {}
    content = data.get("content", "").strip()
    mem_type = data.get("type", "").strip().lower()

    fpath = _find_memory_path(mem_id)
    if not fpath:
        return jsonify({"error": "Memory not found"}), 404

    # Read existing to preserve created date
    existing = _parse_memory_file(fpath)
    if not existing:
        return jsonify({"error": "Could not parse memory"}), 500

    if not content:
        content = existing["content"]
    if not mem_type:
        mem_type = existing["type"]
    if mem_type not in MEMORY_TYPES:
        return jsonify({"error": f"Invalid type"}), 400

    # Delete old file
    fpath.unlink()

    # Write updated
    _write_memory_file(mem_id, mem_type, content, created=existing["created"])
    _update_memory_index()
    return jsonify({"ok": True})


@app.route("/api/memory/<int:mem_id>", methods=["DELETE"])
def api_memory_delete(mem_id):
    fpath = _find_memory_path(mem_id)
    if not fpath:
        return jsonify({"error": "Memory not found"}), 404
    fpath.unlink()
    _update_memory_index()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Knowledge API
# ---------------------------------------------------------------------------

@app.route("/api/knowledge")
def api_knowledge():
    topics = list_knowledge_topics()
    return jsonify(topics)


@app.route("/api/knowledge/<slug>")
def api_knowledge_topic(slug):
    files = get_topic_files(slug)
    sessions = get_topic_sessions(slug)
    return jsonify({"files": files, "sessions": sessions})


@app.route("/api/knowledge/<slug>/<filename>")
def api_knowledge_file(slug, filename):
    content = get_file_content(slug, filename)
    return jsonify({"content": content})


@app.route("/api/usage")
def api_usage():
    raw = (request.args.get("range") or "all").lower()
    if raw in ("all", ""):
        range_days = None
    elif raw.endswith("d") and raw[:-1].isdigit():
        range_days = int(raw[:-1])
    elif raw.isdigit():
        range_days = int(raw)
    else:
        range_days = None
    return jsonify(get_usage_stats(range_days))


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    threading.Timer(0.3, _shutdown_server).start()
    return jsonify({"ok": True})


def _shutdown_server():
    """Kill browser and exit."""
    global _browser_pid
    if _browser_pid:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(_browser_pid)],
                               capture_output=True, timeout=5)
            else:
                os.kill(_browser_pid, signal.SIGTERM)
        except Exception:
            pass
        _browser_pid = None
    os._exit(0)


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_web(host="0.0.0.0", port=8080, open_browser=False):
    """Start the web server."""
    global _browser_pid

    print("Updating session index...")
    update_index()
    print("Updating knowledge index...")
    update_knowledge_index()

    url = f"http://localhost:{port}" if host == "0.0.0.0" else f"http://{host}:{port}"
    print(f"Starting web UI at {url}")

    if open_browser:
        try:
            if sys.platform == "win32":
                # Try Edge in app mode first
                edge_paths = [
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                ]
                edge_exe = None
                for p in edge_paths:
                    if Path(p).exists():
                        edge_exe = p
                        break

                if edge_exe:
                    proc = subprocess.Popen(
                        [edge_exe, f"--app={url}", "--start-maximized"],
                    )
                    _browser_pid = proc.pid
                else:
                    import webbrowser
                    webbrowser.open(url)
            else:
                # Linux: prefer a chromium-based browser in app mode so it
                # launches as a standalone window rather than a regular tab.
                edge_candidates = [
                    "/usr/bin/microsoft-edge",
                    "/usr/bin/microsoft-edge-stable",
                    "/usr/bin/google-chrome",
                    "/usr/bin/chromium",
                    "/usr/bin/chromium-browser",
                    "/usr/bin/brave-browser",
                ]
                edge_exe = next((p for p in edge_candidates if Path(p).exists()), None)
                if edge_exe:
                    proc = subprocess.Popen(
                        [edge_exe, f"--app={url}", "--new-window"],
                        start_new_session=True,
                    )
                    _browser_pid = proc.pid
                else:
                    import webbrowser
                    webbrowser.open(url)
        except Exception:
            pass

    app.run(host=host, port=port, debug=False, threaded=True)
