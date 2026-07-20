"""PTY-backed terminals for the browser UI.

Each spawned terminal is tracked by a uuid. The web UI opens a WebSocket to
stream input/output; lifecycle is owned by the browser — close the tab or
hit the terminate endpoint and the child is SIGTERM'd.

POSIX uses ptyprocess + select on the master fd. Windows uses pywinpty's
ConPTY-backed PtyProcess plus a per-terminal reader thread feeding a queue,
because the Windows handle is not selectable.
"""

import os
import queue
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    try:
        from winpty import PtyProcess as _PtyProcess
    except ImportError:
        _PtyProcess = None
else:
    try:
        import ptyprocess as _ptyprocess_mod
        _PtyProcess = _ptyprocess_mod.PtyProcess
    except ImportError:
        _PtyProcess = None


@dataclass
class Terminal:
    id: str
    proc: object
    cols: int
    rows: int
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    # Windows-only: bytes buffer + lock + event for the reader thread → API.
    # Replaces the previous queue-swap race condition (rebuilding the queue
    # under the reader thread dropped output during high-throughput bursts,
    # which is exactly what happens during a resize repaint).
    buf: bytearray = field(default_factory=bytearray)
    buf_lock: threading.Lock = field(default_factory=threading.Lock)
    data_event: threading.Event = field(default_factory=threading.Event)
    eof: bool = False
    reader_thread: threading.Thread | None = None
    session_id: str | None = None
    agent: str = ""
    graphics_protocol: str | None = None
    runtime_state: str = "live"
    runtime_busy: bool = False
    turn_file_version: tuple[int, int] | None = None
    started_at: float = field(default_factory=time.monotonic)
    state_lock: threading.Lock = field(default_factory=threading.Lock)


_terminals: dict[str, Terminal] = {}
_registry_lock = threading.Lock()
_terminfo_lock = threading.Lock()


