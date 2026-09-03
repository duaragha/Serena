"""Durable one-shot call hello claims across websocket and process restarts."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path

DEFAULT_HELLO_STATE_PATH = (
    Path.home() / ".config" / "serena" / "call_hello.sqlite3"
)


class CallHelloState:
    """Persist unresolved call IDs without retaining them in runtime memory."""

    def __init__(self, path: Path | None = None) -> None:
        configured = os.environ.get("SERENA_CALL_HELLO_STATE_PATH", "").strip()
        self.path = Path(path or configured or DEFAULT_HELLO_STATE_PATH).expanduser()
        self._lock = threading.Lock()

    def claim(self, call_id: str) -> bool:
        call_id = call_id.strip()
        if not call_id:
            return False
        with self._lock:
            try:
                with self._connect() as connection:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO call_hello(call_id, claimed_at) "
                        "VALUES (?, ?)",
                        (call_id, time.time()),
                    )
                    return cursor.rowcount == 1
            except sqlite3.Error:
                return False

    def finish(self, call_id: str) -> None:
        call_id = call_id.strip()
        if not call_id:
            return
        with self._lock:
            try:
                with self._connect() as connection:
                    connection.execute(
                        "DELETE FROM call_hello WHERE call_id = ?", (call_id,)
                    )
            except sqlite3.Error:
                pass

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS call_hello ("
            "call_id TEXT PRIMARY KEY, claimed_at REAL NOT NULL)"
        )
        if os.name != "nt":
            with suppress(OSError):
                self.path.chmod(0o600)
        return connection
