"""Auto-link consecutive codex chats into one thread group.

Codex's context window is small, so on a big task it plans in one session then
spawns a FRESH session to execute — a new rollout with a new id, no parent
field recorded. Left alone they scatter as separate rows. But the handoff is
near-instant: the exec session starts seconds after the plan session goes idle,
in the same cwd. We detect that and drop the new session into the predecessor's
group (creating one if needed, or joining the predecessor's existing
claude↔codex group), so the whole chain renders as one row.

Conservative by design: only acts on sessions started in the last `recent_hours`
(never retroactively scrambles old history) and only links within `window_sec`
of the predecessor's last activity, same cwd.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from core.config import DB_PATH
from core import metadata as meta

_WINDOW_SEC = 300       # exec must start within 5 min of the plan going idle
_OVERLAP_GRACE = 90     # allow slight overlap (plan still flushing as exec starts)
_RECENT_HOURS = 48      # only consider freshly-created sessions


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def auto_link_codex_chains(window_sec: int = _WINDOW_SEC, recent_hours: int = _RECENT_HOURS,
                           dry_run: bool = False) -> list[tuple[str, str, str]]:
    """Link recent codex plan→exec chains. Returns [(new_sid, predecessor_sid, group)]."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT session_id, cwd, project_dir, first_timestamp, last_timestamp, "
            "custom_title, title, originator FROM sessions WHERE agent='codex' "
            "AND first_timestamp IS NOT NULL"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []

    items = []
    for r in rows:
        ft = _parse(r["first_timestamp"])
        if not ft:
            continue
        lt = _parse(r["last_timestamp"]) or ft
        cwd = (r["cwd"] or r["project_dir"] or "").strip()
        if not cwd:
            continue
        items.append({
            "sid": r["session_id"], "cwd": cwd, "ft": ft, "lt": lt,
            "custom_title": r["custom_title"], "title": r["title"],
            "originator": str(r["originator"] or ""),
        })
    items.sort(key=lambda x: x["ft"])

    now = datetime.now(timezone.utc)
    done: list[tuple[str, str, str]] = []
    for s in items:
        if (now - s["ft"]).total_seconds() > recent_hours * 3600:
            continue
        # Only ever treat a genuine exec continuation as the "exec" side of a
        # plan→exec chain. A codex-tui session is a HUMAN opening a chat — two
        # interactive chats in the same cwd within five minutes are unrelated,
        # and linking them stole the predecessor's group and title (the
        # recurring wrong-group / renamed-chat bug).
        if "exec" not in s["originator"].lower():
            continue
        if meta.get_group(s["sid"]):
            continue  # already linked
        # predecessor: same-cwd codex starting earlier, ending closest before S
        best = None
        for p in items:
            if p["sid"] == s["sid"] or p["cwd"] != s["cwd"] or p["ft"] >= s["ft"]:
                continue
            gap = (s["ft"] - p["lt"]).total_seconds()
            if -_OVERLAP_GRACE <= gap <= window_sec:
                if best is None or p["lt"] > best["lt"]:
                    best = p
        if not best:
            continue
        gid = meta.get_group(best["sid"])
        # Inherit the planning chat's name — codex auto-titles the exec session
        # "a previous agent produced…" which is useless. Use the predecessor's
        # custom name if it has one, else its (better) auto-title.
        inherit = (meta.get_meta(best["sid"]).get("custom_title")
                   or best.get("custom_title") or best.get("title") or "").strip()
        if not dry_run:
            if not gid:
                gid = meta._new_group_id()
                meta.set_group(best["sid"], gid)
            meta.set_group(s["sid"], gid)
            if inherit and not meta.get_meta(s["sid"]).get("custom_title"):
                meta.set_custom_title(s["sid"], inherit)
        done.append((s["sid"], best["sid"], gid or "(new)"))
    return done


if __name__ == "__main__":
    import sys
    dry = "--apply" not in sys.argv
    res = auto_link_codex_chains(dry_run=dry)
    print(f"{'WOULD link' if dry else 'linked'}: {len(res)}")
    for new_sid, pre_sid, gid in res:
        print(f"  {new_sid[:8]} -> group of {pre_sid[:8]}  ({gid[:14] if gid else '?'})")
