"""Narrow recovery for SQLite journal-mode initialization races."""

import sqlite3
import time


def enable_wal(connection: sqlite3.Connection) -> None:
    """SQLite can reject a concurrent journal-mode switch without busy-handler waiting."""
    for attempt in range(6):
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", None)
            if code is None or (code & 0xFF) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or attempt == 5:
                raise
            time.sleep(0.05 * 2 ** attempt)
