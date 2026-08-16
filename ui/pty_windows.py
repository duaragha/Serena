"""Windows ConPTY terminal host backed by pywinpty.

The module mirrors :mod:`ui.pty_terminal`'s public host interface but keeps
all Windows-only imports and lifecycle state isolated. The shared shell can
select it without importing pywinpty on other platforms::

    backends = {}
    pty_windows.register_backend(backends)
    host = backends[sys.platform]

pywinpty exposes blocking reads and its Windows handle is not selectable.
Each terminal therefore owns a reader thread which appends to a locked byte
buffer and wakes non-blocking API consumers with an event.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from collections.abc import MutableMapping
from contextlib import suppress
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

try:
    from winpty import PtyProcess as _PtyProcess
except ImportError:  # Import-safe on Linux and in source-only installations.
    _PtyProcess = None


@dataclass
class Terminal:
    id: str
    proc: object
    cols: int
    rows: int
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    buf: bytearray = field(default_factory=bytearray)
    buf_lock: threading.Lock = field(default_factory=threading.Lock)
    data_event: threading.Event = field(default_factory=threading.Event)
    stop_event: threading.Event = field(default_factory=threading.Event)
    eof: bool = False
    reader_thread: threading.Thread | None = None
    session_id: str | None = None
    agent: str = ""
    runtime_state: str = "live"
    runtime_busy: bool = False
    turn_file_version: tuple[int, int] | None = None
    started_at: float = field(default_factory=time.monotonic)
    state_lock: threading.Lock = field(default_factory=threading.Lock)


_terminals: dict[str, Terminal] = {}
_session_tids: dict[str, str] = {}
_registry_lock = threading.Lock()
_backend_registry: dict[str, ModuleType] = {}


def register_backend(
    registry: MutableMapping[str, ModuleType] | None = None,
) -> ModuleType:
    """Register and return this host under Python's Windows platform key."""
    backend = sys.modules[__name__]
    target = _backend_registry if registry is None else registry
    target["win32"] = backend
    return backend


def activate_backend() -> ModuleType:
    """Route ``ui.pty_terminal`` imports to this Windows implementation."""
    backend = register_backend()
    import ui as ui_package

    sys.modules["ui.pty_terminal"] = backend
    ui_package.pty_terminal = backend
    return backend


def backend_for(
    platform: str | None = None,
    registry: MutableMapping[str, ModuleType] | None = None,
) -> ModuleType | None:
    """Return the registered backend for *platform*, if one is available."""
    target = _backend_registry if registry is None else registry
    return target.get(sys.platform if platform is None else platform)


def _mark_eof(term: Terminal) -> None:
    with term.buf_lock:
        term.eof = True
    term.data_event.set()


def _reader_loop(term: Terminal) -> None:
    while not term.stop_event.is_set():
        try:
            chunk = term.proc.read(8192)
        except (EOFError, OSError):
            _mark_eof(term)
            return

        if not chunk:
            try:
                alive = bool(term.proc.isalive())
            except Exception:
                alive = False
            if not alive:
                _mark_eof(term)
                return
            term.stop_event.wait(0.01)
            continue

        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        elif not isinstance(chunk, (bytes, bytearray)):
            chunk = bytes(chunk)

        with term.buf_lock:
            term.buf.extend(chunk)
        term.data_event.set()

    _mark_eof(term)


def spawn(
    argv: list[str],
    cwd: str,
    cols: int = 100,
    rows: int = 30,
    *,
    session_id: str | None = None,
    agent: str = "",
    env: dict[str, str] | None = None,
) -> str:
    """Spawn *argv* inside a ConPTY and return its terminal id."""
    if _PtyProcess is None:
        raise RuntimeError("Windows PTY backend not installed. Install 'pywinpty==3.0.5'.")

    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("TERM", "xterm-256color")
    child_env.setdefault("COLORTERM", "truecolor")
    child_env.setdefault("COLUMNS", str(cols))
    child_env.setdefault("LINES", str(rows))
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUTF8", "1")

    proc = _PtyProcess.spawn(
        argv,
        cwd=cwd,
        dimensions=(rows, cols),
        env=child_env,
    )
    tid = uuid.uuid4().hex
    term = Terminal(
        id=tid,
        proc=proc,
        cols=cols,
        rows=rows,
        session_id=session_id,
        agent=(agent or "").lower(),
    )

    with _registry_lock:
        _terminals[tid] = term

    term.reader_thread = threading.Thread(
        target=_reader_loop,
        args=(term,),
        daemon=True,
        name=f"conpty-reader-{tid[:8]}",
    )
    term.reader_thread.start()
    return tid


