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
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field

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
    queue: "queue.Queue[bytes | None] | None" = None
    reader_thread: threading.Thread | None = None


_terminals: dict[str, Terminal] = {}
_registry_lock = threading.Lock()


def _windows_reader_loop(proc, q: "queue.Queue[bytes | None]") -> None:
    while True:
        try:
            chunk = proc.read(8192)
        except EOFError:
            q.put(None)
            return
        except OSError:
            q.put(None)
            return
        if not chunk:
            time.sleep(0.02)
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        q.put(chunk)


def spawn(argv: list[str], cwd: str, cols: int = 100, rows: int = 30) -> str:
    if _PtyProcess is None:
        raise RuntimeError(
            "PTY backend not installed. Install 'pywinpty' on Windows or "
            "'ptyprocess' on POSIX."
        )

    proc = _PtyProcess.spawn(argv, cwd=cwd, dimensions=(rows, cols))
    tid = uuid.uuid4().hex
    term = Terminal(id=tid, proc=proc, cols=cols, rows=rows)

    if _IS_WINDOWS:
        term.queue = queue.Queue()
        term.reader_thread = threading.Thread(
            target=_windows_reader_loop,
            args=(proc, term.queue),
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
        q = term.queue
        if q is None:
            return None
        try:
            chunk = q.get(timeout=timeout)
        except queue.Empty:
            return b""
        if chunk is None:
            return None
        if len(chunk) > max_bytes:
            # Hand back what fits, requeue the rest at the front so order holds.
            head, tail = chunk[:max_bytes], chunk[max_bytes:]
            new_q: "queue.Queue[bytes | None]" = queue.Queue()
            new_q.put(tail)
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                new_q.put(item)
            term.queue = new_q
            return head
        return chunk

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
    if not term:
        return
    try:
        if _IS_WINDOWS:
            term.proc.terminate()
        else:
            term.proc.terminate(force=True)
    except Exception:
        pass
