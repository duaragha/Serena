"""Terminal-style web UI for browsing Claude Code conversations, memories, and knowledge."""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request

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
)
from core.config import ensure_session_visible, resolve_session_cwd
from knowledge.reader import get_topic_content, get_file_content, get_topic_files
from core.parser import parse_full
from chats.llm_titles import generate_titles_batch

app = Flask(__name__)

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
    """Convert slugified project dir to readable short path."""
    if cwd:
        # Use cwd for more accurate path display
        path = cwd
    else:
        # Decode from slugified dir name
        if project.startswith("C--"):
            path = project.replace("C--", "C:\\", 1).replace("-", "\\")
        elif project.startswith("-home-") or project.startswith("-root-"):
            path = "/" + project[1:].replace("-", "/")
        else:
            path = project

    # Normalize to forward slashes for consistent matching
    norm = path.replace("\\", "/").rstrip("/")

    # Shorten known prefixes (longest first to match most specific)
    home_prefixes = [
        ("C:/Users/ragha/Projects/", ""),
        ("/home/raghav/Documents/Projects/", ""),
        ("/home/raghav/Projects/", ""),
        ("C:/Users/ragha/", "~[win]/"),
        ("/home/raghav/", "~[lin]/"),
        ("C:/Users/ragha", "~[win]"),
        ("/home/raghav", "~[lin]"),
    ]
    for prefix, replacement in home_prefixes:
        p = prefix.rstrip("/")
        if norm == p:
            # Exact match: return the OS-tagged home if that's what it is, else "~"
            return replacement.rstrip("/") or "~"
        if norm.startswith(prefix):
            return replacement + norm[len(prefix):]

    # Generic fallback: show last 2 path segments
    parts = norm.split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return norm


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
  width: 40%;
  min-width: 280px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
  overflow: hidden;
}
.panel-right {
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

/* ── Filter Chips ── */
.filter-bar {
  display: flex;
  gap: 4px;
  padding: 6px 12px;
  overflow-x: auto;
  flex-shrink: 0;
  border-bottom: 1px solid var(--border);
}
.chip {
  flex-shrink: 0;
  padding: 3px 10px;
  border-radius: 3px;
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-dim);
  font-size: 11px;
  font-family: var(--mono);
  cursor: pointer;
  transition: all 0.15s;
  user-select: none;
  white-space: nowrap;
}
.chip:hover { color: var(--text); border-color: var(--border-bright); }
.chip.active { color: var(--green); border-color: var(--green); background: var(--green-dim); }

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

/* ── Conversation View ── */
.conv-header {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  flex-shrink: 0;
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
    <div class="tab-spacer"></div>
    <button class="tab-action" onclick="shutdownServer()" title="Shutdown server">Quit</button>
  </div>

  <!-- ═══ CHATS VIEW ═══ -->
  <div class="main" id="viewChats">
    <div class="panel-left">
      <div class="search-bar">
        <input type="text" id="searchInput" placeholder="Search conversations... ( / )" autocomplete="off">
      </div>
      <div class="filter-bar" id="filterBar"></div>
      <div class="selection-info hidden" id="selectionInfo">
        <span id="selectionText">0 selected</span>
        <div class="selection-actions">
          <button class="sel-btn" onclick="bulkRetitle()">AI Title</button>
          <button class="sel-btn danger" onclick="bulkDelete()">Delete</button>
        </div>
      </div>
      <div class="session-list" id="sessionList"></div>
    </div>
    <div class="panel-right" id="convPanel">
      <div class="panel-right-empty" id="convEmpty">Select a conversation</div>
      <div class="hidden" id="convContent">
        <div class="conv-header">
          <h2 id="convTitle"></h2>
          <div class="meta" id="convMeta"></div>
        </div>
        <div class="conv-body" id="convBody"></div>
      </div>
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

// ═══════════════════════════════════════════════════════════════
// Tab Switching
// ═══════════════════════════════════════════════════════════════
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.getElementById('viewChats').classList.toggle('hidden', tab !== 'chats');
  document.getElementById('viewMemory').classList.toggle('hidden', tab !== 'memory');
  document.getElementById('viewKnowledge').classList.toggle('hidden', tab !== 'knowledge');
  updateShortcutBar();
  if (tab === 'memory') { if (memories.length) renderMemoryList(); else loadMemories(); }
  if (tab === 'knowledge') { if (topics.length) renderTopicList(); else loadTopics(); }
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
  return (s.input_tokens || 0) + (s.output_tokens || 0) + (s.cache_read_tokens || 0) + (s.cache_create_tokens || 0);
}

