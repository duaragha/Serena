"""Project-scoped chat context for Serena agents.

Gives every session awareness of recent sibling work in the same repository,
so new chats don't start blind. Queries SQLite for the last N sessions matching
the project root or slug, formats a tight recap, and points to `chats recall`
for older sessions.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from core.config import DB_PATH


def find_project_root(path: str | Path | None) -> Path:
    """Find the root of the project/repository for a given cwd."""
    if not path:
        return Path.cwd()
    p = Path(path).resolve()
    curr = p
    for parent in [curr] + list(curr.parents):
        if (parent / ".git").exists():
            return parent
    return p


def get_recent_project_chats(
    cwd: str | Path | None,
    limit: int = 6,
    exclude_sid: str | None = None,
) -> list[dict[str, Any]]:
    """Query the last `limit` chats matching this project directory."""
    if not os.path.exists(DB_PATH):
        return []

    root = find_project_root(cwd)
    root_str = str(root)
    root_name = root.name

    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    cursor = conn.cursor()

    query = """
        SELECT session_id, agent, coalesce(custom_title, title, substr(first_message, 1, 60)) as display_title,
               last_timestamp, git_branch
        FROM sessions
        WHERE (cwd = ? OR cwd LIKE ? OR project_dir LIKE ?)
    """
    params: list[Any] = [root_str, root_str + "/%", "%" + root_name + "%"]
    if exclude_sid:
        query += " AND session_id != ?"
        params.append(exclude_sid)
    query += " ORDER BY last_timestamp DESC LIMIT ?"
    params.append(limit)

    try:
        rows = cursor.execute(query, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    results = []
    for sid, agent, title, ts, branch in rows:
        results.append({
            "session_id": sid,
            "agent": agent,
            "title": (title or "Untitled").replace("\n", " ").strip(),
            "timestamp": ts or "",
            "date": ts[:10] if ts else "unknown",
            "branch": branch or "",
        })
    return results


def format_project_context(
    cwd: str | Path | None,
    limit: int = 6,
    exclude_sid: str | None = None,
) -> str:
    """Return a compact markdown block with recent chats for this project."""
    root = find_project_root(cwd)
    name = root.name
    chats = get_recent_project_chats(root, limit=limit, exclude_sid=exclude_sid)

    lines = [f"[PROJECT CONTEXT: {name}]"]
    if chats:
        lines.append(f"Recent sessions in this repository (last {len(chats)}):")
        for c in chats:
            branch_info = (
                f" [{c['branch']}]"
                if c["branch"] and c["branch"] not in ("main", "master")
                else ""
            )
            lines.append(
                f"  - [{c['session_id'][:8]}] {c['date']} ({c['agent']}){branch_info}: {c['title']}"
            )
        lines.append('(For older chats in this project: run `chats recall "<topic>"` or `chats show <sid>`)')
    else:
        lines.append("No previous chats recorded for this repository.")

    return "\n".join(lines)


if __name__ == "__main__":
    target_cwd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    output = format_project_context(target_cwd)
    if output:
        print(output)
