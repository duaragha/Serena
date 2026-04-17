"""Centralized memory storage for Claude Code sessions.

Memories are stored in the shared SQLite database and accessible
from any Claude Code session via `chats memory`.
"""

import sqlite3

from core.config import DATA_DIR, DB_PATH


def _get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_table(conn)
    return conn


def _ensure_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            type TEXT DEFAULT 'general',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def list_memories(type_filter: str | None = None) -> list[dict]:
    conn = _get_db()
    if type_filter:
        rows = conn.execute(
            "SELECT * FROM memories WHERE type = ? ORDER BY type, updated_at DESC",
            (type_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memories ORDER BY type, updated_at DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_memory(content: str, mem_type: str = "general") -> int:
    conn = _get_db()
    cursor = conn.execute(
        "INSERT INTO memories (content, type) VALUES (?, ?)",
        (content, mem_type),
    )
    conn.commit()
    memory_id = cursor.lastrowid
    conn.close()
    return memory_id


def update_memory(memory_id: int, content: str | None = None, mem_type: str | None = None):
    conn = _get_db()
    updates = ["updated_at = datetime('now')"]
    params: list = []
    if content is not None:
        updates.append("content = ?")
        params.append(content)
    if mem_type is not None:
        updates.append("type = ?")
        params.append(mem_type)
    params.append(memory_id)
    conn.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    conn.close()


def delete_memory(memory_id: int) -> bool:
    conn = _get_db()
    cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


def get_memory(memory_id: int) -> dict | None:
    conn = _get_db()
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def search_memories(query: str) -> list[dict]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM memories WHERE content LIKE ? ORDER BY updated_at DESC",
        (f"%{query}%",),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def format_for_claude() -> str:
    """Format all memories as compact text for Claude Code to consume."""
    memories = list_memories()
    if not memories:
        return "No memories stored yet. Use `chats memory add \"content\"` to save one."

    by_type: dict[str, list[dict]] = {}
    for m in memories:
        by_type.setdefault(m["type"], []).append(m)

    lines = ["# Memories"]
    for type_name in sorted(by_type):
        lines.append(f"\n## {type_name.title()}")
        for item in by_type[type_name]:
            lines.append(f"- [{item['id']}] {item['content']}")

    return "\n".join(lines)
