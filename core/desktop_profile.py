"""One-time desktop cache migration; native session stores stay untouched."""

import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time


def seed_dev_index(*, home=None, environ=None):
    env = os.environ if environ is None else environ
    if env.get("SERENA_DESKTOP_CHANNEL") != "dev":
        return False
    home = Path.home() if home is None else Path(home)
    source = home / ".local/share/chats/index.db"
    target = Path(env.get("CHATS_DATA_DIR", home / ".local/share/chats-dev")) / "index.db"
    if target.exists() or not source.is_file() or source.resolve() == target.resolve():
        return False
    staged = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".index-seed-", dir=target.parent)
        os.close(fd)
        staged = Path(name)
        deadline = time.monotonic() + 10

        def progress(*unused):
            if time.monotonic() > deadline:
                raise TimeoutError("Index snapshot exceeded the startup budget")

        # A filesystem copy can omit committed WAL pages. SQLite's online
        # backup reads a consistent cache without stopping the stable host.
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as src:
            with closing(sqlite3.connect(staged)) as dst:
                src.backup(dst, pages=1024, progress=progress, sleep=0.05)
                dst.execute("PRAGMA journal_mode=DELETE")
        # Publish only if another startup has not already created its index.
        os.link(staged, target)
        return True
    except (OSError, sqlite3.Error) as error:
        print(f"[desktop] Dev cache snapshot skipped: {error}", flush=True)
        return False
    finally:
        if staged is not None:
            for suffix in ("", "-wal", "-shm"):
                Path(str(staged) + suffix).unlink(missing_ok=True)
