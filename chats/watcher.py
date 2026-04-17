"""Watch ~/.claude/projects/ for jsonl changes and trigger reindex.

Used by the TUI to auto-refresh when Syncthing drops in new session files
from another machine. Debounces bursts of events so rapid writes (or
syncthing copying many files) collapse into a single reindex.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from core.config import PROJECTS_DIR


class _JsonlHandler(FileSystemEventHandler):
    def __init__(self, on_change: Callable[[], None], debounce: float = 0.75) -> None:
        self._on_change = on_change
        self._debounce = debounce
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _interesting(self, event: FileSystemEvent) -> bool:
        if event.is_directory:
            return False
        path = event.src_path or ""
        return path.endswith(".jsonl")

    def _schedule(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        try:
            self._on_change()
        except Exception:
            pass

    def on_created(self, event: FileSystemEvent) -> None:
        if self._interesting(event):
            self._schedule()

    def on_modified(self, event: FileSystemEvent) -> None:
        if self._interesting(event):
            self._schedule()

    def on_moved(self, event: FileSystemEvent) -> None:
        dest = getattr(event, "dest_path", "") or ""
        if not event.is_directory and dest.endswith(".jsonl"):
            self._schedule()


class ProjectsWatcher:
    """Watch PROJECTS_DIR recursively for *.jsonl changes."""

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._observer: Observer | None = None
        self._handler = _JsonlHandler(on_change)

    def start(self) -> None:
        if self._observer is not None:
            return
        path = Path(PROJECTS_DIR)
        path.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        observer.schedule(self._handler, str(path), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._observer is None:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=2.0)
        except Exception:
            pass
        self._observer = None
