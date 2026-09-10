"""Operation-owned SQLite connections with deterministic transaction cleanup."""

import sqlite3


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback as SQLite normally does, then release the owned handle.

    A scope owns this connection's lifetime. Do not nest scopes or reuse it after
    exit. Raw callers may still manage transactions and call close explicitly.
    """

    def __exit__(self, *exception):
        try:
            return super().__exit__(*exception)
        finally:
            self.close()


def connect_database(path, *, timeout=10, foreign_keys=False, uri=False):
    connection = sqlite3.connect(path, timeout=timeout, uri=uri, factory=ClosingConnection)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        if foreign_keys:
            connection.execute("PRAGMA foreign_keys = ON")
        return connection
    except BaseException:
        connection.close()
        raise
