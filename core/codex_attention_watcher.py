"""Background watcher: detect when a codex session finishes a turn and
mark it as needing attention in `core.chat_attention`.

Codex has no Stop hook, but every turn it writes a `task_completed` (or
`task_complete`) event to its rollout JSONL at
`~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl`.

Strategy: poll today + yesterday for ambient completion cues, and explicitly
tail any older rollout whose turn began inside Serena. Each file is read only
from its last offset. If a terminal event appears, mark the session.

This runs in a daemon thread started by Serena at boot.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from core import chat_attention

CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
ROLLOUT_RE = re.compile(r"rollout-[\dT_-]+-(?P<sid>[0-9a-f-]{36})\.jsonl$")
POLL_INTERVAL = 2.0

# Per-file read offset so we don't re-scan the entire JSONL each tick
_offsets: dict[str, int] = {}
# Track sids we've already marked attention for this turn, to avoid re-marking
# until the user clears + a new turn starts.
_marked_at_offset: dict[str, int] = {}
_watched_paths: dict[str, Path] = {}
_watch_lock = threading.Lock()
_stop_event = threading.Event()


def _recent_session_dirs() -> list[Path]:
    """Directories covered by the low-cost ambient completion scan."""
    now = datetime.now()
    out = []
    for delta in (0, 1):
        d = now - timedelta(days=delta)
        p = CODEX_SESSIONS_ROOT / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
        if p.is_dir():
            out.append(p)
    return out


def _scan_file(path: Path) -> str | None:
    """Read NEW bytes from path since last check and mark attention if a
    task_completed event appears. Returns the completed session id."""
    key = str(path)
    m = ROLLOUT_RE.search(path.name)
    if not m:
        return None
    sid = m.group("sid")
    try:
        size = path.stat().st_size
    except OSError:
        return None
    with _watch_lock:
        last = _offsets.get(key, 0)
    if size <= last:
        return None  # no new data
    try:
        with path.open("rb") as fh:
            fh.seek(last)
            new_bytes = fh.read(size - last)
    except OSError:
        return None
    with _watch_lock:
        _offsets[key] = size
    # Parse new lines, look for terminal event types
    triggered = False
    for raw in new_bytes.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "event_msg":
            continue
        payload = ev.get("payload") or {}
        ev_type = payload.get("type")
        if ev_type in ("task_complete", "task_completed"):
            triggered = True
    if triggered:
        # Only mark if we haven't already for this turn (avoid duplicate
        # marks if task_completed fires multiple times in one file)
        if _marked_at_offset.get(sid) != size:
            _marked_at_offset[sid] = size
            chat_attention.mark(sid)
        return sid
    return None


def watch(sid: str, file_path: str | Path | None) -> bool:
    """Tail one exact rollout from its current end for the next completion.

    Resumed Codex sessions keep writing to their creation-date rollout, so a
    today/yesterday directory scan cannot observe many active Serena chats.
    """
    if not sid or not file_path:
        return False
    path = Path(file_path)
    if not ROLLOUT_RE.search(path.name):
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    with _watch_lock:
        _watched_paths[sid] = path
        _offsets[str(path)] = size
        _marked_at_offset.pop(sid, None)
    return True


def _watch_loop() -> None:
    while not _stop_event.is_set():
        try:
            paths: set[Path] = set()
            for d in _recent_session_dirs():
                paths.update(d.glob("rollout-*.jsonl"))
            with _watch_lock:
                paths.update(_watched_paths.values())
            for path in paths:
                completed_sid = _scan_file(path)
                if completed_sid:
                    with _watch_lock:
                        _watched_paths.pop(completed_sid, None)
        except Exception as e:
            print(f"[codex-watcher] error: {e}", flush=True)
        _stop_event.wait(POLL_INTERVAL)


_thread: threading.Thread | None = None


def start() -> None:
    """Start the watcher in a daemon thread. Idempotent."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    # On first start, seed _offsets to current file sizes so we don't mark
    # attention for OLD finished turns at startup.
    for d in _recent_session_dirs():
        for p in d.glob("rollout-*.jsonl"):
            try:
                with _watch_lock:
                    _offsets[str(p)] = p.stat().st_size
            except OSError:
                pass
    _thread = threading.Thread(target=_watch_loop, name="codex-attention-watcher", daemon=True)
    _thread.start()
    print("[codex-watcher] started", flush=True)


def stop() -> None:
    _stop_event.set()
