"""Shared MCP multiplexer — run each stateless MCP server ONCE and let every
claude session connect to it over HTTP, instead of spawning N subprocesses
per MCP for N concurrent claudes.

Architecture:
  - At Serena startup, we spawn one subprocess per `shared: true` MCP
  - Each gets a dedicated reader thread that parses JSON-RPC responses
    from its stdout and routes them back to the originating Flask request
    by request ID
  - All shared MCPs are exposed over a single fixed-port HTTP server at
    /mcp/<name> so Claude's config points to stable URLs that survive
    Serena restarts (port doesn't change)
  - JSON-RPC ID multiplexing: each Flask request gets a unique internal ID
    when forwarded to the subprocess; responses are routed via Future map

Safeguards (prevent the "duplicate spawn" issues we saw with codex auto-link):
  - PID file per shared MCP at ~/.config/serena/multiplex/<name>.pid
  - Startup checks PID file; if PID is alive, reuse instead of spawning
  - Subprocess crashes are detected by reader thread; pending requests get
    error responses; next request triggers respawn
  - SIGTERM/atexit handler reaps all subprocesses on Serena shutdown

Concurrency model:
  - One subprocess per MCP, one reader thread per subprocess
  - N Flask request threads call `proxy.call(...)` concurrently
  - Lock-protected pending-future map for response routing
  - Most MCP SDKs handle concurrent requests in-flight via JSON-RPC IDs
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

# Fixed port for the multiplexer's HTTP server. Doesn't change across
# Serena restarts so claude's config URLs stay valid.
MULTIPLEX_PORT = 17541
MULTIPLEX_HOST = "127.0.0.1"
PIDFILE_DIR = Path.home() / ".config" / "serena" / "multiplex"

_REGISTRY: dict[str, "SharedMCPProxy"] = {}
_REGISTRY_LOCK = threading.Lock()


def shared_url(name: str) -> str:
    return f"http://{MULTIPLEX_HOST}:{MULTIPLEX_PORT}/mcp/{name}"


class SharedMCPProxy:
    """One running stdio MCP subprocess + a router that maps JSON-RPC IDs
    between concurrent Flask requests and the subprocess."""

    def __init__(self, name: str, command: str, args: list[str],
                 env: dict[str, str], cwd: str | None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env_extra = env or {}
        self.cwd = cwd or None
        self.proc: subprocess.Popen | None = None
        self.pending: dict[str, Future] = {}
        self.pending_lock = threading.Lock()
        self.id_counter = 0
        self.id_lock = threading.Lock()
        self.reader_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self._restart_lock = threading.Lock()
        self._stopped = False
        self.start()

    # ----- lifecycle -----
    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        env = {**os.environ, **self.env_extra}
        # Force UTF-8 on Python child processes so MCP servers don't write
        # their JSON-RPC in cp1252 (Windows default) and get garbled.
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        try:
            # Explicit UTF-8 encoding instead of `text=True` (which uses the
            # system default — cp1252 on Windows, would break MCP JSON-RPC
            # streams containing non-ASCII characters).
            self.proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self.cwd,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line buffered
            )
        except (OSError, FileNotFoundError) as e:
            print(f"[multiplex] failed to spawn {self.name}: {e}", flush=True)
            self.proc = None
            return
        _write_pid(self.name, self.proc.pid)
        self.reader_thread = threading.Thread(
            target=self._reader_loop, name=f"mcp-mux-{self.name}", daemon=True
        )
        self.reader_thread.start()
        self.stderr_thread = threading.Thread(
            target=self._stderr_loop, name=f"mcp-mux-{self.name}-err", daemon=True
        )
        self.stderr_thread.start()
        print(f"[multiplex] started {self.name} pid={self.proc.pid}", flush=True)

    def stop(self) -> None:
        self._stopped = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except (OSError, ProcessLookupError):
                pass
        _clear_pid(self.name)

    def restart(self) -> None:
        with self._restart_lock:
            print(f"[multiplex] restarting {self.name}", flush=True)
            # Fail any inflight requests
            with self.pending_lock:
                for fut in self.pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError(f"{self.name} subprocess crashed, restarting"))
                self.pending.clear()
            try:
                if self.proc:
                    self.proc.kill()
            except (OSError, ProcessLookupError):
                pass
            self.proc = None
            self.start()

    # ----- request/response -----
    def call(self, request: dict, timeout: float = 60.0) -> dict | None:
        """Forward a JSON-RPC request to the subprocess and return the
        response. Notifications (no `id`) are fire-and-forget."""
        if self.proc is None or self.proc.poll() is not None:
            self.restart()
            if self.proc is None:
                return _error_response(request.get("id"), -32603, f"{self.name} not running")

        original_id = request.get("id")
        is_notification = original_id is None

        if is_notification:
            self._send(request)
            return None

        # Generate a unique internal ID to route the response back to us
        # without colliding with concurrent claudes.
        with self.id_lock:
            self.id_counter += 1
            internal_id = f"mux-{self.id_counter}"
        forwarded = {**request, "id": internal_id}
        fut: Future = Future()
        with self.pending_lock:
            self.pending[internal_id] = fut
        try:
            self._send(forwarded)
            response = fut.result(timeout=timeout)
        except FutureTimeout:
            return _error_response(original_id, -32603, f"{self.name} timed out after {timeout:.0f}s")
        except RuntimeError as e:
            return _error_response(original_id, -32603, str(e))
        finally:
            with self.pending_lock:
                self.pending.pop(internal_id, None)
        # Restore the caller's original ID so claude sees its own ID back
        if isinstance(response, dict):
            response["id"] = original_id
        return response

    def _send(self, msg: dict) -> None:
        if not self.proc or not self.proc.stdin:
            return
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            print(f"[multiplex] {self.name} stdin broken: {e}", flush=True)
            self.restart()

    def _reader_loop(self) -> None:
        """Parse JSON-RPC responses from the subprocess and dispatch to
        the originating request's future."""
        if not self.proc or not self.proc.stdout:
            return
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Non-JSON output (e.g. logs printed to stdout instead of stderr).
                # Most MCPs are well-behaved but defend against it.
                continue
            mid = msg.get("id") if isinstance(msg, dict) else None
            if mid is None:
                # Server-initiated notification (e.g. log message, progress).
                # We don't currently forward these to specific claudes — they'd
                # need session-aware routing which most stateless MCPs don't use.
                continue
            with self.pending_lock:
                fut = self.pending.get(mid)
            if fut and not fut.done():
                fut.set_result(msg)
        # Subprocess exited
        if not self._stopped:
            print(f"[multiplex] {self.name} subprocess exited unexpectedly", flush=True)
            # Will be respawned on next request via the poll check in call()

    def _stderr_loop(self) -> None:
        if not self.proc or not self.proc.stderr:
            return
        for line in self.proc.stderr:
            # Keep stderr visible for debugging but tag it
            print(f"[multiplex:{self.name}:stderr] {line.rstrip()}", flush=True)