// ═══════════════════════════════════════════════════════════════
// CHATS: Data Loading
// ═══════════════════════════════════════════════════════════════
async function loadSessions(project) {
  currentProject = project || null;
  const params = new URLSearchParams();
  if (project) params.set('project', project);
  try {
    const r = await fetch('/api/sessions?' + params);
    allSessions = await r.json();
    sessions = allSessions;
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

async function loadProjects() {
  try {
    const r = await fetch('/api/projects');
    const projects = await r.json();
    const bar = document.getElementById('filterBar');
    let html = '<div class="chip active" onclick="filterProject(null, this)">All</div>';
    for (const p of projects) {
      html += '<div class="chip" onclick="filterProject(\'' + esc(p.project_dir).replace(/'/g, "\\'") + '\', this)">' + esc(p.short) + '</div>';
    }
    bar.innerHTML = html;
  } catch(e) {
    console.error('loadProjects:', e);
  }
}

function filterProject(p, el) {
  document.querySelectorAll('.filter-bar .chip').forEach(c => c.classList.remove('active'));
  if (el) el.classList.add('active');
  else document.querySelector('.filter-bar .chip').classList.add('active');
  loadSessions(p);
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
    return;
  }

  // Separate starred from non-starred
  const starred = sessions.filter(s => s.starred);
  const unstarred = sessions.filter(s => !s.starred);

  let html = '';
  let globalIdx = 0;

  // Starred section
  if (starred.length) {
    html += '<div class="group-header starred-header">\u2605 Starred</div>';
    for (const s of starred) {
      html += renderSessionRow(s, globalIdx);
      globalIdx++;
    }
  }

  // Unstarred, grouped by date
  let group = null;
  for (const s of unstarred) {
    const g = timeGroup(s.last_timestamp || s.first_timestamp);
    if (g !== group) {
      group = g;
      html += '<div class="group-header">' + esc(g) + '</div>';
    }
    html += renderSessionRow(s, globalIdx);
    globalIdx++;
  }

  el.innerHTML = html;

  // Restore focus
  if (focusedIndex >= 0 && focusedIndex < sessions.length) {
    const rows = el.querySelectorAll('.session-row');
    if (rows[focusedIndex]) rows[focusedIndex].classList.add('focused');
  }
}

function renderSessionRow(s, idx) {
  const isFocused = idx === focusedIndex;
  const isSelected = selectedIds.has(s.session_id);
  let cls = 'session-row';
  if (isFocused) cls += ' focused';
  if (isSelected) cls += ' selected';

  const tokens = totalTokens(s);
  const starCls = s.starred ? 'session-star starred' : 'session-star';
  const starChar = s.starred ? '\u2605' : '\u2606';

  return '<div class="' + cls + '" data-idx="' + idx + '" data-sid="' + s.session_id + '" '
    + 'onclick="onRowClick(event,' + idx + ')" ondblclick="openConv(\'' + s.session_id + '\')">'
    + '<span class="' + starCls + '" onclick="event.stopPropagation();toggleStar(\'' + s.session_id + '\')">' + starChar + '</span>'
    + '<span class="session-title">' + esc(s.display_title || 'Untitled') + '</span>'
    + '<span class="session-project">' + esc(s.project_short || '') + '</span>'
    + '<span class="session-tokens">' + formatTokens(tokens) + '</span>'
    + '<span class="session-date">' + formatDate(s.last_timestamp || s.first_timestamp) + '</span>'
    + '</div>';
}

// ═══════════════════════════════════════════════════════════════
// CHATS: Focus & Selection
// ═══════════════════════════════════════════════════════════════
function setFocus(idx, scroll) {
  if (idx < 0 || idx >= sessions.length) return;
  focusedIndex = idx;
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
  currentSessionId = sid;
  document.getElementById('convEmpty').classList.add('hidden');
  document.getElementById('convContent').classList.remove('hidden');
  document.getElementById('convBody').innerHTML = '<div class="loading-text">Loading...</div>';

  // Focus the row
  const idx = sessions.findIndex(s => s.session_id === sid);
  if (idx >= 0) setFocus(idx, true);

  try {
    const r = await fetch('/api/conversation/' + sid);
    const data = await r.json();

    document.getElementById('convTitle').textContent = data.title || 'Untitled';
    const tokens = (data.input_tokens || 0) + (data.output_tokens || 0) + (data.cache_read_tokens || 0) + (data.cache_create_tokens || 0);
    document.getElementById('convMeta').textContent =
      (data.date || '') + '  \u00b7  ' + formatTokens(tokens) + ' tokens' + (data.cwd ? '  \u00b7  ' + data.cwd : '');

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
  } catch(e) {
    document.getElementById('convBody').innerHTML = '<div class="empty-text">Error loading conversation</div>';
  }
}

function closeConv() {
  currentSessionId = null;
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
  if (!confirm('Delete this conversation? This cannot be undone.')) return;
  try {
    await fetch('/api/session/' + sid, { method: 'DELETE' });
    if (currentSessionId === sid) closeConv();
    await loadSessions(currentProject);
  } catch(e) {}
}

async function bulkDelete() {
  if (selectedIds.size === 0) return;
  if (!confirm('Delete ' + selectedIds.size + ' conversations? This cannot be undone.')) return;
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
  const title = prompt('Rename conversation:', current);
  if (title === null || title.trim() === '') return;
  try {
    await fetch('/api/rename/' + sid, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title.trim() }),
    });
    await loadSessions(currentProject);
    if (currentSessionId === sid) {
      document.getElementById('convTitle').textContent = title.trim();
    }
  } catch(e) {}
}

