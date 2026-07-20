"""pywebview launcher — starts the Flask UI on loopback and opens it in a native window."""

import json
import socket
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

import webview
from webview.dom import DOMEventHandler


# === LOGGING === Capture stdout/stderr to a rotating log file BEFORE any
# imports that might log. When Serena is launched via pythonw.exe (Start
# Menu shortcut → no console window), stdout/stderr go nowhere by default
# — every print() and unhandled exception is lost, making "things just
# stop working" silently. Redirecting to a file gives us actual debug info.
def _install_file_logging() -> Path:
    log_dir = Path.home() / ".local" / "share" / "chats" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "serena.log"
    # Rotate if the existing log is over ~5MB
    try:
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            backup = log_dir / "serena.log.1"
            if backup.exists():
                backup.unlink()
            log_path.rename(backup)
    except OSError:
        pass

    # If we're attached to a real console (foreground `chats desktop`), keep
    # printing there too. With pythonw.exe sys.stdout/stderr are still
    # writable but redirect to nowhere, so swap them for the file unless a
    # console is attached.
    try:
        has_console = bool(sys.stdout and sys.stdout.fileno() >= 0) and (
            sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False
        )
    except (OSError, ValueError):
        has_console = False

    log_handle = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    log_handle.write(f"\n=== Serena launched {time.strftime('%Y-%m-%d %H:%M:%S')} (console={has_console}) ===\n")

    class _Tee:
        def __init__(self, *streams):
            self._streams = [s for s in streams if s is not None]
        def write(self, data):
            for s in self._streams:
                try:
                    s.write(data)
                    s.flush()
                except Exception:
                    pass
        def flush(self):
            for s in self._streams:
                try:
                    s.flush()
                except Exception:
                    pass
        def isatty(self):
            return False

    if has_console:
        sys.stdout = _Tee(sys.__stdout__, log_handle)
        sys.stderr = _Tee(sys.__stderr__, log_handle)
    else:
        sys.stdout = log_handle
        sys.stderr = log_handle

    def _hook(exc_type, exc, tb):
        print("\n*** UNHANDLED EXCEPTION ***", file=sys.stderr, flush=True)
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
        sys.stderr.flush()

    sys.excepthook = _hook
    return log_path

_LOG_PATH = _install_file_logging()
print(f"[boot] logging to {_LOG_PATH}", flush=True)
# === LOGGING END ===


from core.indexer import update_index, update_knowledge_index
from ui.web import app as flask_app


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False


def _serve(host: str, port: int) -> None:
    # Werkzeug's threaded dev server. Required for flask-sock: Waitress does
    # not expose the underlying socket to WSGI apps, so /ws/terminal/<tid>
    # upgrades fail with `RuntimeError: Cannot obtain socket from WSGI
    # environment` and every PTY terminal in the UI shows "Disconnected".
    # Werkzeug hands the socket off via environ['werkzeug.socket'], which
    # flask-sock / simple-websocket use for the WS handshake. For a single-
    # user desktop app the dev server is plenty.
    flask_app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


def run(width: int = 1400, height: int = 900) -> None:
    # === STARTUP === Mirror of app_gtk.py — defer heavy work to background
    # threads so the UI window appears immediately. MCP sync skips entirely
    # when the master config hasn't changed (hash check in writers.py).
    boot_t0 = time.monotonic()
    print(f"[boot] launching @ {time.strftime('%H:%M:%S')}")

    def _bg_indexing():
        t = time.monotonic()
        try:
            update_index()
        except Exception as e:
            print(f"[bg] update_index failed: {e}")
        print(f"[bg] sessions indexed in {time.monotonic()-t:.1f}s")
        t = time.monotonic()
        try:
            update_knowledge_index()
        except Exception as e:
            print(f"[bg] update_knowledge_index failed: {e}")
        print(f"[bg] knowledge indexed in {time.monotonic()-t:.1f}s")

    def _bg_mcp():
        # === RETIRED (2026-06-04) === Multiplexer + config sync are dead: MCP
        # servers run as Docker containers on the PC tailnet (Projects/
        # mcp_servers/gateway, :8801+), registered directly in the shared
        # ~/.claude.json with tailnet URLs. The old sync kept re-injecting
        # machine-local 127.0.0.1:17541 URLs into the shared config, causing
        # the "retrying…" spam on whichever machine lacked the multiplexer.
        print("[bg] mcp multiplex/sync retired — servers live on the tailnet gateway")

    def _bg_attention():
        try:
            from core import codex_attention_watcher
            codex_attention_watcher.start()
        except Exception as e:
            print(f"[bg] attention watcher failed: {e}")

    def _bg_archive():
        try:
            from core.locket_archive_sync import start_auto_sync

            start_auto_sync()
        except Exception as e:
            print(f"[bg] archive sync failed: {e}")

    for fn in (_bg_indexing, _bg_mcp, _bg_attention, _bg_archive):
        threading.Thread(target=fn, daemon=True).start()

    host = "127.0.0.1"
    port = _find_free_port()
    url = f"http://{host}:{port}"

    threading.Thread(target=_serve, args=(host, port), daemon=True).start()

    if not _wait_for_server(url):
        print(f"Flask didn't start at {url}")
        return

    print(f"[boot] window opening @ {time.monotonic()-boot_t0:.2f}s")
    window = webview.create_window(
        title="Chats",
        url=url,
        width=width,
        height=height,
        min_size=(900, 600),
    )

    # === DROP === Drop handling lives entirely in JS now (see
    # setupTerminalDrop in ui/web.py). The previous pywebview DOMEventHandler
    # path used `pywebviewFullPath` which is undefined for in-memory image
    # blobs (Win Snipping Tool screenshots, etc.), and its
    # stop_propagation=True consumption left WebView2 in a stuck dragging
    # state that blocked typing. JS handler uploads via /api/upload-image
    # and types the resulting path into the PTY — works uniformly across
    # real files + in-memory blobs.
    # Set the window/taskbar icon explicitly (pywebview's winforms backend reads
    # this into Form.Icon → the Windows taskbar). Without it the running window
    # falls back to pythonw.exe's icon / a stale shortcut association.
    _icon = Path(__file__).resolve().parent.parent / "static" / "serena-icon.ico"
    webview.start(None, window, icon=str(_icon) if _icon.exists() else None)


if __name__ == "__main__":
    run()