# ----- registry / startup / shutdown -----

def init_all() -> None:
    """Spawn every `shared: true` server from Serena's master config.
    Idempotent: if already running (PID file alive), reuses the existing
    subprocess instead of double-spawning."""
    from core.mcp.config import list_servers
    from concurrent.futures import ThreadPoolExecutor
    PIDFILE_DIR.mkdir(parents=True, exist_ok=True)
    _reap_dead_pidfiles()

    # Filter to the servers we'll actually spawn first, then fan out in
    # parallel. Sequential spawning was costing ~3-5s on boot for 8 MCPs.
    candidates = []
    for server in list_servers():
        if not server.get("enabled", True):
            continue
        if not server.get("shared", False):
            continue
        if (server.get("transport") or "stdio") != "stdio":
            continue
        if server["name"] in _REGISTRY:
            continue
        candidates.append(server)

    def _spawn_one(server: dict) -> tuple[str, SharedMCPProxy | None]:
        name = server["name"]
        existing_pid = _read_pid(name)
        if existing_pid and _pid_alive(existing_pid):
            try:
                os.kill(existing_pid, signal.SIGTERM)
                time.sleep(0.2)
                if _pid_alive(existing_pid):
                    os.kill(existing_pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            _clear_pid(name)
        try:
            proxy = SharedMCPProxy(
                name=name,
                command=server.get("command") or "",
                args=server.get("args") or [],
                env=server.get("env") or {},
                cwd=server.get("cwd"),
            )
            if proxy.proc is None:
                return name, None
            return name, proxy
        except Exception as e:
            print(f"[multiplex] init {name} failed: {e}", flush=True)
            return name, None

    with ThreadPoolExecutor(max_workers=min(8, len(candidates) or 1)) as ex:
        for name, proxy in ex.map(_spawn_one, candidates):
            if proxy is not None:
                with _REGISTRY_LOCK:
                    _REGISTRY[name] = proxy


def shutdown_all() -> None:
    """Stop every shared MCP. Called on Serena exit via atexit + signal handlers."""
    with _REGISTRY_LOCK:
        for name, proxy in list(_REGISTRY.items()):
            try:
                proxy.stop()
            except Exception as e:
                print(f"[multiplex] stop {name} failed: {e}", flush=True)
        _REGISTRY.clear()


def get_proxy(name: str) -> SharedMCPProxy | None:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(name)


def list_proxies() -> dict[str, dict]:
    """Status snapshot for /api/multiplex-status."""
    with _REGISTRY_LOCK:
        out = {}
        for name, p in _REGISTRY.items():
            alive = p.proc is not None and p.proc.poll() is None
            out[name] = {
                "alive": alive,
                "pid": p.proc.pid if p.proc else None,
                "inflight_requests": len(p.pending),
                "url": shared_url(name),
            }
        return out


# ----- helpers -----

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _write_pid(name: str, pid: int) -> None:
    PIDFILE_DIR.mkdir(parents=True, exist_ok=True)
    (PIDFILE_DIR / f"{name}.pid").write_text(str(pid))


def _read_pid(name: str) -> int | None:
    p = PIDFILE_DIR / f"{name}.pid"
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return None


def _clear_pid(name: str) -> None:
    p = PIDFILE_DIR / f"{name}.pid"
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def _reap_dead_pidfiles() -> None:
    """On boot, remove PID files whose process is dead. Prevents stale
    state from a crashed Serena."""
    if not PIDFILE_DIR.exists():
        return
    for f in PIDFILE_DIR.glob("*.pid"):
        try:
            pid = int(f.read_text().strip())
        except (OSError, ValueError):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            continue
        if not _pid_alive(pid):
            try:
                f.unlink()
            except FileNotFoundError:
                pass


def _error_response(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP server — runs on MULTIPLEX_PORT, exposes /mcp/<name> for each shared
# MCP. Stays on a FIXED port so claude's config URLs survive Serena restarts.
# ─────────────────────────────────────────────────────────────────────────────
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

_HTTP_SERVER: ThreadingHTTPServer | None = None
_HTTP_THREAD: threading.Thread | None = None


class _MultiplexHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # silence default request logging
        return

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/mcp/"):
            self._send_json(404, _error_response(None, -32601, f"not found: {path}"))
            return
        name = path[len("/mcp/"):].strip("/")
        proxy = get_proxy(name)
        if proxy is None:
            self._send_json(404, _error_response(None, -32601, f"no shared MCP named {name!r}"))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body_bytes = self.rfile.read(length) if length > 0 else b""
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, _error_response(None, -32700, f"invalid JSON: {e}"))
            return
        # Pass JSON-RPC request through to the shared subprocess
        try:
            response = proxy.call(body, timeout=120.0)
        except Exception as e:
            self._send_json(500, _error_response(body.get("id"), -32603, str(e)))
            return
        # Notifications return None — respond with 204 No Content per MCP spec
        if response is None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send_json(200, response)

    def do_GET(self):
        # Used by claude to detect MCP server availability (some clients ping
        # the URL with GET before opening a session).
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"ok": True, "servers": list(_REGISTRY.keys())})
            return
        if path.startswith("/mcp/"):
            name = path[len("/mcp/"):].strip("/")
            proxy = get_proxy(name)
            if proxy is None:
                self._send_json(404, _error_response(None, -32601, f"no shared MCP named {name!r}"))
                return
            self._send_json(200, {"ok": True, "name": name, "transport": "stdio-multiplexed"})
            return
        self._send_json(404, _error_response(None, -32601, "not found"))

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_http_server() -> bool:
    """Bind the multiplexer's HTTP listener on the fixed port. Returns True
    on success, False if the port is in use (caller can log + skip)."""
    global _HTTP_SERVER, _HTTP_THREAD
    if _HTTP_SERVER is not None:
        return True
    try:
        _HTTP_SERVER = ThreadingHTTPServer((MULTIPLEX_HOST, MULTIPLEX_PORT), _MultiplexHandler)
    except OSError as e:
        print(f"[multiplex] cannot bind {MULTIPLEX_HOST}:{MULTIPLEX_PORT}: {e}", flush=True)
        _HTTP_SERVER = None
        return False
    _HTTP_THREAD = threading.Thread(
        target=_HTTP_SERVER.serve_forever, name="mcp-multiplex-http", daemon=True
    )
    _HTTP_THREAD.start()
    print(f"[multiplex] HTTP listening on http://{MULTIPLEX_HOST}:{MULTIPLEX_PORT}", flush=True)
    return True


def stop_http_server() -> None:
    global _HTTP_SERVER
    if _HTTP_SERVER is not None:
        try:
            _HTTP_SERVER.shutdown()
        except Exception:
            pass
        _HTTP_SERVER = None


# Register cleanup hooks so the subprocesses don't outlive Serena
def _shutdown_everything():
    shutdown_all()
    stop_http_server()


atexit.register(_shutdown_everything)


def _on_signal(signum, frame):
    shutdown_all()
    raise SystemExit(0)


# Defer signal hooks until init_all() is called — we don't want to clobber
# them if multiplex.py is just imported without being used.
def install_signal_handlers() -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass  # Not main thread, skip