def _enable_sixel_environment(env: dict[str, str]) -> bool:
    """Advertise Sixel without leaving child programs with an unknown TERM."""
    term_name = "xterm-sixel"
    if _IS_WINDOWS:
        env["TERM"] = term_name
        return True

    tic = shutil.which("tic")
    if not tic:
        return False

    cache_root = Path(
        os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    ) / "serena" / "terminfo"
    compiled = cache_root / term_name[0] / term_name
    with _terminfo_lock:
        if not compiled.exists():
            cache_root.mkdir(parents=True, exist_ok=True)
            source = (
                f"{term_name}|xterm.js with Sixel graphics,\n"
                "\tuse=xterm-256color,\n"
            )
            try:
                result = subprocess.run(
                    [tic, "-x", "-o", str(cache_root), "-"],
                    input=source,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            if result.returncode != 0 or not compiled.exists():
                return False

    existing = env.get("TERMINFO_DIRS", "")
    env["TERMINFO_DIRS"] = f"{cache_root}:{existing}"
    env["TERM"] = term_name
    return True


def _windows_reader_loop(term: "Terminal") -> None:
    proc = term.proc
    while True:
        try:
            chunk = proc.read(8192)
        except EOFError:
            with term.buf_lock:
                term.eof = True
            term.data_event.set()
            return
        except OSError:
            with term.buf_lock:
                term.eof = True
            term.data_event.set()
            return
        if not chunk:
            time.sleep(0.01)
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        with term.buf_lock:
            term.buf.extend(chunk)
        term.data_event.set()


def spawn(
    argv: list[str],
    cwd: str,
    cols: int = 100,
    rows: int = 30,
    *,
    session_id: str | None = None,
    agent: str = "",
    terminal_protocol: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    if _PtyProcess is None:
        raise RuntimeError(
            "PTY backend not installed. Install 'pywinpty' on Windows or "
            "'ptyprocess' on POSIX."
        )

    # Ensure claude/codex see a real terminal type. On Windows, the inherited
    # env from pythonw.exe has no TERM, so apps fall back to plain ASCII
    # rendering — claude's TUI then can't position its statusline/input bar
    # correctly + may skip the alt-screen switch entirely.
    env = dict(os.environ if env is None else env)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    env.setdefault("COLUMNS", str(cols))
    env.setdefault("LINES", str(rows))
    graphics_protocol = None
    if terminal_protocol == "sixel" and _enable_sixel_environment(env):
        graphics_protocol = "sixel"
    if _IS_WINDOWS:
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        proc = _PtyProcess.spawn(argv, cwd=cwd, dimensions=(rows, cols), env=env)
    else:
        proc = _PtyProcess.spawn(argv, cwd=cwd, dimensions=(rows, cols), env=env)
    tid = uuid.uuid4().hex
    term = Terminal(
        id=tid,
        proc=proc,
        cols=cols,
        rows=rows,
        session_id=session_id,
        agent=(agent or "").lower(),
        graphics_protocol=graphics_protocol,
    )

    if _IS_WINDOWS:
        term.reader_thread = threading.Thread(
            target=_windows_reader_loop,
            args=(term,),
            daemon=True,
            name=f"pty-reader-{tid[:8]}",
        )
        term.reader_thread.start()

    with _registry_lock:
        _terminals[tid] = term
    return tid


def get(tid: str) -> Terminal | None:
    with _registry_lock:
        return _terminals.get(tid)


# ── session ↔ terminal mapping ──────────────────────────────────────────────
# Lets server-side callers (the ask-codex / ask-claude bridges) find the live
# PTY for a chat. The GTK shell tracks VTEs per-sid in-process; the
# Windows/pywebview path runs terminals here, so the bridges need this map.
_session_tids: dict[str, str] = {}


def register_session(sid: str, tid: str) -> None:
    with _registry_lock:
        _session_tids[sid] = tid
        term = _terminals.get(tid)
        if term:
            term.session_id = sid


def migrate_session(old_sid: str, new_sid: str, tid: str | None = None) -> bool:
    """Move a live pseudo-session mapping onto its on-disk session id."""
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
    """The live terminal for a session id, or None. Drops stale entries."""
    with _registry_lock:
        tid = _session_tids.get(sid)
    if tid and is_alive(tid):
        return tid
    if tid:
        with _registry_lock:
            _session_tids.pop(sid, None)
    return None


def write(tid: str, data: bytes) -> bool:
    term = get(tid)
    if not term:
        return False
    try:
        with term.write_lock:
            if _IS_WINDOWS and isinstance(data, (bytes, bytearray)):
                term.proc.write(data.decode("utf-8", errors="replace"))
            else:
                term.proc.write(data)
        return True
    except (OSError, EOFError):
        return False


def mark_turn_started(tid: str, file_version: tuple[int, int] | None = None) -> bool:
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
    """Clear a busy lease only after this turn changed the transcript and ended."""
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


def pause(tid: str, *, protected: bool = False, prewarm_seconds: float = 5.0) -> bool:
    """Freeze an idle POSIX process group for a millisecond-scale resume."""
    term = get(tid)
    if not term or _IS_WINDOWS:
        return False
    with term.state_lock:
        if (
            protected
            or term.runtime_busy
            or term.runtime_state == "paused"
            or time.monotonic() - term.started_at < prewarm_seconds
        ):
            return False
        try:
            os.killpg(os.getpgid(term.proc.pid), signal.SIGSTOP)
        except (OSError, ProcessLookupError):
            return False
        term.runtime_state = "paused"
    return True


def resume(tid: str) -> bool:
    term = get(tid)
    if not term:
        return False
    with term.state_lock:
        if term.runtime_state != "paused":
            return True
        if _IS_WINDOWS:
            term.runtime_state = "live"
            return True
        try:
            os.killpg(os.getpgid(term.proc.pid), signal.SIGCONT)
        except (OSError, ProcessLookupError):
            return False
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


def read_available(tid: str, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
    """Non-blocking read. Returns b'' when nothing's ready, None when the PTY is gone."""
    term = get(tid)
    if not term:
        return None

    if _IS_WINDOWS:
        # Drain up to max_bytes from the byte buffer atomically — no queue
        # swap, no race with the reader thread. Order is preserved because
        # the reader only ever appends to term.buf under the same lock.
        if not term.data_event.wait(timeout):
            return b""
        with term.buf_lock:
            if term.buf:
                head = bytes(term.buf[:max_bytes])
                del term.buf[:max_bytes]
                if not term.buf:
                    term.data_event.clear()
                return head
            if term.eof:
                return None
            # Spurious wakeup — clear and indicate nothing ready
            term.data_event.clear()
            return b""

    try:
        fd = term.proc.fd
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return b""
        return os.read(fd, max_bytes)
    except (OSError, EOFError):
        return None


def is_alive(tid: str) -> bool:
    term = get(tid)
    if not term:
        return False
    try:
        return term.proc.isalive()
    except Exception:
        return False


def kill(tid: str) -> None:
    with _registry_lock:
        term = _terminals.pop(tid, None)
        stale_sids = [sid for sid, mapped_tid in _session_tids.items() if mapped_tid == tid]
        for sid in stale_sids:
            _session_tids.pop(sid, None)
    if not term:
        return
    try:
        if _IS_WINDOWS:
            term.proc.terminate()
        else:
            term.proc.terminate(force=True)
    except Exception:
        pass
