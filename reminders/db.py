import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from config import DB_PATH


def get_conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            trigger_type TEXT NOT NULL,  -- 'time' | 'payment' | 'immediate'
            trigger_at TEXT,            -- ISO datetime for time-based
            status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'fired' | 'cancelled'
            created_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'cli'  -- 'cli' | 'google_tasks' | 'sms' | 'telegram'
        )
    """)
    conn.commit()
    return conn


def add_reminder(message: str, trigger_type: str, trigger_at: datetime | None = None,
                 source: str = "cli") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO reminders (message, trigger_type, trigger_at, created_at, source) VALUES (?, ?, ?, ?, ?)",
        (message, trigger_type, trigger_at.isoformat() if trigger_at else None,
         datetime.now(timezone.utc).isoformat(), source),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def get_due_reminders() -> list[dict]:
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE status = 'pending' AND trigger_type = 'time' AND trigger_at <= ?",
        (now,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pending_by_trigger(trigger_type: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE status = 'pending' AND trigger_type = ?",
        (trigger_type,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_fired(reminder_id: int):
    conn = get_conn()
    conn.execute("UPDATE reminders SET status = 'fired' WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()


def cancel_reminder(reminder_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
        (reminder_id,),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def list_reminders(status: str | None = None) -> list[dict]:
    conn = get_conn()
    if status:
        rows = conn.execute("SELECT * FROM reminders WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM reminders ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]
