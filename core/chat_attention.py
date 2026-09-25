"""Track which chats have "finished a turn" and are awaiting user attention.

When claude or codex finishes its current turn, we add its session id to a
shared in-memory set. Serena's UI polls this set and renders a visual cue
(orange stripe on the sidebar entry + glow border on the VTE pane in split
view) so the user sees which chats are done without having to open each.

The set is cleared per-sid when:
  - User focuses that pane (UI click → /api/chat-attention/clear)
  - User sends a new prompt to that chat (any input → clear)
  - The chat is deleted

Sources of "turn finished" signals:
  - claude: the Stop hook calls `chats mark-done <sid>` which POSTs to
    /api/chat-finished (claude exports CLAUDE_CODE_SESSION_ID in hook env)
  - codex: a background watcher thread polls ~/.codex/sessions/<today>/
    rollout files for `task_completed` events (codex has no hook concept)
  - structured panes (Serena Dev): the workspace host's `turn/completed`

Each mark also records one finish EVENT naming the chat that actually
finished, which the UI turns into a "<chat> has finished (Codex)" toast. The
flag spreads to linked siblings; the event does not, or a Claude reply would
be announced as its Codex partner too. Two sources can report the same turn
(a structured Codex pane also writes the rollout the watcher tails), so a
second report for the same chat inside EVENT_DEDUPE_SECONDS is not news.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Iterable

# {sid: timestamp_finished_at}
_ATTENTION: dict[str, float] = {}
_LOCK = threading.Lock()

EVENT_DEDUPE_SECONDS = 10.0
_EVENTS: deque[dict] = deque(maxlen=200)
_LAST_EVENT_AT: dict[str, float] = {}
_seq = 0


def _group_siblings(sid: str) -> set[str]:
    """All session ids in the same linked group as `sid` (incl. sid itself).
    Returns just {sid} if the chat isn't linked."""
    try:
        from core import metadata as meta
        gid = meta.get_group(sid)
        if not gid:
            return {sid}
        return set(meta.list_group_members(gid)) | {sid}
    except Exception:
        return {sid}


def mark(sid: str) -> None:
    """Mark a chat as needing attention (turn finished). Propagates to all
    linked siblings so the whole thread shows as awaiting attention — not
    just whichever agent finished its turn first."""
    global _seq
    if not sid:
        return
    now = time.time()
    targets = _group_siblings(sid)
    with _LOCK:
        for s in targets:
            _ATTENTION[s] = now
        last = _LAST_EVENT_AT.get(sid)
        if last is None or now - last >= EVENT_DEDUPE_SECONDS:
            _seq += 1
            _EVENTS.append({"seq": _seq, "sid": sid, "at": now})
            _LAST_EVENT_AT[sid] = now
            if len(_LAST_EVENT_AT) > 1000:
                for stale in sorted(_LAST_EVENT_AT, key=_LAST_EVENT_AT.get)[:500]:
                    _LAST_EVENT_AT.pop(stale, None)
    try:
        from core.voice_inbox import get_default_voice_inbox

        get_default_voice_inbox().finish_work_target(sid)
    except Exception:
        pass
    try:
        from desktop.app_gtk import ChatsApp

        inst = ChatsApp.INSTANCE
        if inst is not None:
            inst.notify_runtime_turn_finished(sid)
    except (ImportError, AttributeError):
        pass


def clear(sid: str) -> None:
    """Clear attention for a chat AND its linked siblings."""
    if not sid:
        return
    targets = _group_siblings(sid)
    with _LOCK:
        for s in targets:
            _ATTENTION.pop(s, None)


def clear_many(sids: Iterable[str]) -> int:
    """Clear attention for multiple sids; returns count cleared."""
    n = 0
    with _LOCK:
        for s in sids:
            if s in _ATTENTION:
                _ATTENTION.pop(s, None)
                n += 1
    return n


def list_active() -> dict[str, float]:
    """Return a snapshot of {sid: timestamp} pairs currently flagged."""
    with _LOCK:
        return dict(_ATTENTION)


def is_active(sid: str) -> bool:
    with _LOCK:
        return sid in _ATTENTION


def events_since(seq: int | None) -> tuple[list[dict], int]:
    """Finish events after `seq`, oldest first, and the current cursor.

    `None` means the caller has no cursor yet: it gets the cursor and no
    events, because whatever finished before it looked is not news. A cursor
    ahead of ours means this backend restarted under a page that stayed open,
    so everything it has recorded since is new to that page.
    """
    with _LOCK:
        current = _seq
        if seq is None:
            return [], current
        if seq > current:
            seq = 0
        return [dict(event) for event in _EVENTS if event["seq"] > seq], current