def get(tid: str) -> Terminal | None:
    with _registry_lock:
        return _terminals.get(tid)


def register_session(sid: str, tid: str) -> None:
    with _registry_lock:
        _session_tids[sid] = tid
        term = _terminals.get(tid)
        if term:
            term.session_id = sid


def migrate_session(old_sid: str, new_sid: str, tid: str | None = None) -> bool:
    if not old_sid or not new_sid:
        return False
    with _registry_lock:
        mapped_tid = tid or _session_tids.get(old_sid)
        if not mapped_tid or mapped_tid not in _terminals:
            return False
        if _session_tids.get(old_sid) == mapped_tid:
            _session_tids.pop(old_sid, None)
        _session_tids[new_sid] = mapped_tid
        _terminals[mapped_tid].session_id = new_sid
    return True


def tid_for_session(sid: str) -> str | None:
    with _registry_lock:
        tid = _session_tids.get(sid)
    if tid and is_alive(tid):
        return tid
    if tid:
        with _registry_lock:
            _session_tids.pop(sid, None)
    return None


def write(tid: str, data: bytes | bytearray | str) -> bool:
    term = get(tid)
    if not term:
        return False
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8", errors="replace")
    try:
        with term.write_lock:
            term.proc.write(data)
        return True
    except (OSError, EOFError):
        return False


def mark_turn_started(
    tid: str,
    file_version: tuple[int, int] | None = None,
) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        term.runtime_busy = True
        term.turn_file_version = file_version
    return True


def refresh_turn_state(
    tid: str,
    active: bool | None,
    file_version: tuple[int, int] | None,
) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        if (
            term.runtime_busy
            and active is False
            and file_version is not None
            and file_version != term.turn_file_version
        ):
            term.runtime_busy = False
            term.turn_file_version = file_version
        return term.runtime_busy


def is_runtime_busy(tid: str) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        return term.runtime_busy


def pause(
    tid: str,
    *,
    protected: bool = False,
    prewarm_seconds: float = 5.0,
) -> bool:
    """ConPTY has no safe SIGSTOP equivalent, so Windows stays live."""
    del protected, prewarm_seconds
    return False


def resume(tid: str) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        term.runtime_state = "live"
    return True


def get_runtime_state(tid: str) -> str:
    term = get(tid)
    if not term:
        return "closed"
    with term.state_lock:
        return term.runtime_state


def resize(tid: str, rows: int, cols: int) -> bool:
    term = get(tid)
    if not term:
        return False
    try:
        term.proc.setwinsize(rows, cols)
        term.rows, term.cols = rows, cols
        return True
    except OSError:
        return False


def read_available(
    tid: str,
    max_bytes: int = 4096,
    timeout: float = 0.05,
) -> bytes | None:
    """Read buffered output, returning empty bytes for idle and None for EOF."""
    term = get(tid)
    if not term:
        return None
    if not term.data_event.wait(timeout):
        return b""

    with term.buf_lock:
        if term.buf:
            head = bytes(term.buf[:max_bytes])
            del term.buf[:max_bytes]
            if not term.buf and not term.eof:
                term.data_event.clear()
            return head
        if term.eof:
            return None
        term.data_event.clear()
        return b""


def is_alive(tid: str) -> bool:
    term = get(tid)
    if not term:
        return False
    try:
        return bool(term.proc.isalive())
    except Exception:
        return False


def _call_forcefully(proc: object, method_name: str) -> None:
    method: Any = getattr(proc, method_name, None)
    if method is None:
        return
    try:
        method(force=True)
    except TypeError:
        method()


def kill(tid: str) -> None:
    with _registry_lock:
        term = _terminals.pop(tid, None)
        stale_sids = [sid for sid, mapped_tid in _session_tids.items() if mapped_tid == tid]
        for sid in stale_sids:
            _session_tids.pop(sid, None)
    if not term:
        return

    term.stop_event.set()
    for method_name in ("terminate", "close"):
        with suppress(Exception):
            _call_forcefully(term.proc, method_name)
    term.data_event.set()
    if term.reader_thread and term.reader_thread is not threading.current_thread():
        term.reader_thread.join(timeout=0.2)
