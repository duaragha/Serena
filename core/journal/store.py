"""Journal state on the PC: each day's facts, her questions, his answers, places.

The entry itself lives in Locket, where he reads his journal. This is the
working state behind it -- what was found, what was asked, what he said -- so
an answer that arrives at 11pm, or the next morning, lands in the right day.

Places are learned, not looked up. No coordinate is ever sent to a geocoder;
the first time he says where a visit was, that name is kept with its
coordinates, and the next visit within PLACE_RADIUS_M resolves to it.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import closing, suppress
from pathlib import Path
from typing import Any

PLACE_RADIUS_M = 150.0


def db_path() -> Path:
    configured = os.environ.get("SERENA_JOURNAL_DB", "").strip()
    return Path(configured).expanduser() if configured else (
        Path.home() / ".local" / "state" / "serena" / "journal.sqlite3")


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS days (
            day TEXT PRIMARY KEY,
            facts_json TEXT NOT NULL DEFAULT '{}',
            questions_json TEXT NOT NULL DEFAULT '[]',
            answers_json TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '',
            entry_id INTEGER,
            drafted_at REAL, sent_at REAL, called_at REAL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL,
            radius_m REAL NOT NULL DEFAULT 150, created_at REAL NOT NULL
        );
        """)
    # Added after the first release; older databases get them here.
    for column in ("entry_base", "entry_written", "refreshed_at"):
        with suppress(sqlite3.OperationalError):
            connection.execute(f"ALTER TABLE days ADD COLUMN {column} TEXT")
    if os.name != "nt":
        with suppress(OSError):
            path.chmod(0o600)
    return connection


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def place_for(lat: float, lng: float, *, radius_m: float | None = None) -> str:
    """What to call a spot: the name he gave it, else home or work, else ''."""

    named = _named_place(lat, lng, radius_m=radius_m)
    if named:
        return named
    try:
        from core.journal.places import anchor_for

        return anchor_for(lat, lng, radius_m=radius_m)
    except Exception:
        return ""


def _named_place(lat: float, lng: float, *, radius_m: float | None = None) -> str:
    """The name he gave the nearest known place, or '' when there is none."""

    with closing(_connect()) as connection:
        rows = connection.execute("SELECT name, lat, lng, radius_m FROM places").fetchall()
    best, best_d = "", float("inf")
    for row in rows:
        d = distance_m(lat, lng, row["lat"], row["lng"])
        if d <= max(row["radius_m"], radius_m or 0) and d < best_d:
            best, best_d = row["name"], d
    return best


def name_place(name: str, lat: float, lng: float, *, radius_m: float = PLACE_RADIUS_M) -> None:
    clean = " ".join(str(name).split())[:120]
    if not clean:
        return
    with closing(_connect()) as connection, connection:
        # Renaming a spot he already named replaces it rather than stacking a
        # second name on the same coordinates.
        for row in connection.execute("SELECT id, lat, lng FROM places").fetchall():
            if distance_m(lat, lng, row["lat"], row["lng"]) <= radius_m / 2:
                connection.execute("UPDATE places SET name = ? WHERE id = ?", (clean, row["id"]))
                return
        connection.execute(
            "INSERT INTO places(name, lat, lng, radius_m, created_at) VALUES (?, ?, ?, ?, ?)",
            (clean, lat, lng, radius_m, time.time()))


def load_day(day: str) -> dict[str, Any] | None:
    with closing(_connect()) as connection:
        row = connection.execute("SELECT * FROM days WHERE day = ?", (day,)).fetchone()
    if row is None:
        return None
    return {
        "day": row["day"],
        "facts": json.loads(row["facts_json"]),
        "questions": json.loads(row["questions_json"]),
        "answers": json.loads(row["answers_json"]),
        "summary": row["summary"],
        "entry_id": row["entry_id"],
        "drafted_at": row["drafted_at"],
        "sent_at": row["sent_at"],
        "called_at": row["called_at"],
        "entry_base": row["entry_base"],
        "entry_written": row["entry_written"],
        "refreshed_at": row["refreshed_at"],
    }


def save_day(day: str, **fields: Any) -> None:
    columns = {
        "facts": "facts_json", "questions": "questions_json", "answers": "answers_json",
        "summary": "summary", "entry_id": "entry_id", "drafted_at": "drafted_at",
        "sent_at": "sent_at", "called_at": "called_at",
        "entry_base": "entry_base", "entry_written": "entry_written",
        "refreshed_at": "refreshed_at",
    }
    values = {}
    for key, value in fields.items():
        column = columns[key]
        values[column] = json.dumps(value) if column.endswith("_json") else value
    with closing(_connect()) as connection, connection:
        connection.execute("INSERT OR IGNORE INTO days(day, updated_at) VALUES (?, ?)", (day, time.time()))
        for column, value in values.items():
            connection.execute(f"UPDATE days SET {column} = ?, updated_at = ? WHERE day = ?",
                               (value, time.time(), day))


def open_days(limit: int = 3) -> list[dict[str, Any]]:
    """Recent days that still have questions he has not answered."""

    with closing(_connect()) as connection:
        rows = connection.execute(
            "SELECT day FROM days WHERE sent_at IS NOT NULL ORDER BY day DESC LIMIT ?",
            (limit * 3,)).fetchall()
    out = []
    for row in rows:
        day = load_day(row["day"])
        if day and unanswered(day):
            out.append(day)
        if len(out) >= limit:
            break
    return out


def unanswered(day: dict[str, Any]) -> list[dict[str, Any]]:
    answered = {a.get("question_id") for a in day.get("answers") or []}
    return [q for q in day.get("questions") or [] if q.get("id") not in answered]