async function retitleSession(sid) {
  try {
    const r = await fetch('/api/retitle/' + sid, { method: 'POST' });
    const data = await r.json();
    if (data.title) {
      await loadSessions(currentProject);
      if (currentSessionId === sid) {
        document.getElementById('convTitle').textContent = data.title;
      }
    }
  } catch(e) {}
}

async function bulkRetitle() {
  if (selectedIds.size === 0) return;
  try {
    const r = await fetch('/api/retitle-bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: Array.from(selectedIds) }),
    });
    const data = await r.json();
    selectedIds.clear();
    updateSelectionInfo();
    await loadSessions(currentProject);
  } catch(e) {}
}

async function resumeSession(sid) {
  try {
    const r = await fetch('/api/resume/' + sid, { method: 'POST' });
    const data = await r.json();
  } catch(e) {}
}

async function newChat() {
  const cwd = prompt('Working directory (leave empty for home):');
  try {
    const body = cwd && cwd.trim() ? { cwd: cwd.trim() } : {};
    await fetch('/api/new-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch(e) {}
}

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
  if (!confirm('Delete memory #' + id + '?')) return;
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
function updateShortcutBar() {
  const bar = document.getElementById('shortcutBar');
  let shortcuts = [];

  if (currentTab === 'chats') {
    shortcuts = [
      ['\u2191\u2193', 'navigate'], ['Enter', 'open'], ['/', 'search'],
      ['o', 'resume'], ['s', 'star'], ['r', 'rename'], ['t', 'AI title'],
      ['n', 'new chat'], ['Del', 'delete'], ['Ctrl+A', 'select all'],
      ['Shift+\u2191\u2193', 'extend sel'], ['Esc', 'deselect'],
    ];
  } else if (currentTab === 'memory') {
    shortcuts = [
      ['\u2191\u2193', 'navigate'], ['a', 'add'], ['e', 'edit'],
      ['Del', 'delete'], ['Esc', 'close'],
    ];
  } else if (currentTab === 'knowledge') {
    shortcuts = [
      ['\u2191\u2193', 'navigate'], ['Enter', 'open'], ['Esc', 'back'],
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
      if (e.key === 'd' || e.key === 'Delete' || e.key === 'Backspace') {
        e.preventDefault();
        if (selectedIds.size > 1) {
          bulkDelete();
        } else {
          deleteSession(sid);
        }
        return;
      }
    }
    if (e.key === 'n') { e.preventDefault(); newChat(); return; }
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
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return HTML


@app.route("/api/sessions")
def api_sessions():
    project = request.args.get("project")
    sessions = list_sessions(project=project, limit=500)
    for s in sessions:
        cwd = _get_session_cwd(s)
        s["project_short"] = _shorten_project(s.get("project_dir", ""), cwd)
        s["input_tokens"] = s.get("input_tokens") or 0
        s["output_tokens"] = s.get("output_tokens") or 0
        s["cache_read_tokens"] = s.get("cache_read_tokens") or 0
        s["cache_create_tokens"] = s.get("cache_create_tokens") or 0
    return jsonify(sessions)


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
            cwd = _get_session_cwd(s)
            s["project_short"] = _shorten_project(s.get("project_dir", ""), cwd)
            s["input_tokens"] = s.get("input_tokens") or 0
            s["output_tokens"] = s.get("output_tokens") or 0
            s["cache_read_tokens"] = s.get("cache_read_tokens") or 0
            s["cache_create_tokens"] = s.get("cache_create_tokens") or 0
            sessions.append(s)
    return jsonify(sessions)


@app.route("/api/projects")
def api_projects():
    projects = list_projects()
    for p in projects:
        p["short"] = _shorten_project(p["project_dir"])
    return jsonify(projects)


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

    # Schedule shutdown after a short delay
    threading.Timer(0.3, _shutdown_server).start()
    return jsonify({"ok": True, "cwd": cwd})


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

    threading.Timer(0.3, _shutdown_server).start()
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
                # Linux: try xdg-open or webbrowser
                import webbrowser
                webbrowser.open(url)
        except Exception:
            pass

    app.run(host=host, port=port, debug=False, threaded=True)
