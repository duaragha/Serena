"""Terminal-style web UI for browsing Claude Code conversations, memories, and knowledge."""

import functools
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from flask import Flask, Response, jsonify, request, stream_with_context
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
from core.config import DATA_DIR, ensure_session_visible, resolve_session_cwd
from core.codex_usage_reader import CodexUsageReader
from core.runtime_activity import TurnActivityReader
from knowledge.reader import get_topic_content, get_file_content, get_topic_files
from core.parser import parse_full
from core.voice_transcripts import VOICE_SESSION_ID
from chats.llm_titles import generate_titles_batch

app = Flask(__name__)
sock = Sock(app)

from core.sidestore_source import sidestore_bp  # noqa: E402
from ui.fleet_web import fleet_bp  # noqa: E402
from ui.operator_web import operator_bp  # noqa: E402
from ui.webhook_web import webhook_bp  # noqa: E402

app.register_blueprint(sidestore_bp)
app.register_blueprint(fleet_bp)
app.register_blueprint(operator_bp)
app.register_blueprint(webhook_bp)


def _is_serena_voice_session(session: dict | None) -> bool:
    return bool(session) and (
        session.get("session_id") == VOICE_SESSION_ID
        or str(session.get("agent") or "").lower() == "serena-voice"
    )

_INDEX_REFRESH_LOCK = threading.Lock()
_INDEX_REFRESH_PROC: subprocess.Popen | None = None
_TERMINAL_SPAWN_LOCK = threading.Lock()
_CONPTY_SPAWN_RETRY_DELAY_SECONDS = 0.12
_CONPTY_TRANSIENT_SEMAPHORE_ERROR = "HRESULT(0x800700BB)"


def _is_pywinpty_panic(error: BaseException) -> bool:
    error_type = type(error)
    return (
        error_type.__module__ == "pyo3_runtime"
        and error_type.__name__ == "PanicException"
    )


def _is_transient_conpty_spawn_panic(error: BaseException) -> bool:
    return (
        _is_pywinpty_panic(error)
        and _CONPTY_TRANSIENT_SEMAPHORE_ERROR in str(error)
    )


def _spawn_terminal_with_recovery(*args, **kwargs) -> str:
    try:
        return pty_terminal.spawn(*args, **kwargs)
    except BaseException as error:
        if not _is_transient_conpty_spawn_panic(error):
            raise
        print(
            "[terminal] transient ConPTY semaphore failure; retrying once",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(_CONPTY_SPAWN_RETRY_DELAY_SECONDS)
        return pty_terminal.spawn(*args, **kwargs)


def _launch_index_refresh_process() -> subprocess.Popen:
    command = (
        "from core.indexer import update_index; "
        "update_index(skip_if_running=True)"
    )
    kwargs = {
        "cwd": str(Path(__file__).resolve().parents[1]),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen([sys.executable, "-c", command], **kwargs)


def _schedule_index_refresh() -> bool:
    """Run the expensive transcript scan outside the UI/PTY process.

    A refresh can spend several seconds parsing active JSONLs. Running it in a
    Flask thread holds the GIL and stalls terminal keystrokes travelling over
    the local WebSocket. The child updates the same WAL-backed SQLite index;
    this request can return the current snapshot immediately.
    """
    global _INDEX_REFRESH_PROC
    with _INDEX_REFRESH_LOCK:
        if _INDEX_REFRESH_PROC is not None and _INDEX_REFRESH_PROC.poll() is None:
            return False
        try:
            _INDEX_REFRESH_PROC = _launch_index_refresh_process()
        except OSError:
            _INDEX_REFRESH_PROC = None
            return False
        return True

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
try:
    from memory.store import MEMORY_TYPES  # single source of truth
except Exception:
    MEMORY_TYPES = ["loop", "feedback", "user", "project", "reference", "general"]


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
        return "Home"

    parts = [p for p in norm.split("/") if p]
    return parts[-1] if parts else norm or "Home"


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
<link rel="stylesheet" href="/static/vendor/xterm/xterm.css">
<link rel="stylesheet" href="/static/operator_workspace.css">
<script src="/static/vendor/xterm/xterm.js"></script>
<script src="/static/vendor/xterm/addon-fit.js"></script>
<script src="/static/vendor/xterm/addon-web-links.js"></script>
<script src="/static/vendor/xterm/addon-webgl.js"></script>
<script src="/static/vendor/xterm/addon-canvas.js"></script>
<script src="/static/terminal_lifecycle.js"></script>
<script src="/static/terminal_links.js"></script>
<style>
/* ── Reset & Vars ── */
:root {
  --bg: #0d0a0c;
  --surface: #151014;
  --surface2: #1c161b;
  --border: #2a2430;
  --border-bright: #393140;
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
  --accent: #e07ba8;
  --menu: #d18cb0;
  --accent-dim: rgba(224,123,168,0.12);
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
  position: relative;
  z-index: 9997;
  display: flex;
  align-items: center;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  padding: 0 12px;
  height: 42px;
  gap: 0;
  overflow: visible;
}
.tab {
  padding: 8px 16px;
  cursor: pointer;
  color: var(--menu);
  font-size: 12px;
  font-family: var(--mono);
  text-transform: uppercase;
  letter-spacing: 1px;
  border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
  user-select: none;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab .count {
  font-size: 10px;
  color: var(--menu);
  margin-left: 4px;
}
.tab.active .count { color: var(--accent); }
.tab-spacer { flex: 1; }
.tab-action {
  padding: 4px 10px;
  cursor: pointer;
  color: var(--menu);
  font-size: 11px;
  font-family: var(--mono);
  border: 1px solid var(--border);
  background: transparent;
  border-radius: 3px;
  transition: all 0.15s;
}
.tab-action:hover { color: var(--accent); border-color: var(--accent); }
.live-usage-ribbon {
  position: relative;
  z-index: 9997;
  min-width: 0;
  overflow: visible;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-dim);
}
.live-usage-compact {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 32px;
  padding: 4px 10px;
  background: rgba(255,255,255,0.025);
  border: 1px solid var(--border);
  border-radius: 6px;
  white-space: nowrap;
  cursor: default;
  transition: border-color 0.15s, background 0.15s;
}
.live-usage-ribbon:hover .live-usage-compact,
.live-usage-ribbon:focus-within .live-usage-compact {
  background: var(--surface2);
  border-color: var(--accent);
}
.live-usage-label {
  color: #b7a9b7;
  font-size: 11px;
  letter-spacing: 0;
}
.live-usage-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.live-usage-chip.claude { color: #f29b6f; }
.live-usage-chip.codex { color: #9dc5ff; }
.live-usage-chip.waiting { color: var(--text-dim); opacity: 0.75; }
.live-usage-chip.stale { opacity: 0.58; }
.live-usage-dial {
  --pct: 0;
  --dial-color: var(--green);
  position: relative;
  width: 20px;
  height: 20px;
  border-radius: 999px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  background: conic-gradient(var(--dial-color) calc(var(--pct) * 1%), rgba(255,255,255,0.08) 0);
}
.live-usage-dial::after {
  content: "";
  position: absolute;
  inset: 3px;
  border-radius: inherit;
  background: var(--surface);
  border: 1px solid rgba(255,255,255,0.06);
}
.live-usage-dial span {
  position: relative;
  z-index: 1;
  color: var(--text-bright);
  font-size: 8px;
  font-weight: 700;
  line-height: 1;
}
.live-usage-chip .live-usage-pct {
  color: inherit;
  font-weight: 700;
  min-width: 34px;
  text-align: right;
}
.live-usage-popover {
  display: none;
  position: absolute;
  top: calc(100% + 7px);
  right: 0;
  width: 360px;
  z-index: 9997;
  padding: 10px;
  background: rgba(18,13,18,0.99);
  border: 1px solid rgba(224,123,168,0.22);
  border-radius: 8px;
  box-shadow: 0 14px 40px rgba(0,0,0,0.55);
}
.live-usage-ribbon:hover .live-usage-popover,
.live-usage-ribbon:focus-within .live-usage-popover {
  display: block;
}
.gtk-shell .live-usage-popover { display: none !important; }
.live-usage-popover::before {
  content: "";
  position: absolute;
  top: -8px;
  right: 18px;
  width: 14px;
  height: 14px;
  transform: rotate(45deg);
  background: rgba(18,13,18,0.99);
  border-left: 1px solid rgba(224,123,168,0.22);
  border-top: 1px solid rgba(224,123,168,0.22);
}
.live-usage-card {
  position: relative;
  display: grid;
  gap: 8px;
  padding: 9px;
  background: #181219;
  border: 1px solid rgba(255,255,255,0.13);
  border-radius: 6px;
}
.live-usage-card.stale {
  border-color: rgba(255,255,255,0.12);
  opacity: 0.72;
}
.live-usage-card + .live-usage-card { margin-top: 8px; }
.live-usage-card-head {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: baseline;
}
.live-usage-name {
  font-weight: 700;
  font-size: 11px;
}
.live-usage-name.claude { color: #ff9f72; }
.live-usage-name.codex { color: #a7caff; }
.live-usage-meta {
  color: #b4a8b8;
  font-size: 9px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.live-usage-window {
  display: grid;
  grid-template-columns: 32px 1fr 42px 62px;
  align-items: center;
  gap: 7px;
}
.live-usage-window-label {
  color: #b9acba;
  font-size: 10px;
}
.live-usage-meter {
  height: 6px;
  border-radius: 999px;
  overflow: hidden;
  background: rgba(255,255,255,0.12);
  border: 1px solid rgba(255,255,255,0.13);
}
.live-usage-fill {
  display: block;
  height: 100%;
  width: 0%;
  border-radius: inherit;
  background: var(--green);
  transition: width 0.25s ease;
}
.live-usage-window .live-usage-pct {
  color: #f4eef5;
  text-align: right;
  font-variant-numeric: tabular-nums;
  font-weight: 700;
}
.live-usage-reset {
  color: #b4a8b8;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.live-usage-empty {
  color: var(--text-dim);
  opacity: 0.75;
}
.live-usage-age {
  margin-top: 8px;
  color: #a99bad;
  font-size: 9px;
  text-align: right;
}
.live-usage-age.stale { color: var(--amber); }
.live-usage-fill.tone-ok { background: #78d88f; }
.live-usage-fill.tone-warn { background: #f2c66d; }
.live-usage-fill.tone-danger { background: #ff6f7a; }
#usageAlertStack {
  position: fixed;
  top: 54px;
  right: 16px;
  width: 360px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 9997;
  pointer-events: none;
}
.usage-alert {
  pointer-events: auto;
  padding: 10px;
  background: rgba(18,13,18,0.99);
  border: 1px solid rgba(224,123,168,0.26);
  border-radius: 8px;
  box-shadow: 0 14px 40px rgba(0,0,0,0.55);
  font-family: var(--mono);
  color: #f4eef5;
  opacity: 0;
  transform: translateY(-6px);
  transition: opacity 0.16s ease, transform 0.16s ease;
}
.usage-alert.visible { opacity: 1; transform: translateY(0); }
.usage-alert.warn { border-color: rgba(242,198,109,0.54); }
.usage-alert.danger { border-color: rgba(255,111,122,0.62); }
.usage-alert.critical {
  border-color: rgba(255,111,122,0.84);
  box-shadow: 0 14px 42px rgba(255,111,122,0.15), 0 14px 40px rgba(0,0,0,0.55);
}
.usage-alert-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}
.usage-alert-name {
  font-weight: 700;
  font-size: 11px;
}
.usage-alert-name.claude { color: #ff9f72; }
.usage-alert-name.codex { color: #a7caff; }
.usage-alert-level {
  color: #f4eef5;
  font-size: 11px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}
.usage-alert-body {
  display: grid;
  grid-template-columns: 42px 1fr 42px 58px;
  align-items: center;
  gap: 7px;
  color: #b9acba;
  font-size: 10px;
}
.usage-alert-body .live-usage-meter { height: 7px; }

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
  flex: 0 0 var(--chats-w, 20%);
  min-width: 220px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.panel-right {
  flex: 1 1 0;
  min-width: 280px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Draggable pane dividers ── */
.pane-divider {
  flex: 0 0 5px;
  position: relative;
  cursor: col-resize;
  background: var(--border);
  user-select: none;
  transition: background 0.12s ease;
  z-index: 5;
}
.pane-divider::after {
  /* center hairline + invisible hit area to make grabbing easier */
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: -3px;
  right: -3px;
}
.pane-divider:hover { background: var(--border-bright); }
.pane-divider.dragging { background: var(--accent); }
body.pane-dragging,
body.pane-dragging * {
  cursor: col-resize !important;
  user-select: none !important;
}
.pane-divider.hidden { display: none; }
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
  flex: 0 0 var(--files-w, 9%);
  min-width: 120px;
  max-width: 480px;
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

/* ── Code-area editor tabs (terminal/split pane + opened files) ── */
.code-tabs {
  display: flex;
  align-items: stretch;
  flex-shrink: 0;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
  overflow-y: hidden;
}
.code-tabs::-webkit-scrollbar { height: 0; }
.code-tab {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 7px 12px;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-dim);
  background: transparent;
  border: none;
  border-right: 1px solid var(--border);
  border-bottom: 2px solid transparent;
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
}
.code-tab:hover { color: var(--text); background: rgba(255,255,255,0.03); }
.code-tab.active { color: var(--text-bright); background: var(--bg); border-bottom-color: var(--accent); }
.code-tab .agent-icon { width: 12px; height: 12px; display: inline-flex; }
.code-tab .agent-icon svg { width: 100%; height: 100%; }
.code-tab .ct-close {
  font-size: 13px; line-height: 1; opacity: 0.55; padding: 0 2px; border-radius: 3px; margin-left: 2px;
}
.code-tab .ct-close:hover { opacity: 1; background: rgba(255,255,255,0.14); }
.code-pane-wrap { flex: 1; min-height: 0; position: relative; }
.code-pane { position: absolute; inset: 0; display: flex; flex-direction: column; }
.code-pane.hidden { display: none; }
.file-pane { background: var(--bg); }
.file-pane-head {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 12px; flex-shrink: 0;
  border-bottom: 1px solid var(--border); background: var(--surface);
  font-family: var(--mono); font-size: 11px; color: var(--text-dim);
}
.file-pane-head .fv-spacer { flex: 1; }
.fv-btn {
  background: var(--surface2); border: 1px solid var(--border); border-radius: 4px;
  color: var(--text-dim); font-family: var(--mono); font-size: 11px;
  padding: 3px 9px; cursor: pointer; flex-shrink: 0;
}
.fv-btn:hover { color: var(--text); border-color: var(--text-dim); }
.file-pane-body { flex: 1; min-height: 0; overflow: auto; background: var(--bg); }
.fv-code {
  display: flex;
  min-height: 100%;
  align-items: stretch;
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.6;
}
.fv-gutter {
  position: sticky;
  left: 0;
  z-index: 1;
  flex-shrink: 0;
  text-align: right;
  padding: 14px 12px 14px 16px;
  color: var(--text-dim);
  background: var(--surface2);
  border-right: 1px solid var(--border);
  user-select: none;
}
.fv-gutter pre { margin: 0; opacity: 0.6; }
.fv-content {
  padding: 14px 18px;
  margin: 0;
  white-space: pre;
  color: var(--text);
  tab-size: 4;
  flex: 1;
}
.fileviewer-empty { padding: 28px; color: var(--text-dim); font-family: var(--mono); font-size: 13px; }

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
.search-bar input:focus { border-color: var(--accent); }
.search-bar input::placeholder { color: var(--text-dim); }
.agent-filter-row {
  display: flex;
  gap: 6px;
  margin-top: 8px;
}
.agent-filter-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  flex: 1;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 4px 8px;
  color: var(--text-dim);
  font-family: var(--mono);
  font-size: 11px;
  cursor: pointer;
  transition: border-color .12s, color .12s, background .12s;
}
.agent-filter-btn:hover { color: var(--text); border-color: var(--text-dim); }
.agent-filter-btn .agent-icon { display: inline-flex; width: 13px; height: 13px; }
.agent-filter-btn .agent-icon svg { width: 100%; height: 100%; }
.agent-filter-btn.claude.active { color: #C15F3C; border-color: rgba(193,95,60,0.7); background: rgba(193,95,60,0.10); }
.agent-filter-btn.codex.active  { color: #b07cff; border-color: rgba(176,124,255,0.7); background: rgba(176,124,255,0.10); }

/* ── Project Sidebar ── */
.project-sidebar {
  flex: 0 0 var(--proj-w, 8%);
  min-width: 88px;
  max-width: 360px;
  overflow-y: auto;
  overflow-x: hidden;
  background: var(--surface);
  padding: 4px 0;
}
.project-item {
  padding: 6px 10px;
  cursor: pointer;
  border-left: 3px solid transparent;
  font-size: 11px;
  font-family: var(--mono);
  color: var(--menu);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: background 0.1s, color 0.1s;
  user-select: none;
}
.project-item:hover { color: var(--text); background: rgba(255,255,255,0.04); }
.project-item.active {
  color: var(--accent);
  border-left-color: var(--accent);
  background: rgba(224,123,168,0.08);
}
.proj-chev {
  display: inline-block;
  width: 10px;
  margin-right: 4px;
  color: var(--menu);
  font-size: 9px;
  cursor: pointer;
  user-select: none;
  text-align: center;
}
.proj-chev:hover { color: var(--text); }
.proj-chev-empty { cursor: default; }
.project-syn { font-style: italic; opacity: 0.85; }
.project-folder { font-weight: 600; }

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
  color: var(--menu);
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
.group-header.fleet-header {
  color: #b07cff;
  cursor: pointer;
  user-select: none;
  transition: color 0.12s;
}
.group-header.fleet-header:hover { color: #ceb5ff; }
.fleet-project-header {
  padding: 3px 10px 3px 22px;
  font-size: 10px;
  letter-spacing: 0.5px;
  color: var(--text-dim);
  font-family: var(--mono);
  text-transform: lowercase;
}
.fleet-project-count { opacity: 0.6; }
.fleet-chats-section.collapsed { display: none; }
.group-header.voice-chats-header {
  color: var(--accent);
  cursor: pointer;
  user-select: none;
  transition: color 0.12s;
}
.group-header.voice-chats-header:hover { color: #f3a6c9; }
.voice-chats-section.collapsed { display: none; }

/* Agent badges (Claude / Codex / Serena) use inline SVG and currentColor. */
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
.agent-icon.serena { color: #f472b6; }
.group-header.serena-header {
  color: #f9a8d4;
  background: linear-gradient(90deg, rgba(244, 114, 182, 0.1), var(--bg) 72%);
}
.session-row.serena-voice {
  background: linear-gradient(90deg, rgba(244, 114, 182, 0.08), transparent 78%);
  border-left: 2px solid rgba(244, 114, 182, 0.62);
}
.session-row.serena-voice:hover,
.session-row.serena-voice.focused {
  background: linear-gradient(90deg, rgba(244, 114, 182, 0.16), rgba(255,255,255,0.025) 78%);
}
.group-header.done-header {
  color: var(--text-dim);
  cursor: pointer;
  user-select: none;
  margin-top: 8px;
  transition: color 0.12s;
}
.group-header.done-header:hover { color: var(--text); }
.done-section.collapsed { display: none; }
.starred-section.collapsed { display: none; }
.time-section.collapsed { display: none; }
.group-header.starred-header,
.group-header.time-header { cursor: pointer; }
.group-header.time-header:hover { color: var(--text); }
.session-row.done .session-title { color: var(--text-dim); }
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
/* === ATTENTION === Visual flag when a chat finishes a turn. Clears on
   focus. The stripe + subtle glow matches the existing visual vocabulary
   (active-terminal uses a similar idiom). Pulses slowly so peripheral
   vision picks it up without being annoying. */
.session-row.needs-attention {
  box-shadow: inset 3px 0 0 #f5a623;
  animation: attention-pulse 2.4s ease-in-out infinite;
}
.session-row.needs-attention .session-title {
  color: #ffb84d;
}
@keyframes attention-pulse {
  0%, 100% { background-color: transparent; }
  50%      { background-color: rgba(245, 166, 35, 0.08); }
}
/* === ATTENTION END === */
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
  color: var(--menu);
  flex-shrink: 0;
  user-select: none;
}
.session-list-header .col-star { width: 16px; flex-shrink: 0; }
.session-list-header .col-title { flex: 1; overflow: hidden; }
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
  border-left-color: var(--accent);
  background: rgba(224,123,168,0.10);
}
.session-row.selected {
  background: rgba(224,123,168,0.16);
}
.session-row.focused.selected {
  border-left-color: var(--accent);
  background: rgba(224,123,168,0.22);
}
.session-row.child-session {
  padding-left: 28px;
  opacity: 0.82;
}
.session-row.child-session:hover,
.session-row.child-session.focused {
  opacity: 1;
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
.session-disclosure {
  flex-shrink: 0;
  width: 12px;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 11px;
  text-align: center;
}
.session-disclosure:hover { color: var(--text); }

/* === GROUP FEATURE === (delete this block + the JS section to remove) */
.session-row.has-group {
  box-shadow: inset 4px 0 0 0 var(--group-color, var(--text-dim));
}
.session-row.focused.has-group {
  box-shadow: inset 4px 0 0 0 var(--group-color, var(--text-dim));
}
/* Thread sibling rows render indented under their head, with the same color
   stripe + subtle background tint so the cluster feels like one unit. */
.session-row.thread-sibling {
  padding-left: 18px;
  background: linear-gradient(
    to right,
    color-mix(in srgb, var(--group-color, transparent) 8%, transparent),
    transparent 40%
  );
  opacity: 0.92;
}
.session-row.thread-sibling:hover {
  opacity: 1;
}
.session-row.thread-head {
  background: linear-gradient(
    to right,
    color-mix(in srgb, var(--group-color, transparent) 8%, transparent),
    transparent 40%
  );
}
.thread-count-badge {
  display: inline-flex;
  align-items: center;
  margin-left: 6px;
  padding: 0 5px;
  height: 14px;
  font-size: 9px;
  font-weight: 600;
  background: color-mix(in srgb, var(--group-color, var(--surface2)) 22%, transparent);
  color: var(--group-color, var(--text-bright));
  border-radius: 7px;
  border: 1px solid color-mix(in srgb, var(--group-color, var(--border)) 50%, transparent);
}
.session-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  margin-right: 4px;
  color: var(--group-color, var(--text-dim));
  cursor: pointer;
  flex-shrink: 0;
  border-radius: 3px;
  transition: background 0.1s;
}
.session-link svg { width: 12px; height: 12px; display: block; }
.session-link:hover {
  background: rgba(255,255,255,0.06);
}
/* Picker modal listing recent chats to link to */
.link-picker {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 320px;
  overflow-y: auto;
  padding: 4px 0;
}
.link-picker-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  color: var(--text);
  background: var(--surface);
  border: 1px solid transparent;
}
.link-picker-row:hover { border-color: var(--border-bright); background: rgba(255,255,255,0.04); }
.link-picker-row.focused { border-color: var(--accent); }
.link-picker-row .lp-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.link-picker-row .lp-meta { font-size: 10px; color: var(--text-dim); }
.link-picker-search {
  width: 100%;
  padding: 8px 10px;
  font-family: var(--mono);
  font-size: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
  margin-bottom: 8px;
}
.link-picker-search:focus { outline: none; border-color: var(--accent); }
.link-picker-empty { padding: 16px; text-align: center; color: var(--text-dim); font-size: 11px; }
/* === GROUP FEATURE END === */
.session-title {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  font-size: 12px;
  color: var(--text);
}
.session-title-main {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.session-snippet {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 400;
}
.session-snippet mark {
  background: var(--accent-dim);
  color: var(--text-bright);
  padding: 0 1px;
  border-radius: 2px;
}
.child-count-badge {
  margin-left: 6px;
  color: var(--text-dim);
  font-size: 10px;
}
.term-machine {
  flex-shrink: 0;
  font-size: 10px;
  font-family: var(--mono);
  letter-spacing: 0.3px;
  padding: 2px 7px;
  border-radius: 4px;
  color: var(--text-dim);
  border: 1px solid var(--border, #2a2a35);
  opacity: 0.85;
}
.term-machine.linux { color: #c9a6ff; border-color: #4a3a6a; }
.term-machine.windows { color: #7fb8ff; border-color: #2f4a6a; }
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
  color: var(--menu);
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
  background: var(--accent-dim);
  color: var(--accent);
}
.view-tab + .view-tab { border-left: 1px solid var(--border-bright); }
.conv-hide {
  margin-left: 8px;
  background: transparent;
  color: var(--menu);
  border: 1px solid var(--border-bright);
  border-radius: 4px;
  padding: 4px 9px;
  font: inherit;
  font-size: 11px;
  cursor: pointer;
  transition: color 0.1s, border-color 0.1s, background 0.1s;
}
.conv-hide:hover {
  color: var(--text);
  border-color: var(--menu);
  background: rgba(255, 255, 255, 0.035);
}
.conv-terminal {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: #000;
  overflow: hidden;
}
.conv-terminal.hidden { display: none; }
.term-statusbar {
  min-height: 29px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 8px 0 12px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  flex-shrink: 0;
}
.term-status {
  padding: 6px 0;
  font-size: 11px;
  color: var(--text-dim);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.term-status.error { color: #f85149; }
.term-status.live { color: var(--green); }
.term-session-ids {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
}
.term-session-ids.hidden { display: none; }
.term-session-id {
  height: 21px;
  padding: 0 6px;
  border: 1px solid var(--border-bright);
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.025);
  color: var(--text-dim);
  font-family: var(--mono);
  font-size: 10px;
  cursor: pointer;
  white-space: nowrap;
}
.term-session-id:hover {
  color: var(--text);
  border-color: var(--menu);
  background: rgba(255, 255, 255, 0.055);
}
.term-session-id.claude { color: #ff967d; }
.term-session-id.codex { color: #8cb4ff; }
.runtime-pin {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 21px;
  padding: 0 7px;
  border: 1px solid var(--border-bright);
  border-radius: 4px;
  background: transparent;
  color: var(--text-dim);
  font-family: var(--mono);
  font-size: 10px;
  cursor: pointer;
  flex-shrink: 0;
}
.runtime-pin:hover { color: var(--text); border-color: var(--menu); }
.runtime-pin.active {
  color: var(--accent);
  border-color: rgba(224, 123, 168, 0.62);
  background: rgba(224, 123, 168, 0.09);
}
.runtime-pin.hidden { display: none; }
.runtime-pin-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
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
  outline: 2px dashed var(--accent);
  outline-offset: -6px;
  background: rgba(224,123,168,0.08);
}
.term-split-divider {
  position: absolute;
  top: 6px;
  bottom: 2px;
  width: 5px;
  z-index: 4;
  cursor: col-resize;
  background: transparent;
}
.term-split-divider::after {
  content: '';
  position: absolute;
  inset: 0 2px;
  background: var(--border);
}
.term-split-divider:hover::after,
.term-split-divider.dragging::after { background: var(--accent); }

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
.modal-input:focus { border-color: var(--accent); }
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
  background: var(--accent-dim);
  color: var(--accent);
  border-color: var(--accent);
}
.modal-btn.primary:hover { background: rgba(224,123,168,0.18); }
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

/* ── Context Menu ── */
.ctx-menu {
  position: fixed;
  z-index: 10000;
  min-width: 180px;
  padding: 4px 0;
  background: var(--surface2);
  border: 1px solid var(--border-bright);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.45);
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text);
  user-select: none;
}
.ctx-menu-item {
  padding: 6px 14px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  white-space: nowrap;
}
.ctx-menu-item:hover { background: rgba(255,255,255,0.06); color: var(--text-bright); }
.ctx-menu-item.danger { color: #f85149; }
.ctx-menu-item.danger:hover { background: rgba(248,81,73,0.14); color: #ff6e63; }
.ctx-menu-item.disabled {
  opacity: 0.4;
  pointer-events: none;
}
.ctx-menu-key {
  font-size: 10px;
  color: var(--text-dim);
  letter-spacing: 0.05em;
}
.ctx-menu-sep {
  height: 1px;
  background: var(--border);
  margin: 4px 0;
}

/* ── Agent picker (used in new-chat modal) ── */
.agent-picker {
  display: flex;
  gap: 6px;
  margin: 0 0 12px 0;
}
.agent-pill {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 10px;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text-dim);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  user-select: none;
  transition: background 0.1s, color 0.1s, border-color 0.1s;
}
.agent-pill:hover { color: var(--text); background: rgba(255,255,255,0.04); }
.agent-pill.active {
  color: var(--text-bright);
  background: rgba(224,123,168,0.12);
  border-color: var(--accent);
}
.agent-pill .agent-icon { width: 14px; height: 14px; }
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
.filepath {
  color: var(--accent);
  text-decoration: underline;
  text-decoration-style: dotted;
  text-underline-offset: 2px;
  cursor: pointer;
  border-radius: 3px;
}
.filepath:hover {
  background: var(--accent-dim);
  text-decoration-style: solid;
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
  color: var(--menu);
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

/* ── Settings-style full-page panels ── */
.settings-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 24px 20px 40px;
}
.settings-wrap {
  width: 100%;
  max-width: 1120px;
  margin: 0 auto;
}
.settings-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.settings-title {
  font-size: 12px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.8px;
}

/* === PERSONA FEATURE === */
.persona-hint { color: var(--text-dim); font-size: 12px; margin: 4px 0 16px; }
.persona-editor { margin-bottom: 22px; }
.persona-head { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
.persona-label { font-weight: 600; color: var(--text-bright); }
.persona-label small { font-weight: 400; color: var(--text-dim); }
.persona-status { color: var(--green); font-size: 12px; margin-left: auto; }
.persona-status.dirty { color: #f5a623; }
.persona-status.fail { color: #f85149; }
.persona-area {
  width: 100%;
  min-height: 320px;
  resize: vertical;
  background: #0d1117;
  color: #c9d1d9;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.5;
  tab-size: 2;
}
.persona-area:focus { outline: none; border-color: var(--accent); }
/* === PERSONA FEATURE END === */

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
  border: 1px solid var(--accent);
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  border-radius: 2px;
}
.sel-btn:hover { background: var(--accent-dim); }
.sel-btn.danger { border-color: var(--red); color: var(--red); }
.sel-btn.danger:hover { background: var(--red-dim); }
</style>
</head>
<body>
<div id="app">
  <!-- Tab Bar -->
  <div class="tab-bar">
    <div class="tab active" data-tab="chats" onclick="switchTab('chats')">Chats <span class="count" id="chatCount"></span></div>
    <div class="tab" data-tab="tasks" onclick="switchTab('tasks')">Tasks <span class="count" id="taskCount"></span></div>
    <div class="tab" data-tab="memory" onclick="switchTab('memory')">Memory <span class="count" id="memoryCount"></span></div>
    <div class="tab" data-tab="knowledge" onclick="switchTab('knowledge')">Knowledge <span class="count" id="knowledgeCount"></span></div>
    <div class="tab" data-tab="fleet" onclick="switchTab('fleet')">Fleet <span class="count" id="fleetCount"></span></div>
    <div class="tab" data-tab="usage" onclick="switchTab('usage')">Usage</div>
    <div class="tab" data-tab="persona" onclick="switchTab('persona')">Persona</div>
    <div class="tab-spacer"></div>
    <div class="live-usage-ribbon" id="liveUsageRibbon" tabindex="0" aria-live="polite">
      <span class="live-usage-empty">usage loading</span>
    </div>
    <button class="tab-action" onclick="shutdownServer()" title="Shutdown server">Quit</button>
  </div>

  <!-- ═══ CHATS VIEW ═══ -->
  <div class="main" id="viewChats">
    <div class="project-sidebar" id="projectSidebar"></div>
    <div class="pane-divider" data-divider="proj-chats" title="Drag to resize"></div>
    <div class="chat-list-col" id="chatListCol">
      <div class="search-bar">
        <input type="text" id="searchInput" placeholder="Search conversations... ( / )" autocomplete="off">
        <div class="agent-filter-row">
          <button class="agent-filter-btn claude" id="filterClaude" onclick="toggleAgentFilter('claude')">
            <span class="agent-icon claude"></span>Claude
          </button>
          <button class="agent-filter-btn codex" id="filterCodex" onclick="toggleAgentFilter('codex')">
            <span class="agent-icon codex"></span>Codex
          </button>
        </div>
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
        <span class="col-date">M.Date</span>
      </div>
      <div class="session-list" id="sessionList"></div>
    </div>
    <div class="pane-divider" data-divider="chats-conv" title="Drag to resize"></div>
    <div class="panel-right" id="convPanel">
      <div class="panel-right-empty" id="convEmpty">
        <!-- === FRONT DOOR FEATURE === Serena as the landing surface. Falls
             back to the old static text outside the GTK shell (fdInit). -->
        <div id="frontDoor" class="hidden">
          <style>
            #convEmpty.fd-on { position: relative; overflow: hidden;
              /* panel-right-empty centers its content; the front door wants
                 the full panel */
              align-items: stretch; justify-content: stretch;
              --fd-primary-rgb: 179, 157, 219;
              --fd-secondary-rgb: 244, 143, 177;
              --fd-warm-rgb: 255, 204, 128;
              --fd-deep-rgb: 94, 74, 138; }
            #frontDoor { display: flex; flex-direction: column; height: 100%;
              width: 100%; max-width: 680px; margin: 0 auto; padding: 20px 20px 24px;
              position: relative; isolation: isolate; }
            #convEmpty.fd-on::before { content: ''; position: absolute; inset: -18%;
              z-index: 0;
              background:
                radial-gradient(ellipse 42% 34% at 22% 30%,
                  rgba(var(--fd-secondary-rgb), .14), transparent 70%),
                radial-gradient(ellipse 48% 38% at 78% 23%,
                  rgba(var(--fd-primary-rgb), .17), transparent 72%),
                radial-gradient(ellipse 38% 30% at 58% 72%,
                  rgba(var(--fd-warm-rgb), .075), transparent 74%),
                radial-gradient(ellipse 70% 52% at 48% 44%,
                  rgba(var(--fd-deep-rgb), .08), transparent 76%);
              filter: blur(24px); opacity: .92; pointer-events: none;
              animation: fdAmbientDrift 20s ease-in-out infinite alternate;
              transition: opacity .65s ease; will-change: transform; }
            #convEmpty.fd-on::after { content: ''; position: absolute; inset: -8%;
              z-index: 0; pointer-events: none; opacity: .38;
              background-image:
                radial-gradient(circle, rgba(var(--fd-primary-rgb), .48) 0 1px,
                  transparent 1.5px),
                radial-gradient(circle, rgba(var(--fd-secondary-rgb), .32) 0 .8px,
                  transparent 1.35px),
                radial-gradient(circle, rgba(var(--fd-warm-rgb), .26) 0 .7px,
                  transparent 1.2px);
              background-size: 83px 97px, 137px 151px, 193px 181px;
              background-position: 11px 19px, 61px 43px, 97px 7px;
              -webkit-mask-image: radial-gradient(ellipse 84% 76% at 50% 44%,
                #000 4%, rgba(0,0,0,.78) 54%, transparent 88%);
              mask-image: radial-gradient(ellipse 84% 76% at 50% 44%,
                #000 4%, rgba(0,0,0,.78) 54%, transparent 88%);
              animation: fdParticleDrift 28s ease-in-out infinite alternate;
              transition: opacity .65s ease; will-change: transform; }
            #fdOrb, #fdGreeting, #fdSub, #fdMessages, #fdInputRow {
              position: relative; z-index: 1; }
            @keyframes fdAmbientDrift {
              0% { transform: translate3d(-2.5%, -1.5%, 0) scale(1.01) rotate(-1deg); }
              48% { transform: translate3d(2%, 1%, 0) scale(1.055) rotate(.7deg); }
              100% { transform: translate3d(-.5%, 3%, 0) scale(1.025) rotate(-.3deg); }
            }
            @keyframes fdParticleDrift {
              0% { transform: translate3d(-8px, 10px, 0); }
              50% { transform: translate3d(7px, -13px, 0); }
              100% { transform: translate3d(13px, 5px, 0); }
            }
            #convEmpty.fd-engaged::before { opacity: .3;
              animation-play-state: paused; }
            #convEmpty.fd-engaged::after { opacity: .08;
              animation-play-state: paused; }
            #fdOrb { width: 84px; height: 84px; border-radius: 50%;
              margin: 40px auto 0; position: relative; flex: none;
              background: radial-gradient(circle at 34% 30%,
                #e8d5ff 0%, #b39ddb 32%, #8e6fc9 58%, #5e4a8a 100%);
              box-shadow: 0 0 34px 6px rgba(179, 157, 219, 0.35),
                          0 0 90px 18px rgba(244, 143, 177, 0.12),
                          inset -8px -10px 22px rgba(30, 15, 60, 0.55);
              animation: fdDormant 5.6s ease-in-out infinite; }
            #fdOrb::after { content: ''; position: absolute; inset: -22px;
              border-radius: 50%; border: 1px solid rgba(179, 157, 219, 0.22);
              animation: fdDormantRing 5.6s ease-in-out infinite; }
            @keyframes fdDormant {
              0%, 100% { transform: scale(1); filter: brightness(.97) saturate(.96); }
              50% { transform: scale(1.022); filter: brightness(1.035) saturate(1); }
            }
            @keyframes fdDormantRing {
              0%, 100% { transform: scale(1); opacity: .38; }
              50% { transform: scale(1.045); opacity: .14; }
            }
            @keyframes fdBreathe {
              0%, 100% { transform: scale(1);
                box-shadow: 0 0 34px 6px rgba(179,157,219,0.35),
                            0 0 90px 18px rgba(244,143,177,0.12),
                            inset -8px -10px 22px rgba(30,15,60,0.55); }
              50% { transform: scale(1.045);
                box-shadow: 0 0 46px 10px rgba(179,157,219,0.5),
                            0 0 110px 26px rgba(244,143,177,0.18),
                            inset -8px -10px 22px rgba(30,15,60,0.55); }
            }
            @keyframes fdRing {
              0%, 100% { transform: scale(1); opacity: 0.7; }
              50% { transform: scale(1.06); opacity: 0.25; }
            }
            /* Thinking: directional motion, a traveling arc, not just a
               faster breath. The rotating conic ring is the state cue. */
            #fdOrb.fd-thinking { animation: fdThinkCore 1.25s ease-in-out infinite; }
            #fdOrb.fd-thinking::after { inset: -18px; border: 0;
              background: conic-gradient(from 0turn, transparent 0 52%,
                rgba(179,157,219,.18) 58%, rgba(244,143,177,.72) 69%,
                rgba(255,204,128,.32) 76%, transparent 87%);
              -webkit-mask: radial-gradient(farthest-side,
                transparent calc(100% - 1.5px), #000 calc(100% - 1px));
              animation: fdThinkOrbit 1.45s linear infinite; }
            #fdOrb.fd-thinking::before { animation: fdThinkGlint 1.25s ease-in-out infinite; }
            @keyframes fdThinkCore {
              0%, 100% { transform: scale(.988); filter: brightness(1) saturate(1); }
              50% { transform: scale(1.038); filter: brightness(1.12) saturate(1.14); }
            }
            @keyframes fdThinkOrbit { to { transform: rotate(1turn); } }
            @keyframes fdThinkGlint {
              0%, 100% { opacity: .58; }
              50% { opacity: .95; }
            }
            /* Responding: one 760ms bloom when the reply lands, amber only
               at the peak, then settle back to dormant. */
            #fdOrb.fd-responding { animation: fdReplyBloom .76s cubic-bezier(.16,1,.3,1) both; }
            #fdOrb.fd-responding::after { border: 1px solid rgba(255,204,128,.58);
              background: none; -webkit-mask: none;
              animation: fdReplyHalo .76s ease-out both; }
            #fdOrb.fd-responding::before { animation: fdReplyGlint .76s ease-out both; }
            @keyframes fdReplyBloom {
              0% { transform: scale(1); filter: brightness(1) saturate(1); }
              28% { transform: scale(1.11); filter: brightness(1.3) saturate(1.12);
                box-shadow: 0 0 52px 12px rgba(179,157,219,.58),
                  0 0 112px 28px rgba(244,143,177,.22),
                  0 0 34px 4px rgba(255,204,128,.18),
                  inset -8px -10px 22px rgba(30,15,60,.42); }
              62% { transform: scale(1.025); }
              100% { transform: scale(1); filter: brightness(1) saturate(1); }
            }
            @keyframes fdReplyHalo {
              0% { transform: scale(.82); opacity: 0; }
              24% { opacity: .82; }
              100% { transform: scale(1.34); opacity: 0; }
            }
            @keyframes fdReplyGlint {
              0% { opacity: .7; transform: rotate(-24deg) scale(1); }
              30% { opacity: 1; transform: rotate(-24deg) scale(1.24); }
              100% { opacity: .78; transform: rotate(-24deg) scale(1); }
            }
            /* Time-of-day ambience: class stamped on #frontDoor by fdInit */
            #convEmpty.fd-morning { --fd-primary-rgb: 255, 204, 128;
              --fd-secondary-rgb: 244, 143, 177; --fd-warm-rgb: 179, 157, 219;
              --fd-deep-rgb: 167, 105, 55; }
            .fd-morning #fdOrb { background: radial-gradient(circle at 34% 30%,
              #ffe9c9 0%, #ffcc80 32%, #e8a04c 58%, #8a5e2a 100%); }
            .fd-morning #fdGreeting { background: linear-gradient(90deg, #ffcc80, #f48fb1, #b39ddb);
              -webkit-background-clip: text; background-clip: text; }
            #convEmpty.fd-evening { --fd-primary-rgb: 244, 143, 177;
              --fd-secondary-rgb: 179, 157, 219; --fd-warm-rgb: 255, 204, 128;
              --fd-deep-rgb: 122, 58, 86; }
            .fd-evening #fdOrb { background: radial-gradient(circle at 34% 30%,
              #ffd9e8 0%, #f48fb1 32%, #c9628f 58%, #7a3a56 100%); }
            .fd-evening #fdGreeting { background: linear-gradient(90deg, #f48fb1, #b39ddb, #ffcc80);
              -webkit-background-clip: text; background-clip: text; }
            #convEmpty.fd-night { --fd-primary-rgb: 121, 134, 203;
              --fd-secondary-rgb: 179, 157, 219; --fd-warm-rgb: 244, 143, 177;
              --fd-deep-rgb: 38, 41, 79; }
            .fd-night #fdOrb { background: radial-gradient(circle at 34% 30%,
              #cfd8ff 0%, #7986cb 32%, #4a5490 58%, #26294f 100%); }
            .fd-night #fdGreeting { background: linear-gradient(90deg, #7986cb, #b39ddb, #f48fb1);
              -webkit-background-clip: text; background-clip: text; }
            #fdMessages::-webkit-scrollbar { width: 6px; }
            #fdMessages::-webkit-scrollbar-thumb { background: rgba(179, 157, 219, 0.22);
              border-radius: 3px; }
            #fdMessages::-webkit-scrollbar-thumb:hover { background: rgba(179, 157, 219, 0.4); }
            #fdMessages::-webkit-scrollbar-track { background: transparent; }
            #fdSend { position: absolute; right: 12px; top: 50%;
              transform: translateY(-50%); font-size: 16px; line-height: 1;
              color: rgba(214, 205, 235, 0.25); pointer-events: none;
              transition: color 0.15s, text-shadow 0.15s; }
            #fdSend.fd-ready { color: #d1b3ff;
              text-shadow: 0 0 10px rgba(179, 157, 219, 0.6); }
            /* Hero collapses once the conversation starts */
            #fdOrb, #fdGreeting, #fdSub { transition: width .35s, height .35s,
              margin .35s, font-size .35s, padding .35s; }
            .fd-engaged #fdOrb { width: 54px; height: 54px; margin-top: 12px; }
            .fd-engaged #fdGreeting { font-size: 27px; padding: 14px 0 3px; }
            .fd-engaged #fdSub { font-size: 10px; padding-bottom: 14px; }
            /* Input as a glass dock */
            #fdInputRow { padding: 10px 4px 2px; transition: transform .16s ease; }
            #fdInputRow:focus-within { transform: translateY(-1px); }
            #fdInput { background: linear-gradient(135deg,
                rgba(255,255,255,.075), rgba(140,110,220,.045));
              backdrop-filter: blur(14px);
              box-shadow: 0 10px 30px rgba(12,6,28,.34),
                          inset 0 1px rgba(255,255,255,.06); }
            /* Bubbles emerge from the ambience, no hard scroll edges */
            #fdMessages { padding: 14px 6px 20px;
              mask-image: linear-gradient(to bottom, transparent 0, #000 14px,
                #000 calc(100% - 18px), transparent 100%);
              -webkit-mask-image: linear-gradient(to bottom, transparent 0,
                #000 14px, #000 calc(100% - 18px), transparent 100%); }
            /* Identity spine on Serena's bubbles, warm tint on his */
            .fd-msg { position: relative; }
            .fd-serena::before { content: ''; position: absolute; left: -1px;
              top: 11px; bottom: 11px; width: 2px; border-radius: 2px;
              background: linear-gradient(#b39ddb, #f48fb1);
              box-shadow: 0 0 8px rgba(179,157,219,.45); }
            .fd-user { background: linear-gradient(135deg,
              rgba(244,143,177,.09), rgba(255,204,128,.045)); }
            /* Material glint on the orb */
            #fdOrb::before { content: ''; position: absolute; left: 17%;
              top: 13%; width: 37%; height: 23%; border-radius: 50%;
              background: radial-gradient(ellipse, rgba(255,255,255,.72),
                rgba(255,255,255,.12) 48%, transparent 72%);
              filter: blur(.4px); transform: rotate(-24deg); opacity: .78; }
            /* At-rest presence field. It occupies the empty conversation well,
               then quietly leaves once the first real message arrives. */
            #fdIdleScene { position: absolute; inset: 0; z-index: 0;
              overflow: hidden; pointer-events: none; opacity: 1;
              transform: translateY(0) scale(1);
              transition: opacity .55s ease, transform .55s ease; }
            #fdIdleScene::before { content: ''; position: absolute; left: 50%; top: 44%;
              width: min(72%, 410px); height: 132px; border-radius: 50%;
              transform: translate(-50%, -50%);
              background: radial-gradient(ellipse,
                rgba(var(--fd-primary-rgb), .15) 0%,
                rgba(var(--fd-secondary-rgb), .07) 42%, transparent 72%);
              filter: blur(20px); opacity: .72;
              animation: fdIdleBloom 9s ease-in-out infinite; }
            #fdIdleScene::after { content: ''; position: absolute;
              left: 13%; right: 13%; top: 44%; height: 1px;
              background: linear-gradient(90deg, transparent,
                rgba(var(--fd-primary-rgb), .08) 14%,
                rgba(var(--fd-primary-rgb), .3) 50%,
                rgba(var(--fd-warm-rgb), .09) 72%, transparent);
              box-shadow: 0 0 24px rgba(var(--fd-primary-rgb), .18);
              opacity: .58; animation: fdIdleHorizon 11s ease-in-out infinite; }
            .fd-idle-ring { position: absolute; left: 50%; top: 44%;
              border-radius: 50%; border: 1px solid rgba(var(--fd-primary-rgb), .13);
              transform: translate(-50%, -50%) rotate(-7deg);
              animation: fdIdleOrbit 16s ease-in-out infinite alternate; }
            .fd-idle-ring-a { width: min(66%, 360px); height: 94px; }
            .fd-idle-ring-b { width: min(49%, 268px); height: 68px;
              border-color: rgba(var(--fd-secondary-rgb), .12);
              animation-duration: 20s; animation-delay: -7s;
              animation-direction: alternate-reverse; }
            .fd-idle-ring-c { width: min(32%, 176px); height: 44px;
              border-color: rgba(var(--fd-warm-rgb), .1);
              animation-duration: 13s; animation-delay: -4s; }
            .fd-idle-spark { position: absolute; width: 3px; height: 3px;
              border-radius: 50%; background: rgba(var(--fd-primary-rgb), .78);
              box-shadow: 0 0 9px 2px rgba(var(--fd-primary-rgb), .28);
              animation: fdIdleSpark 8s ease-in-out infinite; }
            .fd-idle-spark-a { left: 23%; top: 27%; animation-delay: -1s; }
            .fd-idle-spark-b { left: 76%; top: 31%; width: 2px; height: 2px;
              background: rgba(var(--fd-secondary-rgb), .8); animation-delay: -5s;
              animation-duration: 10s; }
            .fd-idle-spark-c { left: 31%; top: 65%; width: 2px; height: 2px;
              background: rgba(var(--fd-warm-rgb), .72); animation-delay: -3s;
              animation-duration: 11s; }
            .fd-idle-spark-d { left: 69%; top: 68%; animation-delay: -6s;
              animation-duration: 9s; }
            @keyframes fdIdleBloom {
              0%, 100% { transform: translate(-50%, -50%) scale(.93); opacity: .42; }
              50% { transform: translate(-50%, -50%) scale(1.08); opacity: .78; }
            }
            @keyframes fdIdleHorizon {
              0%, 100% { transform: scaleX(.86); opacity: .34; }
              50% { transform: scaleX(1.04); opacity: .68; }
            }
            @keyframes fdIdleOrbit {
              0% { transform: translate(-50%, -50%) rotate(-8deg) scale(.97); opacity: .34; }
              55% { opacity: .72; }
              100% { transform: translate(-50%, -50%) rotate(7deg) scale(1.035); opacity: .42; }
            }
            @keyframes fdIdleSpark {
              0%, 100% { transform: translate3d(-3px, 6px, 0) scale(.72); opacity: .2; }
              50% { transform: translate3d(7px, -9px, 0) scale(1.18); opacity: .85; }
            }
            .fd-engaged #fdIdleScene { opacity: 0;
              transform: translateY(-5px) scale(.97); }
            .fd-engaged #fdIdleScene::before,
            .fd-engaged #fdIdleScene::after,
            .fd-engaged .fd-idle-ring,
            .fd-engaged .fd-idle-spark { animation-play-state: paused; }
            @media (prefers-reduced-motion: reduce) {
              #convEmpty.fd-on::before, #convEmpty.fd-on::after,
              #fdOrb, #fdOrb::after, #fdIdleScene::before, #fdIdleScene::after,
              .fd-idle-ring, .fd-idle-spark, .fd-typing i, .fd-msg {
                animation: none !important; }
              #convEmpty.fd-on::before, #convEmpty.fd-on::after, #fdIdleScene,
              #fdOrb, #fdGreeting, #fdSub, #fdInputRow { transition: none !important; }
            }
            #fdGreeting { font-size: 38px; font-weight: 700; text-align: center;
              padding: 26px 0 8px; letter-spacing: 0.5px; flex: none;
              background: linear-gradient(90deg, #b39ddb, #f48fb1, #ffcc80);
              -webkit-background-clip: text; background-clip: text;
              -webkit-text-fill-color: transparent;
              filter: drop-shadow(0 2px 14px rgba(179, 157, 219, 0.25)); }
            #fdSub { text-align: center; font-size: 13px; letter-spacing: 2.5px;
              text-transform: uppercase; color: rgba(214, 205, 235, 0.4);
              padding-bottom: 26px; flex: none; }
            #fdMessages { flex: 1; overflow-y: auto; display: flex;
              flex-direction: column; gap: 12px; padding: 4px 2px 14px;
              position: relative; }
            .fd-msg { padding: 10px 14px; border-radius: 14px; line-height: 1.5;
              white-space: pre-wrap; word-break: break-word; font-size: 14px;
              max-width: 82%; animation: fdSlideIn 0.22s ease-out; }
            @keyframes fdSlideIn {
              from { opacity: 0; transform: translateY(6px); }
              to { opacity: 1; transform: translateY(0); }
            }
            .fd-serena { background: rgba(140, 120, 200, 0.12);
              border: 1px solid rgba(179, 157, 219, 0.25);
              border-radius: 14px 14px 14px 4px;
              box-shadow: 0 2px 14px rgba(120, 90, 190, 0.10);
              align-self: flex-start; }
            .fd-user { background: rgba(255, 255, 255, 0.07);
              border: 1px solid rgba(255, 255, 255, 0.10);
              border-radius: 14px 14px 4px 14px;
              align-self: flex-end; }
            .fd-typing { display: flex; gap: 5px; align-items: center;
              padding: 14px 16px; }
            .fd-typing i { width: 7px; height: 7px; border-radius: 50%;
              background: rgba(197, 178, 240, 0.75); display: block;
              animation: fdDot 1.2s ease-in-out infinite; }
            .fd-typing i:nth-child(2) { animation-delay: 0.15s; }
            .fd-typing i:nth-child(3) { animation-delay: 0.3s; }
            @keyframes fdDot {
              0%, 60%, 100% { transform: translateY(0); opacity: 0.45; }
              30% { transform: translateY(-5px); opacity: 1; }
            }
            #fdInputRow { display: flex; gap: 8px; flex: none; position: relative; }
            #fdInput { flex: 1; background: rgba(255, 255, 255, 0.05);
              border: 1px solid rgba(179, 157, 219, 0.28); border-radius: 13px;
              color: inherit; padding: 13px 16px; font-size: 14px; outline: none;
              caret-color: #d1b3ff; transition: border-color 0.15s, box-shadow 0.15s; }
            #fdInput:focus { border-color: rgba(196, 167, 255, 0.55);
              box-shadow: 0 0 0 3px rgba(179, 157, 219, 0.12),
                          0 2px 18px rgba(140, 110, 220, 0.15); }
            #fdInput::placeholder { color: rgba(214, 205, 235, 0.35); }
          </style>
          <div id="fdOrb"></div>
          <div id="fdGreeting"></div>
          <div id="fdSub">what are we on?</div>
          <div id="fdMessages">
            <div id="fdIdleScene" aria-hidden="true">
              <span class="fd-idle-ring fd-idle-ring-a"></span>
              <span class="fd-idle-ring fd-idle-ring-b"></span>
              <span class="fd-idle-ring fd-idle-ring-c"></span>
              <span class="fd-idle-spark fd-idle-spark-a"></span>
              <span class="fd-idle-spark fd-idle-spark-b"></span>
              <span class="fd-idle-spark fd-idle-spark-c"></span>
              <span class="fd-idle-spark fd-idle-spark-d"></span>
            </div>
          </div>
          <div id="fdInputRow">
            <input id="fdInput" placeholder="talk to me" autocomplete="off"/>
            <span id="fdSend">&#10148;</span>
          </div>
        </div>
        <span id="fdFallbackText">Select a conversation</span>
        <!-- === FRONT DOOR FEATURE END === -->
      </div>
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
            <button class="conv-hide" onclick="closeConv()"
                    title="Hide this pane without stopping its work">Hide</button>
          </div>
        </div>
        <div class="conv-body" id="convBody"></div>
        <div class="conv-terminal hidden" id="convTerminal">
          <div class="code-tabs" id="codeTabs"></div>
          <div class="code-pane-wrap" id="codePaneWrap">
            <div class="code-pane term-pane" id="termPane">
              <div class="term-statusbar">
                <div class="term-status" id="termStatus">Ready to resume.</div>
                <div class="term-session-ids hidden" id="termSessionIds"></div>
                <button class="runtime-pin hidden" id="runtimePinBtn"
                        onclick="toggleGtkPinBoth()"
                        title="Keep both linked runtimes live">
                  <span class="runtime-pin-dot"></span><span>pin both</span>
                </button>
              </div>
              <div class="term-mounts" id="termMounts"></div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div class="pane-divider" data-divider="conv-files" id="convFilesDivider" title="Drag to resize"></div>
    <div class="panel-files" id="filesPane">
      <div class="files-header">
        <span class="files-root" id="filesRootName">—</span>
        <button class="files-close" onclick="toggleFilesPane()" title="Close (Alt+B)">✕</button>
      </div>
      <div class="files-tree" id="filesTree"><div class="empty-text">Open a chat to view files</div></div>
    </div>
  </div>

  <!-- ═══ TASKS VIEW ═══ -->
  <div class="main hidden" id="viewTasks">
    <div class="panel-left" style="width:100%;border:none;">
      <div style="padding:10px 12px;border-bottom:1px solid var(--border);display:flex;gap:8px;flex-shrink:0;">
        <input id="taskInput" type="text" placeholder="add a task and hit enter…"
               onkeydown="if(event.key==='Enter')addTask()"
               style="flex:1;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:7px 10px;font-size:13px;outline:none;">
        <button class="tab-action" onclick="addTask()">+ Add</button>
      </div>
      <div class="memory-list" id="taskList"></div>
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

  <!-- ═══ FLEET VIEW ═══ -->
  <div class="main hidden" id="viewFleet">
    <iframe id="fleetFrame" src="/fleet/view" title="Fleet workflow runs"
            onload="syncFleetFrameVisibility()"
            style="width:100%;height:100%;border:0;background:var(--bg);"></iframe>
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

  <!-- === PERSONA FEATURE === (edit Persona.md + Tooling.md in-app) -->
  <div class="main hidden" id="viewPersona">
    <div class="settings-scroll">
      <div class="settings-wrap">
        <div class="settings-topbar">
          <div class="settings-title">Persona &amp; Tooling</div>
          <button class="tab-action" onclick="loadPersona()">Reload</button>
        </div>
        <div class="persona-hint">
          Both files are baked into every claude chat's system prompt on spawn.
          Edits apply to the next chat you open — no restart needed.
        </div>
        <div class="persona-editor">
          <div class="persona-head">
            <span class="persona-label">Persona.md <small>— personality / character</small></span>
            <span class="persona-status" id="personaStatus"></span>
            <button class="modal-btn primary" onclick="savePersonaFile('persona')">Save</button>
          </div>
          <textarea id="personaText" class="persona-area" spellcheck="false" placeholder="Loading…" oninput="_personaMarkDirty('persona')"></textarea>
        </div>
        <div class="persona-editor">
          <div class="persona-head">
            <span class="persona-label">Tooling.md <small>— commands / workflows</small></span>
            <span class="persona-status" id="toolingStatus"></span>
            <button class="modal-btn primary" onclick="savePersonaFile('tooling')">Save</button>
          </div>
          <textarea id="toolingText" class="persona-area" spellcheck="false" placeholder="Loading…" oninput="_personaMarkDirty('tooling')"></textarea>
        </div>
        <div class="persona-editor">
          <div class="persona-head">
            <span class="persona-label">VoiceReminder.txt <small>— injected every turn to fight drift</small></span>
            <span class="persona-status" id="voiceStatus"></span>
            <button class="modal-btn primary" onclick="savePersonaFile('voice')">Save</button>
          </div>
          <textarea id="voiceText" class="persona-area" style="min-height:90px" spellcheck="false" placeholder="Loading…" oninput="_personaMarkDirty('voice')"></textarea>
        </div>
      </div>
    </div>
  </div>
  <!-- === PERSONA FEATURE END === -->

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
let sessionSource = [];
let currentProject = null;
let currentSessionId = null;
let focusedIndex = -1;
let focusedSid = null;  // authoritative: which chat is focused, survives re-renders
let selectedIds = new Set();
let searchTimeout = null;
const _expandedParents = new Set();

// Memory state
let memories = [];
let tasks = [];
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
let liveUsageTimer = null;
let liveUsageData = null;
const USAGE_ALERT_THRESHOLDS = [50, 75, 85, 95, 100];
const usageAlertState = {
  claude: { lastPct: null },
  codex: { lastPct: null },
};

// ═══════════════════════════════════════════════════════════════
// Tab Switching
// ═══════════════════════════════════════════════════════════════
function switchTab(tab) {
  const leavingChats = currentTab === 'chats' && tab !== 'chats';
  if (leavingChats && window.__nativeTerminalBridge) {
    // Native VTEs float above WebKit. Always unmap their GTK host before the
    // Chats DOM disappears, even if JS has already lost its active-sid marker.
    window.gtkSend({ type: 'code-tab-visible', visible: false });
  }
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.getElementById('viewChats').classList.toggle('hidden', tab !== 'chats');
  document.getElementById('viewTasks').classList.toggle('hidden', tab !== 'tasks');
  document.getElementById('viewMemory').classList.toggle('hidden', tab !== 'memory');
  document.getElementById('viewKnowledge').classList.toggle('hidden', tab !== 'knowledge');
  document.getElementById('viewFleet').classList.toggle('hidden', tab !== 'fleet');
  document.getElementById('viewUsage').classList.toggle('hidden', tab !== 'usage');
  document.getElementById('viewPersona').classList.toggle('hidden', tab !== 'persona');
  updateShortcutBar();
  if (tab === 'tasks') { loadTasks(); }
  if (tab === 'memory') { if (memories.length) renderMemoryList(); loadMemories(); }
  if (tab === 'knowledge') { if (topics.length) renderTopicList(); loadTopics(); }
  syncFleetFrameVisibility();
  if (tab === 'usage') { loadUsage(); }
  if (tab === 'persona') loadPersona();
  // Returning to Chats: force the now-visible DOM allocation and remap the
  // native overlay immediately. RAF is only an invalid-rect retry.
  if (tab === 'chats' && window.__nativeTerminalBridge) {
    let attempts = 0;
    const restoreNativeTerminal = () => {
      if (currentTab !== 'chats') return;
      const r = _gtkGetRect();
      if (_gtkRectIsVisible(r)) {
        window.gtkSend({ type: 'code-tab-visible', visible: true, rect: r });
      } else if (++attempts < 12) {
        requestAnimationFrame(restoreNativeTerminal);
      }
    };
    restoreNativeTerminal();
  }
  if (tab === 'chats' && !window.__nativeTerminalBridge) {
    // The PTY and xterm instances never leave the renderer. Wait until the
    // Chats DOM has a real allocation, then restore the exact prior pane or
    // split without sending a transient tiny resize to either CLI.
    window.SerenaTerminalLifecycle.afterReveal(() => {
      if (currentTab !== 'chats' || convMode !== 'live') return;
      const sid = currentSessionId && termSessions.has(currentSessionId)
        ? currentSessionId
        : activeTermSid;
      if (sid && termSessions.has(sid)) _activateTermPane(sid);
    });
  }
}

function syncFleetFrameVisibility() {
  const frame = document.getElementById('fleetFrame');
  if (!frame || !frame.contentWindow) return;
  frame.contentWindow.postMessage({
    type: 'serena-fleet-visible',
    visible: currentTab === 'fleet',
  }, window.location.origin);
}

async function openFleetSessionReadOnly(sid) {
  if (!sid) return;
  switchTab('chats');
  await openConv(String(sid), { mode: 'read' });
}

window.addEventListener('message', event => {
  if (event.origin !== window.location.origin) return;
  const frame = document.getElementById('fleetFrame');
  if (!frame || event.source !== frame.contentWindow) return;
  const message = event.data || {};
  if (message.type === 'serena-fleet-count') {
    const count = Math.max(0, Number(message.active_count) || 0);
    document.getElementById('fleetCount').textContent = count ? '(' + count + ')' : '';
  } else if (message.type === 'serena-fleet-open-session' && message.session_id) {
    openFleetSessionReadOnly(message.session_id);
  }
});

// === PERSONA FEATURE === (view + edit Persona.md / Tooling.md in-app)
const _personaPanes = {
  persona: { text: 'personaText', status: 'personaStatus' },
  tooling: { text: 'toolingText', status: 'toolingStatus' },
  voice:   { text: 'voiceText',   status: 'voiceStatus' },
};

function loadPersona() {
  fetch('/api/persona-files').then(r => r.json()).then(d => {
    for (const [key, ids] of Object.entries(_personaPanes)) {
      const ta = document.getElementById(ids.text);
      ta.value = d[key] || '';
      ta.dataset.saved = ta.value;
      document.getElementById(ids.status).textContent = '';
    }
  }).catch(e => {
    document.getElementById('personaStatus').textContent = 'load failed: ' + e.message;
  });
}

function _personaMarkDirty(which) {
  const ids = _personaPanes[which];
  const ta = document.getElementById(ids.text);
  const st = document.getElementById(ids.status);
  if (ta.value !== ta.dataset.saved) { st.textContent = 'unsaved changes'; st.className = 'persona-status dirty'; }
  else { st.textContent = ''; st.className = 'persona-status'; }
}

function savePersonaFile(which) {
  const ids = _personaPanes[which];
  const ta = document.getElementById(ids.text);
  const st = document.getElementById(ids.status);
  st.textContent = 'saving…'; st.className = 'persona-status';
  fetch('/api/persona-files', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ file: which, content: ta.value }),
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      ta.dataset.saved = ta.value;
      st.textContent = 'saved ✓'; st.className = 'persona-status';
      setTimeout(() => { if (st.textContent === 'saved ✓') st.textContent = ''; }, 2500);
    } else {
      st.textContent = d.error || 'save failed'; st.className = 'persona-status fail';
    }
  }).catch(e => { st.textContent = 'save failed: ' + e.message; st.className = 'persona-status fail'; });
}
// === PERSONA FEATURE END ===

// ═══════════════════════════════════════════════════════════════
// Utilities
// ═══════════════════════════════════════════════════════════════
function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// Wrap absolute file paths in clickable spans. Runs on ALREADY-escaped HTML
// (so <>& are entities; path chars / \ . - _ : are untouched). Click reveals
// in the file manager; Ctrl/Cmd+click opens the file. Avoids URLs via the
// lookbehind (won't fire on the // after http:).
function linkifyPaths(escaped) {
  if (!escaped) return escaped;
  const RE = /(?<![:\w\/\\])((?:[A-Za-z]:[\\/]|\/)[\w.+@\-]+(?:[\\/][\w.+@\-]+)+)/g;
  return escaped.replace(RE, '<span class="filepath" title="click: reveal in files · ctrl+click: open">$1</span>');
}

async function openFilePath(path, reveal) {
  try {
    const r = await fetch('/api/open-path', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path, reveal: reveal }),
    });
    const d = await r.json();
    if (!d.ok) showToast(d.error || 'could not open path', { variant: 'error' });
  } catch (e) { showToast('open failed: ' + e.message, { variant: 'error' }); }
}

// Delegated once: plain click on a path reveals it in the file manager,
// Ctrl/Cmd+click opens the file with its default app.
document.addEventListener('click', (e) => {
  const el = e.target.closest && e.target.closest('.filepath');
  if (!el) return;
  e.preventDefault();
  openFilePath(el.textContent, !(e.ctrlKey || e.metaKey));
});

function formatTokens(n) {
  if (!n) return '';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return Math.round(n / 1e3) + 'K';
  return String(n);
}

function usageResetLabel(epochSeconds) {
  if (!epochSeconds) return '';
  const diff = Math.max(0, epochSeconds * 1000 - Date.now());
  const mins = Math.floor(diff / 60000);
  if (mins <= 0) return 'ready';
  if (mins < 60) return mins + 'm';
  const hours = Math.floor(mins / 60);
  const rem = mins % 60;
  if (hours < 24) return hours + 'h' + (rem ? ' ' + rem + 'm' : '');
  const days = Math.floor(hours / 24);
  return days + 'd ' + (hours % 24) + 'h';
}

function usageUpdatedLabel(epochSeconds) {
  if (!epochSeconds) return '';
  const diff = Math.max(0, Date.now() - epochSeconds * 1000);
  const mins = Math.floor(diff / 60000);
  if (mins <= 0) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h ago';
  return Math.floor(hours / 24) + 'd ago';
}

function usagePct(data) {
  data = data || {};
  const raw = data.used_percentage;
  if (raw == null || !isFinite(raw)) return null;
  return Math.max(0, Math.round(raw));
}

function usageToneClass(pct) {
  if (pct == null) return 'tone-ok';
  if (pct >= 90) return 'tone-danger';
  if (pct >= 70) return 'tone-warn';
  return 'tone-ok';
}

function usageToneColor(pct) {
  if (pct == null) return 'var(--green)';
  if (pct >= 90) return 'var(--red)';
  if (pct >= 70) return 'var(--amber)';
  return 'var(--green)';
}

function usagePressureWindow(svc) {
  if (!svc || !svc.available) return null;
  const fiveHour = usagePct(svc.five_hour);
  if (fiveHour != null) return { label: '5h', data: svc.five_hour, pct: fiveHour };
  const sevenDay = usagePct(svc.seven_day);
  if (sevenDay != null) return { label: '7d', data: svc.seven_day, pct: sevenDay };
  return null;
}

function usagePressure(svc) {
  const window = usagePressureWindow(svc);
  return window ? window.pct : null;
}

function usageSourceLabel(source) {
  if (source === 'claude-statusline') return 'claude statusline';
  if (source === 'codex-jsonl') return 'codex local log';
  if (source === 'claude-statusline-codex-scan') return 'claude codex scan';
  return source || '';
}

function liveUsageWindowHtml(label, data) {
  data = data || {};
  const pct = usagePct(data);
  const width = pct == null ? 0 : Math.min(100, pct);
  const reset = usageResetLabel(data.resets_at);
  const tone = usageToneClass(pct);
  return '<div class="live-usage-window" title="' + esc(label + (reset ? ' resets in ' + reset : '')) + '">'
    + '<span class="live-usage-window-label">' + esc(label) + '</span>'
    + '<span class="live-usage-meter"><span class="live-usage-fill ' + tone + '" style="width:' + width + '%"></span></span>'
    + '<span class="live-usage-pct">' + (pct == null ? '--' : pct + '%') + '</span>'
    + '<span class="live-usage-reset">' + esc(reset || '') + '</span>'
    + '</div>';
}

function liveUsageCompactHtml(name, cls, svc) {
  const pct = usagePressure(svc);
  const initial = cls === 'codex' ? 'x' : name[0];
  if (!svc || !svc.available || pct == null) {
    return '<span class="live-usage-chip waiting">'
      + '<span class="live-usage-dial" style="--pct:0"><span>' + esc(initial) + '</span></span>'
      + '<span>' + esc(name) + '</span>'
      + '</span>';
  }
  const pctText = pct + '%';
  const dialPct = Math.min(100, pct);
  const staleCls = svc.stale ? ' stale' : '';
  return '<span class="live-usage-chip ' + cls + staleCls + '">'
    + '<span class="live-usage-dial" style="--pct:' + dialPct + ';--dial-color:' + usageToneColor(pct) + '"><span>' + esc(initial) + '</span></span>'
    + '<span>' + esc(name) + '</span>'
    + '<span class="live-usage-pct">' + esc(pctText) + '</span>'
    + '</span>';
}

function liveUsageServiceHtml(name, cls, svc, updatedAt) {
  svc = svc || {};
  if (!svc.available) {
    return '<div class="live-usage-card">'
      + '<div class="live-usage-card-head">'
      + '<span class="live-usage-name ' + cls + '">' + esc(name) + '</span>'
      + '<span class="live-usage-empty">waiting</span>'
      + '</div>'
      + '</div>';
  }
  const updated = usageUpdatedLabel(svc.updated_at || updatedAt);
  const source = usageSourceLabel(svc.source);
  const metaParts = [];
  if (svc.model) metaParts.push(svc.model);
  if (source) metaParts.push(source);
  const ageClass = svc.stale ? 'live-usage-age stale' : 'live-usage-age';
  const agePrefix = svc.stale ? 'last known ' : 'live ';
  const cardClass = svc.stale ? 'live-usage-card stale' : 'live-usage-card';
  return '<div class="' + cardClass + '">'
    + '<div class="live-usage-card-head">'
    + '<span class="live-usage-name ' + cls + '">' + esc(name) + '</span>'
    + '<span class="live-usage-meta">' + esc(metaParts.join(' · ')) + '</span>'
    + '</div>'
    + liveUsageWindowHtml('5h', svc.five_hour)
    + liveUsageWindowHtml('7d', svc.seven_day)
    + (updated ? '<div class="' + ageClass + '">' + esc(agePrefix + updated) + '</div>' : '')
    + '</div>';
}

function usageAlertVariant(threshold) {
  if (threshold >= 95) return 'critical';
  if (threshold >= 85) return 'danger';
  if (threshold >= 75) return 'warn';
  return '';
}

function showUsageAlert(name, cls, threshold, pressure) {
  const stack = document.getElementById('usageAlertStack');
  if (!stack) return;
  const pct = pressure.pct;
  const reset = usageResetLabel(pressure.data && pressure.data.resets_at);
  const tone = usageToneClass(pct);
  const width = Math.min(100, pct);
  const variant = usageAlertVariant(threshold);
  const el = document.createElement('div');
  el.className = 'usage-alert' + (variant ? ' ' + variant : '');
  el.setAttribute('role', 'status');
  el.innerHTML = '<div class="usage-alert-head">'
    + '<span class="usage-alert-name ' + cls + '">' + esc(name) + '</span>'
    + '<span class="usage-alert-level">' + esc(String(threshold)) + '%</span>'
    + '</div>'
    + '<div class="usage-alert-body">'
    + '<span>' + esc(pressure.label) + '</span>'
    + '<span class="live-usage-meter"><span class="live-usage-fill ' + tone + '" style="width:' + width + '%"></span></span>'
    + '<span class="live-usage-pct">' + esc(String(pct)) + '%</span>'
    + '<span class="live-usage-reset">' + esc(reset || 'ready') + '</span>'
    + '</div>';
  stack.appendChild(el);
  requestAnimationFrame(() => el.classList.add('visible'));
  setTimeout(() => {
    el.classList.remove('visible');
    setTimeout(() => el.remove(), 180);
  }, 5200);
}

function checkUsageAlerts(data) {
  const services = [
    ['claude', 'claude', data && data.claude],
    ['codex', 'codex', data && data.codex],
  ];
  services.forEach(([name, cls, svc]) => {
    const state = usageAlertState[name] || (usageAlertState[name] = { lastPct: null });
    if (!svc || !svc.available || svc.stale) return;
    const pressure = usagePressureWindow(svc);
    if (!pressure) return;
    const pct = pressure.pct;
    if (state.lastPct == null) {
      state.lastPct = pct;
      return;
    }
    const crossed = USAGE_ALERT_THRESHOLDS.filter(t => state.lastPct < t && pct >= t);
    state.lastPct = pct;
    if (!crossed.length) return;
    showUsageAlert(name, cls, crossed[crossed.length - 1], pressure);
  });
}

function renderLiveUsage(data) {
  const el = document.getElementById('liveUsageRibbon');
  if (!el) return;
  liveUsageData = data || {};
  const c = (data && data.claude) || {};
  const x = (data && data.codex) || {};
  const stateUpdatedAt = data && data.state_updated_at;
  el.innerHTML = '<div class="live-usage-compact">'
    + '<span class="live-usage-label">limits</span>'
    + liveUsageCompactHtml('claude', 'claude', c)
    + liveUsageCompactHtml('codex', 'codex', x)
    + '</div>'
    + '<div class="live-usage-popover">'
    + liveUsageServiceHtml('claude', 'claude', c, stateUpdatedAt)
    + liveUsageServiceHtml('codex', 'codex', x, stateUpdatedAt)
    + '</div>';
  _syncNativeUsagePopover();
}

async function loadLiveUsage() {
  try {
    const r = await fetch('/api/live-usage');
    const data = await r.json();
    renderLiveUsage(data);
    checkUsageAlerts(data);
  } catch(e) {
    liveUsageData = null;
    const el = document.getElementById('liveUsageRibbon');
    if (el) el.innerHTML = '<span class="live-usage-empty">usage unavailable</span>';
    _syncNativeUsagePopover(true);
  }
}

function startLiveUsagePoll() {
  if (liveUsageTimer) return;
  loadLiveUsage();
  liveUsageTimer = setInterval(loadLiveUsage, 5000);
}

let _usagePopoverHover = false;
let _usagePopoverFocus = false;
function _nativeUsageService(name, svc, updatedAt) {
  svc = svc || {};
  if (!svc.available) {
    return { name, available: false, stale: false, meta: '', age: '', windows: [] };
  }
  const source = usageSourceLabel(svc.source);
  const meta = [];
  if (svc.model) meta.push(svc.model);
  if (source) meta.push(source);
  const updated = usageUpdatedLabel(svc.updated_at || updatedAt);
  return {
    name,
    available: true,
    stale: Boolean(svc.stale),
    meta: meta.join(' · '),
    age: updated ? (svc.stale ? 'last known ' : 'live ') + updated : '',
    windows: [
      { label: '5h', pct: usagePct(svc.five_hour), reset: usageResetLabel(svc.five_hour && svc.five_hour.resets_at) },
      { label: '7d', pct: usagePct(svc.seven_day), reset: usageResetLabel(svc.seven_day && svc.seven_day.resets_at) },
    ],
  };
}

function _syncNativeUsagePopover(hideWhenInactive = false) {
  if (!window.__gtkBridge || !window.gtkSend) return;
  const el = document.getElementById('liveUsageRibbon');
  const active = _usagePopoverHover || _usagePopoverFocus;
  if (!active || !el || !liveUsageData) {
    if (hideWhenInactive) window.gtkSend({ type: 'usage-popover-hide' });
    return;
  }
  const rect = el.getBoundingClientRect();
  const updatedAt = liveUsageData.state_updated_at;
  window.gtkSend({
    type: 'usage-popover-show',
    rect: { x: rect.left, y: rect.top, w: rect.width, h: rect.height },
    services: [
      _nativeUsageService('claude', liveUsageData.claude, updatedAt),
      _nativeUsageService('codex', liveUsageData.codex, updatedAt),
    ],
  });
}

function setupLiveUsagePopover() {
  const el = document.getElementById('liveUsageRibbon');
  if (!el || el.dataset.usagePopover === '1') return;
  el.dataset.usagePopover = '1';
  el.addEventListener('mouseenter', () => {
    _usagePopoverHover = true;
    _syncNativeUsagePopover();
  });
  el.addEventListener('mouseleave', () => {
    _usagePopoverHover = false;
    _syncNativeUsagePopover(true);
  });
  el.addEventListener('focusin', () => {
    _usagePopoverFocus = true;
    _syncNativeUsagePopover();
  });
  el.addEventListener('focusout', () => {
    setTimeout(() => {
      _usagePopoverFocus = el.contains(document.activeElement);
      _syncNativeUsagePopover(true);
    }, 0);
  });
  window.addEventListener('beforeunload', () => {
    if (window.gtkSend) window.gtkSend({ type: 'usage-popover-hide', immediate: true });
  });
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

function rowActivityTs(s) {
  return (s && (s.group_last_timestamp || s.last_timestamp || s.first_timestamp)) || '';
}

const SERENA_VOICE_SESSION_ID = 'serena-voice-main';
function _isSerenaVoiceSession(sessionOrSid) {
  if (!sessionOrSid) return false;
  if (typeof sessionOrSid === 'string') return sessionOrSid === SERENA_VOICE_SESSION_ID;
  return sessionOrSid.session_id === SERENA_VOICE_SESSION_ID
    || String(sessionOrSid.agent || '').toLowerCase() === 'serena-voice';
}

function _isFleetSession(session) {
  if (!session || typeof session !== 'object') return false;
  const marker = session.fleet_worker
    || (session.metadata && session.metadata.fleet_worker);
  return Boolean(marker && typeof marker === 'object' && marker.run_id);
}

function setSessionSource(items) {
  sessionSource = items || [];
  sessions = sessionSource;
}

function _clientSessionLists() {
  return [allSessions, sessionSource, sessions, _pseudoSessions];
}

function _patchClientSession(sid, patch) {
  if (!sid || !patch) return;
  for (const arr of _clientSessionLists()) {
    if (!Array.isArray(arr)) continue;
    for (const s of arr) {
      if (s && s.session_id === sid) Object.assign(s, patch);
    }
  }
}

function _findClientSession(sid) {
  if (!sid) return null;
  for (const arr of _clientSessionLists()) {
    if (!Array.isArray(arr)) continue;
    const found = arr.find(s => s && s.session_id === sid);
    if (found) return found;
  }
  return null;
}

function _dropClientSession(sid) {
  if (!sid) return;
  for (const arr of _clientSessionLists()) {
    if (!Array.isArray(arr)) continue;
    for (let i = arr.length - 1; i >= 0; i--) {
      if (arr[i] && arr[i].session_id === sid) arr.splice(i, 1);
    }
  }
}

function _applyClientGroup(sids, groupId) {
  if (!groupId || !sids || !sids.length) return;
  for (const sid of Array.from(new Set(sids.filter(Boolean)))) {
    _patchClientSession(sid, { group: groupId });
  }
}

// Auto-follow: when codex hands a plan off to a fresh exec session (a new chat
// that auto-links into the group you're currently viewing), jump to it — same
// behavior as the GTK split following the live agent. Only fires for chats that
// SPAWN while the app is open (never yanks you to a pre-existing sibling).
let _seenSidsInit = false;
const _seenSids = new Set();
const _freshSids = new Set();
const _autoSwitched = new Set();
function _maybeFollowSpawnedChat() {
  const ids = sessionSource.map(s => s.session_id);
  if (!_seenSidsInit) {
    ids.forEach(id => _seenSids.add(id));
    _seenSidsInit = true;
    return;
  }
  for (const id of ids) {
    if (!_seenSids.has(id)) { _seenSids.add(id); _freshSids.add(id); }
  }
  if (!currentSessionId) return;
  const cur = sessionSource.find(s => s.session_id === currentSessionId);
  if (!cur || !cur.group) return;
  // a freshly-spawned chat that linked into the group I'm viewing
  const target = sessionSource.find(s =>
    _freshSids.has(s.session_id) &&
    !_autoSwitched.has(s.session_id) &&
    !s.external_runtime_active &&
    s.session_id !== currentSessionId &&
    s.group === cur.group);
  if (target) {
    _autoSwitched.add(target.session_id);
    openConv(target.session_id);
  }
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
    setSessionSource(_pseudoSessions.length ? [..._pseudoSessions, ...allSessions] : allSessions);
    _maybeFollowSpawnedChat();
    renderSessionList();
    updateChatCount();
  } catch(e) {
    console.error('loadSessions:', e);
  }
}

let _searchQuery = '';   // active search text; '' = not searching (periodic
                         // refreshers check this so they don't wipe results)
let _searchSeq = 0;      // latest-request-wins guard against out-of-order responses

function _snippetHtml(snip) {
  // FTS returns the match wrapped in >>> <<< — escape, then turn those markers
  // into <mark> so the matching words highlight.
  return esc(snip)
    .replace(/&gt;&gt;&gt;/g, '<mark>')
    .replace(/&lt;&lt;&lt;/g, '</mark>');
}

async function searchSessions(q) {
  _searchQuery = q || '';
  if (!q) { loadSessions(currentProject); return; }
  const seq = ++_searchSeq;
  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(q));
    const results = await r.json();
    // Ignore stale responses: a newer keystroke already fired, or the box was
    // cleared/changed while this request was in flight.
    if (seq !== _searchSeq || _searchQuery !== q) return;
    setSessionSource(results);
    renderSessionList();
  } catch(e) {
    console.error('searchSessions:', e);
  }
}

let projectChips = [];
let currentProjectCwd = null;

// The cwd a new chat really starts in when the user picked no folder. Python
// resolves an empty cwd to $HOME before spawning, so a placeholder row that
// stored '' could never match the real session it created. Fetched once and
// cached; _defaultCwd() falls back to '' so a failed fetch degrades to the old
// behaviour instead of throwing.
let _serverDefaultCwd = null;
async function _loadDefaultCwd() {
  if (_serverDefaultCwd !== null) return _serverDefaultCwd;
  try {
    const r = await fetch('/api/default-cwd');
    if (r.ok) {
      const d = await r.json();
      _serverDefaultCwd = (d && d.cwd) || '';
    }
  } catch(e) {}
  return _serverDefaultCwd || '';
}
function _defaultCwd() {
  return _serverDefaultCwd || '';
}

// Per-launch ephemeral expand state for project tree (paths, not indices, so it
// survives chip reorderings between renders)
const _projectExpanded = new Set();

// Dirs at depth 1 below $HOME that should NOT act as a parent (they'd vacuum
// every project under them). We still walk THROUGH them when finding ancestors,
// we just don't render a node for them.
const _VACUUM_DIRS = new Set([
  'Documents', 'Desktop', 'Downloads', 'Pictures', 'Music', 'Videos',
  'Public', 'Templates', 'snap', '.local', '.config', '.cache',
]);

// Canonical project KEY: a machine-independent path relative to the user's
// Projects dir. This is what makes the same project collapse into ONE tree
// node regardless of which OS recorded the cwd — the core of the cross-machine
// mess (Windows `C:\Users\ragha\Projects\X` vs Linux
// `/home/raghav/Documents/Projects/X`, with/without `Documents`). Returns ''
// for chats run from home or outside Projects (rendered as loose leaves).
function _normCwd(cwd) {
  if (!cwd) return '';
  const p = cwd.replace(/\\/g, '/').replace(/\/+$/, '');
  // Reduce to `Projects/<rest>` regardless of OS home layout (Linux
  // `/home/<u>/Documents/Projects/…`, Windows `C:/Users/<u>/Projects/…`), so
  // the same project collapses into one node under a shared `Projects` root.
  const m = p.match(/(?:\/home\/[^/]+|[A-Za-z]:\/Users\/[^/]+)(?:\/Documents)?\/(Projects\/.+)$/i);
  return m ? m[1] : '';
}

function _isVacuumPath(path) {
  // With keys, "vacuum" simply means no project key — home dir, the Projects
  // root itself, or anywhere outside Projects. Those render as loose leaves.
  return !path;
}

function _buildProjectTree(chips) {
  // Build a true filesystem-style tree from all chip cwds. Synthetic
  // (chip-less) intermediate nodes are inserted so e.g. ~/Projects/serena
  // shows up as a folder above its child chips even if no chat was started
  // directly in it.
  // Each node: { path, short, chipIdx (-1 if synthetic), children: [path...] }
  const nodes = new Map();
  function ensure(path) {
    if (!nodes.has(path)) {
      const segs = path.split('/').filter(Boolean);
      const short = segs[segs.length - 1] || path;
      nodes.set(path, { path, short, chipIdx: -1, children: [] });
    }
    return nodes.get(path);
  }

  // Real chips → tree nodes
  chips.forEach((c, i) => {
    const p = _normCwd(c.cwd);
    if (!p) return;  // empty-cwd chips become flat leaves at the root
    if (_isVacuumPath(p)) return;  // skip chips literally at home/Documents/etc
    const n = ensure(p);
    n.chipIdx = i;
    if (!n.short && c.short) n.short = c.short;
  });

  // Walk each chip path upward, inserting synthetic ancestors. Stop when we
  // hit a vacuum path or the parent would be empty.
  for (const path of [...nodes.keys()]) {
    let cur = path;
    while (true) {
      const slash = cur.lastIndexOf('/');
      if (slash <= 0) break;
      const parent = cur.slice(0, slash);
      if (_isVacuumPath(parent)) break;
      const pn = ensure(parent);
      if (!pn.children.includes(cur)) pn.children.push(cur);
      cur = parent;
    }
  }

  // Roots = nodes that aren't anyone's child
  const childPaths = new Set();
  for (const n of nodes.values()) for (const c of n.children) childPaths.add(c);
  const roots = [...nodes.keys()].filter(p => !childPaths.has(p));

  const cmp = (a, b) => {
    const na = nodes.get(a).short || '';
    const nb = nodes.get(b).short || '';
    return na.localeCompare(nb, undefined, { sensitivity: 'base' });
  };
  for (const n of nodes.values()) n.children.sort(cmp);
  roots.sort(cmp);

  // Chips with empty/vacuum cwds get rendered as flat leaves at the bottom
  const flatChipIdxs = [];
  chips.forEach((c, i) => {
    const p = _normCwd(c.cwd);
    if (!p || _isVacuumPath(p)) flatChipIdxs.push(i);
  });
  flatChipIdxs.sort((a, b) => (chips[a].short || '').localeCompare(chips[b].short || '', undefined, { sensitivity: 'base' }));

  return { nodes, roots, flatChipIdxs };
}

function _collectDescendantChipIdxs(path, tree) {
  const out = [];
  const stack = [path];
  while (stack.length) {
    const p = stack.pop();
    const n = tree.nodes.get(p);
    if (!n) continue;
    if (n.chipIdx >= 0) out.push(n.chipIdx);
    for (const c of n.children) stack.push(c);
  }
  return out;
}

function _renderProjectNode(path, depth, tree) {
  const n = tree.nodes.get(path);
  if (!n) return '';
  const hasKids = n.children.length > 0;
  const isSynthetic = n.chipIdx < 0;
  const expanded = _projectExpanded.has(path);
  const chev = hasKids ? (expanded ? '▾' : '▸') : '';
  const pad = depth * 10 + 8;
  const safePath = path.replace(/&/g,'&amp;').replace(/"/g,'&quot;');

  let html = '<div class="project-item' + (isSynthetic ? ' project-syn' : '')
    + (hasKids ? ' project-folder' : '') + '"'
    + ' data-path="' + safePath + '"'
    + (n.chipIdx >= 0 ? ' data-idx="' + n.chipIdx + '"' : '')
    + ' style="padding-left:' + pad + 'px"'
    + ' onclick="_handleProjectClick(this, event)"'
    + (n.chipIdx >= 0 ? ' ondblclick="newChatInProject(' + n.chipIdx + ')"' : '')
    + ' title="' + esc(n.short) + '">'
    + (hasKids
        ? '<span class="proj-chev" onclick="event.stopPropagation();_toggleProjExpand(this)">' + chev + '</span>'
        : '<span class="proj-chev proj-chev-empty"></span>')
    + esc(n.short)
    + '</div>';
  if (hasKids && expanded) {
    for (const k of n.children) html += _renderProjectNode(k, depth + 1, tree);
  }
  return html;
}

function _toggleProjExpand(chevEl) {
  const item = chevEl.closest('.project-item');
  if (!item) return;
  const path = item.getAttribute('data-path');
  if (!path) return;
  if (_projectExpanded.has(path)) _projectExpanded.delete(path);
  else _projectExpanded.add(path);
  _renderProjectSidebar();
}

function _handleProjectClick(el, evt) {
  const idxAttr = el.getAttribute('data-idx');
  const path = el.getAttribute('data-path');
  const isFolder = el.classList.contains('project-folder');
  // Clicking a folder's NAME (not just the chevron) toggles expand/collapse.
  if (isFolder && path) {
    if (_projectExpanded.has(path)) _projectExpanded.delete(path);
    else _projectExpanded.add(path);
    _renderProjectSidebar();
    return;
  }
  if (idxAttr != null) {
    filterProject(parseInt(idxAttr, 10), el);
    return;
  }
  // Synthetic non-folder (shouldn't happen, but keep the filter fallback).
  if (path) {
    const tree = _lastProjectTree;
    if (tree) {
      const idxs = _collectDescendantChipIdxs(path, tree);
      _filterProjectsByIdxs(idxs, path);
    }
    document.querySelectorAll('.project-sidebar .project-item').forEach(c => c.classList.remove('active'));
    el.classList.add('active');
  }
}

function _filterProjectsByIdxs(idxs, syntheticPath) {
  const dirs = [];
  for (const i of idxs) {
    const c = projectChips[i];
    if (!c) continue;
    for (const d of (c.project_dirs || [c.project_dir])) if (d && !dirs.includes(d)) dirs.push(d);
  }
  // Pick the first chip's cwd or fall back to the synthetic folder path itself
  // so "new chat" while a synthetic is active drops the user into that folder.
  currentProjectCwd = syntheticPath || (idxs.length && projectChips[idxs[0]] && projectChips[idxs[0]].cwd) || null;
  loadSessions(dirs.length ? dirs : null);
}

let _lastProjectTree = null;

let _projExpandInit = false;
function _renderProjectSidebar() {
  const tree = _buildProjectTree(projectChips);
  _lastProjectTree = tree;

  // Expand every folder by default on first render; user toggles persist after.
  if (!_projExpandInit) {
    for (const [path, n] of tree.nodes) {
      if (n.children && n.children.length) _projectExpanded.add(path);
    }
    _projExpandInit = true;
  }

  // Auto-expand ancestors of the active chip so it stays visible
  let activeChip = -1;
  if (typeof currentProject === 'object' && currentProject && currentProject.length) {
    activeChip = projectChips.findIndex(c => (c.project_dirs || [c.project_dir]).some(d => currentProject.includes(d)));
    if (activeChip >= 0) {
      const c = projectChips[activeChip];
      const p = _normCwd(c.cwd);
      if (p && !_isVacuumPath(p)) {
        let cur = p;
        while (true) {
          const slash = cur.lastIndexOf('/');
          if (slash <= 0) break;
          const parent = cur.slice(0, slash);
          if (_isVacuumPath(parent)) break;
          if (tree.nodes.has(parent)) _projectExpanded.add(parent);
          cur = parent;
        }
      }
    }
  }

  const bar = document.getElementById('projectSidebar');
  let html = '<div class="project-item project-all" onclick="filterProject(-1, this)" title="Show all chats">All</div>';
  for (const rootPath of tree.roots) html += _renderProjectNode(rootPath, 0, tree);
  // Flat (cwdless) chips at the bottom
  for (const idx of tree.flatChipIdxs) {
    const c = projectChips[idx];
    if (!c) continue;
    html += '<div class="project-item" data-idx="' + idx + '"'
      + ' style="padding-left:8px"'
      + ' onclick="filterProject(' + idx + ', this)"'
      + ' ondblclick="newChatInProject(' + idx + ')"'
      + ' title="' + esc(c.short) + '">'
      + '<span class="proj-chev proj-chev-empty"></span>'
      + esc(c.short)
      + '</div>';
  }
  bar.innerHTML = html;

  if (activeChip >= 0) {
    const el = bar.querySelector('[data-idx="' + activeChip + '"]');
    if (el) el.classList.add('active');
  } else {
    const all = bar.querySelector('.project-all');
    if (all) all.classList.add('active');
  }
}

async function loadProjects() {
  try {
    const r = await fetch('/api/projects');
    projectChips = await r.json();
    _renderProjectSidebar();
  } catch(e) {
    console.error('loadProjects:', e);
  }
}

async function newChatInProject(idx) {
  const p = projectChips[idx];
  if (!p) return;
  // Filter first so the sidebar reflects where we're working
  const el = document.querySelector('.project-sidebar [data-idx="' + idx + '"]');
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
  const source = sessionSource.length ? sessionSource : sessions;
  if (!source.length) {
    el.innerHTML = '<div class="empty-text">No conversations found</div>';
    focusedIndex = -1;
    focusedSid = null;
    return;
  }

  const tree = buildSessionTree(source);
  const topSessions = tree.top;
  const childrenByParent = tree.childrenByParent;
  if (!topSessions.length) {
    el.innerHTML = '<div class="empty-text">No conversations found</div>';
    sessions = [];
    focusedIndex = -1;
    focusedSid = null;
    return;
  }

  // === GROUP FEATURE === (collapse thread members under their most-recent
  // representative so a linked thread renders as a single visual cluster
  // instead of scattering members across multiple time groups)
  const groupBuckets = new Map(); // gid -> [sessions sorted by last_timestamp desc]
  for (const s of topSessions) {
    if (!s.group || _isSerenaVoiceSession(s)) continue;
    if (!groupBuckets.has(s.group)) groupBuckets.set(s.group, []);
    groupBuckets.get(s.group).push(s);
  }
  for (const arr of groupBuckets.values()) {
    // Sort: claude always first, then by recency among same-agent. So in mixed
    // threads the claude chat is the head; the codex chat tucks under it.
    arr.sort((a, b) => {
      const aClaude = (a.agent || 'claude').toLowerCase() === 'claude' ? 0 : 1;
      const bClaude = (b.agent || 'claude').toLowerCase() === 'claude' ? 0 : 1;
      if (aClaude !== bClaude) return aClaude - bClaude;
      return (b.last_timestamp || '').localeCompare(a.last_timestamp || '');
    });
  }
  const groupSiblings = new Map(); // representative_sid -> [sibling sessions]
  const consumedByGroup = new Set(); // sids that should NOT render at top-level
  for (const [gid, members] of groupBuckets.entries()) {
    if (members.length < 2) continue;
    const rep = members[0];
    const sibs = members.slice(1);
    rep.group_last_timestamp = members
      .map(rowActivityTs)
      .filter(Boolean)
      .sort()
      .pop() || rowActivityTs(rep);
    groupSiblings.set(rep.session_id, sibs);
    for (const sib of sibs) consumedByGroup.add(sib.session_id);
  }
  let visibleTop = topSessions.filter(s => !consumedByGroup.has(s.session_id));
  visibleTop.sort((a, b) => rowActivityTs(b).localeCompare(rowActivityTs(a)));
  // === GROUP FEATURE END ===

  // Agent filter (Claude / Codex buttons). A linked cluster shows both icons,
  // so keep it if EITHER the rep or any folded sibling matches the filter.
  if (_agentFilter) {
    visibleTop = visibleTop.filter(s => {
      const sibs = groupSiblings.get(s.session_id) || [];
      return [s, ...sibs].some(x => (x.agent || 'claude').toLowerCase() === _agentFilter);
    });
  }

  // The lifelong Serena transcript is a relationship-level chat, not a
  // dated coding session. Keep its single row in a stable home above every
  // ordinary time bucket even if it has not received a turn today.
  const serenaVoice = visibleTop.filter(_isSerenaVoiceSession);
  visibleTop = visibleTop.filter(s => !_isSerenaVoiceSession(s));

  const rowMembers = (s) => [s, ...(groupSiblings.get(s.session_id) || [])];
  // Fleet worker sessions have durable metadata even after their runtime ends.
  // Give every run cluster one stable home instead of letting it leak into
  // Active, Starred, Done, or date buckets.
  const fleetChats = visibleTop.filter(s => rowMembers(s).some(_isFleetSession));
  const fleetSet = new Set(fleetChats.map(s => s.session_id));
  visibleTop = visibleTop.filter(s => !fleetSet.has(s.session_id));

  // Reserved until voice-created chats carry their own explicit origin tag.
  // Do not infer voice ownership from broader Serena work metadata.
  const voiceChats = [];

  const active = _activeTerms.size
    ? visibleTop.filter(s => rowMembers(s).some(x => _activeTerms.has(x.session_id)))
    : [];
  const activeSet = new Set(active.map(s => s.session_id));

  // Done chats — hidden from Active/Starred/time groups, rendered at bottom.
  const doneList = visibleTop.filter(s => s.is_done && !activeSet.has(s.session_id));
  const doneSet = new Set(doneList.map(s => s.session_id));

  const remaining = visibleTop.filter(s => !activeSet.has(s.session_id) && !doneSet.has(s.session_id));
  const starred = remaining.filter(s => s.starred);
  const unstarred = remaining.filter(s => !s.starred);
  const rendered = [];

  let html = '';
  const appendRow = (s) => {
    const children = childrenByParent.get(s.session_id) || [];
    const sibs = groupSiblings.get(s.session_id) || [];
    // A linked claude↔codex pair renders as ONE row showing BOTH agent icons
    // (claude-first, per the group sort). Clicking it opens the split pane and
    // the sibling is reachable there — so siblings no longer get their own row.
    const agents = Array.from(new Set([s, ...sibs].map(x => (x.agent || 'claude').toLowerCase())));
    html += renderSessionRow(s, rendered.length, {
      childCount: children.length,
      isExpanded: _expandedParents.has(s.session_id),
      threadCount: sibs.length,
      agents: agents,
    });
    rendered.push(s);
    // codex children spawned via MCP (legacy nesting) — still expandable
    if (children.length && _expandedParents.has(s.session_id)) {
      for (const child of children) {
        html += renderSessionRow(child, rendered.length, { isChild: true });
        rendered.push(child);
      }
    }
    // (linked siblings are folded into the row above — no separate rows)
  };

  if (serenaVoice.length) {
    html += '<div class="group-header serena-header">Serena</div>';
    for (const s of serenaVoice) appendRow(s);
  }

  if (active.length) {
    html += '<div class="group-header active-header">\u25CF Active Terminals</div>';
    for (const s of active) {
      appendRow(s);
    }
  }

  if (fleetChats.length) {
    const chev = _collapsedState.fleetChats ? '▸' : '▾';
    html += '<div class="group-header fleet-header" data-testid="fleet-chats-header" role="button" aria-expanded="'
      + (!_collapsedState.fleetChats) + '" onclick="toggleFleetChatsCollapsed()">'
      + chev + ' Fleet Chats (' + fleetChats.length + ')</div>';
    // Keep rows mounted so focus, search, and direct Fleet deep-links retain
    // their real session indexes while the visual section is collapsed.
    html += '<div class="fleet-chats-section' + (_collapsedState.fleetChats ? ' collapsed' : '')
      + '" data-testid="fleet-chats-section">';
    // Fleet runs pile up fast, and one flat list of forty-nine worker chats
    // says nothing about what they were for. Group them by the project each
    // run targeted, newest project first, so the section reads like the rest
    // of the sidebar.
    const fleetByProject = new Map();
    for (const s of fleetChats) {
      const label = s.project_short || 'Other';
      if (!fleetByProject.has(label)) fleetByProject.set(label, []);
      fleetByProject.get(label).push(s);
    }
    const fleetProjects = Array.from(fleetByProject.keys()).sort((a, b) => {
      const newest = (label) => fleetByProject.get(label)
        .reduce((acc, x) => (x.last_timestamp || '') > acc ? (x.last_timestamp || '') : acc, '');
      return newest(b).localeCompare(newest(a));
    });
    for (const label of fleetProjects) {
      const rows = fleetByProject.get(label);
      if (fleetProjects.length > 1) {
        html += '<div class="fleet-project-header">' + esc(label)
          + ' <span class="fleet-project-count">' + rows.length + '</span></div>';
      }
      for (const s of rows) appendRow(s);
    }
    html += '</div>';
  }

  const voiceChev = _collapsedState.voiceChats ? '▸' : '▾';
  html += '<div class="group-header voice-chats-header" data-testid="voice-chats-header" role="button" aria-expanded="'
    + (!_collapsedState.voiceChats) + '" onclick="toggleVoiceChatsCollapsed()">'
    + voiceChev + ' Voice Chats (' + voiceChats.length + ')</div>';
  html += '<div class="voice-chats-section' + (_collapsedState.voiceChats ? ' collapsed' : '')
    + '" data-testid="voice-chats-section"></div>';

  if (starred.length) {
    const chev = _collapsedState.starred ? '\u25b8' : '\u25be';
    html += '<div class="group-header starred-header" role="button" aria-expanded="'
      + (!_collapsedState.starred) + '" onclick="toggleStarredCollapsed()">'
      + chev + ' \u2605 Starred (' + starred.length + ')</div>';
    // Rows stay mounted (hidden via CSS) so focus/search keep real indexes.
    html += '<div class="starred-section' + (_collapsedState.starred ? ' collapsed' : '') + '">';
    for (const s of starred) {
      appendRow(s);
    }
    html += '</div>';
  }

  // Count per time group up front so the header can show its size.
  const timeGroupCounts = new Map();
  for (const s of unstarred) {
    const g = timeGroup(rowActivityTs(s));
    timeGroupCounts.set(g, (timeGroupCounts.get(g) || 0) + 1);
  }
  let group = null;
  for (const s of unstarred) {
    const g = timeGroup(rowActivityTs(s));
    if (g !== group) {
      if (group !== null) html += '</div>';
      group = g;
      const collapsed = isTimeGroupCollapsed(g);
      const chev = collapsed ? '\u25b8' : '\u25be';
      html += '<div class="group-header time-header" role="button" aria-expanded="'
        + (!collapsed) + '" onclick="toggleTimeGroupCollapsed(\'' + esc(g).replace(/'/g, "\\'") + '\')">'
        + chev + ' ' + esc(g) + ' (' + (timeGroupCounts.get(g) || 0) + ')</div>';
      // Rows stay mounted (hidden via CSS) so focus/search keep real indexes.
      html += '<div class="time-section' + (collapsed ? ' collapsed' : '') + '">';
    }
    appendRow(s);
  }
  if (group !== null) html += '</div>';

  if (doneList.length) {
    const chev = _collapsedState.done ? '▸' : '▾';
    html += '<div class="group-header done-header" onclick="toggleDoneCollapsed()">'
      + chev + ' ✓ Done (' + doneList.length + ')</div>';
    // Always render the rows so they stay in `sessions`; hide via CSS when collapsed.
    html += '<div class="done-section' + (_collapsedState.done ? ' collapsed' : '') + '">';
    for (const s of doneList) {
      appendRow(s);
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

// === SIDEBAR COLLAPSE STATE ===
// Persisted SERVER-side (/api/ui-state → ~/.config/serena/ui-state.json), not in
// localStorage: the desktop shell binds a fresh port each launch, so a
// localStorage key lives on a throwaway origin and the state would silently
// reset on every restart and never be shared with a browser tab.
let _collapsedState = { fleetChats: true, voiceChats: true, starred: false, done: true, timeGroups: [] };
let _collapsedLoaded = false;
let _timeGroupsCollapsed = new Set();

function _applyCollapsedState(raw) {
  const c = (raw && typeof raw === 'object') ? raw : {};
  if (typeof c.fleetChats === 'boolean') _collapsedState.fleetChats = c.fleetChats;
  if (typeof c.voiceChats === 'boolean') _collapsedState.voiceChats = c.voiceChats;
  if (typeof c.starred === 'boolean') _collapsedState.starred = c.starred;
  if (typeof c.done === 'boolean') _collapsedState.done = c.done;
  _collapsedState.timeGroups = Array.isArray(c.timeGroups) ? c.timeGroups.map(String) : [];
  _timeGroupsCollapsed = new Set(_collapsedState.timeGroups);
}

async function loadCollapsedState() {
  try {
    const r = await fetch('/api/ui-state');
    if (r.ok) {
      const data = await r.json();
      _applyCollapsedState(data && data.collapsed);
    }
  } catch(e) {}
  _collapsedLoaded = true;
  renderSessionList();
}

function _saveCollapsedState() {
  _collapsedState.timeGroups = [..._timeGroupsCollapsed];
  try {
    fetch('/api/ui-state', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ collapsed: _collapsedState }),
    }).catch(() => {});
  } catch(e) {}
}

function toggleFleetChatsCollapsed() {
  _collapsedState.fleetChats = !_collapsedState.fleetChats;
  _saveCollapsedState();
  renderSessionList();
}

function toggleVoiceChatsCollapsed() {
  _collapsedState.voiceChats = !_collapsedState.voiceChats;
  _saveCollapsedState();
  renderSessionList();
}

function toggleStarredCollapsed() {
  _collapsedState.starred = !_collapsedState.starred;
  _saveCollapsedState();
  renderSessionList();
}

// Time buckets (Today / Yesterday / This Week / …) collapse per label, so a
// bucket you shut stays shut while a brand-new bucket (tomorrow's "Today")
// still renders expanded.
function isTimeGroupCollapsed(label) {
  return _timeGroupsCollapsed.has(String(label));
}
function toggleTimeGroupCollapsed(label) {
  const key = String(label);
  if (_timeGroupsCollapsed.has(key)) _timeGroupsCollapsed.delete(key);
  else _timeGroupsCollapsed.add(key);
  _saveCollapsedState();
  renderSessionList();
}

function toggleDoneCollapsed() {
  _collapsedState.done = !_collapsedState.done;
  _saveCollapsedState();
  renderSessionList();
}

function isHiddenCodexPlumbing(s) {
  const origin = String(s.originator || '').toLowerCase();
  return s.parent_session_id === 'orphan-claude-code' || (origin === 'claude code' && !s.parent_session_id);
}

function buildSessionTree(source) {
  const byId = new Map(source.map(s => [s.session_id, s]));
  const childrenByParent = new Map();
  const top = [];

  for (const s of source) {
    if (isHiddenCodexPlumbing(s)) continue;
    const parentId = s.parent_session_id || '';
    if (parentId && byId.has(parentId)) {
      if (!childrenByParent.has(parentId)) childrenByParent.set(parentId, []);
      childrenByParent.get(parentId).push(s);
    } else if (!parentId) {
      top.push(s);
    }
  }

  return { top, childrenByParent };
}

function toggleParentExpansion(sid) {
  if (_expandedParents.has(sid)) _expandedParents.delete(sid);
  else _expandedParents.add(sid);
  renderSessionList();
}

// Bootstrap Icons (MIT) marks for Claude, Codex, and the Serena microphone.
// Currentcolor lets CSS pick the tint.
const _CLAUDE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor"><path d="m3.127 10.604 3.135-1.76.053-.153-.053-.085H6.11l-.525-.032-1.791-.048-1.554-.065-1.505-.08-.38-.081L0 7.832l.036-.234.32-.214.455.04 1.009.069 1.513.105 1.097.064 1.626.17h.259l.036-.105-.089-.065-.068-.064-1.566-1.062-1.695-1.121-.887-.646-.48-.327-.243-.306-.104-.67.435-.48.585.04.15.04.593.456 1.267.981 1.654 1.218.242.202.097-.068.012-.049-.109-.181-.9-1.626-.96-1.655-.428-.686-.113-.411a2 2 0 0 1-.068-.484l.496-.674L4.446 0l.662.089.279.242.411.94.666 1.48 1.033 2.014.302.597.162.553.06.17h.105v-.097l.085-1.134.157-1.392.154-1.792.052-.504.25-.605.497-.327.387.186.319.456-.045.294-.19 1.23-.37 1.93-.243 1.29h.142l.161-.16.654-.868 1.097-1.372.484-.545.565-.601.363-.287h.686l.505.751-.226.775-.707.895-.585.759-.839 1.13-.524.904.048.072.125-.012 1.897-.403 1.024-.186 1.223-.21.553.258.06.263-.218.536-1.307.323-1.533.307-2.284.54-.028.02.032.04 1.029.098.44.024h1.077l2.005.15.525.346.315.424-.053.323-.807.411-3.631-.863-.872-.218h-.12v.073l.726.71 1.331 1.202 1.667 1.55.084.383-.214.302-.226-.032-1.464-1.101-.565-.497-1.28-1.077h-.084v.113l.295.432 1.557 2.34.08.718-.112.234-.404.141-.444-.08-.911-1.28-.94-1.44-.759-1.291-.093.053-.448 4.821-.21.246-.484.186-.403-.307-.214-.496.214-.98.258-1.28.21-1.016.19-1.263.112-.42-.008-.028-.092.012-.953 1.307-1.448 1.957-1.146 1.227-.274.109-.477-.247.045-.44.266-.39 1.586-2.018.956-1.25.617-.723-.004-.105h-.036l-4.212 2.736-.75.096-.324-.302.04-.496.154-.162 1.267-.871z"/></svg>';
const _CODEX_SVG  = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor"><path d="M14.949 6.547a3.94 3.94 0 0 0-.348-3.273 4.11 4.11 0 0 0-4.4-1.934 A4.1 4.1 0 0 0 8.423.2 4.15 4.15 0 0 0 6.305.086a4.1 4.1 0 0 0-1.891.948 4.04 4.04 0 0 0-1.158 1.753 4.1 4.1 0 0 0-1.563.679A4 4 0 0 0 .554 4.72a3.99 3.99 0 0 0 .502 4.731 3.94 3.94 0 0 0 .346 3.274 4.11 4.11 0 0 0 4.402 1.933c.382.425.852.764 1.377.995.526.231 1.095.35 1.67.346 1.78.002 3.358-1.132 3.901-2.804a4.1 4.1 0 0 0 1.563-.68 4 4 0 0 0 1.14-1.253 3.99 3.99 0 0 0-.506-4.716m-6.097 8.406a3.05 3.05 0 0 1-1.945-.694l.096-.054 3.23-1.838a.53.53 0 0 0 .265-.455v-4.49l1.366.778q.02.011.025.035v3.722c-.003 1.653-1.361 2.992-3.037 2.996m-6.53-2.75a2.95 2.95 0 0 1-.36-2.01l.095.057L5.29 12.09a.53.53 0 0 0 .527 0l3.949-2.246v1.555a.05.05 0 0 1-.022.041L6.473 13.3c-1.454.826-3.311.335-4.15-1.098m-.85-6.94A3.02 3.02 0 0 1 3.07 3.949v3.785a.51.51 0 0 0 .262.451l3.93 2.237-1.366.779a.05.05 0 0 1-.048 0L2.585 9.342a2.98 2.98 0 0 1-1.113-4.094zm11.216 2.571L8.747 5.576l1.362-.776a.05.05 0 0 1 .048 0l3.265 1.86a3 3 0 0 1 1.173 1.207 2.96 2.96 0 0 1-.27 3.2 3.05 3.05 0 0 1-1.36.997V8.279a.52.52 0 0 0-.276-.445m1.36-2.015-.097-.057-3.226-1.855a.53.53 0 0 0-.53 0L6.249 6.153V4.598a.04.04 0 0 1 .019-.04L9.533 2.7a3.07 3.07 0 0 1 3.257.139c.474.325.843.778 1.066 1.303.223.526.289 1.103.191 1.664zM5.503 8.575 4.139 7.8a.05.05 0 0 1-.026-.037V4.049c0-.57.166-1.127.476-1.607s.752-.864 1.275-1.105a3.08 3.08 0 0 1 3.234.41l-.096.054-3.23 1.838a.53.53 0 0 0-.265.455zm.742-1.577 1.758-1 1.762 1v2l-1.755 1-1.762-1z"/></svg>';
const _SERENA_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor"><path d="M8 9.5a3 3 0 0 0 3-3v-3a3 3 0 0 0-6 0v3a3 3 0 0 0 3 3Z"/><path d="M3.5 6.5a.5.5 0 0 1 1 0 3.5 3.5 0 0 0 7 0 .5.5 0 0 1 1 0 4.5 4.5 0 0 1-4 4.473V13h2a.5.5 0 0 1 0 1h-5a.5.5 0 0 1 0-1h2v-2.027a4.5 4.5 0 0 1-4-4.473Z"/></svg>';

function _agentBadge(agent) {
  const normalized = String(agent || 'claude').toLowerCase();
  if (normalized === 'codex') return '<span class="agent-icon codex" title="Codex">' + _CODEX_SVG + '</span>';
  if (normalized === 'serena-voice') return '<span class="agent-icon serena" title="Serena">' + _SERENA_SVG + '</span>';
  return '<span class="agent-icon claude" title="Claude">' + _CLAUDE_SVG + '</span>';
}

// Bootstrap Icons "link" — inline so color: var(--group-color) applies.
const _LINK_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor"><path d="M6.354 5.5H4a3 3 0 0 0 0 6h3a3 3 0 0 0 2.83-4H9q-.13 0-.25.031A2 2 0 0 1 7 10.5H4a2 2 0 1 1 0-4h1.535c.218-.376.495-.714.82-1z"/><path d="M9 5.5a3 3 0 0 0-2.83 4h1.098A2 2 0 0 1 9 6.5h3a2 2 0 1 1 0 4h-1.535a4 4 0 0 1-.82 1H12a3 3 0 1 0 0-6z"/></svg>';

function renderSessionRow(s, idx, opts) {
  opts = opts || {};
  const isFocused = idx === focusedIndex;
  const isSelected = selectedIds.has(s.session_id);
  const isActive = _activeTerms.has(s.session_id);
  const isDone = !!s.is_done && !_isSerenaVoiceSession(s);
  const needsAttention = _attentionSids.has(s.session_id);
  const childCount = opts.childCount || 0;
  let cls = 'session-row';
  if (isFocused) cls += ' focused';
  if (isSelected) cls += ' selected';
  if (isActive) cls += ' active-terminal';
  if (isDone && !isActive) cls += ' done';
  if (needsAttention) cls += ' needs-attention';
  if (opts.isChild) cls += ' child-session';
  if (_isSerenaVoiceSession(s)) cls += ' serena-voice';
  // === GROUP FEATURE === (color stripe + link glyph + thread sibling cluster)
  const groupId = s.group || null;
  const groupColor = groupId ? _groupColor(groupId) : null;
  const groupStyle = groupColor ? ' style="--group-color:' + groupColor + '"' : '';
  if (groupId) cls += ' has-group';
  if (opts.isThreadSibling) cls += ' thread-sibling';
  if (opts.threadCount) cls += ' thread-head';
  // === GROUP FEATURE END ===

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

  const disclosure = childCount
    ? '<span class="session-disclosure" title="' + childCount + ' nested Codex session' + (childCount === 1 ? '' : 's') + '" '
      + 'onclick="event.stopPropagation();toggleParentExpansion(\'' + s.session_id + '\')">'
      + (opts.isExpanded ? '\u25BE' : '\u25B8') + '</span>'
    : '';
  const childBadge = childCount
    ? '<span class="child-count-badge">+' + childCount + '</span>'
    : '';
  const childAttr = childCount ? ' data-children-count="' + childCount + '"' : '';

  // === GROUP FEATURE === (link glyph rendered between star and title.
  // Inline SVG with fill=currentColor so the CSS --group-color applies —
  // an emoji 🔗 ignores `color:` and renders with the system emoji palette,
  // which made claude/codex rows show different hues for the same thread.)
  const linkGlyph = groupId
    ? '<span class="session-link" title="Linked thread — click to cycle to next sibling" '
      + 'onclick="event.stopPropagation();_cycleGroup(\'' + s.session_id + '\')">'
      + _LINK_SVG
      + '</span>'
    : '';
  // Linked pair → both agent icons on the one row (claude then codex); the
  // ↪count badge is redundant once both symbols show, so suppress it then.
  const dualAgents = opts.agents && opts.agents.length > 1;
  const agentBadges = dualAgents
    ? opts.agents.map(_agentBadge).join('')
    : _agentBadge(s.agent);
  const threadBadge = (opts.threadCount && opts.threadCount > 0
      && (!dualAgents || opts.threadCount > 1))
    ? '<span class="thread-count-badge" title="' + (opts.threadCount + 1) + ' linked chats">↪ ' + (opts.threadCount + 1) + '</span>'
    : '';
  // === GROUP FEATURE END ===
  // Search mode: show the matched message excerpt (>>>hit<<< → highlighted) on
  // its own line under the title, so a content match is actually visible.
  const snippetHtml = s.search_snippet
    ? '<span class="session-snippet">' + _snippetHtml(s.search_snippet) + '</span>'
    : '';
  return '<div class="' + cls + '" data-idx="' + idx + '" data-sid="' + s.session_id + '"' + childAttr + groupStyle + ' '
    + 'onclick="onRowClick(event,' + idx + ')" ondblclick="openConv(\'' + s.session_id + '\')"'
    + ' oncontextmenu="showSessionContextMenu(event,' + idx + ')">'
    + '<span class="' + starCls + '" onclick="event.stopPropagation();toggleStar(\'' + s.session_id + '\')">' + starChar + '</span>'
    + disclosure
    + linkGlyph
    + '<span class="session-title"><span class="session-title-main">' + liveIndicator + agentBadges + esc(_isSerenaVoiceSession(s) ? 'Serena' : (s.display_title || 'Untitled')) + childBadge + threadBadge + '</span>' + snippetHtml + '</span>'
    + '<span class="session-date" title="Last activity">' + formatDate(rowActivityTs(s)) + '</span>'
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
  // Any click on a chat clears its needs-attention flag (and its linked
  // siblings' too — handled on the server).
  const _sid = sessions[idx] && sessions[idx].session_id;
  if (_sid) _clearAttention(_sid);
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
// CHATS: Right-click context menu
// ═══════════════════════════════════════════════════════════════
let _ctxMenuEl = null;

function closeContextMenu() {
  if (_ctxMenuEl) { _ctxMenuEl.remove(); _ctxMenuEl = null; }
}

function showSessionContextMenu(evt, idx) {
  evt.preventDefault();
  evt.stopPropagation();
  closeContextMenu();
  if (idx < 0 || idx >= sessions.length) return;
  const s = sessions[idx];
  if (!s) return;
  // If the right-clicked row isn't already in selection, focus it and treat
  // the menu as a single-target action. If it IS in a multi-selection, keep
  // the selection intact and offer bulk actions.
  const inMultiSelect = selectedIds.size > 1 && selectedIds.has(s.session_id);
  if (!inMultiSelect) {
    setFocus(idx, false);
    if (!selectedIds.has(s.session_id)) {
      // Single-target: clear other selections, mark this row only
      selectedIds.clear();
    }
  }
  const sid = s.session_id;
  const count = inMultiSelect ? selectedIds.size : 1;
  const label = inMultiSelect ? ' (' + count + ')' : '';
  const isDone = !!s.is_done;
  const isStarred = !!s.starred;
  const isSerenaVoice = _isSerenaVoiceSession(s);
  const isFleetWorker = _isFleetSession(s);
  const isReadOnlyTranscript = isSerenaVoice || isFleetWorker;

  const items = [];
  if (!inMultiSelect) {
    items.push({ label: isReadOnlyTranscript ? 'Open' : 'Resume', key: 'Enter', action: () => openConv(sid) });
    if (!isReadOnlyTranscript) {
      items.push({ label: 'Resume in Terminal', key: 'O', action: () => resumeSession(sid) });
    }
    items.push({ sep: true });
  }
  items.push({
    label: (isStarred ? 'Unstar' : 'Star') + label,
    key: 'S',
    action: () => inMultiSelect ? bulkToggleStar() : toggleStar(sid),
  });
  if (!isReadOnlyTranscript) {
    items.push({
      label: (isDone ? 'Reopen' : 'Mark Done') + label,
      key: 'D',
      action: () => inMultiSelect ? bulkToggleDone() : toggleDone(sid),
    });
  }
  if (!inMultiSelect && !isReadOnlyTranscript) {
    items.push({ label: 'Rename',         key: 'R', action: () => renameSession(sid) });
  }
  if (!isReadOnlyTranscript) {
    items.push({
      label: 'AI Title' + label,
      key: 'T',
      action: () => inMultiSelect ? bulkRetitle() : retitleSession(sid),
    });
  }
  // === HANDOFF FEATURE START === (remove this block to unwire the menu items)
  if (!inMultiSelect && !isReadOnlyTranscript) {
    // Both directions, always. Each jumps to that agent's chat in the thread
    // (reuse the linked one if it exists; spin one up if it doesn't). Handing off
    // to the agent you're already on just refocuses it — no dup, no nag.
    items.push({ sep: true });
    items.push({ label: 'Hand off → Claude', action: () => handoffSession(sid, 'claude') });
    items.push({ label: 'Hand off → Codex',  action: () => handoffSession(sid, 'codex') });
  }
  // === HANDOFF FEATURE END ===
  if (!inMultiSelect && !isReadOnlyTranscript && s.group) {
    items.push({ sep: true });
    items.push({ label: 'Fork context → Claude', action: () => forkLinkedContext(sid, 'claude') });
    items.push({ label: 'Fork context → Codex', action: () => forkLinkedContext(sid, 'codex') });
  }
  // === GROUP FEATURE START === (remove this block to unwire the menu items)
  if (!isReadOnlyTranscript) {
    items.push({ sep: true });
    if (inMultiSelect) {
      items.push({
        label: 'Link ' + count + ' chats',
        action: () => linkSessions([...selectedIds]).then(r => r && showToast('Linked ' + count, { variant: 'success' })),
      });
    } else {
      items.push({
        label: 'Link to chat…',
        action: () => linkChatPickerFlow(sid),
      });
      if (s.group) {
        const sibs = _siblingsInGroup(s.group, sid);
        for (const sib of sibs.slice(0, 6)) {
          items.push({
            label: '↳ ' + (sib.display_title || 'Untitled'),
            action: () => {
              const i = sessions.findIndex(x => x.session_id === sib.session_id);
              if (i >= 0) { setFocus(i, true); openConv(sib.session_id); }
            },
          });
        }
        items.push({ label: 'Unlink this', action: () => unlinkSession(sid) });
        items.push({ label: 'Disband linked thread', danger: true, action: () => disbandGroup(s.group) });
      }
    }
  }
  // === GROUP FEATURE END ===
  if (!isReadOnlyTranscript) {
    items.push({ sep: true });
    items.push({
      label: 'Delete' + label,
      key: 'Alt+Del',
      danger: true,
      action: () => inMultiSelect ? bulkDelete() : deleteSession(sid),
    });
  }

  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  let html = '';
  for (const it of items) {
    if (it.sep) { html += '<div class="ctx-menu-sep"></div>'; continue; }
    html += '<div class="ctx-menu-item' + (it.danger ? ' danger' : '') + '">'
         +    '<span>' + esc(it.label) + '</span>'
         +    (it.key ? '<span class="ctx-menu-key">' + esc(it.key) + '</span>' : '')
         +  '</div>';
  }
  menu.innerHTML = html;
  document.body.appendChild(menu);

  // Wire actions
  const itemEls = menu.querySelectorAll('.ctx-menu-item');
  let actionIdx = 0;
  for (const it of items) {
    if (it.sep) continue;
    const action = it.action;
    const el = itemEls[actionIdx++];
    el.addEventListener('click', () => { closeContextMenu(); try { action(); } catch (e) { console.error(e); } });
  }

  // Position so it stays on-screen
  const pad = 4;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  menu.style.visibility = 'hidden';
  menu.style.left = '0px';
  menu.style.top = '0px';
  const rect = menu.getBoundingClientRect();
  let x = evt.clientX;
  let y = evt.clientY;
  if (x + rect.width + pad > vw) x = Math.max(pad, vw - rect.width - pad);
  if (y + rect.height + pad > vh) y = Math.max(pad, vh - rect.height - pad);
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.style.visibility = 'visible';
  _ctxMenuEl = menu;
}

document.addEventListener('click', (e) => {
  if (_ctxMenuEl && !_ctxMenuEl.contains(e.target)) closeContextMenu();
});
document.addEventListener('contextmenu', (e) => {
  // If the right-click was outside any session row AND outside our menu, close
  if (_ctxMenuEl && !_ctxMenuEl.contains(e.target) && !e.target.closest('.session-row')) {
    closeContextMenu();
  }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && _ctxMenuEl) { closeContextMenu(); }
}, true);
window.addEventListener('blur', closeContextMenu);
window.addEventListener('scroll', closeContextMenu, true);

// ═══════════════════════════════════════════════════════════════
// CHATS: Conversation
// ═══════════════════════════════════════════════════════════════
async function openConv(sid, opts) {
  opts = opts || {};
  const switching = currentSessionId !== sid;
  const local = _findClientSession(sid);
  const externallyRunning = Boolean(local && local.external_runtime_active);
  const serenaVoice = _isSerenaVoiceSession(local || sid);
  const fleetWorker = _isFleetSession(local);
  const readOnly = externallyRunning || serenaVoice || fleetWorker;
  const showReadView = opts.mode === 'read' || readOnly;
  document.getElementById('viewLiveBtn').classList.toggle('hidden', serenaVoice || fleetWorker);
  if (switching) {
    // GTK: Python keeps every VTE alive in a stack — code-on swaps the visible child.
    // Web: termSessions Map keeps every xterm + WebSocket alive — switch panes only.
    convMode = showReadView ? 'read' : 'live';
    document.getElementById('viewReadBtn').classList.toggle('active', showReadView);
    document.getElementById('viewLiveBtn').classList.toggle('active', !showReadView);
    document.getElementById('convBody').classList.toggle('hidden', !showReadView);
    document.getElementById('convTerminal').classList.toggle('hidden', showReadView);
    if (showReadView) {
      if (window.__nativeTerminalBridge) stopGtkCode();
      else _hideAllTermPanes();
    }
  } else if (showReadView && convMode !== 'read') {
    // A Fleet deep-link can target the already-selected chat. Honour the
    // explicit read-only request instead of leaving its terminal visible.
    convMode = 'read';
    document.getElementById('viewReadBtn').classList.add('active');
    document.getElementById('viewLiveBtn').classList.remove('active');
    document.getElementById('convBody').classList.remove('hidden');
    document.getElementById('convTerminal').classList.add('hidden');
    if (window.__nativeTerminalBridge) stopGtkCode();
    else _hideAllTermPanes();
  }
  currentSessionId = sid;
  if (switching) resetCodeTabs();  // drop previous chat's file tabs
  _convLoaded.delete(sid);  // invalidate so Read view re-fires if user switches to it
  document.getElementById('convEmpty').classList.add('hidden');
  document.getElementById('convContent').classList.remove('hidden');

  // Fire Code immediately — don't wait for any fetch
  if (switching && convMode === 'live') {
    if (window.__nativeTerminalBridge) startGtkCode(sid);
    else startLiveTerminal(sid);
  }

  // Pull session metadata from the already-loaded sessions array; no network hop.
  if (local) {
    document.getElementById('convTitle').textContent = serenaVoice
      ? 'Serena'
      : (local.display_title || 'Untitled');
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
  else _stopExternalReadRefresh();
  if (_filesVisible) loadFiles(sid);
}

function closeConv() {
  // Hide the panel; keep terminals alive so re-opening is instant.
  // Use Alt+w (close-terminal) or app-close to actually kill a session.
  if (window.__nativeTerminalBridge) stopGtkCode();
  convMode = 'live';
  document.getElementById('viewReadBtn').classList.remove('active');
  document.getElementById('viewLiveBtn').classList.add('active');
  document.getElementById('viewLiveBtn').classList.remove('hidden');
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

async function bulkToggleStar() {
  if (!selectedIds.size) return;
  const sids = [...selectedIds];
  // Star the whole selection, and only clear it when every chat in that
  // selection is already starred. Toggling each row independently meant a
  // mixed selection silently UNstarred whatever was already marked, which is
  // how a batch of stars disappeared without anyone touching them.
  const picked = sids
    .map(sid => _findClientSession(sid))
    .filter(Boolean);
  const desired = !(picked.length && picked.every(s => s.starred));
  const needsFlip = sids.filter(sid => {
    const s = _findClientSession(sid);
    return !s || Boolean(s.starred) !== desired;
  });
  if (!needsFlip.length) return;
  try {
    await Promise.all(needsFlip.map(sid => fetch('/api/star/' + sid, { method: 'POST' })));
    await loadSessions(currentProject);
  } catch(e) {}
}

async function deleteSession(sid) {
  const target = sessions.find(s => s.session_id === sid);
  if (_isSerenaVoiceSession(target || sid)) {
    showToast('Serena is permanent', { variant: 'error' });
    return;
  }
  const title = target && target.display_title ? target.display_title : sid.slice(0, 8);
  const ok = await showConfirm({
    title: 'Move conversation to trash?',
    body: 'Move "' + title + '" to recoverable trash?',
    confirm: 'Move to Trash',
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
  const ids = Array.from(selectedIds).filter(sid => !_isSerenaVoiceSession(_findClientSession(sid) || sid));
  if (ids.length === 0) {
    showToast('Serena is permanent', { variant: 'error' });
    return;
  }
  const n = ids.length;
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
      body: JSON.stringify({ ids }),
    });
    if (ids.includes(currentSessionId)) closeConv();
    for (const sid of ids) selectedIds.delete(sid);
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
    // Instant: the title is already persisted server-side, so just patch the
    // local copies + re-render. No full /api/sessions refetch (that reload was
    // the lag — it re-pulled and re-rendered every chat just to change one row).
    const t = title.trim();
    for (const arr of [allSessions, sessionSource, sessions]) {
      if (!Array.isArray(arr)) continue;
      const o = arr.find(x => x && x.session_id === sid);
      if (o) { o.display_title = t; o.custom_title = t; }
    }
    renderSessionList();
    if (currentSessionId === sid) {
      document.getElementById('convTitle').textContent = t;
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
  const target = _findClientSession(sid);
  if (_isSerenaVoiceSession(target || sid)) {
    openConv(sid);
    return;
  }
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
      _collapsedState.done = false;
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
      _collapsedState.done = false;
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
  const local = _findClientSession(sid);
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
      + 'onclick="openFileTab(\'' + escAttr(abs) + '\')" '
      + 'style="padding-left:' + pad + 'px" '
      + 'title="Click to open · Drag into terminal">'
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
  const div = document.getElementById('convFilesDivider');
  if (div) div.classList.toggle('hidden', !_filesVisible || _focusMode);
  if (_filesVisible) {
    if (currentSessionId && _filesSid !== currentSessionId) loadFiles(currentSessionId);
  }
  // Terminal rect changed — nudge Python to re-lay-out the VTE overlay
  if (window.__nativeTerminalBridge && convMode === 'live') {
    requestAnimationFrame(() => {
      const rect = _gtkGetRect();
      if (rect) window.gtkSend({ type: 'code-rect', rect });
    });
  }
}

// ═══════════════════════════════════════════════════════════════
// FOCUS MODE — Alt+B hides project sidebar + chat list + files pane
// (and all 3 dividers) so the conv/code area takes the entire
// window. Useful when running a split (claude+codex) so both VTEs
// get max width. Toggle Alt+B again to restore.
// ═══════════════════════════════════════════════════════════════
let _focusMode = false;
let _focusPriorFiles = null;  // remembers files-pane visibility from before focus
// Intentionally NOT persisted across launches — too easy to forget you're in
// focus mode and think the app is broken when the chat list is hidden.

function applyFocusMode() {
  const proj = document.getElementById('projectSidebar');
  const chats = document.getElementById('chatListCol');
  const files = document.getElementById('filesPane');
  const dProjChats = document.querySelector('.pane-divider[data-divider="proj-chats"]');
  const dChatsConv = document.querySelector('.pane-divider[data-divider="chats-conv"]');
  const dConvFiles = document.getElementById('convFilesDivider');
  if (_focusMode) {
    if (proj) proj.classList.add('hidden');
    if (chats) chats.classList.add('hidden');
    if (files) files.classList.add('hidden');
    if (dProjChats) dProjChats.classList.add('hidden');
    if (dChatsConv) dChatsConv.classList.add('hidden');
    if (dConvFiles) dConvFiles.classList.add('hidden');
  } else {
    if (proj) proj.classList.remove('hidden');
    if (chats) chats.classList.remove('hidden');
    if (dProjChats) dProjChats.classList.remove('hidden');
    if (dChatsConv) dChatsConv.classList.remove('hidden');
    // Restore files pane to its pre-focus state
    if (files) files.classList.toggle('hidden', !_filesVisible);
    if (dConvFiles) dConvFiles.classList.toggle('hidden', !_filesVisible);
  }
}

function toggleFocusMode() {
  if (!_focusMode) {
    _focusPriorFiles = _filesVisible;
  } else if (_focusPriorFiles !== null) {
    _filesVisible = _focusPriorFiles;
  }
  _focusMode = !_focusMode;
  applyFocusMode();
  // Nudge GTK to recompute the VTE rect — split panes need the new dimensions
  if (window.__nativeTerminalBridge && convMode === 'live') {
    requestAnimationFrame(() => {
      const rect = _gtkGetRect();
      if (rect) window.gtkSend({ type: 'code-rect', rect });
    });
  }
}


function insertFilePath(absPath) {
  if (!currentSessionId) return;
  const text = "'" + String(absPath).replace(/'/g, "'\\''") + "' ";
  if (window.__nativeTerminalBridge) {
    window.gtkSend({ type: 'feed-text', sid: currentSessionId, text });
    return;
  }
  const runtime = termSessions.get(currentSessionId);
  if (runtime && runtime.ws && runtime.ws.readyState === 1) runtime.ws.send(text);
}

// ═══════════════════════════════════════════════════════════════
// Code-area editor tabs — the terminal/split pane is one tab; clicking
// a file in the tree opens it as another tab (read-only, line numbers),
// VS-Code style. Files live INSIDE the Code pane, not a fullscreen modal.
// ═══════════════════════════════════════════════════════════════
const _openFiles = [];          // [{path, name}] — open file tabs, in order
let _codeTab = '__term__';      // active tab: '__term__' or a file path
const _filePanes = new Map();   // path -> {el, bodyEl, metaEl, loaded, text}

function _codeTermAgents() {
  const src = (sessionSource.length ? sessionSource : sessions);
  const me = src.find(x => x && x.session_id === currentSessionId);
  const agents = [];
  if (me) agents.push((me.agent || 'claude').toLowerCase());
  const sib = _linkedSiblingSid(currentSessionId);
  if (sib) {
    const s = src.find(x => x && x.session_id === sib);
    if (s) agents.push((s.agent || 'claude').toLowerCase());
  }
  return Array.from(new Set(agents));
}

function renderCodeTabs() {
  const el = document.getElementById('codeTabs');
  if (!el) return;
  const agents = _codeTermAgents();
  const icons = agents.map(_agentBadge).join('');
  const tlabel = agents.length > 1 ? 'Split' : 'Terminal';
  let html = '<div class="code-tab' + (_codeTab === '__term__' ? ' active' : '') + '" data-tabid="__term__">'
    + icons + '<span>' + tlabel + '</span></div>';
  for (const f of _openFiles) {
    html += '<div class="code-tab' + (_codeTab === f.path ? ' active' : '') + '" '
      + 'data-tabid="' + escAttr(f.path) + '" title="' + escAttr(f.path) + '">'
      + '<span>' + esc(f.name) + '</span>'
      + '<span class="ct-close" data-close="' + escAttr(f.path) + '" title="Close">✕</span></div>';
  }
  el.innerHTML = html;
}

async function openFileTab(absPath) {
  if (convMode !== 'live') setConvMode('live');
  const name = absPath.split('/').pop();
  if (!_openFiles.some(f => f.path === absPath)) _openFiles.push({ path: absPath, name });
  _codeTab = absPath;
  renderCodeTabs();
  switchCodeTab(absPath);
}

function switchCodeTab(id) {
  _codeTab = id;
  document.getElementById('termPane').classList.toggle('hidden', id !== '__term__');
  for (const [p, info] of _filePanes) info.el.classList.toggle('hidden', p !== id);
  document.querySelectorAll('#codeTabs .code-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tabid === id));
  if (id === '__term__') {
    // Bring the live terminal/split pane back to front.
    if (window.__nativeTerminalBridge) {
      if (currentSessionId) startGtkCode(currentSessionId);
    } else if (currentSessionId && termSessions.has(currentSessionId)) {
      _activateTermPane(currentSessionId);
    }
  } else {
    // Showing a file — on GTK the native VTE overlay floats on top, so hide it.
    if (window.__nativeTerminalBridge) stopGtkCode();
    _ensureFilePane(id);
  }
}

function _ensureFilePane(path) {
  let info = _filePanes.get(path);
  if (info) { info.el.classList.toggle('hidden', _codeTab !== path); return info; }
  const wrap = document.getElementById('codePaneWrap');
  const el = document.createElement('div');
  el.className = 'code-pane file-pane';
  const head = document.createElement('div');
  head.className = 'file-pane-head';
  const meta = document.createElement('span'); meta.className = 'fv-meta'; meta.textContent = 'Loading…';
  const spacer = document.createElement('span'); spacer.className = 'fv-spacer';
  const bCopy = document.createElement('button'); bCopy.className = 'fv-btn'; bCopy.textContent = 'Copy';
  const bRev = document.createElement('button'); bRev.className = 'fv-btn'; bRev.textContent = 'Reveal';
  const bOpen = document.createElement('button'); bOpen.className = 'fv-btn'; bOpen.textContent = 'Open';
  bCopy.onclick = () => {
    const i = _filePanes.get(path);
    if (i && i.text != null) navigator.clipboard.writeText(i.text)
      .then(() => showToast('copied', { variant: 'success' }))
      .catch(() => showToast('copy failed', { variant: 'error' }));
  };
  bRev.onclick = () => openFilePath(path, true);
  bOpen.onclick = () => openFilePath(path, false);
  head.appendChild(meta); head.appendChild(spacer);
  head.appendChild(bCopy); head.appendChild(bRev); head.appendChild(bOpen);
  const body = document.createElement('div');
  body.className = 'file-pane-body';
  body.innerHTML = '<div class="fileviewer-empty">Loading…</div>';
  el.appendChild(head); el.appendChild(body);
  wrap.appendChild(el);
  info = { el, bodyEl: body, metaEl: meta, loaded: false, text: null };
  _filePanes.set(path, info);
  el.classList.toggle('hidden', _codeTab !== path);
  _loadFilePane(path);
  return info;
}

async function _loadFilePane(path) {
  const info = _filePanes.get(path);
  if (!info) return;
  try {
    const r = await fetch('/api/read-file?path=' + encodeURIComponent(path));
    const d = await r.json();
    if (!r.ok) {
      info.bodyEl.innerHTML = '<div class="fileviewer-empty">' + esc(d.error || ('HTTP ' + r.status)) + '</div>';
      return;
    }
    info.text = d.content || '';
    info.loaded = true;
    const kb = d.size != null ? (d.size < 1024 ? d.size + ' B' : Math.round(d.size / 1024) + ' KB') : '';
    info.metaEl.textContent = [d.lang || '', (d.lines || 0) + ' lines', kb].filter(Boolean).join(' · ');
    const lines = info.text.split('\n');
    let gutter = '';
    for (let i = 1; i <= lines.length; i++) gutter += i + '\n';
    info.bodyEl.innerHTML =
      '<div class="fv-code"><div class="fv-gutter"><pre>' + esc(gutter) + '</pre></div>'
      + '<pre class="fv-content">' + esc(info.text) + '</pre></div>';
  } catch (e) {
    info.bodyEl.innerHTML = '<div class="fileviewer-empty">load failed: ' + esc(e.message) + '</div>';
  }
}

function closeFileTab(path, ev) {
  if (ev) ev.stopPropagation();
  const idx = _openFiles.findIndex(f => f.path === path);
  if (idx >= 0) _openFiles.splice(idx, 1);
  const info = _filePanes.get(path);
  if (info) { info.el.remove(); _filePanes.delete(path); }
  const wasActive = _codeTab === path;
  const next = wasActive
    ? (_openFiles.length ? _openFiles[Math.min(idx, _openFiles.length - 1)].path : '__term__')
    : _codeTab;
  renderCodeTabs();
  if (wasActive) switchCodeTab(next);
}

// Clear file tabs when switching chats — files are per-chat context.
function resetCodeTabs() {
  for (const [, info] of _filePanes) info.el.remove();
  _filePanes.clear();
  _openFiles.length = 0;
  _codeTab = '__term__';
  renderCodeTabs();
}

// Re-render tabs + show the active pane (called on entering Code view).
function syncCodeView() {
  renderCodeTabs();
  if (_codeTab !== '__term__' && !_filePanes.has(_codeTab)) _codeTab = '__term__';
  document.getElementById('termPane').classList.toggle('hidden', _codeTab !== '__term__');
  for (const [p, info] of _filePanes) info.el.classList.toggle('hidden', p !== _codeTab);
}

document.getElementById('codeTabs').addEventListener('click', (e) => {
  const closeEl = e.target.closest('.ct-close');
  if (closeEl) { closeFileTab(closeEl.dataset.close, e); return; }
  const tab = e.target.closest('.code-tab');
  if (tab) switchCodeTab(tab.dataset.tabid);
});

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
const _pendingTermPartners = new Map(); // temporary pairs created by bridge auto-spawn
const _termStarting = new Set();  // prevent duplicate PTYs from fast repeated opens
let _webRuntimePollTimer = null;
let _webRuntimeFocusSid = null;

async function _pasteTerminalClipboard(sid, ws) {
  let images = [];
  if (navigator.clipboard && navigator.clipboard.read) {
    try {
      const items = await navigator.clipboard.read();
      for (const item of items) {
        const type = (item.types || []).find(value => value.startsWith('image/'));
        if (!type) continue;
        const blob = await item.getType(type);
        const ext = (type.split('/')[1] || 'png').replace('jpeg', 'jpg');
        images.push(new File([blob], 'clipboard.' + ext, { type }));
      }
    } catch(e) {
      images = [];
    }
  }

  if (images.length) {
    const toast = showToast('Attaching clipboard image…', { spinner: true, sticky: true });
    const paths = [];
    try {
      for (const file of images) {
        const form = new FormData();
        form.append('file', file, file.name);
        const response = await fetch('/api/upload-image', { method: 'POST', body: form });
        const data = await response.json();
        if (!response.ok || !data.path) throw new Error(data.error || 'upload failed');
        paths.push(data.path);
      }
      const input = paths.map(path => "'" + path.replace(/'/g, "'\\''") + "' ").join('');
      if (ws.readyState === 1) ws.send(input);
      if (window.__termDrafts) {
        window.__termDrafts.set(sid, (window.__termDrafts.get(sid) || '') + input);
      }
      toast.update(images.length === 1 ? 'Image attached' : (images.length + ' images attached'), 'success');
    } catch(e) {
      toast.update('Image paste failed: ' + e.message, 'error');
    }
    return;
  }

  try {
    const text = await navigator.clipboard.readText();
    if (!text) return;
    if (window.__termDrafts) {
      window.__termDrafts.set(sid, (window.__termDrafts.get(sid) || '') + text);
    }
    if (ws.readyState === 1) ws.send(text);
  } catch(e) {}
}

function _extendTermKeyboardSelection(state, key) {
  const term = state && state.term;
  const buffer = term && term.buffer && term.buffer.active;
  if (!term || !buffer) return false;

  const cols = Math.max(1, term.cols || 1);
  const lastOffset = Math.max(0, (buffer.length || 1) * cols);
  let selection = state.keyboardSelection;
  if (!selection) {
    const cursorRow = Math.max(0, buffer.baseY + buffer.cursorY);
    const cursorCol = Math.max(0, Math.min(cols, buffer.cursorX));
    const cursorOffset = Math.min(lastOffset, cursorRow * cols + cursorCol);
    selection = { anchor: cursorOffset, focus: cursorOffset };
  }

  const delta = {
    arrowleft: -1,
    arrowright: 1,
    arrowup: -cols,
    arrowdown: cols,
  }[key];
  if (delta === undefined) return false;

  selection.focus = Math.max(0, Math.min(lastOffset, selection.focus + delta));
  state.keyboardSelection = selection;
  if (selection.focus === selection.anchor) {
    term.clearSelection();
    return true;
  }

  const start = Math.min(selection.anchor, selection.focus);
  const end = Math.max(selection.anchor, selection.focus);
  term.select(start % cols, Math.floor(start / cols), end - start);

  const focusRow = Math.min(
    Math.max(0, (buffer.length || 1) - 1),
    Math.floor(selection.focus / cols),
  );
  if (focusRow < buffer.viewportY) {
    term.scrollToLine(focusRow);
  } else if (focusRow >= buffer.viewportY + term.rows) {
    term.scrollToLine(Math.max(0, focusRow - term.rows + 1));
  }
  return true;
}

function _resetTermKeyboardSelection(state, clearVisible = false) {
  if (!state) return;
  state.keyboardSelection = null;
  if (clearVisible && state.term) state.term.clearSelection();
}

// === TERM CAPTURE KEYS === WebView2 on Windows intercepts Ctrl+C/V/A as
// browser accelerators BEFORE xterm.js's custom-key handler runs. We win
// the race by hooking the document-level keydown event in the CAPTURE phase
// (runs before any element handler + before browser default). Scoped: only
// fires when focus is inside the active xterm.js terminal.
function _installTermKeyCapture() {
  if (window.__termKeyCaptureInstalled) return;
  window.__termKeyCaptureInstalled = true;
  if (!window.__termDrafts) window.__termDrafts = new Map();
  document.addEventListener('keydown', (e) => {
    // Only act when the active terminal has focus
    const sid = activeTermSid;
    if (!sid) return;
    const s = termSessions.get(sid);
    if (!s || !s.term) return;
    // xterm.js focuses its helper textarea / screen — check that focus is
    // inside the terminal's root element, not in some other textbox.
    const root = s.term.element;
    if (!root) return;
    if (!root.contains(document.activeElement)) return;
    const ws = s.ws;
    if (!ws) return;
    const k = (e.key || '').toLowerCase();

    // These editing keys must be handled before xterm/WebKit can reinterpret
    // them as browser or widget shortcuts.
    if (e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey && k === 'enter') {
      _resetTermKeyboardSelection(s, true);
      window.__termDrafts.set(sid, (window.__termDrafts.get(sid) || '') + '\n');
      try { ws.send('\n'); } catch (_) {}
      e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
      return;
    }
    if (e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey && k === 'backspace') {
      _resetTermKeyboardSelection(s, true);
      const draft = window.__termDrafts.get(sid) || '';
      const stripped = draft.replace(/\s+$/, '');
      const lastSpace = Math.max(stripped.lastIndexOf(' '), stripped.lastIndexOf('\n'));
      window.__termDrafts.set(sid, lastSpace >= 0 ? stripped.slice(0, lastSpace + 1) : '');
      try { ws.send('\x17'); } catch (_) {}
      e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
      return;
    }
    if (e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey && k.startsWith('arrow')) {
      if (_extendTermKeyboardSelection(s, k)) {
        e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
      }
      return;
    }

    // Ctrl+A — copy current input draft to clipboard
    if (e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey && k === 'a') {
      const draft = window.__termDrafts.get(sid) || '';
      if (draft) navigator.clipboard.writeText(draft).catch(() => {});
      e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
      return;
    }
    // Ctrl+C — smart: copy selection if any, else SIGINT to PTY
    if (e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey && k === 'c') {
      const sel = s.term.getSelection();
      if (sel) {
        navigator.clipboard.writeText(sel).catch(() => {});
        s.term.clearSelection();
        _resetTermKeyboardSelection(s);
      } else {
        _resetTermKeyboardSelection(s, true);
        window.__termDrafts.set(sid, '');
        try { ws.send('\x03'); } catch (_) {}
      }
      e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
      return;
    }
    // Ctrl+V — paste clipboard into PTY + append to draft
    if (e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey && k === 'v') {
      _resetTermKeyboardSelection(s, true);
      _pasteTerminalClipboard(sid, ws);
      e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
      return;
    }
    // Copy shortcuts preserve the highlight. Any other non-modifier key ends
    // keyboard selection before xterm or the PTY handles the input.
    const copyKey = e.ctrlKey && !e.altKey && !e.metaKey && k === 'c';
    const modifierKey = k === 'shift' || k === 'control' || k === 'alt' || k === 'meta';
    if (!copyKey && !modifierKey) _resetTermKeyboardSelection(s, true);
  }, /* capture */ true);

  document.addEventListener('pointerdown', (e) => {
    for (const state of termSessions.values()) {
      if (state.term && state.term.element && state.term.element.contains(e.target)) {
        _resetTermKeyboardSelection(state);
        break;
      }
    }
  }, /* capture */ true);
}
// === TERM CAPTURE KEYS END ===
let _termResizeObs = null;        // single observer on #termMounts container
const _convLoaded = new Set();    // sids whose transcript is already in the DOM
const _activeTerms = new Set();   // sids with a running terminal — cleared on app close
const _gtkReadyTerms = new Set(); // sids whose GTK VTE has been built server-side
const _activeMeta = new Map();    // sid -> { cwd, activatedAt } for /clear migration
const _pseudoSessions = [];       // synthetic rows for brand-new chats (temp ids)
const _resolvedPseudoSids = new Map(); // stale UI events can still resolve after migration
// === ATTENTION === (sids of chats that finished a turn since user last
// looked at them — visual glow on sidebar entry + split-view VTE)
const _attentionSids = new Set();
let _attentionPollTimer = null;
function _startAttentionPoll() {
  if (_attentionPollTimer) return;
  const tick = async () => {
    try {
      const r = await fetch('/api/chat-attention');
      if (!r.ok) return;
      const data = await r.json();
      const fresh = new Set(Object.keys(data.sessions || {}));
      // Only re-render if the set actually changed
      let changed = fresh.size !== _attentionSids.size;
      if (!changed) {
        for (const s of fresh) { if (!_attentionSids.has(s)) { changed = true; break; } }
      }
      if (changed) {
        _attentionSids.clear();
        for (const s of fresh) _attentionSids.add(s);
        renderSessionList();
        _applyAttentionToSplitView();
      }
    } catch(e) {}
  };
  tick();
  _attentionPollTimer = setInterval(tick, 2000);
}
function _clearAttention(sid) {
  if (!_attentionSids.has(sid)) return;
  _attentionSids.delete(sid);
  fetch('/api/chat-attention/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid }),
  }).catch(() => {});
  renderSessionList();
  _applyAttentionToSplitView();
}
function _applyAttentionToSplitView() {
  if (!window.__nativeTerminalBridge) return;
  const flagged = Array.from(_attentionSids);
  window.gtkSend({ type: 'attention-state', sids: flagged });
}
// === ATTENTION END ===

function _isPseudoSid(sid) {
  return typeof sid === 'string' && (sid === 'new' || sid.startsWith('new-'));
}

// Periodic poll that watches for real session files to appear for any open
// pseudo. When a match lands, we apply any pending rename, move active-terminal
// tracking onto the real sid, and drop the pseudo row.
let _pseudoTimer = null;
let _pseudoInFlight = false;
function _startPseudoReconciler() {
  if (_pseudoTimer) return;
  const tick = async () => {
    if (_pseudoSessions.length === 0) {
      if (_pseudoTimer) clearInterval(_pseudoTimer);
      _pseudoTimer = null;
      return;
    }
    const hasHandoffPseudo = _pseudoSessions.some(p => p && p.pending_group_link_with);
    if (_searchQuery && !hasHandoffPseudo) return;
    if (_pseudoInFlight) return;
    _pseudoInFlight = true;
    try {
      const params = new URLSearchParams();
      if (currentProject && currentProject.length) params.set('projects', currentProject.join(','));
      params.set('refresh', '1');
      const r = await fetch('/api/sessions?' + params);
      const fresh = await r.json();
      await _reconcilePseudos(fresh, { reload: !_searchQuery });
    } catch(e) { /* ignore, will retry */ }
    finally { _pseudoInFlight = false; }
  };
  tick();
  _pseudoTimer = setInterval(tick, 1000);
}

// Periodic session-list refresh while any terminal is live. Picks up new
// sessions claude writes after /clear, compact, or starting fresh within a PTY.
let _activeRefreshTimer = null;
let _activeRefreshInFlight = false;
function _ensureActiveRefresh() {
  if (_activeRefreshTimer) return;
  _activeRefreshTimer = setInterval(async () => {
    if (_activeTerms.size === 0) {
      clearInterval(_activeRefreshTimer);
      _activeRefreshTimer = null;
      return;
    }
    if (currentTab !== 'chats') return;
    if (_searchQuery) return;   // an active search owns the list — don't clobber it
    if (_activeRefreshInFlight) return;
    _activeRefreshInFlight = true;
    try { await loadSessions(currentProject, { refresh: true }); } catch(e) {}
    finally { _activeRefreshInFlight = false; }
  }, 30000);
}

function _migrateActiveOnClear(freshSessions) {
  // Pseudos get first dibs on any new session in their cwd — otherwise the
  // /clear-migration logic here steals the pseudo's real session and merges it
  // into a pre-existing active terminal. Compute which sessions the pseudos
  // will claim and exclude them from the migration pool.
  const claimedByPseudo = new Set();
  for (const pseudo of _pseudoSessions) {
    // Same default-cwd resolution as the reconciler: a cwd-less placeholder was
    // still spawned somewhere real ($HOME), so it must keep its claim here or
    // an unrelated active terminal steals the session it created.
    const pseudoCwd = pseudo.cwd || _defaultCwd();
    if (!pseudoCwd) continue;
    const pCands = freshSessions.filter(s =>
      s.agent === 'claude' &&
      s.cwd === pseudoCwd &&
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
      s.agent === 'claude' &&
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
    if (window.__nativeTerminalBridge) {
      window.gtkSend({ type: 'code-migrate-sid', old: oldSid, new: target.session_id });
    } else {
      _migrateLiveTerminalSid(oldSid, target.session_id);
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

function _pseudoCandidateTs(s) {
  return s.first_timestamp || s.created_at || s.last_timestamp || '';
}

function _sortNewestFirst(a, b) {
  const first = (_pseudoCandidateTs(b) || '').localeCompare(_pseudoCandidateTs(a) || '');
  if (first) return first;
  return (b.last_timestamp || '').localeCompare(a.last_timestamp || '');
}

async function _reconcilePseudos(fresh, opts) {
  opts = opts || {};
  let changed = false;
  for (const pseudo of [..._pseudoSessions]) {
    // Front-door pseudos expire: if the pane never wrote a session file,
    // drop the pseudo (and its pair bucket) instead of letting it claim an
    // unrelated session later.
    if (pseudo.fd_expires && Date.now() > pseudo.fd_expires) {
      const idx = _pseudoSessions.indexOf(pseudo);
      if (idx >= 0) _pseudoSessions.splice(idx, 1);
      if (pseudo.fd_pair_id) delete _fdPairResolved[pseudo.fd_pair_id];
      setSessionSource(sessionSource.filter(s => s.session_id !== pseudo.session_id));
      changed = true;
      continue;
    }
    // Match heuristic: same cwd + same agent + real session started at or after
    // the pseudo was created. Pick the newest candidate to avoid stealing an
    // older session's id.
    const pseudoAgent = (pseudo.agent || 'claude').toLowerCase();
    const _normCwd = c => (c || '').replace(/[\\/]+$/, '');
    // A placeholder with no cwd was spawned with Python's default, which is
    // $HOME. Comparing '' against the real session's resolved '/home/<user>'
    // never matched, so the placeholder stranded and the chat rendered twice.
    // Resolve it the same way the spawner does before comparing.
    const pseudoCwd = _normCwd(pseudo.cwd) || _normCwd(_defaultCwd());
    let candidates = fresh.filter(s =>
      (s.agent || 'claude').toLowerCase() === pseudoAgent &&
      _normCwd(s.cwd) === pseudoCwd &&
      _pseudoCandidateTs(s) &&
      _pseudoCandidateTs(s) >= pseudo.first_timestamp
    );
    // Handoff-spawned partner: the real session's recorded cwd can differ from
    // the pseudo's (resolved path, Windows-slug source, home fallback), so the
    // strict cwd match misses and the auto-link never fires. When a link is
    // pending and there's no exact-cwd candidate, match the freshly-appeared
    // session of that agent by recency instead.
    if (!candidates.length && pseudo.pending_group_link_with) {
      candidates = fresh.filter(s =>
        (s.agent || 'claude').toLowerCase() === pseudoAgent &&
        _pseudoCandidateTs(s) &&
        _pseudoCandidateTs(s) >= pseudo.first_timestamp
      );
    }
    if (!candidates.length) continue;
    candidates.sort(_sortNewestFirst);
    const match = candidates[0];

    // Apply pending rename to the real session
    if (pseudo.pending_rename_title) {
      try {
        const rr = await fetch('/api/rename/' + match.session_id, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: pseudo.pending_rename_title }),
        });
        const rd = await rr.json().catch(() => ({}));
        if (rr.ok && rd.error === undefined) {
          match.display_title = pseudo.pending_rename_title;
          match.custom_title = pseudo.pending_rename_title;
          _patchClientSession(match.session_id, {
            display_title: pseudo.pending_rename_title,
            custom_title: pseudo.pending_rename_title,
          });
        }
      } catch(e) {}
    }
    // === GROUP FEATURE === (handoff auto-link: pair the new chat with its source)
    if (pseudo.pending_group_link_with) {
      try {
        const linkSids = Array.from(new Set([
          ...((pseudo.pending_group_member_sids || [pseudo.pending_group_link_with]).filter(Boolean)),
          match.session_id,
        ]));
        const lr = await fetch('/api/group/link', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_ids: linkSids }),
        });
        const ld = await lr.json().catch(() => ({}));
        if (lr.ok && ld.ok && ld.group_id) {
          match.group = ld.group_id;
          _applyClientGroup(linkSids, ld.group_id);
        }
      } catch(e) {}
    }
    // === GROUP FEATURE END ===
    // === FRONT DOOR FEATURE === dual-spawn pairing: when the front door
    // opens claude+codex together, link the two real sessions once both
    // pseudos have resolved so they render as one thread and the bridge works.
    if (pseudo.fd_pair_id) {
      const bucket = (_fdPairResolved[pseudo.fd_pair_id] = _fdPairResolved[pseudo.fd_pair_id] || []);
      bucket.push(match.session_id);
      if (bucket.length === 2) {
        _fdLinkPair([...bucket], 0);
        delete _fdPairResolved[pseudo.fd_pair_id];
      }
    }
    // === FRONT DOOR FEATURE END ===

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
      if (_gtkReadyTerms.has(pseudo.session_id)) {
        _gtkReadyTerms.delete(pseudo.session_id);
        _gtkReadyTerms.add(match.session_id);
      }
      if (window.__nativeTerminalBridge) {
        window.gtkSend({
          type: 'code-migrate-sid',
          old: pseudo.session_id,
          new: match.session_id,
        });
      }
    }
    // The session file can appear before the terminal WebSocket reaches
    // onopen, so migrate xterm state independently of the active marker.
    if (!window.__nativeTerminalBridge && termSessions.has(pseudo.session_id)) {
      _migrateLiveTerminalSid(pseudo.session_id, match.session_id);
    }
    if (currentSessionId === pseudo.session_id) {
      currentSessionId = match.session_id;
    }
    if (focusedSid === pseudo.session_id) {
      focusedSid = match.session_id;
    }
    if (_gtkCodeSid === pseudo.session_id) {
      _gtkCodeSid = match.session_id;
    }
    if (_gtkSplitSids) {
      _gtkSplitSids = _gtkSplitSids.map(x => x === pseudo.session_id ? match.session_id : x);
    }
    _migrateGtkRuntimeState(pseudo.session_id, match.session_id);

    // Drop the pseudo
    pseudo.resolved_session_id = match.session_id;
    _resolvedPseudoSids.set(pseudo.session_id, match.session_id);
    _dropClientSession(pseudo.session_id);
    changed = true;
  }
  if (changed) {
    if (opts.reload === false) {
      allSessions = fresh;
      renderSessionList();
    } else {
      await loadSessions(currentProject, { refresh: true });
    }
  }
}

function _markActive(sid) {
  if (!sid) return;
  // Focusing a chat clears any "needs attention" flag for it
  _clearAttention(sid);
  if (_activeTerms.has(sid)) return;
  _rememberActive(sid);
  renderSessionList();
  _ensureActiveRefresh();
}

function _rememberActive(sid) {
  if (!sid) return;
  _activeTerms.add(sid);
  const local = _findClientSession(sid);
  if (!_activeMeta.has(sid)) {
    _activeMeta.set(sid, {
      cwd: (local && local.cwd) || '',
      activatedAt: new Date().toISOString(),
    });
  }
}

function _unmarkActive(sid) {
  if (!sid) return;
  _gtkReadyTerms.delete(sid);
  _activeMeta.delete(sid);
  const had = _activeTerms.delete(sid);
  // Prune any pseudo row that matches — temp sessions don't survive a close
  if (_isPseudoSid(sid)) {
    const pi = _pseudoSessions.findIndex(p => p.session_id === sid);
    if (pi >= 0) _pseudoSessions.splice(pi, 1);
    const srcIdx = sessionSource.findIndex(s => s.session_id === sid);
    if (srcIdx >= 0) sessionSource.splice(srcIdx, 1);
    const si = sessions.findIndex(s => s.session_id === sid);
    if (si >= 0) sessions.splice(si, 1);
  }
  if (had || _isPseudoSid(sid)) renderSessionList();
}

async function closeActiveTerminal(sid) {
  if (!sid || !_activeTerms.has(sid)) return;
  if (window.__nativeTerminalBridge) {
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

let _externalReadTimer = null;
function _stopExternalReadRefresh() {
  if (_externalReadTimer) clearTimeout(_externalReadTimer);
  _externalReadTimer = null;
}
function _scheduleExternalReadRefresh(sid) {
  _stopExternalReadRefresh();
  _externalReadTimer = setTimeout(() => {
    if (currentSessionId !== sid || convMode !== 'read') return;
    _convLoaded.delete(sid);
    loadReadTranscript(sid, true);
  }, 2000);
}

async function loadReadTranscript(sid, force) {
  if (_convLoaded.has(sid) && !force) return;
  if (_isPseudoSid(sid)) {
    document.getElementById('convBody').innerHTML =
      '<div class="empty-text">New chat — no transcript yet.</div>';
    return;
  }
  if (!force) {
    document.getElementById('convBody').innerHTML = '<div class="loading-text">Loading...</div>';
  }
  try {
    const r = await fetch('/api/conversation/' + sid);
    const data = await r.json();
    if (currentSessionId !== sid) return;  // user switched away while we loaded
    const externallyRunning = Boolean(data.external_runtime_active);
    _patchClientSession(sid, { external_runtime_active: externallyRunning });

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
        if (m.tool_input) html += ': ' + linkifyPaths(esc(m.tool_input.substring(0, 200)));
        html += '</div></div>';
      } else if (m.role === 'tool_result') {
        html += '<div class="msg"><div class="msg-tool-output">' + linkifyPaths(esc(m.text)) + '</div></div>';
      } else {
        const defaultAgentLabel = data.agent === 'serena-voice' ? 'Serena' : 'Claude';
        const roleLabel = m.role === 'user'
          ? 'You'
          : (data.agent === 'codex' ? 'Codex' : defaultAgentLabel);
        html += '<div class="msg">'
          + '<div class="msg-role ' + m.role + '">' + roleLabel + '</div>'
          + '<div class="msg-body">' + linkifyPaths(esc(m.text)) + '</div>'
          + '</div>';
      }
    }
    document.getElementById('convBody').innerHTML = html || '<div class="empty-text">No messages</div>';
    _convLoaded.add(sid);
    if ((externallyRunning || _isSerenaVoiceSession(data.agent || sid)) && convMode === 'read') _scheduleExternalReadRefresh(sid);
    else _stopExternalReadRefresh();
  } catch(e) {
    document.getElementById('convBody').innerHTML = '<div class="empty-text">Error loading conversation</div>';
  }
}

function setConvMode(mode) {
  const local = currentSessionId ? _findClientSession(currentSessionId) : null;
  if (mode === 'live' && (_isSerenaVoiceSession(local || currentSessionId) || _isFleetSession(local))) {
    convMode = 'read';
    document.getElementById('viewReadBtn').classList.add('active');
    document.getElementById('viewLiveBtn').classList.remove('active');
    document.getElementById('convBody').classList.remove('hidden');
    document.getElementById('convTerminal').classList.add('hidden');
    if (currentSessionId) loadReadTranscript(currentSessionId);
    return;
  }
  if (mode === 'live' && local && local.external_runtime_active) {
    showToast('This workflow agent is still running. Its transcript is live in Read.', { variant: 'error' });
    convMode = 'read';
    document.getElementById('viewReadBtn').classList.add('active');
    document.getElementById('viewLiveBtn').classList.remove('active');
    document.getElementById('convBody').classList.remove('hidden');
    document.getElementById('convTerminal').classList.add('hidden');
    loadReadTranscript(currentSessionId, true);
    return;
  }
  if (mode === convMode) return;
  convMode = mode;
  document.getElementById('viewReadBtn').classList.toggle('active', mode === 'read');
  document.getElementById('viewLiveBtn').classList.toggle('active', mode === 'live');
  document.getElementById('convBody').classList.toggle('hidden', mode !== 'read');
  document.getElementById('convTerminal').classList.toggle('hidden', mode !== 'live');
  if (mode === 'live') {
    _stopExternalReadRefresh();
    if (!currentSessionId) {
      setTermStatus('Select a conversation first.', 'error');
      return;
    }
    if (window.__nativeTerminalBridge) {
      startGtkCode(currentSessionId);
    } else if (termSessions.has(currentSessionId)) {
      _activateTermPane(currentSessionId);
    } else {
      startLiveTerminal(currentSessionId);
    }
    // Linked claude↔codex pair: bring the sibling up in the background so the
    // split view shows both — and so the ask-codex/ask-claude bridge has a
    // live PTY to feed on this machine.
    if (!window.__nativeTerminalBridge) {
      const _sib = _linkedSiblingSid(currentSessionId);
      if (_sib && !termSessions.has(_sib)) startLiveTerminal(_sib, { background: true });
    }
    syncCodeView();
  } else {
    if (window.__nativeTerminalBridge) stopGtkCode();
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

async function _describeSpawnFailure(error) {
  // "Failed to fetch" names the symptom and nothing else. The question it
  // leaves open is the only one that matters: is the backend gone, or did this
  // one request fail? Answer it in the message rather than making someone
  // reproduce it with devtools open.
  const base = 'Failed to spawn terminal: ' + (error && error.message ? error.message : error);
  const reachable = await _terminalBackendReachable();
  if (reachable) {
    return base + ' · the backend is up, but the retry also failed; try again';
  }
  return base + ' · nothing is answering on ' + location.origin +
    '. This window is showing a page whose server is gone. Reopen Serena.';
}

async function _terminalBackendReachable() {
  try {
    const probe = await fetch('/api/health', { cache: 'no-store' });
    return probe.ok;
  } catch (e) {
    return false;
  }
}

async function _spawnTerminalRequest(body) {
  const requestOnce = async () => {
    const response = await fetch('/api/spawn-terminal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return await response.json();
  };
  try {
    return await requestOnce();
  } catch (firstError) {
    if (!await _terminalBackendReachable()) throw firstError;
    await new Promise((resolve) => setTimeout(resolve, 120));
    return await requestOnce();
  }
}

function _decodeOsc52(payload) {
  // Base64 in, UTF-8 out. atob yields one byte per char, so the bytes have to
  // be reassembled before decoding or anything non-ASCII arrives mangled.
  const binary = atob(payload);
  const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
  return new TextDecoder('utf-8').decode(bytes);
}

function _copyPlainText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  // Older WebKitGTK builds expose no async clipboard in this context.
  return new Promise((resolve, reject) => {
    const scratch = document.createElement('textarea');
    scratch.value = text;
    scratch.setAttribute('readonly', '');
    scratch.style.position = 'fixed';
    scratch.style.opacity = '0';
    document.body.appendChild(scratch);
    scratch.select();
    const ok = document.execCommand('copy');
    scratch.remove();
    ok ? resolve() : reject(new Error('execCommand copy was refused'));
  });
}

/**
 * Let programs inside the terminal reach the system clipboard.
 *
 * A TUI that cannot see a native clipboard copies by emitting OSC 52 and
 * trusting the terminal to do the rest. xterm.js does not handle that sequence
 * on its own, so those bytes were being dropped: Claude Code reported "sent N
 * chars via OSC 52" and nothing ever arrived.
 */
function _installClipboardBridge(term) {
  if (!term.parser || !term.parser.registerOscHandler) return;
  term.parser.registerOscHandler(52, (data) => {
    const semi = String(data).indexOf(';');
    if (semi < 0) return false;
    const payload = String(data).slice(semi + 1);

    // OSC 52 can also READ the clipboard. Answering that would let anything
    // running in a pane, including a command someone else's chat pasted,
    // exfiltrate whatever is on the clipboard. Never reply.
    if (payload === '?') return true;

    let text;
    try {
      text = _decodeOsc52(payload);
    } catch (e) {
      return false;
    }
    _copyPlainText(text)
      .then(() => showToast('copied ' + text.length + ' chars', { variant: 'success' }))
      .catch((e) => showToast('clipboard refused the copy: ' + e.message, { variant: 'error' }));
    return true;
  });
}

function _machineBadge() {
  // Chats sync between the Linux laptop and the Windows PC, and a resumed pane
  // looks identical on either. Naming the host here settles at a glance which
  // machine the work in front of you is actually running on.
  const machine = (window.SERENA && window.SERENA.machine) || {};
  const os = String(machine.os || '').split(' ')[0] || 'unknown';
  const badge = document.createElement('span');
  badge.className = 'term-machine ' + os.toLowerCase();
  badge.textContent = machine.name ? `${os} · ${machine.name}` : os;
  badge.title = machine.name
    ? `running on Raghav's ${machine.name} (${machine.os})`
    : `running on ${machine.os || 'this machine'}`;
  return badge;
}

function _visibleRuntimeSids() {
  // A linked pair shows both sides; anything else shows the one pane in front
  // of you. Unlinked chats used to fall through to nothing, so a solo claude or
  // codex pane reported neither its machine nor its session id.
  if (_gtkSplitActive && _gtkSplitSids) return _gtkSplitSids;
  const focused = window.__nativeTerminalBridge ? _gtkCodeSid : activeTermSid;
  return focused ? [focused] : [];
}

function _renderOpenSessionIds(sids) {
  const root = document.getElementById('termSessionIds');
  if (!root) return;
  root.replaceChildren();
  const unique = [...new Set((sids || []).filter(Boolean))];
  root.classList.toggle('hidden', unique.length === 0);
  if (unique.length) root.appendChild(_machineBadge());
  for (const sid of unique) {
    const session = _findClientSession(sid);
    const runtime = termSessions.get(sid);
    const agent = ((session && session.agent) || (runtime && runtime.agent) || 'session').toLowerCase();
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'term-session-id ' + agent;
    btn.textContent = agent + ' ' + sid.slice(0, 8);
    btn.title = agent + ' session\n' + sid + '\nClick to copy';
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText(sid)
        .then(() => showToast(agent + ' session id copied', { variant: 'success' }))
        .catch(() => showToast('copy failed', { variant: 'error' }));
    });
    root.appendChild(btn);
  }
}

function _sendResizeForSid(sid, force) {
  const s = termSessions.get(sid);
  if (!s || !s.ws || s.ws.readyState !== 1) return;
  const rows = s.term.rows;
  const cols = s.term.cols;
  // Only push real geometry changes: each one is a setwinsize syscall (a slow
  // ConPTY call on Windows) plus a SIGWINCH repaint in the TUI. `force` is for
  // reconnects, where the backend may still hold the pre-drop dimensions and
  // the renderer has no way to know whether they match.
  if (!force && s.sentRows === rows && s.sentCols === cols) return;
  try {
    s.ws.send(JSON.stringify({ resize: { rows, cols } }));
    s.sentRows = rows;
    s.sentCols = cols;
  } catch(e) {}
}

function sendResize() {
  if (activeTermSid) _sendResizeForSid(activeTermSid);
}

// Electron delivers BrowserWindow focus to the renderer as a plain window
// focus event. GTK's VTE grabbed the keyboard itself; xterm.js does not, so
// without this the first keystroke after alt-tabbing back lands nowhere.
let _termWindowFocusBound = false;
function _bindWebTerminalWindowFocus() {
  if (_termWindowFocusBound) return;
  _termWindowFocusBound = true;
  const refocus = () => {
    if (currentTab !== 'chats' || convMode !== 'live' || !activeTermSid) return;
    const runtime = termSessions.get(activeTermSid);
    if (!runtime || !runtime.term) return;
    // Never yank focus out from under a text field or an open modal.
    const el = document.activeElement;
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
    if (document.getElementById('modalBackdrop')?.classList.contains('visible')) return;
    // A window resize while hidden leaves xterm measured for the old box.
    try { runtime.fit.fit(); } catch(e) {}
    _sendResizeForSid(activeTermSid);
    try { runtime.term.focus(); } catch(e) {}
    _setWebTerminalFocus(activeTermSid);
  };
  window.addEventListener('focus', refocus);
  window.addEventListener('blur', () => _setWebTerminalFocus(null));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refocus();
  });
}

function _setWebTerminalFocus(sid) {
  for (const [runtimeSid, runtime] of termSessions) {
    if (!runtime || !runtime.term) continue;
    const shouldBlink = runtimeSid === sid && !runtime.mount.classList.contains('hidden');
    if (runtime.term.options.cursorBlink !== shouldBlink) {
      runtime.term.options.cursorBlink = shouldBlink;
    }
  }
}

function _reportWebRuntimeContext(sid) {
  let runtime = sid ? termSessions.get(sid) : null;
  if (!runtime) {
    runtime = Array.from(termSessions.values()).find(item => item.ws && item.ws.readyState === 1);
  }
  if (!runtime || !runtime.ws || runtime.ws.readyState !== 1) return;
  const splitSids = _gtkSplitActive && _gtkSplitSids ? _gtkSplitSids : [];
  const draft = sid && window.__termDrafts
    ? (window.__termDrafts.get(sid) || '')
    : undefined;
  try {
    runtime.ws.send(JSON.stringify({
      runtime_context: {
        focused_sid: sid || '',
        split_sids: splitSids,
        ...(draft === undefined ? {} : { draft }),
      },
    }));
  } catch (_) {}
}

function _hideAllTermPanes() {
  const container = document.getElementById('termMounts');
  if (!container) return;
  for (const child of container.children) child.classList.add('hidden');
  activeTermSid = null;
  _webRuntimeFocusSid = null;
  _renderOpenSessionIds([]);
  _setWebTerminalFocus(null);
  _reportWebRuntimeContext(null);
  if (_webRuntimePollTimer) clearInterval(_webRuntimePollTimer);
  _webRuntimePollTimer = null;
  if (!window.__nativeTerminalBridge) {
    _gtkSplitActive = false;
    _gtkSplitSids = null;
    _gtkCurrentGroup = null;
    _syncRuntimePinButton();
  }
}

function _linkedSiblingSid(sid) {
  // The other half of a linked claude↔codex pair, if any.
  const pending = _pendingTermPartners.get(sid);
  if (pending) return pending;
  const src = (sessionSource.length ? sessionSource : sessions);
  const me = src.find(x => x && x.session_id === sid);
  if (!me || !me.group) return null;
  const sibs = _siblingsInGroup(me.group, sid).filter(s =>
    !s.external_runtime_active && !s.fleet_worker && !(s.metadata && s.metadata.fleet_worker));
  if (!sibs.length) return null;
  const visible = sibs.find(s =>
    (_gtkSplitSids && _gtkSplitSids.includes(s.session_id)) || termSessions.has(s.session_id));
  if (visible) return visible.session_id;
  const myAgent = (me.agent || 'claude').toLowerCase();
  const opposite = sibs.find(s => (s.agent || 'claude').toLowerCase() !== myAgent);
  return (opposite || sibs[0]).session_id;
}

let _webRuntimeSyncing = false;
async function _syncWebRuntimePolicy() {
  if (_webRuntimeSyncing || !_webRuntimeFocusSid) return;
  const focus = termSessions.get(_webRuntimeFocusSid);
  if (!focus) return;
  const siblingSid = _linkedSiblingSid(_webRuntimeFocusSid);
  const sibling = siblingSid ? termSessions.get(siblingSid) : null;
  // Unsent input is the user's work in progress. Freezing a pane that holds a
  // half-typed message is the one sleep the user would actually notice, so a
  // draft protects its runtime the same way an in-flight turn does.
  const protectedTids = [];
  const standbyTids = [];
  for (const [runtimeSid, runtime] of termSessions) {
    if ((window.__termDrafts && window.__termDrafts.get(runtimeSid)) || runtime.busy) {
      protectedTids.push(runtime.tid);
    }
    if (runtimeSid !== _webRuntimeFocusSid) standbyTids.push(runtime.tid);
  }
  // The sibling is named explicitly so it sleeps the moment focus leaves it;
  // the rest are swept by the server once they have been quiet long enough.
  if (sibling && !standbyTids.includes(sibling.tid)) standbyTids.push(sibling.tid);
  const pinned = Boolean(_gtkCurrentGroup && _gtkPinnedGroups.has(_gtkCurrentGroup));
  _webRuntimeSyncing = true;
  try {
    const r = await fetch('/api/terminal-runtime/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        focus_tid: focus.tid,
        standby_tids: sibling ? [sibling.tid] : [],
        all_open_tids: standbyTids,
        protected_tids: protectedTids,
        pin_both: pinned,
      }),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) return;
    for (const [runtimeSid, runtime] of termSessions) {
      const info = data.states && data.states[runtime.tid];
      if (!info) continue;
      runtime.busy = !!info.busy;
      _gtkRuntimeStates.set(runtimeSid, info.state || 'live');
    }
    _refreshGtkRuntimeStatus();
  } catch(e) {
  } finally {
    _webRuntimeSyncing = false;
  }
}

function _scheduleWebRuntimePolicy() {
  _syncWebRuntimePolicy();
  if (_webRuntimePollTimer) clearInterval(_webRuntimePollTimer);
  // Poll whenever any terminal is open, not only in split view. Gating this on
  // the split meant a single-pane session never swept at all, so every
  // background runtime stayed awake for the life of the app.
  _webRuntimePollTimer = setInterval(
    _syncWebRuntimePolicy,
    _gtkSplitActive ? 2000 : 15000,
  );
}

function _markWebTurnStarted(sid) {
  const runtime = termSessions.get(sid);
  if (!runtime) return;
  runtime.busy = true;
  fetch('/api/terminal-runtime/turn-start/' + encodeURIComponent(runtime.tid), {
    method: 'POST',
  }).catch(() => {});
  _scheduleWebRuntimePolicy();
}

function _fitVisibleWebTerms() {
  const container = document.getElementById('termMounts');
  if (!window.SerenaTerminalLifecycle.isRenderable({
    tab: currentTab,
    mode: convMode,
    hidden: !container || container.closest('.hidden') !== null,
    rect: container ? container.getBoundingClientRect() : null,
  })) return false;
  const sids = _gtkSplitActive && _gtkSplitSids ? _gtkSplitSids : [activeTermSid];
  for (const sid of sids.filter(Boolean)) {
    const runtime = termSessions.get(sid);
    if (!runtime) continue;
    try { runtime.fit.fit(); _sendResizeForSid(sid); } catch(e) {}
  }
  return true;
}

function _applyWebSplitGeometry(container) {
  if (!_gtkSplitActive || !_gtkSplitSids) return;
  const pct = (_gtkSplitRatio * 100).toFixed(3) + '%';
  const [leftSid, rightSid] = _gtkSplitSids;
  const left = termSessions.get(leftSid);
  const right = termSessions.get(rightSid);
  if (left) {
    left.mount.style.left = '8px';
    left.mount.style.right = 'calc(100% - ' + pct + ' + 3px)';
  }
  if (right) {
    right.mount.style.left = 'calc(' + pct + ' + 3px)';
    right.mount.style.right = '8px';
  }
  const divider = container.querySelector('.term-split-divider');
  if (divider) divider.style.left = 'calc(' + pct + ' - 2px)';
}

function _layoutWebSplitDivider(container, split) {
  let divider = container.querySelector('.term-split-divider');
  if (!split) {
    if (divider) divider.classList.add('hidden');
    return;
  }
  if (!divider) {
    divider = document.createElement('div');
    divider.className = 'term-split-divider';
    divider.title = 'Drag to resize';
    container.appendChild(divider);
    divider.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      divider.dataset.dragging = '1';
      divider.classList.add('dragging');
      divider.setPointerCapture(ev.pointerId);
    });
    divider.addEventListener('pointermove', (ev) => {
      if (divider.dataset.dragging !== '1') return;
      const rect = container.getBoundingClientRect();
      if (!rect.width) return;
      _gtkSplitRatio = Math.max(0.15, Math.min(0.85, (ev.clientX - rect.left) / rect.width));
      _applyWebSplitGeometry(container);
      if (_resizeDebounceTimer) clearTimeout(_resizeDebounceTimer);
      _resizeDebounceTimer = setTimeout(_fitVisibleWebTerms, 50);
    });
    const endDrag = () => {
      if (divider.dataset.dragging !== '1') return;
      delete divider.dataset.dragging;
      divider.classList.remove('dragging');
      try { localStorage.setItem('serena.splitRatio', String(_gtkSplitRatio)); } catch(e) {}
      _fitVisibleWebTerms();
    };
    divider.addEventListener('pointerup', endDrag);
    divider.addEventListener('pointercancel', endDrag);
  }
  divider.classList.remove('hidden');
  _applyWebSplitGeometry(container);
}

function _cancelWebTerminalTail(runtime) {
  if (runtime) runtime.tailFollowUntil = 0;
}

function _scrollWebTerminalTail(runtime) {
  if (!runtime || !window.SerenaTerminalLifecycle.shouldFollowTail(
    runtime.tailFollowUntil, performance.now()
  )) return false;
  try {
    runtime.term.scrollToBottom();
    return true;
  } catch(e) {
    return false;
  }
}

function _armWebTerminalTail(runtime) {
  if (!runtime) return;
  runtime.tailFollowUntil = window.SerenaTerminalLifecycle.tailDeadline(performance.now());
}

function _activateTermPane(sid) {
  const container = document.getElementById('termMounts');
  if (!container) return;
  // Linked pair with both terminals alive -> side-by-side split. Keep Claude
  // on the left and Codex on the right even when focus changes.
  const sibSid = _linkedSiblingSid(sid);
  const split = !!(sibSid && termSessions.has(sibSid));
  let leftSid = sid;
  let rightSid = sibSid;
  if (split) {
    const mine = _findClientSession(sid);
    const sibling = _findClientSession(sibSid);
    const mineAgent = ((mine && mine.agent) || (termSessions.get(sid) || {}).agent || 'claude').toLowerCase();
    const siblingAgent = ((sibling && sibling.agent) || (termSessions.get(sibSid) || {}).agent || 'claude').toLowerCase();
    if (mineAgent === 'codex' && siblingAgent === 'claude') {
      leftSid = sibSid;
      rightSid = sid;
    }
  }
  for (const child of container.children) {
    if (child.classList.contains('term-split-divider')) continue;
    const isMain = child.dataset.sid === sid;
    const isSib = split && child.dataset.sid === sibSid;
    child.classList.toggle('hidden', !(isMain || isSib));
    child.classList.toggle('runtime-focused', child.dataset.sid === sid);
    if (isMain || isSib) {
      child.style.visibility = '';  // clear background-spawn invisibility
      if (!split) {
        child.style.left = '';
        child.style.right = '';
      }
    }
  }
  activeTermSid = sid;
  _webRuntimeFocusSid = sid;
  _setWebTerminalFocus(sid);
  const local = _findClientSession(sid);
  _gtkSplitActive = split;
  _gtkSplitSids = split ? [leftSid, rightSid] : null;
  _renderOpenSessionIds(_visibleRuntimeSids());
  _reportWebRuntimeContext(sid);
  _gtkCurrentGroup = split
    ? ((local && local.group) || ('pending:' + [leftSid, rightSid].sort().join(':')))
    : null;
  _layoutWebSplitDivider(container, split);
  _syncRuntimePinButton();
  const s = termSessions.get(sid);
  if (!s) return;
  const visibleRuntimes = (split ? [sid, sibSid] : [sid])
    .map(runtimeSid => termSessions.get(runtimeSid))
    .filter(Boolean);
  for (const runtime of visibleRuntimes) _armWebTerminalTail(runtime);
  // Status reflects the now-visible session
  if (s.ws && s.ws.readyState === 1) {
    setTermStatus('● live · cwd: ' + (s.cwd || '(unknown)') + (split ? '  ·  ⛓ split' : ''), 'live');
  } else if (s.ws && s.ws.readyState === 0) {
    setTermStatus('Connecting…');
  } else {
    setTermStatus('Disconnected.', 'error');
  }
  requestAnimationFrame(() => {
    _fitVisibleWebTerms();
    for (const runtime of visibleRuntimes) _scrollWebTerminalTail(runtime);
    try { s.term.focus(); } catch(e) {}
  });
  _scheduleWebRuntimePolicy();
}

let _resizeDebounceTimer = null;
function _ensureTermResizeObserver() {
  if (_termResizeObs) return;
  const container = document.getElementById('termMounts');
  if (!container) return;
  _termResizeObs = new ResizeObserver(() => {
    if (!activeTermSid || currentTab !== 'chats' || convMode !== 'live') return;
    // Debounce — ResizeObserver fires per pixel during window drag (60+ Hz).
    // Each event calls proc.setwinsize() on the backend which on Windows is
    // an expensive ConPTY call AND races with the reader thread. Coalesce
    // into a single resize once the user stops dragging.
    if (_resizeDebounceTimer) clearTimeout(_resizeDebounceTimer);
    _resizeDebounceTimer = setTimeout(() => {
      _resizeDebounceTimer = null;
      _fitVisibleWebTerms();
    }, 80);
  });
  _termResizeObs.observe(container);
}

async function startLiveTerminal(sid, opts) {
  // `opts` is for new-chat mode: { cwd: string, agent?: string, isNew: true }.
  // For existing chats (omit opts), we resume by session_id.
  opts = opts || {};
  const localSession = _findClientSession(sid);
  if (!opts.isNew && (_isSerenaVoiceSession(localSession || sid) || _isFleetSession(localSession))) {
    if (!opts.background) openConv(sid);
    return null;
  }
  if (!opts.isNew && localSession && localSession.external_runtime_active) {
    if (!opts.background) setConvMode('read');
    return null;
  }
  // Already alive? Just bring its pane to front.
  if (termSessions.has(sid)) {
    if (!opts.background) _activateTermPane(sid);
    return termSessions.get(sid);
  }
  if (_termStarting.has(sid)) {
    const started = performance.now();
    while (_termStarting.has(sid) && performance.now() - started < 10000) {
      await _sleep(50);
    }
    if (termSessions.has(sid) && !opts.background) _activateTermPane(sid);
    return termSessions.get(sid);
  }
  _termStarting.add(sid);

  setTermStatus(opts.isNew
    ? 'Starting ' + (opts.agent || 'claude') + '…'
    : 'Starting claude --resume ' + sid.slice(0, 8) + '…');

  const container = document.getElementById('termMounts');
  const mount = document.createElement('div');
  mount.className = 'term-pane';
  mount.dataset.sid = sid;
  container.appendChild(mount);
  if (opts.background) {
    // Spawned as the hidden half of a linked pair — don't steal the visible
    // pane; the split layout reveals it once its WS opens. visibility (not
    // display:none): xterm can't open/measure inside display:none, which
    // killed the spawn before it ever reached the server.
    mount.style.visibility = 'hidden';
  } else {
    // Hide the others, show this fresh one immediately
    for (const child of container.children) {
      if (child !== mount) child.classList.add('hidden');
    }
    activeTermSid = sid;
  }

  // Detect Windows so we can hand xterm.js the ConPTY-specific tweaks. Without
  // `windowsPty`, claude/codex's TUI breaks: alt-screen mode is unstable,
  // cursor positioning loses sync, and the bottom rows (input bar + status
  // line) render off-viewport — what Raghav saw as "input bar gone, statusline
  // gone". This option tells xterm.js to apply the ConPTY workarounds.
  const _isWin = (navigator.userAgent || '').includes('Windows');
  const term = new Terminal({
    cursorBlink: !opts.background,
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
    // ConPTY normalizes some escape sequences and re-emits cursor moves
    // differently from a real Unix PTY. Tell xterm.js the backend so it
    // applies the right workarounds. Linux ignores this option.
    ...(_isWin ? { windowsPty: { backend: 'conpty', buildNumber: 22000 } } : {}),
    // ConPTY emits bare \r\n sometimes when the underlying app sent \n —
    // double newlines garble TUI redraws. convertEol normalises that.
    convertEol: _isWin,
    linkHandler: {
      activate: (_event, uri) => window.SerenaTerminalLinks.openExternalUri(uri),
    },
  });
  let state = null;
  const _liveSid = () => (state && state.sid) || sid;
  // Reconnect swaps the socket out, so every sender reads the current one off
  // `state` instead of closing over the socket it was created with.
  const _liveSocket = () => (state && state.ws) || null;
  _installClipboardBridge(term);
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  try {
    term.loadAddon(new WebLinksAddon.WebLinksAddon(
      (_event, uri) => window.SerenaTerminalLinks.openExternalUri(uri)
    ));
  } catch(e) {}

  term.open(mount);
  fit.fit();

  // The default xterm renderer is DOM-based and is expensive in WebKitGTK
  // when a TUI redraws its spinner/status line. Prefer WebGL2, then the 2D
  // canvas renderer, and retain DOM only as the final compatibility fallback.
  let rendererAddon = null;
  let rendererKind = 'dom';
  const loadCanvasRenderer = () => {
    if (!window.CanvasAddon || !window.CanvasAddon.CanvasAddon) return null;
    const addon = new window.CanvasAddon.CanvasAddon();
    term.loadAddon(addon);
    rendererAddon = addon;
    rendererKind = 'canvas';
    if (state) {
      state.rendererAddon = addon;
      state.renderer = rendererKind;
    }
    return addon;
  };
  // Probe before constructing. Electron falls back to SwiftShader or disables
  // the GPU entirely on some Linux boxes, and WebglAddon throws from its
  // constructor when there's no WebGL2 context. A caught throw still leaves a
  // dead canvas attached, so ask first.
  const webgl2Available = () => {
    try {
      return !!document.createElement('canvas').getContext('webgl2');
    } catch(e) {
      return false;
    }
  };
  try {
    if (window.WebglAddon && window.WebglAddon.WebglAddon && webgl2Available()) {
      const addon = new window.WebglAddon.WebglAddon();
      term.loadAddon(addon);
      rendererAddon = addon;
      rendererKind = 'webgl';
      addon.onContextLoss(() => {
        if (rendererAddon !== addon) return;
        try { addon.dispose(); } catch(e) {}
        rendererAddon = null;
        rendererKind = 'dom';
        try { loadCanvasRenderer(); } catch(e) {}
      });
    } else {
      loadCanvasRenderer();
    }
  } catch(e) {
    rendererAddon = null;
    rendererKind = 'dom';
    try { loadCanvasRenderer(); } catch(_) {}
  }

  // Install document-level capture-phase listener for Ctrl+C/V/A. WebView2
  // grabs these before xterm.js's customKeyEventHandler runs, so the
  // attachCustomKeyEventHandler approach below is too late for them.
  _installTermKeyCapture();

  // Intercept xterm key events so the Linux-equivalent in-terminal shortcuts
  // (Shift+Enter newline, Ctrl+Backspace delete-word, Ctrl+Shift+C/V copy/paste,
  // plus the Linux-VTE-style smart Ctrl+C/V and Ctrl+A copy-input-draft)
  // work on Windows/macOS too. Returning false suppresses xterm's default.
  //
  // Per-terminal input draft buffer — mirrors what GTK does on Linux. Tracks
  // chars typed since last Enter so Ctrl+A copies the in-progress prompt to
  // the clipboard without forcing the user to mouse-select.
  if (!window.__termDrafts) window.__termDrafts = new Map();
  const _getDraft = () => window.__termDrafts.get(_liveSid()) || '';
  const _setDraft = (s) => window.__termDrafts.set(_liveSid(), s);

  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== 'keydown') return true;
    // Shadow the outer socket with the live one: after a reconnect the
    // original is closed and every ws.send below would silently drop.
    const ws = _liveSocket() || { readyState: 3, send() {} };
    if (state && (e.key === 'PageUp' || (e.key === 'Home' && e.ctrlKey))) {
      _cancelWebTerminalTail(state);
    }

    // ─── Ctrl+A: copy current input draft to clipboard (plain Ctrl, no Shift)
    if (e.ctrlKey && !e.shiftKey && !e.altKey && e.key && e.key.toLowerCase() === 'a') {
      const draft = _getDraft();
      if (draft) navigator.clipboard.writeText(draft).catch(() => {});
      e.preventDefault(); return false;
    }
    // ─── Ctrl+C: smart — copy selection if any, else SIGINT
    if (e.ctrlKey && !e.shiftKey && !e.altKey && e.key && e.key.toLowerCase() === 'c') {
      const sel = term.getSelection();
      if (sel) {
        navigator.clipboard.writeText(sel).catch(() => {});
        term.clearSelection();
        e.preventDefault(); return false;
      }
      // No selection — SIGINT to the PTY, AND reset our draft (interrupt
      // typically cancels in-progress input).
      _setDraft('');
      try { ws.send('\x03'); } catch (_) {}
      e.preventDefault(); return false;
    }
    // ─── Ctrl+V: paste from clipboard, append to draft
    if (e.ctrlKey && !e.shiftKey && !e.altKey && e.key && e.key.toLowerCase() === 'v') {
      _pasteTerminalClipboard(_liveSid(), ws);
      e.preventDefault(); return false;
    }

    // ─── Draft shadow tracking (printable chars + edits) ────────────────────
    // Runs BEFORE xterm processes the key so we can keep our buffer in sync.
    // Skip when modifier keys other than Shift are held (those are control
    // sequences, not text). Don't track arrow keys / Home / End / etc.
    if (!e.ctrlKey && !e.altKey && !e.metaKey) {
      if (e.key === 'Backspace') {
        const d = _getDraft();
        if (d) _setDraft(d.slice(0, -1));
      } else if (e.key === 'Enter' && !e.shiftKey) {
        _setDraft('');  // plain Enter = submit, clear our shadow
      } else if (e.key === 'Enter' && e.shiftKey) {
        _setDraft(_getDraft() + '\n');  // Shift+Enter = newline in draft
      } else if (e.key && e.key.length === 1) {
        // Single-character key (printable). e.key is the actual character.
        _setDraft(_getDraft() + e.key);
      }
    }

    // ─── Existing keybindings via _matchBinding (term-newline etc.) ─────────
    const action = _matchBinding(e);
    if (!action) return true;
    if (action === 'term-newline') {
      try { ws.send('\n'); } catch (_) {}
      e.preventDefault(); return false;
    }
    if (action === 'term-delete-word') {
      // Pop word from our shadow draft too
      const d = _getDraft();
      const stripped = d.replace(/\s+$/, '');
      const lastSpace = Math.max(stripped.lastIndexOf(' '), stripped.lastIndexOf('\n'));
      _setDraft(lastSpace >= 0 ? stripped.slice(0, lastSpace + 1) : '');
      try { ws.send('\x17'); } catch (_) {}  // Ctrl+W
      e.preventDefault(); return false;
    }
    if (action === 'term-copy') {
      const sel = term.getSelection();
      if (sel) navigator.clipboard.writeText(sel).catch(() => {});
      e.preventDefault(); return false;
    }
    if (action === 'term-paste') {
      _pasteTerminalClipboard(_liveSid(), ws);
      e.preventDefault(); return false;
    }
    // Any other matched action (Alt+W close, Alt+J next, etc) is handled by
    // the document-level listener — let it bubble there by stopping xterm
    // from consuming the event.
    return false;
  });

  let spawnResp;
  try {
    // For new chats: send cwd + agent so the backend spawns a fresh agent.
    // For existing chats: send session_id so the backend resumes.
    const body = opts.isNew
      ? {
          cwd: opts.cwd || '',
          agent: opts.agent || 'claude',
          seed: opts.seed || '',
          client_session_id: sid,
          rows: term.rows,
          cols: term.cols,
        }
      : {
          session_id: sid,
          rows: term.rows,
          cols: term.cols,
        };
    spawnResp = await _spawnTerminalRequest(body);
  } catch(e) {
    setTermStatus('Failed to spawn terminal: ' + e.message, 'error');
    // Refine it once we know whether the backend is reachable at all.
    _describeSpawnFailure(e).then((detail) => setTermStatus(detail, 'error')).catch(() => {});
    mount.remove();
    if (activeTermSid === sid) activeTermSid = null;
    _termStarting.delete(sid);
    return;
  }
  if (!spawnResp.ok) {
    if (spawnResp.external_runtime) {
      _patchClientSession(sid, { external_runtime_active: true });
      if (!opts.background) setConvMode('read');
    }
    setTermStatus(spawnResp.error || 'Failed to spawn terminal', 'error');
    mount.remove();
    if (activeTermSid === sid) activeTermSid = null;
    _termStarting.delete(sid);
    return;
  }

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = proto + '//' + location.host + '/ws/terminal/' + spawnResp.terminal_id;
  let ws;
  try {
    ws = new WebSocket(wsUrl);
  } catch(e) {
    fetch('/api/kill-terminal/' + spawnResp.terminal_id, { method: 'POST' }).catch(() => {});
    setTermStatus('Failed to connect terminal: ' + e.message, 'error');
    mount.remove();
    if (activeTermSid === sid) activeTermSid = null;
    _termStarting.delete(sid);
    return;
  }
  ws.binaryType = 'arraybuffer';

  state = {
    sid,
    term, fit, ws, mount, wsUrl,
    tid: spawnResp.terminal_id,
    cwd: spawnResp.cwd || '',
    agent: (spawnResp.agent || opts.agent || '').toLowerCase(),
    rendererAddon,
    renderer: rendererKind,
    keyboardSelection: null,
    busy: !!opts.seed,
    closed: false,
    // Reconnect bookkeeping. The PTY outlives a dropped socket now, so a drop
    // is a retry, not the end of the session.
    reconnectAttempts: 0,
    reconnectTimer: null,
    dropAt: 0,
    giveUp: false,
    sentRows: 0,
    sentCols: 0,
    tailFollowUntil: opts.isNew
      ? 0
      : window.SerenaTerminalLifecycle.tailDeadline(performance.now()),
  };
  termSessions.set(sid, state);
  _termStarting.delete(sid);

  // Bound xterm's parser queue. PTY output arrives as binary frames and is
  // coalesced for at most 2ms, then the next batch waits for xterm's write
  // callback. This keeps animated TUIs responsive without building an
  // unbounded pile of render jobs.
  const outputParts = [];
  let outputBytes = 0;
  let outputTimer = null;
  let outputWriting = false;
  // Ack-based flow control, the renderer half of the xterm.js flow control
  // guide. We credit the backend only for bytes xterm has actually parsed
  // (the write callback), and batch those credits so a busy TUI doesn't turn
  // every frame into a control message. The backend stops draining the PTY
  // once too much is outstanding, which is what bounds memory here.
  const FLOW_ACK_BYTES = 131072;
  let ackPending = 0;
  const sendAck = (force) => {
    if (!ackPending) return;
    if (!force && ackPending < FLOW_ACK_BYTES) return;
    const socket = state.ws;
    if (!socket || socket.readyState !== 1) { ackPending = 0; return; }
    try {
      socket.send(JSON.stringify({ ack: ackPending }));
      ackPending = 0;
    } catch(e) {}
  };
  state.resetAckCredit = () => { ackPending = 0; };
  const flushOutput = () => {
    if (outputWriting || !outputParts.length || state.closed) return;
    if (outputTimer) clearTimeout(outputTimer);
    outputTimer = null;
    let batch;
    if (outputParts.length === 1) {
      batch = outputParts.shift();
    } else {
      batch = new Uint8Array(outputBytes);
      let offset = 0;
      for (const part of outputParts) {
        batch.set(part, offset);
        offset += part.byteLength;
      }
      outputParts.length = 0;
    }
    outputBytes = 0;
    outputWriting = true;
    const written = batch.byteLength;
    term.write(batch, () => {
      outputWriting = false;
      ackPending += written;
      _scrollWebTerminalTail(state);
      if (outputParts.length && !state.closed) {
        // Mid-burst: only ack once a batch's worth has piled up.
        sendAck(false);
        flushOutput();
      } else {
        // Drained. Flush the credit now so the backend never sits paused
        // waiting for an ack that a quiet terminal would never reach.
        sendAck(true);
      }
    });
  };
  const queueOutput = (data) => {
    if (state.closed) return;
    const part = data instanceof Uint8Array ? data : new TextEncoder().encode(data);
    outputParts.push(part);
    outputBytes += part.byteLength;
    if (outputBytes >= 65536) flushOutput();
    else if (!outputTimer) outputTimer = setTimeout(flushOutput, 2);
  };
  state.cancelOutput = () => {
    state.closed = true;
    if (outputTimer) clearTimeout(outputTimer);
    outputTimer = null;
    outputParts.length = 0;
    outputBytes = 0;
  };

  // The PTY survives a dropped socket for a grace period, so a drop is a
  // retry rather than the end of the session: reconnect to the SAME terminal
  // id and the backend replays whatever the child printed while we were gone.
  const RECONNECT_CEILING_MS = 90000;
  const _openTerminalSocket = (isResume) => {
    if (state.closed || state.giveUp) return;
    let socket;
    try {
      socket = new WebSocket(state.wsUrl);
    } catch(e) {
      _scheduleTerminalReconnect();
      return;
    }
    socket.binaryType = 'arraybuffer';
    state.ws = socket;
    _wireTerminalSocket(socket, isResume);
  };
  const _scheduleTerminalReconnect = () => {
    if (state.closed || state.giveUp || state.reconnectTimer) return;
    if (!state.dropAt) state.dropAt = performance.now();
    if (performance.now() - state.dropAt > RECONNECT_CEILING_MS) {
      state.giveUp = true;
      if (activeTermSid === state.sid) {
        setTermStatus('Disconnected. Reopen this chat to resume.', 'error');
      }
      return;
    }
    state.reconnectAttempts += 1;
    // Exponential backoff with jitter: a backend restart brings every pane
    // back at once, and synchronised retries would stampede the new server.
    const backoff = Math.min(8000, 250 * Math.pow(2, state.reconnectAttempts - 1));
    const delay = Math.round(backoff * (0.75 + Math.random() * 0.5));
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      _openTerminalSocket(true);
    }, delay);
  };
  const _wireTerminalSocket = (socket, isResume) => {
    socket.onopen = () => {
      if (state.ws !== socket || state.closed) return;
      state.reconnectAttempts = 0;
      state.dropAt = 0;
      // The backend resets its ack ledger on every attach; drop any credit we
      // were still holding for the dead socket so the two stay in step.
      if (state.resetAckCredit) state.resetAckCredit();
      try { socket.send(JSON.stringify({ flow: { enabled: true } })); } catch(e) {}
      if (!opts.isNew || isResume) _armWebTerminalTail(state);
      if (activeTermSid === state.sid) {
        setTermStatus('● live · cwd: ' + (state.cwd || '(unknown)'), 'live');
        term.focus();
      }
      _markActive(state.sid);
      _reportWebRuntimeContext(activeTermSid || state.sid);
      // Force on every attach: the backend still holds the geometry from
      // before the drop, and the window may have been resized meanwhile.
      _sendResizeForSid(state.sid, true);
      if (isResume) return;
      // If this terminal is the background half of a linked pair, re-run the
      // active pane's layout so the split appears now that both are live.
      if (activeTermSid && activeTermSid !== state.sid && _linkedSiblingSid(activeTermSid) === state.sid) {
        _activateTermPane(activeTermSid);
      }
      if (!opts.background) {
        const siblingSid = _linkedSiblingSid(state.sid);
        if (siblingSid && !termSessions.has(siblingSid)) {
          startLiveTerminal(siblingSid, { background: true });
        }
      }
    };
    socket.onmessage = (ev) => {
      if (state.ws !== socket) return;
      if (typeof ev.data === 'string') {
        if (ev.data.startsWith('{')) {
          try {
            const msg = JSON.parse(ev.data);
            if (msg && msg.exit) {
              if (activeTermSid === state.sid) setTermStatus('Session ended.', 'error');
              // Auto-clean on natural exit
              teardownLiveTerminal(state.sid);
              return;
            }
            if (msg && msg.error) {
              // The PTY is really gone (killed, or the grace period lapsed).
              // Retrying can't bring it back, so stop and say so.
              state.giveUp = true;
              if (activeTermSid === state.sid) setTermStatus('Error: ' + msg.error, 'error');
              return;
            }
            if (msg && msg.attached) {
              // Control frame; any replayed backlog follows as binary frames,
              // already ordered behind this one on the same socket.
              return;
            }
            if (msg && msg.input_blocked) {
              if (window.__termDrafts) window.__termDrafts.set(state.sid, '');
              if (activeTermSid === state.sid) {
                setTermStatus('Serena is working in this chat. Manual input is locked.', 'live');
              }
              return;
            }
          } catch(e) { /* not JSON — fall through to write */ }
        }
        queueOutput(ev.data);
      } else {
        queueOutput(new Uint8Array(ev.data));
      }
    };
    socket.onerror = () => {
      if (state.ws !== socket || state.closed || state.giveUp) return;
      if (activeTermSid === state.sid) setTermStatus('WebSocket error', 'error');
    };
    socket.onclose = () => {
      if (state.ws !== socket || state.closed || state.giveUp) return;
      if (activeTermSid === state.sid) setTermStatus('Reconnecting…', 'error');
      _scheduleTerminalReconnect();
    };
  };
  _wireTerminalSocket(ws, false);

  term.onData((data) => {
    if (data.indexOf('\r') !== -1) _markWebTurnStarted(state.sid);
    const ws = _liveSocket();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({
        input: data,
        runtime_context: {
          focused_sid: state.sid,
          split_sids: _gtkSplitActive && _gtkSplitSids ? _gtkSplitSids : [],
          draft: window.__termDrafts ? (window.__termDrafts.get(state.sid) || '') : '',
        },
      }));
    }
  });

  mount.addEventListener('pointerdown', () => {
    _clearAttention(state.sid);
    _setWebTerminalFocus(state.sid);
    if (activeTermSid !== state.sid) {
      activeTermSid = state.sid;
      _webRuntimeFocusSid = state.sid;
      _reportWebRuntimeContext(state.sid);
      for (const child of container.children) {
        child.classList.toggle('runtime-focused', child.dataset.sid === state.sid);
      }
      _scheduleWebRuntimePolicy();
    }
  });
  mount.addEventListener('wheel', (ev) => {
    if (ev.deltaY < 0) _cancelWebTerminalTail(state);
  }, { passive: true, capture: true });

  _ensureTermResizeObserver();
  _bindWebTerminalWindowFocus();
  setupTerminalDrop(mount, _liveSocket);
  return state;
}

function setupTerminalDrop(mount, getSocket) {
  // === DROP === We route ALL drops through this JS handler regardless of
  // platform. Previously we deferred to pywebview's Python on_drop on Windows
  // — that worked for real file drops but broke on screenshot drops (no
  // pywebviewFullPath for in-memory image blobs), and left WebView2 in a
  // stuck "dragging" state that blocked further input. Using FormData upload
  // for everything is uniform across Linux GTK / Windows / web.

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
    if (!ev.dataTransfer) return;

    // Collect dropped images. Prefer `files` (covers real file drops), then
    // fall back to `items` (covers in-memory image blobs from Win Snipping
    // Tool, GNOME Screenshot, etc. — these often skip `files` entirely).
    const collected = new Map();
    const isImageFile = (f) => {
      if (!f) return false;
      if (f.type) return f.type.startsWith('image/');
      return /\.(png|jpe?g|gif|webp|bmp|svg|heic|heif)$/i.test(f.name || '');
    };
    const addImage = (f) => {
      if (!isImageFile(f)) return;
      const key = [f.name || '', f.size || 0, f.lastModified || 0, f.type || ''].join(':');
      if (!collected.has(key)) collected.set(key, f);
    };
    if (ev.dataTransfer.files) {
      for (const f of ev.dataTransfer.files) {
        addImage(f);
      }
    }
    if (ev.dataTransfer.items) {
      for (const it of ev.dataTransfer.items) {
        if (it.kind !== 'file') continue;
        addImage(it.getAsFile());
      }
    }
    const images = Array.from(collected.values());
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
    const ws = getSocket();
    if (ws && ws.readyState === 1) {
      const input = paths.map(p => "'" + p.replace(/'/g, "'\\''") + "' ").join('');
      ws.send(input);
      const sid = mount.dataset.sid;
      if (sid && window.__termDrafts) {
        window.__termDrafts.set(sid, (window.__termDrafts.get(sid) || '') + input);
      }
    }
    toast.update(
      images.length === 1 ? 'Image attached' : (images.length + ' images attached'),
      'success'
    );
  });
}

function _migrateLiveTerminalSid(oldSid, newSid) {
  if (!oldSid || !newSid || oldSid === newSid) return false;
  const runtime = termSessions.get(oldSid);
  if (!runtime) return false;
  termSessions.delete(oldSid);
  runtime.sid = newSid;
  runtime.mount.dataset.sid = newSid;
  termSessions.set(newSid, runtime);
  if (activeTermSid === oldSid) activeTermSid = newSid;
  if (_webRuntimeFocusSid === oldSid) _webRuntimeFocusSid = newSid;
  if (window.__termDrafts && window.__termDrafts.has(oldSid)) {
    window.__termDrafts.set(newSid, window.__termDrafts.get(oldSid));
    window.__termDrafts.delete(oldSid);
  }
  const partner = _pendingTermPartners.get(oldSid);
  if (partner) {
    _pendingTermPartners.delete(oldSid);
    _pendingTermPartners.set(newSid, partner);
    if (_pendingTermPartners.get(partner) === oldSid) {
      _pendingTermPartners.set(partner, newSid);
    }
  }
  fetch('/api/terminal-runtime/migrate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      old_sid: oldSid,
      new_sid: newSid,
      terminal_id: runtime.tid,
    }),
  }).catch(() => {});
  return true;
}

window.__spawnLinkedTerminal = async function(sourceSid, pseudoSid, agent, cwd) {
  if (!sourceSid || !pseudoSid || !agent) return false;
  _pendingTermPartners.set(sourceSid, pseudoSid);
  _pendingTermPartners.set(pseudoSid, sourceSid);
  if (!termSessions.has(sourceSid)) await startLiveTerminal(sourceSid);
  await startLiveTerminal(pseudoSid, {
    cwd: cwd || '',
    agent,
    isNew: true,
    background: true,
  });
  _activateTermPane(sourceSid);
  return true;
};

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
  const partner = _pendingTermPartners.get(sid);
  _pendingTermPartners.delete(sid);
  if (partner && _pendingTermPartners.get(partner) === sid) {
    _pendingTermPartners.delete(partner);
  }
  if (window.__termDrafts) window.__termDrafts.delete(sid);
  _unmarkActive(sid);
  if (s.cancelOutput) s.cancelOutput();
  // Deliberate teardown: stop the reconnect machinery before closing, or the
  // close handler would immediately try to resume a PTY we're about to kill.
  s.giveUp = true;
  if (s.reconnectTimer) { clearTimeout(s.reconnectTimer); s.reconnectTimer = null; }
  try { if (s.ws && s.ws.readyState <= 1) s.ws.close(); } catch(e) {}
  // The WebGL renderer holds a GPU context; dropping the Terminal alone leaks
  // it until GC, and Chromium caps live contexts per process.
  try { if (s.rendererAddon) s.rendererAddon.dispose(); } catch(e) {}
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
let _gtkStartSeq = 0;

function _gtkGetRect() {
  const mount = document.getElementById('termMounts');
  if (!mount) return null;
  const r = mount.getBoundingClientRect();
  return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) };
}

function _gtkRectIsVisible(rect) {
  return Boolean(rect && rect.w > 10 && rect.h > 10);
}

function _sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function _nextFrame() {
  return new Promise(resolve => requestAnimationFrame(() => resolve()));
}

async function _prepareGtkTermMount(timeoutMs) {
  if (currentTab !== 'chats') return null;
  _codeTab = '__term__';
  renderCodeTabs();
  syncCodeView();
  await _nextFrame();
  await _nextFrame();
  if (currentTab !== 'chats') return null;
  const started = performance.now();
  let rect = _gtkGetRect();
  while (
    currentTab === 'chats' &&
    (!rect || rect.w < 160 || rect.h < 120) &&
    performance.now() - started < (timeoutMs || 5000)
  ) {
    await _sleep(100);
    rect = _gtkGetRect();
  }
  return currentTab === 'chats' ? rect : null;
}

async function startGtkCode(sid) {
  const seq = ++_gtkStartSeq;
  const local = _findClientSession(sid);
  if (_isSerenaVoiceSession(local || sid) || _isFleetSession(local)) {
    openConv(sid);
    return;
  }
  if (local && local.external_runtime_active) {
    setConvMode('read');
    return;
  }
  const agent = (local && local.agent) || 'claude';
  setTermStatus('Starting ' + agent + ' ' + sid.slice(0, 8) + '…', 'live');
  _gtkCodeSid = sid;
  _renderOpenSessionIds([sid]);
  const rect = await _prepareGtkTermMount();
  // The user may leave Chats while the terminal mount waits for layout.
  // Never let that stale continuation remap a VTE over Fleet/Memory/etc.
  if (currentTab !== 'chats' || seq !== _gtkStartSeq || _gtkCodeSid !== sid) return;
  const cwd = (local && local.cwd) || '';

  // === SPLIT FEATURE === (linked threads → split-pane view)
  if (local && local.group) {
    const sibs = _siblingsInGroup(local.group, sid);
    if (sibs.length) {
      _gtkCurrentGroup = local.group;
      _syncRuntimePinButton();
      const pinBoth = _gtkPinnedGroups.has(local.group);
      const partnerSid = _linkedSiblingSid(sid);
      const partner = sibs.find(s => s.session_id === partnerSid);
      if (!partner) {
        _gtkCurrentGroup = null;
      } else {
      // Always render claude on the left, codex on the right — regardless of
      // which side the user clicked or which is more recent.
      const aClaude = (local.agent || 'claude').toLowerCase() === 'claude';
      const bClaude = (partner.agent || 'claude').toLowerCase() === 'claude';
      let leftSid, rightSid, leftAgent, rightAgent, leftCwd, rightCwd;
      if (aClaude && !bClaude) {
        leftSid = sid; rightSid = partner.session_id;
        leftAgent = local.agent; rightAgent = partner.agent;
        leftCwd = cwd; rightCwd = partner.cwd || cwd || '';
      } else if (!aClaude && bClaude) {
        leftSid = partner.session_id; rightSid = sid;
        leftAgent = partner.agent; rightAgent = local.agent;
        leftCwd = partner.cwd || cwd || ''; rightCwd = cwd;
      } else {
        // Both same agent — keep clicked-first on the left
        leftSid = sid; rightSid = partner.session_id;
        leftAgent = local.agent || 'claude'; rightAgent = partner.agent || 'claude';
        leftCwd = cwd; rightCwd = partner.cwd || cwd || '';
      }
      // Already split with this exact pair? Just refocus.
      const pair = new Set([leftSid, rightSid]);
      const cur = _gtkSplitSids ? new Set(_gtkSplitSids) : null;
      if (_gtkSplitActive && cur && cur.size === 2 && [...pair].every(x => cur.has(x))) {
        _gtkCodeSid = sid;
        window.gtkSend({ type: 'code-focus', sid, rect });
        window.gtkSend({ type: 'code-pin-both', pinned: pinBoth, focus_sid: sid });
      } else {
        const meta = {};
        meta[leftSid] = { cwd: leftCwd, agent: leftAgent, isNew: _isPseudoSid(leftSid) };
        meta[rightSid] = { cwd: rightCwd, agent: rightAgent, isNew: _isPseudoSid(rightSid) };
        window.gtkSend({
          type: 'code-split-on',
          sids: [leftSid, rightSid],
          spawn_meta: meta,
          rect,
          focus_sid: sid,
          ratio: _gtkSplitRatio,
          pin_both: pinBoth,
        });
        _gtkSplitActive = true;
        _gtkSplitSids = [leftSid, rightSid];
        _syncRuntimePinButton();
      }
        _refreshGtkRuntimeStatus();
        _rememberActive(leftSid);
        _rememberActive(rightSid);
        renderSessionList();
        _ensureActiveRefresh();
        _wireGtkRectObs();
        return;
      }
    }
  }
  _gtkCurrentGroup = null;
  _syncRuntimePinButton();
  // Not linked (or sibling not in view) → exit any active split, single view.
  if (_gtkSplitActive) {
    window.gtkSend({ type: 'code-split-off', focus_sid: sid });
    _gtkSplitActive = false;
    _gtkSplitSids = null;
  }
  // === SPLIT FEATURE END ===

  window.gtkSend({ type: 'code-on', sid, cwd, rect, isNew: _isPseudoSid(sid), agent });
  _wireGtkRectObs();
}

function _wireGtkRectObs() {
  if (_gtkRectObs) _gtkRectObs.disconnect();
  _gtkRectObs = new ResizeObserver(() => {
    if (currentTab !== 'chats' || !_gtkCodeSid) return;
    const r = _gtkGetRect();
    if (_gtkRectIsVisible(r)) window.gtkSend({ type: 'code-rect', rect: r });
  });
  const mount = document.getElementById('termMounts');
  if (mount) _gtkRectObs.observe(mount);
  window.addEventListener('resize', _gtkOnResize);
}

// === SPLIT FEATURE ===
let _gtkSplitActive = false;
let _gtkSplitSids = null;
let _gtkSplitRatio = 0.5;  // 0..1 fraction of conv-area width given to the LEFT pane
let _gtkCurrentGroup = null;
const _gtkRuntimeStates = new Map();
let _gtkPinnedGroups = new Set();
try {
  const saved = localStorage.getItem('serena.splitRatio');
  if (saved) {
    const r = parseFloat(saved);
    if (!isNaN(r) && r > 0.05 && r < 0.95) _gtkSplitRatio = r;
  }
} catch(e) {}
try {
  const savedPins = JSON.parse(localStorage.getItem('serena.pinnedRuntimeGroups') || '[]');
  if (Array.isArray(savedPins)) _gtkPinnedGroups = new Set(savedPins.filter(Boolean));
} catch(e) {}

function _syncRuntimePinButton() {
  const btn = document.getElementById('runtimePinBtn');
  if (!btn) return;
  const visible = Boolean(_gtkCurrentGroup && _gtkSplitActive);
  const pinned = Boolean(_gtkCurrentGroup && _gtkPinnedGroups.has(_gtkCurrentGroup));
  btn.classList.toggle('hidden', !visible);
  btn.classList.toggle('active', pinned);
  btn.setAttribute('aria-pressed', pinned ? 'true' : 'false');
}

function toggleGtkPinBoth() {
  if (!_gtkCurrentGroup || !_gtkSplitActive) return;
  const pinned = !_gtkPinnedGroups.has(_gtkCurrentGroup);
  if (pinned) _gtkPinnedGroups.add(_gtkCurrentGroup);
  else _gtkPinnedGroups.delete(_gtkCurrentGroup);
  try {
    localStorage.setItem('serena.pinnedRuntimeGroups', JSON.stringify([..._gtkPinnedGroups]));
  } catch(e) {}
  _syncRuntimePinButton();
  if (window.__nativeTerminalBridge) {
    window.gtkSend && window.gtkSend({
      type: 'code-pin-both',
      pinned,
      focus_sid: _gtkCodeSid,
    });
  } else {
    _scheduleWebRuntimePolicy();
  }
}

function _refreshGtkRuntimeStatus() {
  _renderOpenSessionIds(_visibleRuntimeSids());
  // Below is split-only: one status line summarising BOTH runtimes. A single
  // pane keeps the live/cwd line its own websocket handlers already maintain.
  if (!_gtkSplitActive || !_gtkSplitSids) return;
  const labels = _gtkSplitSids.map(sid => {
    const session = _findClientSession(sid);
    const runtime = termSessions.get(sid);
    const agent = ((session && session.agent) || (runtime && runtime.agent) || 'session').toLowerCase();
    const state = _gtkRuntimeStates.get(sid) || 'ready';
    const label = state === 'paused' ? 'standby' : state;
    return agent + ' ' + label;
  });
  const states = _gtkSplitSids.map(sid => _gtkRuntimeStates.get(sid));
  const cls = states.some(state => state === 'crashed') ? 'error' :
              states.some(state => state === 'live' || state === 'waking') ? 'live' : '';
  setTermStatus(labels.join('  ·  '), cls);
}

function _migrateGtkRuntimeState(oldSid, newSid) {
  if (!oldSid || !newSid || oldSid === newSid) return;
  if (_gtkRuntimeStates.has(oldSid)) {
    _gtkRuntimeStates.set(newSid, _gtkRuntimeStates.get(oldSid));
    _gtkRuntimeStates.delete(oldSid);
  }
  if (_gtkSplitSids) {
    _gtkSplitSids = _gtkSplitSids.map(sid => sid === oldSid ? newSid : sid);
  }
  _refreshGtkRuntimeStatus();
}

window.onGtkRuntimeState = function(sid, state) {
  if (!sid || !state) return;
  _gtkRuntimeStates.set(sid, state);
  if (state === 'live') {
    _gtkReadyTerms.add(sid);
    _markActive(sid);
  }
  _refreshGtkRuntimeStatus();
};

window.onGtkPanedPos = function(pos, ratio) {
  // GTK side reports both the pixel position and the derived ratio. We only
  // persist the ratio because pixels are meaningless across resizes.
  if (typeof ratio === 'number' && ratio > 0.05 && ratio < 0.95) {
    _gtkSplitRatio = ratio;
    try { localStorage.setItem('serena.splitRatio', String(ratio)); } catch(e) {}
  }
};
// === SPLIT FEATURE END ===

function _gtkOnResize() {
  if (currentTab !== 'chats' || !_gtkCodeSid) return;
  const r = _gtkGetRect();
  if (_gtkRectIsVisible(r)) window.gtkSend({ type: 'code-rect', rect: r });
}

function stopGtkCode() {
  _gtkStartSeq++;
  _gtkCodeSid = null;
  if (_gtkRectObs) { _gtkRectObs.disconnect(); _gtkRectObs = null; }
  window.removeEventListener('resize', _gtkOnResize);
  // === SPLIT FEATURE === (a code-off in python already dismantles the split,
  // but mirror the JS state so the next openConv() rebuilds correctly)
  _gtkSplitActive = false;
  _gtkSplitSids = null;
  _gtkCurrentGroup = null;
  _syncRuntimePinButton();
  // === SPLIT FEATURE END ===
  window.gtkSend && window.gtkSend({ type: 'code-off' });
  _renderOpenSessionIds([]);
  setTermStatus('Ready to resume.');
}

window.onGtkCodeStart = function(sid) {
  if (sid) _gtkReadyTerms.add(sid);
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

window.onGtkExternalRuntime = function(sid) {
  if (!sid) return;
  _patchClientSession(sid, { external_runtime_active: true });
  if (sid === currentSessionId) setConvMode('read');
};

// Windows/macOS path: GTK isn't running, so the Alt+key intercepts in
// app_gtk.py never fire. Mirror them in JS so the same shortcuts work in
// the pywebview/xterm.js fallback. Bindings come from /api/keybindings,
// which reads the same ~/.config/serena/keybindings.json the GTK side uses,
// so user customizations apply on both platforms. Pre-seed with defaults so
// shortcuts work immediately on first paint, before the fetch returns.
let _customBindings = {
  'view-chats':         { alt:true,  ctrl:false, shift:false, meta:false, key:'1' },
  'view-memory':        { alt:true,  ctrl:false, shift:false, meta:false, key:'2' },
  'view-knowledge':     { alt:true,  ctrl:false, shift:false, meta:false, key:'3' },
  'view-usage':         { alt:true,  ctrl:false, shift:false, meta:false, key:'4' },
  'next':               { alt:true,  ctrl:false, shift:false, meta:false, key:'j' },
  'prev':               { alt:true,  ctrl:false, shift:false, meta:false, key:'k' },
  'focus-search':       { alt:true,  ctrl:false, shift:false, meta:false, key:'/' },
  'toggle-done':        { alt:true,  ctrl:false, shift:false, meta:false, key:'d' },
  'close-terminal':     { alt:true,  ctrl:false, shift:false, meta:false, key:'w' },
  'delete':             { alt:true,  ctrl:false, shift:false, meta:false, key:'Delete' },
  'rename':             { alt:true,  ctrl:false, shift:false, meta:false, key:'r' },
  'retitle':            { alt:true,  ctrl:false, shift:false, meta:false, key:'t' },
  'star':               { alt:true,  ctrl:false, shift:false, meta:false, key:'s' },
  'resume-ext':         { alt:true,  ctrl:false, shift:false, meta:false, key:'o' },
  'new-chat-external':  { alt:true,  ctrl:false, shift:false, meta:false, key:'n' },
  'new-chat-pick-dir':  { alt:true,  ctrl:false, shift:true,  meta:false, key:'n' },
  'toggle-files':       { alt:true,  ctrl:false, shift:false, meta:false, key:'b' },
  'term-newline':       { alt:false, ctrl:false, shift:true,  meta:false, key:'Enter' },
  'term-delete-word':   { alt:false, ctrl:true,  shift:false, meta:false, key:'Backspace' },
  'term-copy':          { alt:false, ctrl:true,  shift:true,  meta:false, key:'c' },
  'term-paste':         { alt:false, ctrl:true,  shift:true,  meta:false, key:'v' },
};
fetch('/api/keybindings').then(r => r.json()).then(b => {
  if (b && typeof b === 'object' && Object.keys(b).length) {
    _customBindings = b;
    console.log('[keybindings] loaded', Object.keys(b).length, 'actions');
  }
}).catch((e) => { console.warn('[keybindings] fetch failed, using defaults', e); });

function _matchBinding(e) {
  if (!_customBindings) return null;
  const evtKey = (e.key || '');
  const evtKeyLower = evtKey.length === 1 ? evtKey.toLowerCase() : evtKey;
  for (const [action, b] of Object.entries(_customBindings)) {
    if (!b) continue;
    if (!!b.alt   !== !!e.altKey)   continue;
    if (!!b.ctrl  !== !!e.ctrlKey)  continue;
    if (!!b.shift !== !!e.shiftKey) continue;
    if (!!b.meta  !== !!e.metaKey)  continue;
    const wantKey = (b.key || '').length === 1 ? b.key.toLowerCase() : b.key;
    if (wantKey === evtKeyLower) return action;
  }
  return null;
}

document.addEventListener('keydown', (e) => {
  // Modal dialogs own the keyboard while visible. In particular, do not let
  // an app shortcut race the dialog's Enter/Escape handler.
  if (document.getElementById('modalBackdrop')?.classList.contains('visible')) return;
  // Skip if focus is on an editable surface — let the user type freely
  const tag = (e.target && e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') {
    // …unless the binding explicitly uses a modifier (Alt/Ctrl/Meta)
    if (!e.altKey && !e.ctrlKey && !e.metaKey) return;
  }
  const action = _matchBinding(e);
  if (!action) return;
  // term-* actions are handled by xterm's customKeyEventHandler (which has
  // the WS reference) — let xterm consume them, don't dispatch here.
  if (action.startsWith('term-')) return;
  if (typeof window.__gtkShortcut !== 'function') {
    console.warn('[keybindings] no __gtkShortcut for', action);
    return;
  }
  e.preventDefault();
  e.stopPropagation();
  window.__gtkShortcut(action);
}, true);

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
    // Alt+N — used to call newChat() (legacy external-terminal flow that
    // spawned gnome-terminal). Now routes to inline: works on Windows
    // (xterm.js) AND Linux (GTK VTE). External terminal flow lives on
    // only as the fallback inside newChatInline when no in-app path works.
    case 'new-chat-external': newChatInline(); return;
    case 'new-chat-pick-dir': newChatPickDir(); return;
    case 'focus-search': {
      const el = document.getElementById('searchInput');
      if (el) el.focus();
      return;
    }
    case 'close-terminal': {
      const sid = currentSessionId || focusedSid();
      if (!sid) return;
      // Close the WHOLE linked thread, not just the focused side. Otherwise the
      // row stays in Active because the linked sibling's agent is still running,
      // and the chat never drops back down into the normal list.
      const chat = _findClientSession(sid)
        || (typeof sessionSource !== 'undefined' && sessionSource.find(x => x.session_id === sid));
      let toClose = [sid];
      if (chat && chat.group) {
        const pool = (typeof sessionSource !== 'undefined' && sessionSource.length) ? sessionSource : sessions;
        toClose = pool.filter(s => s.group === chat.group).map(s => s.session_id);
      }
      for (const id of toClose) if (_activeTerms.has(id)) closeActiveTerminal(id);
      return;
    }
    case 'toggle-files': toggleFocusMode(); return;
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

async function newChatInline(cwdOverride) {
  // In-app new chat — spawns a fresh claude in the in-app terminal:
  //   - Linux GTK shell: uses native VTE widget
  //   - Windows/macOS pywebview: uses xterm.js + PTY via WebSocket
  //   - Browser-only: same as Windows (xterm.js)
  // `cwdOverride` (string) bypasses the active-project default — used by
  // `newChatPickDir()` after the user picks a folder via the file chooser.

  const res = await showPrompt({
    title: 'New chat' + (cwdOverride ? ` (${cwdOverride.split('/').filter(Boolean).pop() || cwdOverride})` : ''),
    body: '',
    placeholder: 'Name this chat (e.g. Debug deploy)',
    confirm: 'Create',
    agentPicker: true,
    defaultAgent: _lastNewChatAgent || 'claude',
  });
  if (res === null) return;
  const typedTitle = (res.value || '').trim();
  let agent = res.agent || 'claude';
  _lastNewChatAgent = agent;

  // cwd: explicit override (from folder picker) > active project chip > empty
  // (Python defaults empty to $HOME).
  // Resolve the no-project case to the SAME directory Python will spawn in.
  // Storing '' here while the terminal starts in $HOME is what left the
  // placeholder unmatchable, so the chat showed up twice: once as this row and
  // once as the real session under Today.
  await _loadDefaultCwd();
  const cwd = (cwdOverride !== undefined && cwdOverride !== null)
    ? cwdOverride
    : (currentProjectCwd || _defaultCwd() || '');
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
    agent,
    // If the user typed a name, remember it so the REAL session gets the same
    // title once the reconciler migrates the pseudo onto it.
    pending_rename_title: typedTitle || null,
  };
  _pseudoSessions.unshift(pseudo);
  setSessionSource([pseudo, ...sessionSource]);
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

  setTermStatus('Starting ' + agent + '…', 'live');
  if (window.__nativeTerminalBridge) {
    // Linux GTK shell — spawn a native VTE
    const rect = await _prepareGtkTermMount();
    window.gtkSend({ type: 'code-on', sid: tempId, cwd, rect, isNew: true, agent });
    _gtkCodeSid = tempId;
  } else {
    // Windows / macOS / web — spawn via xterm.js + WebSocket PTY
    startLiveTerminal(tempId, { cwd, agent, isNew: true });
  }
  _startPseudoReconciler();
}

let _lastNewChatAgent = 'claude';

// ═══════════════════════════════════════════════════════════════
// === FOLDER PICKER === (Alt+Shift+N → GTK file chooser → new chat
// in arbitrary cwd. Replaces the old "open external terminal, cd,
// relaunch claude" workflow for working in dirs not yet tracked
// as Serena projects.)
// ═══════════════════════════════════════════════════════════════
window.__pickerResolvers = window.__pickerResolvers || {};

function pickFolder({ title = 'Choose a folder', startDir = null } = {}) {
  // Promise-returning helper: opens a GTK FileChooserNative on the python
  // side, resolves with the absolute path the user picked (or null if they
  // cancelled). Token-keyed so multiple pickers can race without clobbering.
  if (!window.gtkSend) return Promise.resolve(null);
  const token = 'pf_' + Math.random().toString(36).slice(2, 12);
  return new Promise((resolve) => {
    window.__pickerResolvers[token] = resolve;
    window.gtkSend({ type: 'pick-folder', token, title, startDir });
    // Safety net: if GTK never calls back (e.g., dialog crashed), free the
    // resolver after 2 minutes so we don't leak forever.
    setTimeout(() => {
      if (window.__pickerResolvers[token]) {
        delete window.__pickerResolvers[token];
        resolve(null);
      }
    }, 120000);
  });
}

window.__resolveFolderPick = function(token, path) {
  // Called from python via _eval_js once the chooser closes.
  const r = window.__pickerResolvers[token];
  if (!r) return;
  delete window.__pickerResolvers[token];
  r(path || null);
};

// === CODEX BRIDGE === (live notification when backend auto-spawns a linked
// codex via /api/spawn-linked-codex — refresh the sidebar so the new chat
// appears without requiring the user to click off and back.)
window.__onLinkedCodexSpawned = async function(pseudoSid, realSid, claudeSid) {
  try {
    if (!window.__nativeTerminalBridge) _migrateLiveTerminalSid(pseudoSid, realSid);
    _migrateGtkRuntimeState(pseudoSid, realSid);
    // Force a disk rescan so the new codex JSONL gets indexed before we
    // re-render the sidebar; `loadSessions` itself doesn't refresh.
    await loadSessions(currentProject, { refresh: true });
    // Mark the linked codex as the active terminal partner of claude
    try {
      if (typeof _activeTerms !== 'undefined' && claudeSid && _activeTerms.has(claudeSid)) {
        _activeTerms.add(realSid);
      }
      if (typeof _gtkReadyTerms !== 'undefined' && pseudoSid && _gtkReadyTerms.has(pseudoSid)) {
        _gtkReadyTerms.delete(pseudoSid);
        _gtkReadyTerms.add(realSid);
      }
    } catch(e) {}
    _pendingTermPartners.delete(pseudoSid);
  } catch(e) {
    console.warn('[bridge] post-spawn refresh failed:', e);
  }
};
// Mirror for codex-initiated claude spawns (core/claude_spawn.py), same
// bookkeeping with the agent roles flipped.
window.__onLinkedClaudeSpawned = async function(pseudoSid, realSid, codexSid) {
  try {
    if (!window.__nativeTerminalBridge) _migrateLiveTerminalSid(pseudoSid, realSid);
    _migrateGtkRuntimeState(pseudoSid, realSid);
    await loadSessions(currentProject, { refresh: true });
    try {
      if (typeof _activeTerms !== 'undefined' && codexSid && _activeTerms.has(codexSid)) {
        _activeTerms.add(realSid);
      }
      if (typeof _gtkReadyTerms !== 'undefined' && pseudoSid && _gtkReadyTerms.has(pseudoSid)) {
        _gtkReadyTerms.delete(pseudoSid);
        _gtkReadyTerms.add(realSid);
      }
    } catch(e) {}
    _pendingTermPartners.delete(pseudoSid);
  } catch(e) {
    console.warn('[bridge] post-spawn refresh failed:', e);
  }
};
// === CODEX BRIDGE END ===

async function newChatPickDir() {
  // Alt+Shift+N: pick a folder, then create an inline chat there.
  if (!window.__gtkBridge) {
    showToast('Folder picker requires the GTK desktop shell', { variant: 'error' });
    return;
  }
  // Default the chooser to the current project cwd if there is one — saves a
  // few clicks when picking a sibling dir.
  const cwd = await pickFolder({
    title: 'New chat — pick working directory',
    startDir: currentProjectCwd || null,
  });
  if (!cwd) return;
  await newChatInline(cwd);
}

// ═══════════════════════════════════════════════════════════════
// === HANDOFF FEATURE START === (delete this block to remove the
// JS half of cross-agent session handoff. Pairs with /api/handoff
// in the python side and chats/handoff.py.)
// ═══════════════════════════════════════════════════════════════
async function _feedTerminalWhenReady(sid, text, submit, opts) {
  opts = opts || {};
  const timeoutMs = opts.timeoutMs || 12000;
  const settleMs = opts.settleMs || 700;
  const start = performance.now();
  while (performance.now() - start < timeoutMs) {
    if (window.__nativeTerminalBridge && _gtkReadyTerms.has(sid)) {
      if (settleMs) await _sleep(settleMs);
      await _prepareGtkTermMount();
      window.gtkSend({ type: 'feed-text', sid, text, submit: !!submit });
      return true;
    }
    if (!window.__nativeTerminalBridge) {
      const runtime = termSessions.get(sid);
      if (runtime && runtime.ws && runtime.ws.readyState === 1) {
        if (settleMs) await _sleep(settleMs);
        runtime.ws.send('\x1b[200~' + text + '\x1b[201~');
        if (submit) {
          _markWebTurnStarted(sid);
          await _sleep(250);
          runtime.ws.send('\r');
        }
        runtime.term.focus();
        return true;
      }
    }
    await _sleep(250);
  }
  if (window.__nativeTerminalBridge) {
    await _prepareGtkTermMount();
    window.gtkSend({ type: 'feed-text', sid, text, submit: !!submit });
  }
  return false;
}

async function _resolveHandoffSid(sid, timeoutMs) {
  if (!_isPseudoSid(sid)) return sid;
  const alreadyResolved = _resolvedPseudoSids.get(sid);
  if (alreadyResolved) return alreadyResolved;

  const pseudo = _pseudoSessions.find(p => p && p.session_id === sid);
  if (!pseudo) {
    throw new Error('New chat is still starting. Send one message, then try the handoff again.');
  }

  const deadline = performance.now() + (timeoutMs || 15000);
  while (performance.now() < deadline) {
    const resolved = pseudo.resolved_session_id || _resolvedPseudoSids.get(sid);
    if (resolved) return resolved;

    if (!_pseudoInFlight) {
      _pseudoInFlight = true;
      try {
        const params = new URLSearchParams();
        if (currentProject && currentProject.length) params.set('projects', currentProject.join(','));
        params.set('refresh', '1');
        const r = await fetch('/api/sessions?' + params);
        if (r.ok) {
          const fresh = await r.json();
          await _reconcilePseudos(fresh);
        }
      } catch(e) {
        // The regular pseudo reconciler will retry; keep the bounded wait alive.
      } finally {
        _pseudoInFlight = false;
      }
    }
    await _sleep(400);
  }

  throw new Error('New chat has not created a session yet. Send one message, then try again.');
}

async function handoffSession(srcSid, targetAgent) {
  if (!srcSid || !targetAgent) return;

  const toast = showToast('Preparing handoff…', { spinner: true, sticky: true });
  try {
    srcSid = await _resolveHandoffSid(srcSid, 15000);
  } catch(e) {
    toast.update('Handoff failed: ' + e.message, 'error');
    return;
  }

  // === GROUP FEATURE === (a linked thread renders as ONE collapsed row whose
  // representative is the claude chat, so renderSessionList folds the codex out
  // of `sessions`. Use sessionSource — the full, unfolded list — to see EVERY
  // member of the thread.)
  const _pool = (typeof sessionSource !== 'undefined' && sessionSource.length) ? sessionSource : sessions;
  const srcChat = _pool.find(x => x.session_id === srcSid);
  let members = srcChat ? [srcChat] : [];
  if (srcChat && srcChat.group) {
    members = _pool.filter(s => s.group === srcChat.group);
  }
  const _byRecent = (a, b) => (b.last_timestamp || '').localeCompare(a.last_timestamp || '');
  // Where we LAND: the thread's chat of the requested agent (most recent).
  const targetChat = members
    .filter(s => (s.agent || 'claude').toLowerCase() === targetAgent)
    .sort(_byRecent)[0] || null;
  // What we BRIEF FROM: the latest work on the OTHER side of the thread (that's
  // what you're handing over). No other-agent chat → brief from the chat you're
  // in. This is why "→ Claude" on the claude-rep row still does something useful:
  // it carries the codex's progress into claude.
  let briefFrom = members
    .filter(s => (s.agent || 'claude').toLowerCase() !== targetAgent)
    .sort(_byRecent)[0] || srcChat || null;
  let briefSid = briefFrom ? briefFrom.session_id : srcSid;
  try {
    briefSid = await _resolveHandoffSid(briefSid, 15000);
    briefFrom = _findClientSession(briefSid) || briefFrom;
  } catch(e) {
    toast.update('Handoff failed: ' + e.message, 'error');
    return;
  }
  // === GROUP FEATURE END ===

  let resp;
  try {
    const r = await fetch('/api/handoff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_sid: briefSid, target_agent: targetAgent }),
    });
    resp = await r.json();
    if (!r.ok || !resp.ok) throw new Error(resp.error || 'handoff failed');
  } catch(e) {
    toast.update('Handoff failed: ' + e.message, 'error');
    return;
  }

  // === GROUP FEATURE === (land on the thread's chat of the requested agent)
  if (targetChat) {
    const targetSid = targetChat.session_id;
    const targetLabel = targetAgent === 'claude' ? 'Claude' : 'Codex';
    if (targetSid === briefSid) {
      // Target and brief-source are the same single chat — nothing to carry over;
      // just open it.
      openConv(targetSid);
      toast.update('Opened ' + targetLabel + ' chat', 'success');
      return;
    }
    // If the target is ALREADY on screen in the current split, just type the
    // briefing in — don't re-openConv/re-mount (that would relayout and resize).
    const alreadyVisible = _gtkSplitActive && _gtkSplitSids
      && _gtkSplitSids.indexOf(targetSid) !== -1 && _activeTerms.has(targetSid);
    if (alreadyVisible) {
      if (window.__nativeTerminalBridge) {
        window.gtkSend({ type: 'feed-text', sid: targetSid, text: resp.prompt, submit: true });
        toast.update('Handed off to ' + targetLabel, 'success');
      } else {
        const ok = await _feedTerminalWhenReady(targetSid, resp.prompt, true, {
          timeoutMs: 5000,
          settleMs: 0,
        });
        toast.update(ok ? 'Handed off to ' + targetLabel : 'Handoff did not reach ' + targetLabel,
          ok ? 'success' : 'error');
      }
    } else {
      openConv(targetSid);
      const ok = await _feedTerminalWhenReady(targetSid, resp.prompt, true, {
        timeoutMs: 15000,
        settleMs: 1200,
      });
      toast.update(ok ? 'Handed off to ' + targetLabel : 'Opened ' + targetLabel + ', but handoff may not have landed',
        ok ? 'success' : 'error');
    }
    return;
  }
  // === GROUP FEATURE END ===

  const cwd = resp.cwd || '';
  const shortProj = cwd ? (cwd.split('/').filter(Boolean).pop() || '~') : '~';
  // Inherit the source chat's title so both halves of the handed-off thread
  // share a name. The shared group color makes them feel like one continuous
  // conversation across agents instead of "↪ Handoff from codex" duplicates.
  // (srcChat was already resolved above for the existing-sibling reuse path.)
  const label = (briefFrom && briefFrom.display_title)
    || (srcChat && srcChat.display_title)
    || resp.source_title
    || ('↪ Handoff from ' + (resp.source_agent || 'previous'));
  const tempId = 'new-' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  const iso = new Date().toISOString();
  const pendingLinkSids = Array.from(new Set([
    srcSid,
    briefSid,
    ...members.map(m => m && m.session_id),
  ].filter(Boolean)));
  const optimisticGroup = (srcChat && srcChat.group)
    || (briefFrom && briefFrom.group)
    || ('pending_' + tempId);
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
    agent: targetAgent,
    group: optimisticGroup,
    pending_rename_title: label,
    // === GROUP FEATURE === (auto-link src <-> dest as soon as reconciler resolves)
    pending_group_link_with: srcSid,
    pending_group_member_sids: pendingLinkSids,
    // === GROUP FEATURE END ===
  };
  _pseudoSessions.unshift(pseudo);
  setSessionSource([pseudo, ...sessionSource]);
  _applyClientGroup([...pendingLinkSids, tempId], optimisticGroup);
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

  setTermStatus('Starting ' + targetAgent + '…', 'live');
  if (window.__nativeTerminalBridge) await startGtkCode(tempId);
  else await startLiveTerminal(tempId, { cwd, agent: targetAgent, isNew: true });
  _startPseudoReconciler();

  const ok = await _feedTerminalWhenReady(tempId, resp.prompt, true, {
    timeoutMs: 18000,
    settleMs: 2500,
  });
  toast.update(ok
    ? 'Handed off to ' + (targetAgent === 'claude' ? 'Claude' : 'Codex')
    : 'Started ' + targetAgent + ', but handoff may not have landed',
    ok ? 'success' : 'error');
}
// === HANDOFF FEATURE END ===

async function forkLinkedContext(srcSid, targetAgent) {
  if (!srcSid || !targetAgent) return;
  const toast = showToast('Building standalone context…', { spinner: true, sticky: true });
  try {
    srcSid = await _resolveHandoffSid(srcSid, 15000);
  } catch(e) {
    toast.update('Context fork failed: ' + e.message, 'error');
    return;
  }

  let resp;
  try {
    const r = await fetch('/api/context-fork', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_sid: srcSid, target_agent: targetAgent }),
    });
    resp = await r.json();
    if (!r.ok || !resp.ok) throw new Error(resp.error || 'context fork failed');
  } catch(e) {
    toast.update('Context fork failed: ' + e.message, 'error');
    return;
  }

  const cwd = resp.cwd || '';
  const shortProj = cwd ? (cwd.split('/').filter(Boolean).pop() || '~') : '~';
  const tempId = 'new-' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  const iso = new Date().toISOString();
  const pseudo = {
    session_id: tempId,
    display_title: resp.title || 'Context fork',
    project_short: shortProj,
    cwd,
    first_timestamp: iso,
    last_timestamp: iso,
    starred: false,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_create_tokens: 0,
    isPseudo: true,
    agent: targetAgent,
    pending_rename_title: resp.title || 'Context fork',
  };
  // Intentionally no group or pending_group_link_with. This is a fresh branch.
  _pseudoSessions.unshift(pseudo);
  setSessionSource([pseudo, ...sessionSource]);
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
  document.getElementById('convTitle').textContent = pseudo.display_title;
  document.getElementById('convMeta').textContent = cwd || '~';

  setTermStatus('Starting ' + targetAgent + '…', 'live');
  if (window.__nativeTerminalBridge) {
    const rect = await _prepareGtkTermMount();
    window.gtkSend({
      type: 'code-on',
      sid: tempId,
      cwd,
      rect,
      isNew: true,
      agent: targetAgent,
      seed: resp.prompt,
    });
    _gtkCodeSid = tempId;
  } else {
    await startLiveTerminal(tempId, {
      cwd,
      agent: targetAgent,
      isNew: true,
      seed: resp.prompt,
    });
  }
  _startPseudoReconciler();
  toast.update('Started standalone ' + (targetAgent === 'claude' ? 'Claude' : 'Codex') + ' context fork', 'success');
}

// Voice coding jobs are executed only by the resident work supervisor. The
// web shell intentionally has no queue poller and cannot choose a project or
// spawn a worker for an accepted job.

// ═══════════════════════════════════════════════════════════════
// === GROUP FEATURE START === (linked-chats / shared-thread.
// Pairs with /api/group/* in python and the metadata helpers
// also marked GROUP FEATURE. Delete this block to remove.)
// ═══════════════════════════════════════════════════════════════
function _hashStr(s) {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
  return h;
}
function _groupColor(gid) {
  if (!gid) return null;
  // One color for every linked thread — visually consistent across the list.
  // Teal: distinct from claude-orange, codex-purple, the rose brand, and the
  // green "live" cue, so a linked pair reads as its own thing.
  return 'hsl(182, 58%, 52%)';
}

function _siblingsInGroup(gid, excludeSid) {
  if (!gid) return [];
  const seen = new Set();
  const out = [];
  for (const s of (sessionSource.length ? sessionSource : sessions)) {
    if (!s || s.group !== gid) continue;
    if (s.session_id === excludeSid) continue;
    if (seen.has(s.session_id)) continue;
    seen.add(s.session_id);
    out.push(s);
  }
  return out;
}

function _cycleGroup(sid) {
  const cur = sessions.find(x => x.session_id === sid);
  if (!cur || !cur.group) return;
  const members = [cur, ..._siblingsInGroup(cur.group, sid)];
  members.sort((a, b) => {
    const aClaude = (a.agent || 'claude').toLowerCase() === 'claude' ? 0 : 1;
    const bClaude = (b.agent || 'claude').toLowerCase() === 'claude' ? 0 : 1;
    if (aClaude !== bClaude) return aClaude - bClaude;
    return rowActivityTs(b).localeCompare(rowActivityTs(a));
  });
  if (members.length < 2) {
    showToast('Linked sibling not in current view', { variant: 'error' });
    return;
  }
  const openIndex = members.findIndex(member => member.session_id === currentSessionId);
  const currentIndex = openIndex >= 0
    ? openIndex
    : members.findIndex(member => member.session_id === sid);
  const next = members[(currentIndex + 1) % members.length];
  const idx = sessions.findIndex(s => s.session_id === next.session_id);
  if (idx >= 0) {
    setFocus(idx, true);
  }
  openConv(next.session_id);
}

async function linkSessions(sids) {
  if (!sids || sids.length < 2) return null;
  try {
    const r = await fetch('/api/group/link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_ids: sids }),
    });
    const resp = await r.json();
    if (!r.ok || !resp.ok) throw new Error(resp.error || 'link failed');
    await loadSessions(currentProject);
    // If the currently-open chat is now linked, engage split view immediately
    // instead of waiting for the user to reopen it.
    if (currentSessionId) {
      const cur = sessions.find(s => s.session_id === currentSessionId);
      if (cur && cur.group) {
        if (window.__nativeTerminalBridge) startGtkCode(currentSessionId);
        else startLiveTerminal(currentSessionId);
      }
    }
    return resp;
  } catch(e) {
    showToast('Link failed: ' + e.message, { variant: 'error' });
    return null;
  }
}

async function unlinkSession(sid) {
  if (!sid) return;
  try {
    await fetch('/api/group/unlink', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid }),
    });
    await loadSessions(currentProject);
  } catch(e) {
    showToast('Unlink failed: ' + e.message, { variant: 'error' });
  }
}

async function disbandGroup(gid) {
  if (!gid) return;
  const ok = await showConfirm({
    title: 'Disband linked thread?',
    body: 'Removes the link from every chat in this group. The chats themselves stay.',
    confirm: 'Disband',
    danger: true,
  });
  if (!ok) return;
  try {
    await fetch('/api/group/disband', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_id: gid }),
    });
    await loadSessions(currentProject);
  } catch(e) {
    showToast('Disband failed: ' + e.message, { variant: 'error' });
  }
}

function showLinkPicker(srcSid) {
  return new Promise((resolve) => {
    const bd = document.getElementById('modalBackdrop');
    const titleEl = document.getElementById('modalTitle');
    const bodyEl = document.getElementById('modalBody');
    const input = document.getElementById('modalInput');
    const okBtn = document.getElementById('modalConfirmBtn');
    const cancelBtn = document.getElementById('modalCancelBtn');
    const picker = document.getElementById('modalAgentPicker');

    titleEl.textContent = 'Link to chat';
    bodyEl.innerHTML = '';
    input.style.display = 'none';
    if (picker) { picker.style.display = 'none'; picker.innerHTML = ''; }
    okBtn.style.display = 'none';
    cancelBtn.textContent = 'Cancel';

    const wrap = document.createElement('div');
    wrap.innerHTML =
      '<input class="link-picker-search" id="lpSearch" placeholder="Search recent chats…" />'
      + '<div class="link-picker" id="lpList"></div>';
    bodyEl.appendChild(wrap);
    const search = wrap.querySelector('#lpSearch');
    const list = wrap.querySelector('#lpList');

    const candidates = (sessions || []).filter(s => s && s.session_id !== srcSid && !_isPseudoSid(s.session_id));
    let filtered = candidates.slice(0, 100);
    let focusedIdx = 0;

    function render() {
      if (!filtered.length) {
        list.innerHTML = '<div class="link-picker-empty">No matches</div>';
        return;
      }
      list.innerHTML = filtered.map((s, i) =>
        '<div class="link-picker-row' + (i === focusedIdx ? ' focused' : '') + '" data-i="' + i + '">'
          + (s.group ? '<span class="session-link" style="color:' + _groupColor(s.group) + '">' + _LINK_SVG + '</span>' : '')
          + '<span class="lp-title">' + esc(s.display_title || 'Untitled') + '</span>'
          + '<span class="lp-meta">' + esc(s.project_short || '') + '</span>'
        + '</div>'
      ).join('');
      list.querySelectorAll('.link-picker-row').forEach(row => {
        row.addEventListener('click', () => {
          const i = parseInt(row.getAttribute('data-i'), 10);
          if (filtered[i]) close(filtered[i].session_id);
        });
      });
    }
    render();

    search.addEventListener('input', () => {
      const q = search.value.trim().toLowerCase();
      if (!q) filtered = candidates.slice(0, 100);
      else filtered = candidates.filter(s =>
        (s.display_title || '').toLowerCase().includes(q) ||
        (s.project_short || '').toLowerCase().includes(q) ||
        (s.first_message || '').toLowerCase().includes(q)
      ).slice(0, 100);
      focusedIdx = 0;
      render();
    });

    const close = (result) => {
      bd.classList.remove('visible');
      bd.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey, true);
      cancelBtn.removeEventListener('click', onCancel);
      bodyEl.innerHTML = '';
      okBtn.style.display = '';
      _modalRestoreTerminal();
      resolve(result);
    };
    const onBackdrop = (e) => { if (e.target === bd) close(null); };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(null); }
      else if (e.key === 'Enter' && filtered[focusedIdx]) { e.preventDefault(); e.stopPropagation(); close(filtered[focusedIdx].session_id); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); focusedIdx = Math.min(filtered.length - 1, focusedIdx + 1); render(); }
      else if (e.key === 'ArrowUp')   { e.preventDefault(); focusedIdx = Math.max(0, focusedIdx - 1); render(); }
    };
    const onCancel = () => close(null);
    bd.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey, true);
    cancelBtn.addEventListener('click', onCancel);

    _modalHideTerminal();
    bd.classList.add('visible');
    setTimeout(() => search.focus(), 10);
  });
}

async function linkChatPickerFlow(srcSid) {
  const targetSid = await showLinkPicker(srcSid);
  if (!targetSid) return;
  await linkSessions([srcSid, targetSid]);
  showToast('Linked', { variant: 'success' });
}
// === GROUP FEATURE END ===

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
// TASKS — Raghav's deliberate todo list (memory type=task). Same store
// + reminder rail as everything else; this tab is just the management view.
// ═══════════════════════════════════════════════════════════════
async function loadTasks() {
  try {
    const r = await fetch('/api/memory');
    const all = await r.json();
    tasks = all.filter(m => m.type === 'task');
    tasks.sort((a,b) => (b.updated_at||'').localeCompare(a.updated_at||''));
    renderTasks();
    const c = document.getElementById('taskCount');
    if (c) c.textContent = tasks.length ? '(' + tasks.length + ')' : '';
  } catch(e) { console.error('loadTasks:', e); }
}

function taskAgo(ts) {
  if (!ts) return '';
  const then = new Date(ts.replace(' ', 'T'));
  if (isNaN(then)) return '';
  const s = (Date.now() - then.getTime()) / 1000;
  if (s < 90) return 'just now';
  if (s < 3600) return Math.floor(s/60) + 'm';
  if (s < 86400) return Math.floor(s/3600) + 'h';
  return Math.floor(s/86400) + 'd';
}

function renderTasks() {
  const el = document.getElementById('taskList');
  if (!el) return;
  if (!tasks.length) {
    el.innerHTML = '<div class="empty-text">no tasks yet. add one above to start tracking, and i nudge you on it when a chat opens.</div>';
    return;
  }
  let html = '';
  for (const t of tasks) {
    const age = taskAgo(t.updated_at);
    html += '<div class="memory-row" data-tid="' + t.id + '" style="align-items:center;">'
      + '<button title="mark done" onclick="completeTask(' + t.id + ')" '
      + 'style="background:none;border:1.5px solid var(--text-dim);border-radius:50%;width:16px;height:16px;'
      + 'cursor:pointer;flex-shrink:0;margin-right:8px;padding:0;line-height:1;font-size:11px;color:var(--text-dim);">&#9675;</button>'
      + '<span class="memory-content">' + esc(t.content) + '</span>'
      + (age ? '<span class="memory-id" title="last touched">' + age + '</span>' : '')
      + '</div>';
  }
  el.innerHTML = html;
}

async function addTask() {
  const inp = document.getElementById('taskInput');
  const content = (inp.value || '').trim();
  if (!content) return;
  inp.value = '';
  try {
    await fetch('/api/memory', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, type: 'task' }),
    });
    await loadTasks();
  } catch(e) {}
}

async function completeTask(id) {
  // Done = removed from the active list (v1: completing clears it).
  const row = document.querySelector('[data-tid="' + id + '"]');
  if (row) row.style.opacity = '0.35';
  try {
    await fetch('/api/memory/' + id, { method: 'DELETE' });
    await loadTasks();
  } catch(e) {}
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
  const typeOrder = ['LOOP', 'USER', 'FEEDBACK', 'PROJECT', 'REFERENCE', 'GENERAL'];
  const typeLabel = { LOOP: 'OPEN LOOPS', USER: 'ABOUT RAGHAV', FEEDBACK: 'HOW TO WORK WITH HIM', PROJECT: 'PROJECTS', REFERENCE: 'REFERENCE', GENERAL: 'OTHER' };
  for (const t of typeOrder) {
    const mems = byType[t];
    if (!mems || !mems.length) continue;
    html += '<div class="memory-group-header">' + (typeLabel[t] || t) + ' (' + mems.length + ')</div>';
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
  const typeOrder = ['loop', 'user', 'feedback', 'project', 'reference', 'general'];
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
  const type = prompt('Type (loop/user/feedback/project/reference/general):', 'general');
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
  // showConfirm/showPrompt handle Enter and Escape in the capture phase.
  // Returning here also protects against a terminal retaining DOM focus.
  if (document.getElementById('modalBackdrop')?.classList.contains('visible')) return;
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
      for (const s of sessions) {
        if (!_isSerenaVoiceSession(s)) selectedIds.add(s.session_id);
      }
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
// Agent Filter (Claude / Codex)
// ═══════════════════════════════════════════════════════════════
let _agentFilter = null;  // null | 'claude' | 'codex'
function toggleAgentFilter(agent) {
  _agentFilter = (_agentFilter === agent) ? null : agent;
  const c = document.getElementById('filterClaude');
  const x = document.getElementById('filterCodex');
  if (c) c.classList.toggle('active', _agentFilter === 'claude');
  if (x) x.classList.toggle('active', _agentFilter === 'codex');
  renderSessionList();
}
function _initAgentFilterIcons() {
  const c = document.querySelector('#filterClaude .agent-icon');
  const x = document.querySelector('#filterCodex .agent-icon');
  if (c) c.innerHTML = _CLAUDE_SVG;
  if (x) x.innerHTML = _CODEX_SVG;
}

// ═══════════════════════════════════════════════════════════════
// Search Debounce
// ═══════════════════════════════════════════════════════════════
document.getElementById('searchInput').addEventListener('input', function(e) {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => searchSessions(e.target.value.trim()), 250);
});

// ═══════════════════════════════════════════════════════════════
// === FRONT DOOR FEATURE === Serena greets on launch, routes to panes.
// ═══════════════════════════════════════════════════════════════
const _fd = { history: [], busy: false, started: false };

function fdBubble(role, text) {
  const d = document.createElement('div');
  d.className = 'fd-msg ' + (role === 'user' ? 'fd-user' : 'fd-serena');
  d.textContent = text;
  document.getElementById('fdMessages').appendChild(d);
  d.scrollIntoView({ block: 'end' });
  // Hero shrinks once the conversation is real; panel ambience dims too
  document.getElementById('frontDoor').classList.add('fd-engaged');
  document.getElementById('convEmpty').classList.add('fd-engaged');
  return d;
}

function fdAdd(role, text) {
  _fd.history.push({ role, text });
  return fdBubble(role, text);
}

function fdReplyStarted() {
  const orb = document.getElementById('fdOrb');
  orb.classList.remove('fd-thinking');
  orb.classList.add('fd-responding');
  clearTimeout(_fd.respondTimer);
  _fd.respondTimer = setTimeout(() => orb.classList.remove('fd-responding'), 820);
}

async function fdTurn() {
  if (_fd.busy) return;
  _fd.busy = true;
  document.getElementById('fdOrb').classList.add('fd-thinking');
  const box = document.getElementById('fdMessages');
  const typing = document.createElement('div');
  typing.className = 'fd-msg fd-serena fd-typing';
  typing.innerHTML = '<i></i><i></i><i></i>';
  box.appendChild(typing);
  let reply = null;
  let replyText = '';
  let reader = null;
  try {
    const r = await fetch('/api/frontdoor', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/x-ndjson',
      },
      body: JSON.stringify({ history: _fd.history, stream: true }),
    });
    if (!r.ok) {
      const failure = await r.json().catch(() => ({}));
      throw new Error(failure.error || ('http ' + r.status));
    }
    if (!r.body) throw new Error('streaming response body is unavailable');

    reader = r.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';
    let done = null;
    let receivedBytes = 0;

    const handle = (event) => {
      if (!event || typeof event.type !== 'string') return;
      if (event.type === 'error') throw new Error(event.error || 'turn failed');
      if (event.type === 'delta') {
        if (typeof event.delta !== 'string' || !event.delta) return;
        if (!reply) {
          if (typing.isConnected) typing.remove();
          reply = fdBubble('serena', '');
          fdReplyStarted();
        }
        replyText += event.delta;
        reply.textContent = replyText;
        reply.scrollIntoView({ block: 'end' });
        return;
      }
      if (event.type === 'replace' && typeof event.say === 'string') {
        if (!reply) {
          if (typing.isConnected) typing.remove();
          reply = fdBubble('serena', '');
          fdReplyStarted();
        }
        replyText = event.say;
        reply.textContent = replyText;
        return;
      }
      if (event.type === 'done') done = event;
    };

    while (true) {
      const chunk = await reader.read();
      receivedBytes += chunk.value ? chunk.value.byteLength : 0;
      if (receivedBytes > 2 * 1024 * 1024) throw new Error('front door stream is too large');
      pending += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
      if (pending.length > 1024 * 1024) throw new Error('front door stream is too large');
      let newline;
      while ((newline = pending.indexOf('\n')) !== -1) {
        const line = pending.slice(0, newline).trim();
        pending = pending.slice(newline + 1);
        if (line) handle(JSON.parse(line));
      }
      if (chunk.done) break;
    }
    if (pending.trim()) handle(JSON.parse(pending));
    if (!done) throw new Error('front door ended before its final reply');

    const finalSay = typeof done.say === 'string' ? done.say : replyText;
    if (finalSay) {
      if (!reply) {
        if (typing.isConnected) typing.remove();
        reply = fdBubble('serena', finalSay);
        fdReplyStarted();
      } else if (replyText !== finalSay) {
        reply.textContent = finalSay;
      }
      _fd.history.push({ role: 'serena', text: finalSay });
    } else if (typing.isConnected) {
      typing.remove();
    }
    if (done.spawn) fdSpawn(done.spawn);
  } catch (e) {
    if (reader) await reader.cancel().catch(() => {});
    if (typing.isConnected) typing.remove();
    if (reply && reply.isConnected) reply.remove();
    fdBubble('serena', 'front door unreachable (' + e + '), open a chat manually.');
  } finally {
    if (reader) {
      try { reader.releaseLock(); } catch (_) {}
    }
    _fd.busy = false;
    document.getElementById('fdOrb').classList.remove('fd-thinking');
  }
}

const _fdPairResolved = {};

async function _fdLinkPair(sids, attempt) {
  // Link the two front-door-spawned sessions; retry with backoff instead of
  // giving up on one flaky request (a failed link left the pair permanently
  // unlinked since both pseudos are gone by then).
  try {
    const lr = await fetch('/api/group/link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_ids: sids }),
    });
    const ld = await lr.json().catch(() => ({}));
    if (lr.ok && ld.ok && ld.group_id) {
      _applyClientGroup(sids, ld.group_id);
      return;
    }
    throw new Error(ld.error || ('link http ' + lr.status));
  } catch (e) {
    if (attempt < 3) setTimeout(() => _fdLinkPair(sids, attempt + 1), 2000 * (attempt + 1));
    else console.warn('[fd] pair link failed after retries:', e);
  }
}

// Stamped onto every front-door seed: panes brief up and WAIT. Spawned
// agents were auto-starting work on spawn, which reads as the app going
// rogue; execution only begins when Raghav says go in that pane.
const FD_SEED_FOOTER = '\n\nBrief yourself on the above (read the relevant ' +
  'files/ledger, get oriented) but do NOT start changing anything or running ' +
  'the work yet. Reply with a 1-2 line ready summary of what you see and ' +
  'what you\'d do first, then wait for my go in this pane.';

function fdSpawn(spawn) {
  const agents = spawn.agents || ['claude'];
  const cwd = spawn.cwd || '';
  // Raw seed here; each spawn path composes per-pane so FD_SEED_FOOTER is
  // always the LAST instruction (codex's note used to land after it and
  // weaken the wait-for-go).
  const seed = spawn.seed || '';
  if (agents.includes('claude') && agents.includes('codex')) {
    fdSpawnBoth(cwd, seed);
  } else {
    fdSpawnPane(agents[0], cwd, seed + FD_SEED_FOOTER);
  }
}

function _fdPseudo(agent, cwd, label, pairId) {
  const tempId = 'new-' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  const iso = new Date().toISOString();
  return {
    session_id: tempId, display_title: label,
    project_short: cwd ? (cwd.split('/').filter(Boolean).pop() || '~') : '~',
    cwd, first_timestamp: iso, last_timestamp: iso, starred: false,
    input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_create_tokens: 0,
    isPseudo: true, agent, pending_rename_title: null, fd_pair_id: pairId || null,
    // Expiry so a pane that never materializes can't lurk and claim an
    // unrelated same-agent/same-cwd session opened minutes later.
    fd_expires: pairId ? Date.now() + 120000 : null,
  };
}

async function fdSpawnBoth(cwd, seed) {
  // Open claude and codex TOGETHER in one split, both seeded, instead of
  // spawning claude and waiting minutes for it to pull codex in itself.
  const shortProj = cwd ? (cwd.split('/').filter(Boolean).pop() || '~') : '~';
  const pairId = 'fdp-' + Math.random().toString(36).slice(2, 10);
  const claudeSeed = seed + FD_SEED_FOOTER;
  const codexSeed = seed + '\n\n(the claude pane next to you holds this same thread and drives ' +
      'execution; don\'t message it unprompted, just be briefed and ready for your ' +
      'share when Raghav splits the work.)' + FD_SEED_FOOTER;
  const pClaude = _fdPseudo('claude', cwd, 'Serena: ' + shortProj, pairId);
  const pCodex = _fdPseudo('codex', cwd, 'Serena: ' + shortProj, pairId);
  _pseudoSessions.unshift(pClaude, pCodex);
  setSessionSource([pClaude, pCodex, ...sessionSource]);
  _markActive(pClaude.session_id);
  _markActive(pCodex.session_id);
  currentSessionId = pClaude.session_id;
  focusedSid = pClaude.session_id;
  convMode = 'live';
  document.getElementById('viewReadBtn').classList.remove('active');
  document.getElementById('viewLiveBtn').classList.add('active');
  document.getElementById('convBody').classList.add('hidden');
  document.getElementById('convTerminal').classList.remove('hidden');
  document.getElementById('convEmpty').classList.add('hidden');
  document.getElementById('convContent').classList.remove('hidden');
  document.getElementById('convTitle').textContent = 'Serena: ' + shortProj;
  document.getElementById('convMeta').textContent = cwd || '~';
  setTermStatus('Starting claude + codex…', 'live');
  if (!window.__nativeTerminalBridge) {
    _pendingTermPartners.set(pClaude.session_id, pCodex.session_id);
    _pendingTermPartners.set(pCodex.session_id, pClaude.session_id);
    const codexRuntime = await startLiveTerminal(pCodex.session_id, {
      cwd, agent: 'codex', isNew: true, seed: codexSeed, background: true,
    });
    const claudeRuntime = await startLiveTerminal(pClaude.session_id, {
      cwd, agent: 'claude', isNew: true, seed: claudeSeed,
    });
    if (!codexRuntime || !claudeRuntime) {
      throw new Error('coding workspace terminal spawn failed');
    }
    _activateTermPane(pClaude.session_id);
    _startPseudoReconciler();
    _ensureActiveRefresh();
    return { ok: true, sids: [pClaude.session_id, pCodex.session_id], cwd };
  }
  const rect = await _prepareGtkTermMount();
  const meta = {};
  meta[pClaude.session_id] = { cwd, agent: 'claude', isNew: true, seed: claudeSeed };
  meta[pCodex.session_id] = { cwd, agent: 'codex', isNew: true, seed: codexSeed };
  window.gtkSend({
    type: 'code-split-on',
    sids: [pClaude.session_id, pCodex.session_id],
    spawn_meta: meta,
    rect,
    focus_sid: pClaude.session_id,
    ratio: 0.5,
  });
  _gtkSplitActive = true;
  _gtkSplitSids = [pClaude.session_id, pCodex.session_id];
  _gtkCodeSid = pClaude.session_id;
  _startPseudoReconciler();
  _ensureActiveRefresh();
  _wireGtkRectObs();
  return { ok: true, sids: [pClaude.session_id, pCodex.session_id], cwd };
}

async function fdSpawnPane(agent, cwd, seed, opts) {
  // newChatInline minus the naming modal — agent/cwd/seed come from the brain.
  opts = opts || {};
  const shortProj = cwd ? (cwd.split('/').filter(Boolean).pop() || '~') : '~';
  const tempId = opts.tempId ||
    ('new-' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36));
  const iso = new Date().toISOString();
  const label = opts.label || ('Serena: ' + shortProj);
  const pseudo = {
    session_id: tempId, display_title: label, project_short: shortProj, cwd,
    first_timestamp: iso, last_timestamp: iso, starred: false,
    input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_create_tokens: 0,
    isPseudo: true, agent, pending_rename_title: null,
  };
  _pseudoSessions.unshift(pseudo);
  setSessionSource([pseudo, ...sessionSource]);
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
  setTermStatus('Starting ' + agent + '…', 'live');
  if (window.__nativeTerminalBridge) {
    const rect = await _prepareGtkTermMount();
    window.gtkSend({ type: 'code-on', sid: tempId, cwd, rect, isNew: true, agent, seed });
    _gtkCodeSid = tempId;
  } else {
    const runtime = await startLiveTerminal(tempId, { cwd, agent, isNew: true, seed });
    if (!runtime) throw new Error('coding terminal spawn failed');
  }
  _startPseudoReconciler();
  return { ok: true, sid: tempId, cwd };
}

function fdGreetingText() {
  const h = new Date().getHours();
  if (h >= 5 && h < 12) return 'Good morning, Raghav';
  if (h >= 12 && h < 17) return 'Good afternoon, Raghav';
  if (h >= 17 && h < 22) return 'Good evening, Raghav';
  return 'Late night, Raghav';
}

function fdInit() {
  // v1 is desktop-shell only — seeds ride the GTK spawn argv.
  if (!window.__gtkBridge) return;
  document.getElementById('fdFallbackText').classList.add('hidden');
  const fdRoot = document.getElementById('frontDoor');
  const fdPanel = document.getElementById('convEmpty');
  fdPanel.classList.add('fd-on');
  fdRoot.classList.remove('hidden');
  // Instant local banner, no model call. Serena only speaks when spoken to.
  document.getElementById('fdGreeting').textContent = fdGreetingText();
  // Time-of-day ambience for the orb + banner hues
  const h = new Date().getHours();
  const hue = (h >= 5 && h < 12) ? 'fd-morning'
    : (h >= 17 && h < 22) ? 'fd-evening'
    : (h >= 22 || h < 5) ? 'fd-night' : '';
  if (hue) { fdRoot.classList.add(hue); fdPanel.classList.add(hue); }
  const inp = document.getElementById('fdInput');
  const send = document.getElementById('fdSend');
  inp.addEventListener('input', () => {
    send.classList.toggle('fd-ready', !!inp.value.trim());
  });
  inp.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || _fd.busy) return;
    const v = inp.value.trim();
    if (!v) return;
    fdAdd('user', v);
    inp.value = '';
    send.classList.remove('fd-ready');
    fdTurn();
  });
  inp.focus();
}
// === FRONT DOOR FEATURE END ===

// ═══════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════
updateShortcutBar();
fdInit();
_initAgentFilterIcons();
// Warm the default-cwd cache before any placeholder row can be created, so the
// reconciler and the /clear migration both resolve a cwd-less placeholder the
// same way the spawner does.
_loadDefaultCwd();
loadSessions();
loadProjects();
_startAttentionPoll();
loadCollapsedState();
startLiveUsagePoll();
setupLiveUsagePopover();
// Pre-fetch counts for tabs
fetch('/api/memory').then(r => r.json()).then(m => {
  memories = m;
  tasks = m.filter(x => x.type === 'task');
  document.getElementById('memoryCount').textContent = '(' + m.length + ')';
  const tc = document.getElementById('taskCount');
  if (tc) tc.textContent = tasks.length ? '(' + tasks.length + ')' : '';
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
      <div class="agent-picker" id="modalAgentPicker" style="display:none;"></div>
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
  if (_modalOpenCount === 1 && window.__nativeTerminalBridge) {
    window.gtkSend && window.gtkSend({ type: 'code-off' });
  }
}
function _modalRestoreTerminal() {
  _modalOpenCount = Math.max(0, _modalOpenCount - 1);
  if (_modalOpenCount === 0 && window.__nativeTerminalBridge && convMode === 'live' && currentSessionId) {
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
      if (e.key === 'Escape') {
        e.preventDefault(); e.stopImmediatePropagation(); close(false);
      } else if (e.key === 'Enter') {
        e.preventDefault(); e.stopImmediatePropagation(); close(true);
      }
    };
    const onOk = () => close(true);
    const onCancel = () => close(false);

    bd.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey, true);
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);

    _modalHideTerminal();
    bd.classList.add('visible');
    setTimeout(() => okBtn.focus({ preventScroll: true }), 10);
  });
}

function showPrompt({ title = 'Enter value', body = '', placeholder = '', defaultValue = '', confirm = 'OK', cancel = 'Cancel', agentPicker = false, defaultAgent = 'claude' } = {}) {
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

    // Agent picker (only when requested by caller)
    const picker = document.getElementById('modalAgentPicker');
    let chosenAgent = defaultAgent;
    if (agentPicker) {
      picker.innerHTML =
        '<button type="button" class="agent-pill' + (defaultAgent === 'claude' ? ' active' : '') + '" data-agent="claude">'
          + '<span class="agent-icon claude">' + _CLAUDE_SVG + '</span>Claude'
        + '</button>'
        + '<button type="button" class="agent-pill' + (defaultAgent === 'codex' ? ' active' : '') + '" data-agent="codex">'
          + '<span class="agent-icon codex">' + _CODEX_SVG + '</span>Codex'
        + '</button>';
      picker.style.display = '';
      const pills = picker.querySelectorAll('.agent-pill');
      pills.forEach(p => p.addEventListener('click', () => {
        chosenAgent = p.getAttribute('data-agent');
        pills.forEach(x => x.classList.remove('active'));
        p.classList.add('active');
        input.focus();
      }));
    } else {
      picker.style.display = 'none';
      picker.innerHTML = '';
    }

    const close = (result) => {
      bd.classList.remove('visible');
      bd.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey, true);
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      picker.style.display = 'none';
      picker.innerHTML = '';
      _modalRestoreTerminal();
      if (result === null) resolve(null);
      else if (agentPicker) resolve({ value: result, agent: chosenAgent });
      else resolve(result);
    };
    const onBackdrop = (e) => { if (e.target === bd) close(null); };
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault(); e.stopImmediatePropagation(); close(null);
      } else if (e.key === 'Enter') {
        e.preventDefault(); e.stopImmediatePropagation(); close(input.value);
      }
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
  if (!document.getElementById('toastStack')) {
    const stack = document.createElement('div');
    stack.id = 'toastStack';
    document.body.appendChild(stack);
  }
  if (!document.getElementById('usageAlertStack')) {
    const stack = document.createElement('div');
    stack.id = 'usageAlertStack';
    stack.setAttribute('aria-live', 'polite');
    document.body.appendChild(stack);
  }
})();

// ═══════════════════════════════════════════════════════════════
// PANE DIVIDERS — drag-to-resize the project / chats / conv / files
// columns. Sizes persist across launches via localStorage. Each
// divider sits between two panes; dragging shifts width onto the
// LEFT pane (the right-side pane absorbs the rest via flex:1).
// ═══════════════════════════════════════════════════════════════
(function setupPaneDividers() {
  const STORAGE_KEY = 'serena.paneSizes.v1';
  const DEFAULTS = { 'proj-w': '8%', 'chats-w': '20%', 'files-w': '9%' };
  const VAR_BY_DIVIDER = {
    'proj-chats':  '--proj-w',
    'chats-conv':  '--chats-w',
    'conv-files':  '--files-w',
  };
  const PANE_BY_DIVIDER = {
    'proj-chats':  '.project-sidebar',
    'chats-conv':  '.chat-list-col',
    'conv-files':  '.panel-files',  // dragging this divider resizes the RIGHT pane (files)
  };

  function loadSizes() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const obj = JSON.parse(raw);
      const root = document.documentElement;
      for (const [k, v] of Object.entries(obj)) {
        if (typeof v === 'string' && v.length) root.style.setProperty('--' + k, v);
      }
    } catch (e) { /* ignore */ }
  }

  function saveSize(varName, value) {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      const obj = raw ? JSON.parse(raw) : {};
      obj[varName.replace(/^--/, '')] = value;
      localStorage.setItem(STORAGE_KEY, JSON.stringify(obj));
    } catch (e) { /* ignore */ }
  }

  function attach(divider) {
    const kind = divider.getAttribute('data-divider');
    if (!kind) return;
    const varName = VAR_BY_DIVIDER[kind];
    const targetSel = PANE_BY_DIVIDER[kind];
    if (!varName || !targetSel) return;

    divider.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const target = document.querySelector(targetSel);
      if (!target) return;

      const main = document.getElementById('viewChats');
      const mainRect = main.getBoundingClientRect();
      const startRect = target.getBoundingClientRect();
      const startX = e.clientX;
      const isRightPane = (kind === 'conv-files');  // files-pane: drag LEFT to grow

      divider.classList.add('dragging');
      document.body.classList.add('pane-dragging');

      const minPx = parseInt(getComputedStyle(target).minWidth, 10) || 88;
      const maxPx = parseInt(getComputedStyle(target).maxWidth, 10) || mainRect.width * 0.65;

      const onMove = (ev) => {
        const dx = ev.clientX - startX;
        const newW = isRightPane ? (startRect.width - dx) : (startRect.width + dx);
        const clamped = Math.max(minPx, Math.min(maxPx, newW));
        document.documentElement.style.setProperty(varName, clamped + 'px');
        // Live-update the GTK terminal rect during drag
        if (window.__nativeTerminalBridge && typeof _gtkGetRect === 'function') {
          const r = _gtkGetRect();
          if (r) window.gtkSend({ type: 'code-rect', rect: r });
        }
      };
      const onUp = () => {
        divider.classList.remove('dragging');
        document.body.classList.remove('pane-dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        // Persist as pixel value
        const finalW = getComputedStyle(target).width;
        saveSize(varName, finalW);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    // Double-click resets the divider's pane to its default
    divider.addEventListener('dblclick', () => {
      const def = DEFAULTS[varName.replace(/^--/, '')];
      if (def) {
        document.documentElement.style.setProperty(varName, def);
        saveSize(varName, def);
        if (window.__nativeTerminalBridge && typeof _gtkGetRect === 'function') {
          requestAnimationFrame(() => {
            const r = _gtkGetRect();
            if (r) window.gtkSend({ type: 'code-rect', rect: r });
          });
        }
      }
    });
  }

  loadSizes();
  document.querySelectorAll('.pane-divider').forEach(attach);
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
<script src="/static/operator_workspace.js"></script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dev hot reload
#
# The whole UI is the HTML constant above, baked into this module at import.
# Editing it normally means restarting the sidecar, which kills every PTY and
# every live agent session with it, so a one-line CSS tweak used to cost the
# whole workspace (or a five-minute AppImage rebuild).
#
# In an unpackaged run the page is instead sliced out of this file on disk at
# request time. A reload of the window then shows the change with the Python
# process untouched: terminals stay attached and replay their buffer as usual.
# The frozen sidecar keeps using the in-memory constant, so shipped builds are
# byte-identical to before.
# ---------------------------------------------------------------------------

_HTML_OPEN = 'HTML = r"""'
_HTML_CLOSE = '</html>"""'
_html_disk_cache: dict[str, object] = {"mtime": None, "html": None}


# Where a packaged build looks for the live interface. The bundled copy is
# frozen inside the executable, so without this the installed app can never
# show an edit and every UI tweak costs a full rebuild. When the checkout is
# present on this machine, the installed app serves the page from it.
_UI_SOURCE_CANDIDATES = (
    Path.home() / "Documents" / "Projects" / "serena" / "ui" / "web.py",
    Path.home() / "Projects" / "serena" / "ui" / "web.py",
)


def _ui_source_path() -> Path | None:
    """The on-disk page source, or None when only the bundled copy exists."""

    configured = os.environ.get("SERENA_UI_SOURCE", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_file() else None
    if not getattr(sys, "frozen", False):
        here = Path(__file__).resolve()
        return here if here.is_file() else None
    for candidate in _UI_SOURCE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def ui_hot_reload_enabled() -> bool:
    override = os.environ.get("SERENA_UI_HOTRELOAD", "").strip().lower()
    if override in {"0", "false", "off"}:
        return False
    source = _ui_source_path()
    if source is None:
        # A build shipped to a machine with no checkout has nothing to watch.
        return False
    if override in {"1", "true", "on"}:
        return True
    return True


def ui_source_mtime() -> float:
    source = _ui_source_path()
    if source is None:
        return 0.0
    try:
        return source.stat().st_mtime
    except OSError:
        return 0.0


def _live_html() -> str:
    """Return the page markup, re-read from disk when hot reload is on.

    Any failure falls back to the imported constant. A dev convenience must
    never be able to serve a broken page.
    """

    if not ui_hot_reload_enabled():
        return HTML
    mtime = ui_source_mtime()
    if not mtime:
        return HTML
    if _html_disk_cache["mtime"] == mtime and _html_disk_cache["html"]:
        return str(_html_disk_cache["html"])
    path = _ui_source_path()
    if path is None:
        return HTML
    try:
        source = path.read_text(encoding="utf-8")
        start = source.index(_HTML_OPEN) + len(_HTML_OPEN)
        end = source.index(_HTML_CLOSE, start) + len(_HTML_CLOSE) - len('"""')
        html = source[start:end]
    except (OSError, ValueError):
        return HTML
    if "<!DOCTYPE html>" not in html[:64]:
        return HTML
    _html_disk_cache["mtime"] = mtime
    _html_disk_cache["html"] = html
    return html


@app.route("/api/ui-version")
def api_ui_version():
    """Source mtime, so a dev page can reload itself when the UI changes."""

    return jsonify(
        {
            "mtime": ui_source_mtime(),
            "hot_reload": ui_hot_reload_enabled(),
            # Opt-in: reloading the window unprompted interrupts whoever is
            # using the app, so it stays off unless explicitly asked for.
            "auto_reload": os.environ.get("SERENA_UI_AUTORELOAD", "").strip().lower()
            in {"1", "true", "on"},
        }
    )


_HOT_RELOAD_SCRIPT = """<script>
// Watch the UI source and OFFER a reload. It used to reload the window by
// itself, which meant that editing the interface yanked the app out from under
// whoever was using it, repeatedly, while an agent saved the file. The page
// only reloads when asked, or on the next save after the offer is dismissed
// if SERENA_UI_AUTORELOAD was set.
(function () {
  var known = null;
  var pill = null;

  function offer() {
    if (pill) return;
    pill = document.createElement('button');
    pill.type = 'button';
    pill.textContent = 'UI updated \u00b7 reload';
    pill.setAttribute('data-testid', 'ui-reload-pill');
    pill.style.cssText = [
      'position:fixed', 'bottom:14px', 'right:14px', 'z-index:99999',
      'padding:6px 12px', 'border-radius:999px', 'cursor:pointer',
      'font:11px var(--mono, monospace)', 'letter-spacing:0.4px',
      'color:#0c0c10', 'background:#c9a6ff', 'border:none',
      'box-shadow:0 2px 10px rgba(0,0,0,0.45)', 'opacity:0.92',
    ].join(';');
    pill.onclick = function () { location.reload(); };
    document.body.appendChild(pill);
  }

  setInterval(async function () {
    try {
      var r = await fetch('/api/ui-version', { cache: 'no-store' });
      if (!r.ok) return;
      var d = await r.json();
      if (!d.hot_reload) return;
      if (known === null) { known = d.mtime; return; }
      if (d.mtime === known) return;
      known = d.mtime;
      if (d.auto_reload) { location.reload(); return; }
      offer();
    } catch (e) {}
  }, 1500);
})();
</script>
"""


def _host_machine() -> dict[str, str]:
    """Name and OS of the machine serving this window."""

    try:
        from core.machine_context import describe

        facts = describe()
        return {"name": facts["machine"], "os": facts["os"]}
    except Exception:
        system = {"linux": "Linux", "win32": "Windows", "darwin": "macOS"}.get(
            sys.platform, sys.platform
        )
        return {"name": "", "os": system}


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
        + json.dumps(
            {
                "home": home,
                "homeSlug": home_slug,
                "platform": sys.platform,
                # Which box this window is actually running on. Agents already
                # get this through the SessionStart hook; the header shows the
                # same answer so a glance settles it too.
                "machine": _host_machine(),
            }
        )
        + ';</script>\n'
    )
    if ui_hot_reload_enabled():
        boot += _HOT_RELOAD_SCRIPT
    return boot + _live_html()


def _ambiguous_shorts() -> set[str]:
    """Return the set of shortened-project names that map to more than one project_dir.

    Used to decide when a session row needs a [lin]/[win] tag to disambiguate."""
    shorts: dict[str, set[str]] = {}
    for p in list_projects():
        s = _chip_short(p["project_dir"], p.get("cwd"))
        shorts.setdefault(s, set()).add(p["project_dir"])
    return {s for s, dirs in shorts.items() if len(dirs) > 1}


def _external_runtime_active(session_id: str) -> bool:
    try:
        from core import metadata as meta_sync

        return meta_sync.external_runtime_active(session_id)
    except Exception:
        return False


def _fleet_worker_marker(session_id: str) -> dict | None:
    try:
        from core import metadata as meta_sync

        marker = meta_sync.get_meta(session_id).get("fleet_worker")
    except Exception:
        return None
    if isinstance(marker, dict) and marker.get("run_id"):
        return marker
    return None


def _decorate_sessions(sessions: list[dict]) -> list[dict]:
    ambiguous = _ambiguous_shorts()
    # === GROUP FEATURE === (this block reads group ids from synced metadata)
    try:
        from core import metadata as meta_sync
        all_meta = meta_sync.get_all_meta()
    except Exception:
        all_meta = {}
    # === GROUP FEATURE END ===
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
        # === GROUP FEATURE === (per-row group id — frontend hashes it for color)
        sid = s.get("session_id")
        session_meta = (all_meta.get(sid) or {}) if sid else {}
        gid = session_meta.get("group")
        if gid:
            s["group"] = gid
        fleet_worker = session_meta.get("fleet_worker")
        if isinstance(fleet_worker, dict):
            s["fleet_worker"] = fleet_worker
        s["external_runtime_active"] = bool(
            sid and _external_runtime_active(sid)
        )
        # === GROUP FEATURE END ===
    return sessions


def _include_permanent_serena_session(sessions: list[dict]) -> list[dict]:
    """Keep the one lifelong Serena chat visible across project filters."""

    sid = "serena-voice-main"
    if any(session.get("session_id") == sid for session in sessions):
        return sessions
    serena = get_session(sid)
    if not serena:
        return sessions
    return [serena, *sessions]


_LIVE_USAGE_CACHE: dict = {"at": 0.0, "data": None}
_LIVE_USAGE_LOCK = threading.Lock()
_LIVE_USAGE_STALE_AFTER = 30.0
_CODEX_USAGE_READER = CodexUsageReader()
_PTY_ACTIVITY_READER = TurnActivityReader()


def _pct(value) -> float | None:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _epoch(value) -> int | None:
    try:
        n = int(float(value))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _normalize_usage_window(window: dict | None, now: float | None = None) -> dict:
    window = window if isinstance(window, dict) else {}
    now = time.time() if now is None else now
    reset_at = _epoch(window.get("resets_at"))
    used = _pct(window.get("used_percentage", window.get("used_percent")))
    expired = bool(reset_at and reset_at <= int(now))
    out = {
        "used_percentage": used,
        "resets_at": reset_at,
        "expired": expired,
    }
    observed_at = _epoch(window.get("observed_at"))
    if observed_at:
        out["observed_at"] = observed_at
    if isinstance(window.get("source"), str):
        out["source"] = window["source"]
    return out


def _normalize_usage_service(service: dict | None, now: float | None = None) -> dict:
    if not isinstance(service, dict):
        return {}
    out = dict(service)
    out["five_hour"] = _normalize_usage_window(out.get("five_hour"), now)
    out["seven_day"] = _normalize_usage_window(out.get("seven_day"), now)
    return out


def _mark_usage_freshness(service: dict, fallback_updated_at, now: float) -> dict:
    if not isinstance(service, dict):
        return service
    window_updates = [
        _epoch(window.get("observed_at"))
        for name in ("five_hour", "seven_day")
        if isinstance((window := service.get(name)), dict)
    ]
    updated_at = max((value for value in window_updates if value), default=None)
    updated_at = updated_at or _epoch(service.get("updated_at") or fallback_updated_at)
    if not updated_at:
        if service.get("available"):
            service["stale"] = True
        return service
    age = max(0.0, now - float(updated_at))
    service["updated_at"] = updated_at
    service["age_seconds"] = age
    service["stale"] = age > _LIVE_USAGE_STALE_AFTER
    return service


def _read_codex_model() -> str | None:
    cfg = Path.home() / ".codex" / "config.toml"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'^\s*model\s*=\s*"?([^"#\n]+)"?', text, re.M)
    return m.group(1).strip() if m else None


def _latest_codex_usage() -> dict:
    now = time.time()
    sessions_dir = Path.home() / ".codex" / "sessions"
    out = _CODEX_USAGE_READER.read(
        sessions_dir,
        model=_read_codex_model() or "codex",
        now=now,
    )
    out["five_hour"] = _normalize_usage_window(out.get("five_hour"), now)
    out["seven_day"] = _normalize_usage_window(out.get("seven_day"), now)
    return out


def _read_live_usage_state() -> dict:
    path = DATA_DIR / "live-usage.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _live_usage_payload() -> dict:
    now = time.time()
    if _LIVE_USAGE_CACHE["data"] is not None and now - _LIVE_USAGE_CACHE["at"] < 2.0:
        return _LIVE_USAGE_CACHE["data"]
    with _LIVE_USAGE_LOCK:
        now = time.time()
        if _LIVE_USAGE_CACHE["data"] is not None and now - _LIVE_USAGE_CACHE["at"] < 2.0:
            return _LIVE_USAGE_CACHE["data"]

        state = _read_live_usage_state()
        state_updated_at = state.get("updated_at")
        claude = _mark_usage_freshness(
            _normalize_usage_service(state.get("claude"), now),
            state_updated_at,
            now,
        )
        codex_state = _mark_usage_freshness(
            _normalize_usage_service(state.get("codex"), now),
            state_updated_at,
            now,
        )
        codex = _normalize_usage_service(_latest_codex_usage(), now)
        codex = _mark_usage_freshness(codex, codex.get("updated_at"), now)
        if not codex.get("available") and codex_state:
            codex = codex_state

        payload = {
            "ok": True,
            "updated_at": now,
            "state_updated_at": state_updated_at,
            "stale_after_seconds": _LIVE_USAGE_STALE_AFTER,
            "claude": claude or {"available": False, "source": "claude-statusline"},
            "codex": codex,
        }
        _LIVE_USAGE_CACHE["at"] = now
        _LIVE_USAGE_CACHE["data"] = payload
        return payload


@app.route("/api/live-usage")
def api_live_usage():
    return jsonify(_live_usage_payload())


@app.route("/api/sessions")
def api_sessions():
    # Opt-in disk rescan so the auto-poll can pick up new jsonl files claude
    # writes mid-session (e.g. after /clear). Cheap when nothing changed
    # (mtime/size diff only).
    if request.args.get("refresh"):
        try:
            _schedule_index_refresh()
        except Exception as e:
            print(f"[api_sessions] refresh failed: {e}", flush=True)
            traceback.print_exc()

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

    return jsonify(_decorate_sessions(_include_permanent_serena_session(sessions)))


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "")
    if not q:
        return jsonify([])
    q_lower = q.lower()
    fts_failed = False
    try:
        results = search_fts(q, limit=80)
    except Exception as e:
        print(f"[api_search] fts failed for {q!r}: {e}", flush=True)
        results = []
        fts_failed = True
    if not results and not fts_failed:
        build_fts()
        try:
            results = search_fts(q, limit=80)
        except Exception as e:
            print(f"[api_search] fts failed after rebuild for {q!r}: {e}", flush=True)
            results = []
    seen = set()
    snippets = {}
    sessions = []
    for r in results:
        sid = r["session_id"]
        if sid in seen:
            continue
        seen.add(sid)
        snip = r["snippet"] if "snippet" in r.keys() else None
        if snip:
            snippets[sid] = snip
        s = get_session(sid)
        if s:
            sessions.append(s)
    title_matches = []
    for s in list_sessions(limit=1000):
        haystack = " ".join(
            str(s.get(k) or "")
            for k in ("display_title", "custom_title", "title", "project_dir", "session_id")
        ).lower()
        if q_lower in haystack:
            title_matches.append(s)
    final_seen = set()
    merged = []
    for s in title_matches + sessions:
        sid = s.get("session_id")
        if sid in final_seen:
            continue
        final_seen.add(sid)
        merged.append(s)
    sessions = merged
    decorated = _decorate_sessions(sessions)
    for s in decorated:
        snip = snippets.get(s.get("session_id"))
        if snip:
            s["search_snippet"] = snip
    return jsonify(decorated)


def _current_device() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def _device_tag(device: str | None) -> str:
    if device == "windows":
        return "[win]"
    if device == "linux":
        return "[lin]"
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


@app.route("/api/default-cwd")
def api_default_cwd():
    """The directory a new chat actually starts in when none was chosen.

    The frontend used to record an empty cwd on a brand-new chat's placeholder
    row while the spawned terminal really started in $HOME. The reconciler
    matches a placeholder to its real session by cwd, so that mismatch left the
    placeholder unresolved forever and the chat rendered twice. The client asks
    for this value instead of guessing at a home path it cannot see.
    """

    return jsonify({"cwd": resolve_session_cwd("")})


@app.route("/api/keybindings")
def api_keybindings():
    """Expose the merged keybindings (defaults + user overrides) in a
    JS-friendly form so Windows/macOS pick up the same custom file as Linux."""
    from core.keybindings import load_for_js
    return jsonify(load_for_js())


# ─────────────────────────────────────────────────────────────────────────────
# === UI STATE === (sidebar collapse etc.)
# localStorage is keyed per ORIGIN, and the desktop shell picks a fresh port on
# every launch (desktop/app_gtk.py `_find_free_port`), so browser-local state is
# silently lost on every restart and never shared between the GTK window and a
# browser tab. Persist it server-side instead.
# ─────────────────────────────────────────────────────────────────────────────
_UI_STATE_PATH = Path.home() / ".config" / "serena" / "ui-state.json"


def _load_ui_state() -> dict:
    try:
        data = json.loads(_UI_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@app.route("/api/ui-state")
def api_ui_state_get():
    return jsonify(_load_ui_state())


@app.route("/api/ui-state", methods=["POST"])
def api_ui_state_set():
    """Merge-patch the persisted UI state. Keys are namespaced by the caller
    (e.g. "collapsed"); a null value deletes a key."""
    patch = request.get_json(silent=True) or {}
    if not isinstance(patch, dict):
        return jsonify({"ok": False, "error": "object required"}), 400
    state = _load_ui_state()
    for key, value in patch.items():
        if value is None:
            state.pop(key, None)
        else:
            state[str(key)] = value
    try:
        _UI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _UI_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, _UI_STATE_PATH)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "state": state})


# ─────────────────────────────────────────────────────────────────────────────
# === CODEX BRIDGE === (let claude drive the linked codex VTE in split view)
# ─────────────────────────────────────────────────────────────────────────────
def _local_runtime_request() -> bool:
    try:
        return ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        return False


def _native_runtime_context() -> dict | None:
    try:
        module = sys.modules.get("desktop.app_gtk")
        ChatsApp = getattr(module, "ChatsApp", None) if module is not None else None
        if ChatsApp is None:
            return None
        inst = ChatsApp.INSTANCE
        if inst is None or not getattr(inst, "_use_native_vte", True):
            return None
        return inst.runtime_context_snapshot()
    except (ImportError, AttributeError, RuntimeError):
        return None


def _decorate_runtime_entry(entry: dict) -> dict:
    result = dict(entry)
    sid = str(result.get("sid") or "")
    try:
        from core import metadata

        meta = metadata.get_meta(sid) if sid else {}
        result["group"] = meta.get("group") or None
        result["external"] = bool(
            sid and metadata.external_runtime_active(sid)
        )
        fleet = meta.get("fleet_worker")
        result["fleet"] = bool(
            isinstance(fleet, dict) and fleet.get("run_id")
        )
    except Exception:
        result["group"] = None
        result["external"] = False
        result["fleet"] = False
    for key in ("alive", "busy", "draft", "reserved"):
        result[key] = bool(result.get(key))
    return result


@app.route("/api/runtime-context")
def api_runtime_context():
    """Report exact in-app owners. This endpoint is never tailnet-exposed."""
    if not _local_runtime_request():
        return jsonify({"ok": False, "error": "runtime context is local-only"}), 403
    native = _native_runtime_context()
    browser = pty_terminal.runtime_context_snapshot()
    contexts = [context for context in (native, browser) if context]
    runtimes = [
        _decorate_runtime_entry(entry)
        for context in contexts
        for entry in context.get("runtimes", [])
    ]
    focused_sid = next(
        (context.get("focused_sid") for context in contexts if context.get("focused_sid")),
        None,
    )
    split_pair = next(
        (context.get("split_pair") for context in contexts if context.get("split_pair")),
        [],
    )
    focused_at = max(
        (float(context.get("focused_at") or 0.0) for context in contexts),
        default=0.0,
    )
    window_active = any(bool(context.get("window_active")) for context in contexts)
    try:
        port = int(request.environ.get("SERVER_PORT") or request.host.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        port = None
    return jsonify(
        {
            "ok": True,
            "focused_sid": focused_sid,
            "focused_session_id": focused_sid,
            "split_pair": split_pair,
            "split_session_ids": split_pair,
            "runtimes": runtimes,
            "sessions": runtimes,
            "port": port,
            "bridge_port": port,
            "focused_at": focused_at,
            "window_active": window_active,
        }
    )


@app.route("/api/codex-bridge", methods=["POST"])
def api_codex_bridge():
    from core.codex_bridge import call_codex_via_bridge
    data = request.get_json(silent=True) or {}
    target_sid = (data.get("target_sid") or "").strip()
    prompt = data.get("prompt") or ""
    timeout = float(data.get("timeout") or 300.0)
    if not target_sid or not prompt:
        return jsonify({"ok": False, "message": "target_sid and prompt are required"}), 400
    result = call_codex_via_bridge(target_sid, prompt, timeout=timeout)
    return jsonify(result)


@app.route("/api/codex-work-bridge", methods=["POST"])
def api_codex_work_bridge():
    if not _local_runtime_request():
        return jsonify({"ok": False, "message": "work bridge is local-only"}), 403
    from core.codex_bridge import call_codex_work_via_bridge

    data = request.get_json(silent=True) or {}
    target_sid = str(data.get("target_sid") or "").strip()
    prompt = str(data.get("prompt") or "")
    item_id = str(data.get("item_id") or "").strip()
    try:
        timeout = min(3600.0, max(1.0, float(data.get("timeout") or 300.0)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "timeout must be numeric"}), 400
    if not target_sid or not prompt.strip() or not item_id:
        return jsonify(
            {
                "ok": False,
                "message": "target_sid, prompt, and item_id are required",
            }
        ), 400
    return jsonify(
        call_codex_work_via_bridge(
            target_sid, prompt, item_id, timeout=timeout
        )
    )


@app.route("/api/codex-work-interrupt", methods=["POST"])
def api_codex_work_interrupt():
    if not _local_runtime_request():
        return jsonify({"ok": False, "message": "work interrupt is local-only"}), 403
    from core.codex_bridge import interrupt_codex_work

    data = request.get_json(silent=True) or {}
    target_sid = str(data.get("target_sid") or "").strip()
    item_id = str(data.get("item_id") or "").strip()
    if not target_sid or not item_id:
        return jsonify(
            {"ok": False, "message": "target_sid and item_id are required"}
        ), 400
    result = interrupt_codex_work(target_sid, item_id)
    return jsonify(result), 200 if result.get("ok") else 409


@app.route("/api/claude-bridge", methods=["POST"])
def api_claude_bridge():
    """Mirror of /api/codex-bridge but drives a claude VTE instead. Used by
    `chats ask-claude` so codex (or any caller) can feed a prompt into a
    linked claude session and get its reply back."""
    from core.claude_bridge import call_claude_via_bridge
    data = request.get_json(silent=True) or {}
    target_sid = (data.get("target_sid") or "").strip()
    prompt = data.get("prompt") or ""
    timeout = float(data.get("timeout") or 300.0)
    if not target_sid or not prompt:
        return jsonify({"ok": False, "message": "target_sid and prompt are required"}), 400
    result = call_claude_via_bridge(target_sid, prompt, timeout=timeout)
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# === MOBILE CHAT WS === (the Serena phone app connects here; protocol lives in
# mobile/src/types.ts, handlers in core/chat_daemon.py. Token rides the query
# string because browsers can't set headers on a WebSocket handshake.)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/app")
@app.route("/app/")
@app.route("/app/<path:subpath>")
def serve_mobile_app(subpath: str = ""):
    """Serve the bundled phone/desktop client (mobile/dist) so the laptop can
    run it same-origin over Tailscale. index.html gets the same-origin websocket
    URL. The bearer token stays in the client's saved settings and is never
    disclosed by this unauthenticated static route. Assets are served as-is."""
    from flask import Response, send_file

    dist = (Path(__file__).resolve().parent.parent / "mobile" / "dist").resolve()
    # static asset (js/css/svg/png) — serve directly
    if subpath and subpath != "index.html":
        target = (dist / subpath).resolve()
        if target.is_relative_to(dist) and target.is_file():
            return send_file(str(target))
    # otherwise serve index.html with the boot config injected
    try:
        html = (dist / "index.html").read_text(encoding="utf-8")
    except OSError:
        return Response("mobile app not built (mobile/dist missing)", status=404)
    boot = (
        "<script>window.SERENA_BOOT={url:location.origin.replace(/^http/,'ws')"
        "+'/ws/chat'};</script>"
    )
    # assets reference './assets/...' (relative) — rewrite to '/app/assets/...'
    html = html.replace('="./assets/', '="/app/assets/').replace('="./favicon', '="/app/favicon')
    html = html.replace("</head>", boot + "</head>")
    return Response(html, mimetype="text/html")


@sock.route("/ws/chat")
def ws_chat(ws):
    from core import chat_daemon

    expected = chat_daemon.get_or_create_token()
    authorization = request.headers.get("Authorization", "")
    provided = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else request.args.get("token", "")
    )
    if not provided or provided != expected:
        try:
            ws.send(json.dumps({"type": "error", "message": "unauthorized"}))
        except Exception:
            pass
        return

    def emit(obj):
        ws.send(json.dumps(obj))

    while True:
        try:
            raw = ws.receive()
        except Exception:
            break
        if raw is None:
            break
        try:
            chat_daemon.handle_client_message(raw, emit)
        except Exception as e:
            try:
                emit({"type": "error", "message": str(e)})
            except Exception:
                break
# === MOBILE CHAT WS END ===


# ─────────────────────────────────────────────────────────────────────────────
# === CALL AUDIO WS === (auth stays here; the protocol and pipeline live in
# voice.call so the web surface remains a thin transport adapter.)
# ─────────────────────────────────────────────────────────────────────────────
_CALL_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _CALL_REPO_ROOT not in sys.path:
    sys.path.insert(0, _CALL_REPO_ROOT)

# The call stack is optional weight: it drags in the audio pipeline and numpy.
# Importing it unguarded meant one missing dependency killed the whole sidecar
# on startup, so the desktop app showed a Python traceback dialog and no window
# at all, over a feature the user had not even asked for yet. Chats, memory and
# terminals do not need audio, so a failure here disables calling and nothing
# else.
CALL_STACK_ERROR: str | None = None
try:
    from voice.call import handle_websocket, warm_default_runtime_background
    from voice.call.orchestrator import get_desk_runtime, warm_desk_runtime_background
    from voice.call.browser_auth import (
        CALL_SOCKET_COOKIE,
        CALL_SOCKET_COOKIE_PATH,
        CALL_SOCKET_TICKET_TTL_SECONDS,
        browser_call_tickets,
    )
except Exception as _call_import_error:  # noqa: BLE001 - any failure disables calling
    CALL_STACK_ERROR = f"{type(_call_import_error).__name__}: {_call_import_error}"
    print(f"[serena] voice calling is unavailable ({CALL_STACK_ERROR})", file=sys.stderr)

    CALL_SOCKET_COOKIE = "serena_call_ticket"
    CALL_SOCKET_COOKIE_PATH = "/"
    CALL_SOCKET_TICKET_TTL_SECONDS = 0

    def _call_unavailable(*_args, **_kwargs):
        raise RuntimeError(f"voice calling is unavailable: {CALL_STACK_ERROR}")

    handle_websocket = _call_unavailable
    get_desk_runtime = _call_unavailable
    warm_desk_runtime_background = lambda *_a, **_k: None  # noqa: E731
    warm_default_runtime_background = lambda *_a, **_k: None  # noqa: E731

    class _NoCallTickets:
        def issue(self) -> str:
            raise RuntimeError(f"voice calling is unavailable: {CALL_STACK_ERROR}")

        def consume(self, _ticket: str) -> bool:
            return False

    browser_call_tickets = _NoCallTickets()

# SERENA_CALL_RUNTIME=lazy (set by the desktop shell) skips the eager model
# warm; mobile_host on :8767 already holds the warm stack for wake/phone calls.
# A call started in-app still works: the pool builds workers on first request.
_LAZY_CALL_RUNTIME = os.environ.get("SERENA_CALL_RUNTIME", "").lower() == "lazy"
if not _LAZY_CALL_RUNTIME:
    warm_default_runtime_background()
_DESK_GREETING_POOL = None
_DESK_GREETING_POOL_LOCK = threading.Lock()


def _get_desk_greeting_pool():
    global _DESK_GREETING_POOL
    if _DESK_GREETING_POOL is not None:
        return _DESK_GREETING_POOL
    with _DESK_GREETING_POOL_LOCK:
        if _DESK_GREETING_POOL is None:
            from voice.call.tts import create_tts_backend
            from voice.desk.greetings import DeskGreetingPool

            _DESK_GREETING_POOL = DeskGreetingPool(
                get_desk_runtime(),
                tts_factory=create_tts_backend,
            )
    return _DESK_GREETING_POOL


def _call_websocket_is_authorized(expected: str) -> bool:
    import hmac

    authorization = request.headers.get("Authorization", "")
    provided = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else ""
    )
    if provided and hmac.compare_digest(provided, expected):
        return True
    return browser_call_tickets.consume(request.cookies.get(CALL_SOCKET_COOKIE, ""))


@app.post("/api/call/socket-auth")
def issue_call_websocket_authorization():
    """Exchange a bearer token for one same-origin browser socket handshake."""
    import hmac

    from core import chat_daemon

    expected = chat_daemon.get_or_create_token()
    authorization = request.headers.get("Authorization", "")
    provided = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else ""
    )
    if not provided or not hmac.compare_digest(provided, expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    response = jsonify({"ok": True, "expires_in": CALL_SOCKET_TICKET_TTL_SECONDS})
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        CALL_SOCKET_COOKIE,
        browser_call_tickets.issue(),
        max_age=CALL_SOCKET_TICKET_TTL_SECONDS,
        secure=True,
        httponly=True,
        samesite="Strict",
        path=CALL_SOCKET_COOKIE_PATH,
    )
    return response


def _serve_call_websocket(ws, *, desk: bool = False) -> None:
    from core import chat_daemon

    expected = chat_daemon.get_or_create_token()
    if not _call_websocket_is_authorized(expected):
        try:
            ws.send(json.dumps({
                "type": "error",
                "code": "unauthorized",
                "message": "unauthorized",
                "fatal": True,
            }))
        except Exception:
            pass
        return

    if desk:
        handle_websocket(
            ws,
            runtime=get_desk_runtime(),
            peer_host=request.remote_addr,
        )
    else:
        handle_websocket(ws, peer_host=request.remote_addr)


@sock.route("/ws/call")
def ws_call(ws):
    _serve_call_websocket(ws)


@sock.route("/ws/desk")
def ws_desk(ws):
    _serve_call_websocket(ws, desk=True)


@app.get("/desk/greeting")
def serve_desk_greeting():
    import hmac

    from core import chat_daemon

    expected = chat_daemon.get_or_create_token()
    authorization = request.headers.get("Authorization", "")
    provided = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else ""
    )
    if not provided or not hmac.compare_digest(provided, expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    greeting = _get_desk_greeting_pool().take()
    if greeting is None:
        return (
            jsonify({"ok": False, "error": "desk greeting cache is warming"}),
            503,
            {"Retry-After": "1"},
        )
    return Response(
        greeting.pcm,
        status=200,
        mimetype="application/vnd.serena.pcm16",
        headers={
            "X-Serena-Greeting-Id": greeting.greeting_id,
            "X-Serena-Sample-Rate": str(greeting.sample_rate),
            "X-Serena-Greeting-Daypart": greeting.daypart,
            "Cache-Control": "no-store",
        },
    )


@app.get("/artifacts/<token>")
def serve_call_artifact(token: str):
    from io import BytesIO

    from flask import abort, send_file

    from core.artifacts import (
        ArtifactReceiptCapacityError,
        artifact_client_allowed,
        get_default_artifact_registry,
    )

    if not artifact_client_allowed(request.remote_addr):
        abort(404)
    registry = get_default_artifact_registry()
    payload = registry.read(token)
    if payload is None:
        abort(404)
    response = send_file(
        BytesIO(payload.data),
        mimetype=payload.link.content_type,
        download_name=payload.link.name,
        as_attachment=False,
        conditional=False,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    try:
        response.headers["X-Serena-Artifact-Receipt"] = registry.issue_receipt(
            payload.link
        )
    except ArtifactReceiptCapacityError:
        return jsonify(
            {"ok": False, "error": "artifact receipt capacity reached"}
        ), 503, {"Retry-After": "1"}
    return response
# === CALL AUDIO WS END ===


# ─────────────────────────────────────────────────────────────────────────────
# === MCP MULTIPLEXER === (shared subprocess fan-out so N claudes don't spawn
# N×M MCPs. The actual proxy lives on its own fixed port; these endpoints just
# expose status to Serena's UI.)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/multiplex-status")
def api_multiplex_status():
    from core.mcp.multiplex import list_proxies
    return jsonify({"servers": list_proxies()})
# === MCP MULTIPLEXER END ===


# ─────────────────────────────────────────────────────────────────────────────
# === ATTENTION === (mark/clear/list chats that finished a turn and want eyes)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/chat-attention", methods=["GET"])
def api_chat_attention():
    from core import chat_attention
    return jsonify({"sessions": chat_attention.list_active()})


@app.route("/api/chat-finished", methods=["POST"])
def api_chat_finished():
    """Called by claude's Stop hook (via `chats mark-done`) and codex's
    rollout watcher when a turn completes."""
    from core import chat_attention
    data = request.get_json(silent=True) or {}
    sid = (data.get("session_id") or "").strip()
    if not sid:
        return jsonify({"ok": False, "message": "session_id required"}), 400
    chat_attention.mark(sid)
    try:
        from core.locket_archive_sync import queue_archive_sync

        queue_archive_sync()
    except Exception as error:
        print(f"[archive-sync] queue failed: {error}", flush=True)
    return jsonify({"ok": True})


@app.route("/api/chat-attention/clear", methods=["POST"])
def api_chat_attention_clear():
    from core import chat_attention
    data = request.get_json(silent=True) or {}
    sid = (data.get("session_id") or "").strip()
    if not sid:
        return jsonify({"ok": False, "message": "session_id required"}), 400
    chat_attention.clear(sid)
    return jsonify({"ok": True})
# === ATTENTION END ===


@app.route("/api/frontdoor", methods=["POST"])
def api_frontdoor():
    """One Serena front-door turn. Empty/absent history = greeting turn
    (Raghav just opened the app). Returns {ok, say, spawn, error} where
    spawn is null or {agents, cwd, seed} for the JS to open pane(s) with."""
    try:
        local = ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        local = False
    if not local:
        return jsonify({"ok": False, "say": "", "spawn": None,
                        "error": "front door is local-only"}), 403
    data = request.get_json(silent=True) or {}
    history = data.get("history") or []
    if not isinstance(history, list):
        return jsonify({"ok": False, "say": "", "spawn": None,
                        "error": "history must be a list"}), 400
    if len(history) > 64:
        return jsonify({"ok": False, "say": "", "spawn": None,
                        "error": "history is too long"}), 413
    clean_history = []
    history_chars = 0
    for message in history:
        if not isinstance(message, dict):
            return jsonify({"ok": False, "say": "", "spawn": None,
                            "error": "history entries must be objects"}), 400
        role = message.get("role")
        text = message.get("text")
        if role not in ("user", "serena") or not isinstance(text, str):
            return jsonify({"ok": False, "say": "", "spawn": None,
                            "error": "history entry is invalid"}), 400
        history_chars += len(text)
        if len(text) > 32768 or history_chars > 262144:
            return jsonify({"ok": False, "say": "", "spawn": None,
                            "error": "history text is too large"}), 413
        clean_history.append({"role": role, "text": text})
    history = clean_history
    if data.get("stream") is True:
        from core.frontdoor import stream_turn

        def generate():
            for event in stream_turn(history):
                yield json.dumps(event, separators=(",", ":")) + "\n"

        return Response(
            stream_with_context(generate()),
            mimetype="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
    from core.frontdoor import turn
    return jsonify(turn(history))


@app.route("/api/spawn-linked-codex", methods=["POST"])
def api_spawn_linked_codex():
    """Spawn (only) — does NOT feed a prompt. Used internally; most callers
    should hit /api/ask-linked-codex instead so spawn+feed+wait are atomic."""
    from core.codex_spawn import spawn_linked_codex
    data = request.get_json(silent=True) or {}
    claude_sid = (data.get("claude_sid") or "").strip()
    claude_cwd = (data.get("claude_cwd") or "").strip()
    timeout = float(data.get("timeout") or 30.0)
    if not claude_sid:
        return jsonify({"ok": False, "message": "claude_sid required"}), 400
    result = spawn_linked_codex(claude_sid, claude_cwd, timeout=timeout)
    return jsonify(result)


@app.route("/api/ask-linked-codex", methods=["POST"])
def api_ask_linked_codex():
    """End-to-end: ensure claude has a linked codex (spawn one in split view
    if not), feed the prompt, return codex's response. Handles both the
    already-linked case and the auto-spawn case in one round-trip — the CLI
    doesn't have to coordinate spawn + feed."""
    from core.codex_spawn import ask_linked_codex
    data = request.get_json(silent=True) or {}
    claude_sid = (data.get("claude_sid") or "").strip()
    claude_cwd = (data.get("claude_cwd") or "").strip()
    prompt = data.get("prompt") or ""
    response_timeout = float(data.get("timeout") or 300.0)
    if not claude_sid or not prompt.strip():
        return jsonify({"ok": False, "message": "claude_sid and prompt required"}), 400
    result = ask_linked_codex(claude_sid, prompt, claude_cwd, response_timeout=response_timeout)
    return jsonify(result)


@app.route("/api/ask-linked-claude", methods=["POST"])
def api_ask_linked_claude():
    """Mirror of /api/ask-linked-codex for the reverse direction: ensure a
    codex chat has a linked claude sibling (spawn one in split view if not),
    feed the prompt, return claude's response. Backs `chats ask-claude`
    auto-spawn, codex-initiated."""
    from core.claude_spawn import ask_linked_claude
    data = request.get_json(silent=True) or {}
    codex_sid = (data.get("codex_sid") or "").strip()
    codex_cwd = (data.get("codex_cwd") or "").strip()
    prompt = data.get("prompt") or ""
    response_timeout = float(data.get("timeout") or 300.0)
    if not codex_sid or not prompt.strip():
        return jsonify({"ok": False, "message": "codex_sid and prompt required"}), 400
    result = ask_linked_claude(codex_sid, prompt, codex_cwd, response_timeout=response_timeout)
    return jsonify(result)
# === CODEX BRIDGE END ===


@app.route("/api/conversation/<session_id>")
def api_conversation(session_id):
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "Not found"}), 404

    # Reconcile any slug-copies (laptop/PC/renamed-dir duplicates) so the view
    # shows the full union regardless of which slug this device wrote.
    file_path = Path(session["file_path"])
    try:
        from core import chat_daemon
        canon = chat_daemon._canonicalize_session(session_id)
        if canon:
            file_path = canon
    except Exception:
        pass

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
        "agent": session.get("agent") or "claude",
        "title": session.get("display_title", "Untitled"),
        "date": (session.get("first_timestamp") or "")[:16].replace("T", " "),
        "cwd": cwd,
        "input_tokens": session.get("input_tokens") or 0,
        "output_tokens": session.get("output_tokens") or 0,
        "cache_read_tokens": session.get("cache_read_tokens") or 0,
        "cache_create_tokens": session.get("cache_create_tokens") or 0,
        "external_runtime_active": _external_runtime_active(session["session_id"]),
        "messages": messages,
    })


def _localize_path(raw: str) -> str:
    """Translate a foreign-OS path to this machine so clicking a path in a
    Windows-origin chat works on Linux and vice-versa. Returns raw if it
    already exists or can't be translated."""
    if Path(raw).exists():
        return raw
    norm = raw.replace("\\", "/")
    home = str(Path.home())
    # C:/Users/<x>/rest  or  /home/<x>/rest  ->  <local home>/rest
    m = re.match(r"^(?:[A-Za-z]:)?/(?:Users|home)/[^/]+(?:/(.*))?$", norm)
    if m:
        rest = m.group(1) or ""
        cand = str(Path(home) / rest) if rest else home
        if Path(cand).exists():
            return cand
    return raw


@app.route("/api/open-path", methods=["POST"])
def api_open_path():
    """Open a file path from the conversation. reveal=True shows it in the file
    manager (selecting it); reveal=False opens the file with its default app.
    Local-only convenience for the desktop UI."""
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip().strip("'\"")
    reveal = bool(data.get("reveal"))
    if not raw:
        return jsonify({"ok": False, "error": "no path"}), 400

    target = _localize_path(raw)
    p = Path(target)
    if not p.exists():
        return jsonify({"ok": False, "error": f"not found on this machine: {target}"}), 404

    try:
        if sys.platform == "win32":
            if reveal:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(str(p))])
            else:
                os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if reveal else "", str(p)] if reveal else ["open", str(p)])
        else:  # linux
            if reveal:
                # Select the item in the file manager via the FileManager1 D-Bus
                # API; fall back to just opening the containing directory.
                uri = p.resolve().as_uri()
                try:
                    subprocess.Popen([
                        "dbus-send", "--session", "--print-reply",
                        "--dest=org.freedesktop.FileManager1",
                        "/org/freedesktop/FileManager1",
                        "org.freedesktop.FileManager1.ShowItems",
                        f"array:string:{uri}", "string:",
                    ])
                except Exception:
                    subprocess.Popen(["xdg-open", str(p.parent)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        return jsonify({"ok": True, "path": str(p), "reveal": reveal})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
    session = get_session(session_id)
    if _is_serena_voice_session(session):
        return jsonify({"error": "Serena's permanent conversation cannot be deleted"}), 403
    if _fleet_worker_marker(session_id):
        return jsonify({"error": "Fleet worker chats are durable run history and cannot be deleted"}), 409
    try:
        path = delete_session(session_id, source="serena-web")
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
        if _is_serena_voice_session(get_session(sid)):
            errors.append({"id": sid, "error": "Serena's permanent conversation cannot be deleted"})
            continue
        if _fleet_worker_marker(sid):
            errors.append({"id": sid, "error": "Fleet worker chats are durable run history and cannot be deleted"})
            continue
        try:
            delete_session(sid, source="serena-web-bulk")
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
    if _is_serena_voice_session(session):
        return jsonify({"error": "Serena is a read-only conversation"}), 409
    if _fleet_worker_marker(session["session_id"]):
        return jsonify({"error": "Fleet worker chats are read-only"}), 409

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


_READFILE_MAX = 2 * 1024 * 1024  # 2 MB — refuse to slurp huge files into the viewer


@app.route("/api/read-file")
def api_read_file():
    """Read a file's text for the in-app viewer. Cross-OS path translation +
    size cap + binary detection. Local-only (reads the host filesystem)."""
    raw = (request.args.get("path") or "").strip().strip("'\"")
    if not raw:
        return jsonify({"error": "no path"}), 400
    target = _localize_path(raw)
    p = Path(target)
    if not p.exists():
        return jsonify({"error": f"not found on this machine: {target}"}), 404
    if not p.is_file():
        return jsonify({"error": "not a file"}), 400
    try:
        size = p.stat().st_size
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    if size > _READFILE_MAX:
        return jsonify({"error": f"file too large to view ({size // 1024} KB)",
                        "name": p.name, "path": str(p), "size": size}), 413
    raw_bytes = p.read_bytes()
    if b"\x00" in raw_bytes[:8192]:
        return jsonify({"error": "binary file", "name": p.name,
                        "path": str(p), "size": size, "binary": True}), 415
    text = raw_bytes.decode("utf-8", errors="replace")
    return jsonify({
        "name": p.name,
        "path": str(p),
        "size": size,
        "lines": text.count("\n") + 1,
        "lang": p.suffix.lstrip(".").lower(),
        "content": text,
    })


# ---------------------------------------------------------------------------
# Inline terminal spawn
# ---------------------------------------------------------------------------

@app.route("/api/persona-files", methods=["GET"])
def api_persona_files_get():
    """Return Persona.md + Tooling.md + VoiceReminder.txt for the in-app editor."""
    from core.config import read_persona, read_tooling, read_voice_reminder
    return jsonify({
        "persona": read_persona(),
        "tooling": read_tooling(),
        "voice": read_voice_reminder(),
    })


@app.route("/api/persona-files", methods=["POST"])
def api_persona_files_post():
    """Save Persona.md / Tooling.md / VoiceReminder.txt from the in-app editor."""
    from core.config import PERSONA_PATH, TOOLING_PATH, VOICE_REMINDER_PATH
    data = request.get_json(silent=True) or {}
    which = (data.get("file") or "").strip()
    content = data.get("content")
    targets = {"persona": PERSONA_PATH, "tooling": TOOLING_PATH, "voice": VOICE_REMINDER_PATH}
    if which not in targets:
        return jsonify({"ok": False, "error": "file must be persona, tooling, or voice"}), 400
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "content required"}), 400
    target = targets[which]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "path": str(target)})


def _persona_args() -> list[str]:
    """--append-system-prompt with Persona.md + Tooling.md so every claude
    spawn carries the Serena persona + operational reference, independent of
    the SessionStart hook."""
    try:
        from core.config import read_agent_context
        ctx = read_agent_context()
        if ctx.strip():
            return ["--append-system-prompt", ctx]
    except Exception:
        pass
    return []


def _ensure_resumable(sid: str, cwd: str) -> None:
    """Guarantee `claude -r <sid>` can find the session from `cwd`. claude maps
    cwd -> projects/<slug>/ and looks for <sid>.jsonl there. On the headless
    container the session's real cwd doesn't exist (resolves to /root etc.), so
    the slug won't have the file. We first reconcile all existing slug-copies to
    their union, then stage a current copy under the cwd's slug. No-op on a
    machine where the file already lives under that slug (e.g. the PC)."""
    try:
        import re
        import shutil
        from core import chat_daemon as cd

        cd._canonicalize_session(sid)  # bring every existing copy to the union
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
        target = cd._projects_root() / slug / f"{sid}.jsonl"
        copies = [p for p in cd._slug_copies(sid) if p.exists()]
        if not copies:
            return
        src = max(copies, key=lambda p: p.stat().st_size)
        if src == target:
            return
        # Presence is not enough. A copy left empty or short by an interrupted
        # rewrite still satisfies exists(), and claude then resumes against
        # nothing and exits on the spot. Re-stage unless this copy is already
        # at least as complete as the best one we have.
        if target.exists() and target.stat().st_size >= src.stat().st_size:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    except Exception:
        pass


@app.route("/api/spawn-terminal", methods=["POST"])
def api_spawn_terminal():
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    client_session_id = (data.get("client_session_id") or "").strip()
    agent = (data.get("agent") or "claude").lower()
    cols = int(data.get("cols") or 100)
    rows = int(data.get("rows") or 30)
    seed = str(data.get("seed") or "")
    runtime_sid = client_session_id or None

    if session_id:
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        if _is_serena_voice_session(session):
            return jsonify({"error": "Serena is a read-only conversation"}), 409
        if _fleet_worker_marker(session["session_id"]):
            return jsonify({"error": "Fleet worker chats are read-only"}), 409
        if _external_runtime_active(session["session_id"]):
            return jsonify({
                "ok": False,
                "error": "Workflow agent is still running. Open its Read view until it finishes.",
                "external_runtime": True,
            }), 409
        cwd = resolve_session_cwd(_get_session_cwd(session))
        sid = session["session_id"]
        runtime_sid = sid
        ensure_session_visible(sid, session.get("project_dir", ""), cwd)
        # Resume the right agent based on stored agent value
        agent = (session.get("agent") or "claude").lower()
        if agent == "codex":
            argv = ["codex", "resume", sid]
        else:
            # claude -r is cwd-scoped: it looks for the session under
            # projects/<slug-of-cwd>/. On a headless/cross-OS daemon the cwd
            # won't match where the file lives, so stage a current copy there.
            _ensure_resumable(sid, cwd)
            argv = ["claude", "--dangerously-skip-permissions", "-r", sid]
            argv += _persona_args()
    else:
        raw_cwd = (data.get("cwd") or "").strip()
        cwd = resolve_session_cwd(raw_cwd)
        if agent == "codex":
            argv = ["codex"]  # fresh codex session
            if seed:
                argv.append(seed)
        else:
            argv = ["claude", "--dangerously-skip-permissions"]
            argv += _persona_args()
            if seed:
                argv.append(seed)

    # === MODEL MASK === (mirror of app_gtk: show the assigned Sol, Terra, or
    # Luna model on matching Codex-relay /workflows rows. DEFAULT OFF — the relay
    # adds keystroke latency and Fleet's panel now carries real model identity, so
    # it's obsolete. Strict opt-in: SERENA_MODEL_MASK=on to re-enable.)
    if agent != "codex" and os.environ.get("SERENA_MODEL_MASK", "off").lower() in ("on", "1", "true"):
        _mask = str(Path(__file__).resolve().parent / "pty_model_mask.py")
        if os.path.exists(_mask):
            argv = [sys.executable, _mask, "--"] + argv
    # === MODEL MASK END ===

    with _TERMINAL_SPAWN_LOCK:
        existing_tid = (
            pty_terminal.tid_for_session(runtime_sid) if runtime_sid else None
        )
        if existing_tid:
            return jsonify({
                "ok": True,
                "terminal_id": existing_tid,
                "cwd": cwd,
                "agent": agent,
                "reused": True,
            })

        try:
            from core.billing import strip_metered_auth_env

            tid = _spawn_terminal_with_recovery(
                argv,
                cwd=cwd,
                cols=cols,
                rows=rows,
                session_id=runtime_sid,
                agent=agent,
                env=strip_metered_auth_env(os.environ),
            )
        except FileNotFoundError:
            return jsonify({"error": f"{agent} CLI not found on PATH"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        except BaseException as e:
            # Rust panics raised by pywinpty inherit directly from BaseException.
            # Keep those local to this request; control-flow exceptions still
            # need to retain their normal process semantics.
            if not _is_pywinpty_panic(e):
                raise
            return jsonify({"error": f"Windows terminal startup failed: {e}"}), 500

        # Register while the spawn lock is still held. A retry that arrives
        # after a lost HTTP response must receive this PTY, never create one.
        if runtime_sid:
            pty_terminal.register_session(runtime_sid, tid)
        if seed:
            pty_terminal.mark_turn_started(tid)

    return jsonify({
        "ok": True,
        "terminal_id": tid,
        "cwd": cwd,
        "agent": agent,
    })


def _terminal_file_snapshot(tid: str) -> tuple[bool | None, tuple[int, int] | None]:
    terminal = pty_terminal.get(tid)
    if not terminal or not terminal.session_id:
        return None, None
    session = get_session(terminal.session_id)
    if not session:
        return None, None
    file_path = session.get("file_path")
    if not file_path:
        return None, None
    try:
        stat = Path(file_path).stat()
        version = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None, None
    agent = terminal.agent or (session.get("agent") or "")
    return _PTY_ACTIVITY_READER.read(file_path, agent), version


@app.route("/api/terminal-runtime/turn-start/<tid>", methods=["POST"])
def api_terminal_runtime_turn_start(tid):
    active, version = _terminal_file_snapshot(tid)
    del active
    if not pty_terminal.mark_turn_started(tid, version):
        return jsonify({"ok": False, "error": "Terminal not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/terminal-runtime/migrate", methods=["POST"])
def api_terminal_runtime_migrate():
    data = request.get_json(silent=True) or {}
    old_sid = (data.get("old_sid") or "").strip()
    new_sid = (data.get("new_sid") or "").strip()
    tid = (data.get("terminal_id") or "").strip() or None
    if not pty_terminal.migrate_session(old_sid, new_sid, tid):
        return jsonify({"ok": False, "error": "Live terminal not found"}), 404
    mapped_tid = tid or pty_terminal.tid_for_session(new_sid)
    terminal = pty_terminal.get(mapped_tid) if mapped_tid else None
    session = get_session(new_sid) or {}
    if terminal and session.get("agent"):
        terminal.agent = (session.get("agent") or "").lower()
    try:
        from core.voice_inbox import get_default_voice_inbox

        get_default_voice_inbox().migrate_work_target(old_sid, new_sid)
    except Exception as error:
        print(f"[voice-inbox] work target migration failed: {error}", flush=True)
    return jsonify({"ok": True})


# How long an unfocused pane must be quiet before it is frozen. The GTK shell
# used the same knob and default.
_RUNTIME_IDLE_SECONDS = max(
    30, int(os.environ.get("SERENA_RUNTIME_IDLE_SECONDS", "600"))
)


@app.route("/api/terminal-runtime/sync", methods=["POST"])
def api_terminal_runtime_sync():
    data = request.get_json(silent=True) or {}
    focus_tid = (data.get("focus_tid") or "").strip()
    standby_tids = {
        str(tid).strip() for tid in (data.get("standby_tids") or []) if str(tid).strip()
    }
    protected_tids = {
        str(tid).strip() for tid in (data.get("protected_tids") or []) if str(tid).strip()
    }
    pin_both = bool(data.get("pin_both"))
    # Sweep every runtime the server owns, not just the panes the client named.
    # The client used to send the focused pane and its linked sibling only, so
    # every other terminal a user had opened was never a sleep candidate and
    # stayed fully resident for as long as the app ran.
    tids = set(pty_terminal.live_terminal_ids())
    tids |= ({focus_tid} if focus_tid else set()) | standby_tids
    states = {}
    reclaimed_mb = 0.0

    for tid in tids:
        terminal = pty_terminal.get(tid)
        if not terminal:
            states[tid] = {"state": "closed", "busy": False, "working": False}
            continue
        active, version = _terminal_file_snapshot(tid)
        busy = pty_terminal.refresh_turn_state(tid, active, version)
        working = busy or active is True or tid in protected_tids
        if tid == focus_tid or pin_both:
            pty_terminal.resume(tid)
        else:
            # A pane the user just switched away from sleeps immediately, since
            # waking is a SIGCONT. One it never named has to prove it is idle
            # first: a background agent can be mid-work without a recorded turn,
            # and freezing that stalls it invisibly.
            explicit = tid in standby_tids
            if pty_terminal.pause(
                tid,
                protected=working,
                min_idle_seconds=0.0 if explicit else _RUNTIME_IDLE_SECONDS,
            ) and not explicit:
                # Reclaim ONLY on the idle path, never when the user just
                # switched tabs. Pushing a pane's pages to swap the moment it
                # loses focus means switching back faults hundreds of MB in
                # from disk, so moving between panes stalls the whole app.
                # GTK reclaimed from its idle sweep alone for this reason.
                reclaimed_mb += pty_terminal.reclaim_memory(tid)
        states[tid] = {
            "state": pty_terminal.get_runtime_state(tid),
            "busy": busy,
            "working": working,
        }

    return jsonify(
        {"ok": True, "states": states, "reclaimed_mb": round(reclaimed_mb, 1)}
    )


@app.route("/api/voice-inbox/claim", methods=["POST"])
def api_voice_inbox_claim():
    return jsonify(
        {
            "ok": False,
            "error": "voice coding jobs are owned only by the resident supervisor",
        }
    ), 410


@app.route("/api/voice-inbox/<item_id>/<action>", methods=["POST"])
def api_voice_inbox_finish(item_id, action):
    return jsonify(
        {
            "ok": False,
            "item_id": item_id,
            "action": action,
            "error": "desktop voice-inbox execution is retired; use the resident controls",
        }
    ), 410


@app.route("/api/kill-terminal/<tid>", methods=["POST"])
def api_kill_terminal(tid):
    terminal = pty_terminal.get(tid)
    if terminal and terminal.session_id:
        try:
            from core.voice_inbox import get_default_voice_inbox

            get_default_voice_inbox().finish_work_target(
                terminal.session_id,
                error="working pane was closed",
            )
        except Exception:
            pass
    pty_terminal.kill(tid)
    return jsonify({"ok": True})


import tempfile as _tempfile
# Use the platform's actual temp dir. Previously hardcoded "/tmp/..." which
# on Windows resolves to "\tmp\..." (drive-letter-less). That broke when
# the path was typed into a PTY — claude couldn't open it AND the bare
# backslashes confused something downstream, freezing the chat input.
_UPLOAD_DIR = Path(_tempfile.gettempdir()) / "serena-chats-uploads"
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # 25 MB

_SAFE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic", ".heif", ".tiff"}


@app.route("/api/upload-image", methods=["POST"])
def api_upload_image():
    """Save a dropped image to a temp path so the CLI in the PTY can read it.
    Returns an absolute path with forward slashes — both claude and codex
    accept those on every OS, and forward slashes avoid the backslash-as-
    escape ambiguity that has bitten us on Windows."""
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

    # Absolute path, forward slashes — universal & unambiguous
    out_path = str(dest.resolve()).replace("\\", "/")
    return jsonify({"ok": True, "path": out_path})


@sock.route("/ws/terminal/<tid>")
def ws_terminal(ws, tid):
    """Bidirectional PTY <-> browser stream.

    Frontend sends either a raw string (user keystrokes) or a JSON object:
    ``{"resize": {"rows": R, "cols": C}}``, ``{"flow": {"enabled": true}}`` to
    arm ack-based flow control, or ``{"ack": N}`` to credit N parsed bytes.
    Backend pushes raw output frames.

    Dropping the socket detaches instead of killing the child, so reconnecting
    to the same terminal id resumes the same claude/codex process. Only an
    explicit /api/kill-terminal call, process exit, or the detach grace period
    expiring terminates it.
    """
    if not pty_terminal.get(tid):
        ws.send(json.dumps({"error": "Terminal not found"}))
        ws.close()
        return

    attachment = pty_terminal.attach(tid)
    if attachment is None:
        ws.send(json.dumps({"error": "Terminal not found"}))
        ws.close()
        return
    attach_token, backlog = attachment
    terminal = pty_terminal.get(tid)

    stop_reader = threading.Event()
    pty_dead = threading.Event()

    try:
        ws.send(json.dumps({
            "attached": True,
            "terminal_id": tid,
            "replayed_bytes": len(backlog),
            "rows": terminal.rows if terminal else 0,
            "cols": terminal.cols if terminal else 0,
            "ack_bytes": pty_terminal.FLOW_ACK_BYTES,
        }))
        if backlog:
            ws.send(backlog)
            # Count the replay too. It goes out before the renderer can arm
            # flow control, so without this a large reconnect backlog would
            # never appear in the ack ledger and the first watermark check
            # would start from a false zero.
            pty_terminal.note_sent(tid, len(backlog))
    except Exception:
        pty_terminal.detach(tid, attach_token)
        return

    def reader():
        while not stop_reader.is_set():
            if not pty_terminal.is_attached(tid, attach_token):
                break
            # Ack-based flow control. Park here while the renderer is behind
            # instead of pausing after each chunk. Per the xterm.js flow
            # control guide, per-chunk pausing puts a round trip between every
            # read and collapses throughput on a repainting TUI.
            if not pty_terminal.wait_for_flow(tid, 0.1):
                continue
            chunk = pty_terminal.read_available(tid, max_bytes=65536, timeout=0.05)
            if chunk is None:
                pty_dead.set()
                try:
                    ws.send(json.dumps({"exit": True}))
                except Exception:
                    pass
                stop_reader.set()
                break
            if chunk:
                try:
                    # Keep PTY output binary. xterm's UTF-8 decoder handles
                    # multibyte characters across frame boundaries and this
                    # avoids a Python decode/JS re-encode on every redraw.
                    ws.send(chunk)
                except Exception:
                    break
                pty_terminal.note_sent(tid, len(chunk))

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def write_manual(input_bytes: bytes, runtime: dict | None = None) -> bool:
        if not pty_terminal.write(tid, input_bytes):
            item_id = pty_terminal.reservation(tid)
            if item_id:
                try:
                    ws.send(json.dumps({"input_blocked": True, "reserved": item_id}))
                except Exception:
                    pass
            return False
        if isinstance(runtime, dict):
            pty_terminal.note_web_context(
                tid,
                focused_sid=runtime.get("focused_sid"),
                split_sids=runtime.get("split_sids"),
                draft=runtime.get("draft"),
            )
        else:
            pty_terminal.note_manual_input(tid, input_bytes)
        return True

    try:
        while True:
            # Timeout = let us check if reader died / shutdown was requested.
            # Without this, an ungracefully-disconnected browser leaves this
            # thread blocked forever on Windows (no half-close detection),
            # eventually saturating the WS handler pool.
            try:
                msg = ws.receive(timeout=10)
            except Exception:
                break
            if msg is None:
                # Either timeout (keep looping if reader thread alive) or close
                if stop_reader.is_set() or not reader_thread.is_alive():
                    break
                continue
            if isinstance(msg, bytes):
                write_manual(msg)
                continue
            # Text frame — attempt JSON first (resize control), fallback to raw input
            if msg.startswith("{"):
                try:
                    payload = json.loads(msg)
                except ValueError:
                    payload = None
                # Every control key present in the frame is applied, then one
                # continue at the end swallows it. Chaining `continue` per key
                # instead would let whichever branch came first silently drop
                # the rest, e.g. a resize batched with an ack.
                if isinstance(payload, dict):
                    control_handled = False
                    if "resize" in payload:
                        r = payload["resize"]
                        pty_terminal.resize(
                            tid, int(r.get("rows", 30)), int(r.get("cols", 100))
                        )
                        control_handled = True
                    if "flow" in payload:
                        flow = payload.get("flow") or {}
                        if isinstance(flow, dict) and flow.get("enabled"):
                            pty_terminal.enable_flow_control(tid)
                        control_handled = True
                    if "ack" in payload:
                        try:
                            pty_terminal.note_ack(tid, int(payload["ack"]))
                        except (TypeError, ValueError):
                            pass
                        control_handled = True
                    if "runtime_context" in payload:
                        runtime = payload.get("runtime_context") or {}
                        if isinstance(runtime, dict) and "input" not in payload:
                            pty_terminal.note_web_context(
                                tid,
                                focused_sid=runtime.get("focused_sid"),
                                split_sids=runtime.get("split_sids"),
                                draft=runtime.get("draft"),
                            )
                        control_handled = True
                    if "input" in payload:
                        input_bytes = str(payload["input"]).encode("utf-8")
                        runtime = payload.get("runtime_context") or {}
                        write_manual(
                            input_bytes,
                            runtime if isinstance(runtime, dict) else None,
                        )
                        continue
                    if control_handled:
                        continue
            input_bytes = msg.encode("utf-8")
            write_manual(input_bytes)
    finally:
        stop_reader.set()
        # Join before detaching: the detached drain takes over the read side,
        # and two threads reading one fd would interleave the reconnect replay.
        reader_thread.join(timeout=2.0)
        if pty_dead.is_set():
            pty_terminal.kill(tid)
        else:
            pty_terminal.detach(tid, attach_token)


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


# ─────────────────────────────────────────────────────────────────────────────
# === HANDOFF FEATURE START ===
# Cross-agent session handoff. Generates a briefing markdown from the source
# session and stages it in the cwd so the receiving agent can read it and
# pick up. Self-contained: delete this block + chats/handoff.py + the JS
# block in the frontend (also marked HANDOFF FEATURE) to remove the feature.
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/handoff", methods=["POST"])
def api_handoff():
    try:
        from chats.handoff import build_handoff_briefing
    except ImportError:
        return jsonify({"error": "Handoff feature is disabled"}), 503

    data = request.get_json(silent=True) or {}
    src_sid = (data.get("source_sid") or "").strip()
    target_agent = (data.get("target_agent") or "").strip().lower()
    if not src_sid or target_agent not in ("claude", "codex"):
        return jsonify({"error": "source_sid and target_agent (claude|codex) required"}), 400

    res = build_handoff_briefing(src_sid)
    if not res.get("ok"):
        return jsonify({"error": res.get("error", "handoff build failed")}), 400

    src_cwd = res.get("cwd") or ""
    cwd_path = Path(src_cwd) if src_cwd and Path(src_cwd).is_dir() else Path.home()
    briefing_filename = ".serena-handoff.md"
    briefing_path = cwd_path / briefing_filename
    try:
        briefing_path.write_text(res["briefing"], encoding="utf-8")
    except OSError:
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="serena-handoff-", suffix=".md")
        os.close(fd)
        briefing_path = Path(tmp)
        briefing_path.write_text(res["briefing"], encoding="utf-8")
        briefing_filename = briefing_path.name

    src_agent = res.get("agent") or "claude"
    if target_agent == "claude":
        # Claude Code: @<file> auto-attaches the file content to the message.
        prompt = (
            f"@{briefing_filename} this is a handoff briefing from a prior "
            f"{src_agent} session. read it, re-ground in the current code "
            f"(re-read the files it mentions), then continue from the "
            f"\"Next step\" section."
        )
    else:
        # Codex: no @-attach; ask it to Read the file via tool use.
        prompt = (
            f"Read {briefing_filename} — it's a handoff briefing from a prior "
            f"{src_agent} session. Acknowledge what you see, re-read the files "
            f"it mentions to confirm current state, then continue from the "
            f"\"Next step\" section."
        )

    return jsonify({
        "ok": True,
        "briefing_path": str(briefing_path),
        "briefing_filename": briefing_filename,
        "source_agent": src_agent,
        "source_title": res.get("title") or "",
        "target_agent": target_agent,
        "cwd": str(cwd_path),
        "prompt": prompt,
    })
# === HANDOFF FEATURE END ===


@app.route("/api/context-fork", methods=["POST"])
def api_context_fork():
    from chats.context_fork import build_context_fork

    data = request.get_json(silent=True) or {}
    source_sid = str(data.get("source_sid") or "").strip()
    target_agent = str(data.get("target_agent") or "").strip().lower()
    try:
        result = build_context_fork(source_sid, target_agent)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError as exc:
        return jsonify({"ok": False, "error": f"could not write context fork: {exc}"}), 500
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# === GROUP FEATURE START ===
# Linked-chats / shared-thread: lets the user mark N chats as peers so they
# render with a shared color stripe + 🔗 glyph + sibling navigation. Self-
# contained: delete this block + the JS block + the metadata helpers (also
# marked GROUP FEATURE) to remove the feature.
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/group/link", methods=["POST"])
def api_group_link():
    from core import metadata as meta_sync
    data = request.get_json(silent=True) or {}
    sids = data.get("session_ids") or []
    if not isinstance(sids, list) or len(sids) < 2:
        return jsonify({"error": "session_ids must be a list of >=2"}), 400
    try:
        gid = meta_sync.link_sessions(sids)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    members = meta_sync.list_group_members(gid)
    return jsonify({"ok": True, "group_id": gid, "members": members})


@app.route("/api/group/unlink", methods=["POST"])
def api_group_unlink():
    from core import metadata as meta_sync
    data = request.get_json(silent=True) or {}
    sid = (data.get("session_id") or "").strip()
    if not sid:
        return jsonify({"error": "session_id required"}), 400
    meta_sync.unlink_session(sid)
    return jsonify({"ok": True})


@app.route("/api/group/disband", methods=["POST"])
def api_group_disband():
    from core import metadata as meta_sync
    data = request.get_json(silent=True) or {}
    gid = (data.get("group_id") or "").strip()
    if not gid:
        return jsonify({"error": "group_id required"}), 400
    meta_sync.unlink_group(gid)
    return jsonify({"ok": True})


@app.route("/api/group/<group_id>")
def api_group_members(group_id):
    """Return the sibling sessions in a group, decorated like /api/sessions."""
    from core import metadata as meta_sync
    member_sids = meta_sync.list_group_members(group_id)
    if not member_sids:
        return jsonify([])
    sessions: list[dict] = []
    for sid in member_sids:
        s = get_session(sid)
        if s:
            sessions.append(s)
    sessions.sort(key=lambda s: s.get("last_timestamp") or "", reverse=True)
    return jsonify(_decorate_sessions(sessions))
# === GROUP FEATURE END ===


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

    from memory.store import add_memory

    result = add_memory(content, mem_type)
    if isinstance(result, str):
        return jsonify({"ok": True, "state": "proposed", "proposal_id": result})
    return jsonify({"ok": True, "id": result})


@app.route("/api/memory/<int:mem_id>", methods=["PUT"])
def api_memory_update(mem_id):
    data = request.get_json(silent=True) or {}
    content = data.get("content", "").strip()
    mem_type = data.get("type", "").strip().lower()

    from memory.store import _find_path, _parse_file, update_memory

    fpath = _find_path(mem_id)
    if not fpath:
        return jsonify({"error": "Memory not found"}), 404

    # Read existing to preserve created date
    existing = _parse_file(fpath)
    if not existing:
        return jsonify({"error": "Could not parse memory"}), 500

    if not content:
        content = existing["content"]
    if not mem_type:
        mem_type = existing["type"]
    if mem_type not in MEMORY_TYPES:
        return jsonify({"error": f"Invalid type"}), 400

    result = update_memory(mem_id, content=content, mem_type=mem_type)
    if isinstance(result, str):
        return jsonify({"ok": True, "state": "proposed", "proposal_id": result})
    return jsonify({"ok": True})


@app.route("/api/memory/<int:mem_id>", methods=["DELETE"])
def api_memory_delete(mem_id):
    from memory.store import delete_memory

    result = delete_memory(mem_id)
    if not result:
        return jsonify({"error": "Memory not found"}), 404
    if isinstance(result, str):
        return jsonify({"ok": True, "state": "proposed", "proposal_id": result})
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


# When this process started. Compared against the source tree so the desktop
# shell can tell whether the server it is talking to predates the code on disk.
_PROCESS_STARTED_AT = time.time()
_FRESHNESS_ROOTS = ("core", "ui", "memory", "voice")
_FRESHNESS_CACHE: dict[str, float] = {}


def _newest_source_mtime() -> float:
    """Newest .py mtime in the tree this process was imported from.

    Serena runs from a checkout that changes under her. A restart of the desktop
    app does not reload this process, so a fix can sit on disk for hours while
    every request is still served by the old code. Nothing surfaced that, which
    is exactly how an afternoon gets spent on a bug that was already fixed.
    """
    now = time.time()
    cached_at = _FRESHNESS_CACHE.get("checked_at", 0.0)
    if now - cached_at < 5.0:
        return _FRESHNESS_CACHE.get("newest", 0.0)

    root = Path(__file__).resolve().parent.parent
    newest = 0.0
    for name in _FRESHNESS_ROOTS:
        directory = root / name
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.py"):
            # Virtualenvs and caches live under these roots and churn on their
            # own; only first-party source counts as a reason to restart.
            parts = path.parts
            if any(part in {"__pycache__", "site-packages"} or part.startswith(".venv") for part in parts):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
    _FRESHNESS_CACHE["checked_at"] = now
    _FRESHNESS_CACHE["newest"] = newest
    return newest


@app.get("/api/backend-freshness")
def api_backend_freshness():
    """Whether this process is running the code currently on disk."""
    newest = _newest_source_mtime()
    return jsonify({
        "ok": True,
        "pid": os.getpid(),
        # The packaged shell has no idea where the checkout lives; a frozen
        # build is not even running from one. Say so rather than making it guess.
        "source_root": str(Path(__file__).resolve().parent.parent),
        "frozen": bool(getattr(sys, "frozen", False)),
        "started_at": _PROCESS_STARTED_AT,
        "newest_source_mtime": newest,
        "stale": bool(newest > _PROCESS_STARTED_AT),
        "stale_by_seconds": max(0.0, newest - _PROCESS_STARTED_AT),
    })


@app.get("/api/health")
def api_health():
    """Liveness probe shared by every entry point.

    The desktop shell polls this to decide whether to attach to an already
    running server (mobile_host) or spawn its own sidecar, so it has to live
    on the app rather than on one launcher.
    """
    return jsonify({"ok": True, "pid": os.getpid()})


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_web(host="0.0.0.0", port=8080, open_browser=False):
    """Start the web server."""
    global _browser_pid

    # A previous host leaves its panes running in their own scopes with nobody
    # holding the other end of their PTY. They are unreachable, so clear them
    # before serving; panes owned by a host that is still alive are untouched.
    try:
        pty_terminal.reap_orphaned_scopes()
    except Exception:
        pass

    print("Updating session index...")
    update_index()
    print("Updating knowledge index...")
    update_knowledge_index()

    wake_model = Path.home() / ".config" / "serena" / "models" / "hey_serena.onnx"
    if wake_model.is_file() and not _LAZY_CALL_RUNTIME:
        warm_desk_runtime_background()
        _get_desk_greeting_pool().start_refill()

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
