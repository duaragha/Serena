"""The desktop helper runs once inside the user's graphical login."""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.computer_client import child_command, state_dir
from core.computer_platform import ComputerError, Rect, create_desktop
from core.computer_use import ComputerController


class ComputerServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, controller, *, directory=None):
        self.controller = controller
        self.directory = directory or state_dir()
        self.token = secrets.token_urlsafe(32)
        self.operator_token = secrets.token_urlsafe(32)
        self.indicator_seen = 0.0
        self.visible_session = ""
        self.indicator_process = None
        self.client_slots = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", 0), Handler)

    def dispatch(self, method, params, operator):
        c = self.controller
        if method == "status":
            return {
                **c.status(),
                "monitors": c.desktop.monitors(),
                "session_start": {
                    "mcp_tool": "computer_start",
                    "guidance": (
                        "When the user requests computer use in this chat, the agent may start "
                        "that scoped session directly. No manual user terminal step is required. "
                        "Use computer_start with its default background=true for live coaching. "
                        "If that tool is not loaded, execute chats computer watch --detach "
                        "for live guidance or chats computer run --detach for a GUI task. "
                        "begin --mode watch also starts the watcher unless --interactive is explicit. "
                        "Sharing-only sessions do not produce automatic observations. Pass the user's task, mode and intended "
                        "target explicitly; active may be the terminal. Watch is observation "
                        "only; control needs a specific requested GUI task."
                    ),
                },
            }
        if method == "indicator":
            if "rect" in params:
                value = params["rect"]
                if (
                    not isinstance(value, dict)
                    or set(value) != {"x", "y", "width", "height"}
                    or any(type(v) is not int for v in value.values())
                    or not 1 <= value["width"] <= 4096
                    or not 1 <= value["height"] <= 4096
                    or abs(value["x"]) > 65536
                    or abs(value["y"]) > 65536
                ):
                    raise ComputerError("invalid indicator rectangle")
                # Match the mask to the popup without changing it mid-capture.
                with c.capture_lock:
                    c.indicator_rect = Rect(**value)
            self.indicator_seen = time.monotonic()
            self.visible_session = str(params.get("visible_session", ""))
            return c.status()
        if method in {"begin", "run"}:
            if not operator:
                raise ComputerError(
                    "a computer session must be opened by the local operator surface"
                )
            params = dict(params)
            speak = params.pop("speak", False)
            interactive = params.pop("interactive", False)
            if not isinstance(interactive, bool):
                raise ComputerError("interactive must be a boolean")
            # Old chats used begin --mode watch for coaching. Honor that intent
            # while leaving internal one-shot captures and explicit sharing alone.
            start_agent = method == "run" or (
                params.get("mode") == "watch"
                and params.get("owner", "cli") in {"cli", "mcp"}
                and not interactive
            )
            c.begin(**params, operator_confirmed=True)
            if start_agent:
                from core.computer_agent import ComputerAgent

                c.session.driver = "astra"
                c.session.observation_state = "starting"
                try:
                    c.agent = ComputerAgent(c, speak=speak)
                    c.agent.start()
                except Exception:
                    c.stop("visual worker failed to start")
                    raise
            return c.status()
        if method == "observe":
            return c.observe(**params)
        if method == "act":
            return c.act(**params)
        if method == "next_frame":
            return c.next_frame(**params)
        if method == "events":
            return c.read_events(**params)
        if method == "stop":
            return c.stop(**params)
        if method == "steer":
            if not c.agent:
                raise ComputerError("no running visual task to steer")
            return c.agent.steer(params["message"])
        raise ComputerError("unknown computer operation")

    def start_indicator(self):
        self.controller.indicator_rect = Rect(20, 20, 430, 112)
        self.indicator_process = subprocess.Popen(
            child_command("indicator"),
            cwd=Path(__file__).resolve().parents[1],
            env=self.controller.desktop.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def require_indicator(_session):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if (
                    self.visible_session == _session.id
                    and time.monotonic() - self.indicator_seen < 1
                ):
                    return
                time.sleep(0.05)
            raise ComputerError("visible computer-use indicator did not start")

        self.controller.indicator = require_indicator

    def supervise(self):
        last_lock_check = 0.0
        while not self.controller.shutdown.wait(0.05):
            s = self.controller.session
            if s and s.state == "active":
                with self.controller.lock:
                    for identifier, frame in list(s.frames.items()):
                        if time.time() >= frame["expires_at"]:
                            s.frames.pop(identifier, None)
                if time.time() >= s.expires_at:
                    self.controller.stop("session expired")
                elif self.indicator_process and (
                    self.indicator_process.poll() is not None
                    # begin waits for its first acknowledgement before capture.
                    # An initial zero heartbeat is not a disconnected indicator.
                    or (self.indicator_seen and time.monotonic() - self.indicator_seen > 3)
                ):
                    self.controller.stop("visible indicator disconnected")
                elif self.controller.authority.lock_state()["engaged"]:
                    self.controller.stop("Serena emergency stop")
                elif time.monotonic() - last_lock_check >= 0.5:
                    last_lock_check = time.monotonic()
                    try:
                        if self.controller.desktop.locked():
                            self.controller.stop("desktop locked")
                    except ComputerError:
                        self.controller.stop("desktop lock state unavailable")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(25)

    def do_POST(self):
        server = self.server
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        operator = hmac.compare_digest(supplied, server.operator_token)
        if (
            self.path != "/rpc"
            or self.headers.get("Origin")
            or not (operator or hmac.compare_digest(supplied, server.token))
        ):
            self.send_error(403)
            return
        if not server.client_slots.acquire(blocking=False):
            self.send_error(503)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 64 * 1024:
                raise ComputerError("request too large or empty")
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict) or not isinstance(value.get("params", {}), dict):
                raise ComputerError("invalid request")
            result = server.dispatch(value.get("method"), value.get("params", {}), operator)
            payload = {"ok": True, "result": result}
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)[:500]}
        finally:
            server.client_slots.release()
        data = json.dumps(payload, allow_nan=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(data)


def serve():
    if os.name == "nt":
        raise ComputerError("computer use currently requires the Linux X11 desktop service")
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    # Holding the lock protects the physical desktop even with multiple MCP clients.
    import fcntl

    lock = (directory / "service.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("computer service is already running") from None
    desktop = create_desktop()
    from core.control_plane import ControlPlaneStore

    ledger = ControlPlaneStore()

    def publish(event):
        # Keep lifecycle/latency evidence, not screen text, typed text, or pixels.
        allowed = {
            "mode",
            "target",
            "reason",
            "model",
            "model_ms",
            "request_id",
            "ok",
            "status",
            "actions_executed",
        }
        with contextlib.suppress(Exception):
            ledger.append_event(
                surface="action",
                event_type="computer." + event["type"],
                lifecycle_state=event["type"],
                session_id=event.get("session_id"),
                payload={k: v for k, v in event.items() if k in allowed},
            )

    controller = ComputerController(desktop, publish=publish)
    server = ComputerServer(controller, directory=directory)
    info = {
        "port": server.server_port,
        "pid": os.getpid(),
        "token": server.token,
        "operator_token": server.operator_token,
        "version": 1,
    }
    discovery = directory / "service.json"
    temporary = directory / "service.json.tmp"
    temporary.write_text(json.dumps(info))
    temporary.chmod(0o600)
    temporary.replace(discovery)

    def stopping(_signum, _frame):
        controller.stop("computer service shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopping)
    try:
        desktop.start_input_monitor(controller.physical_input, controller.stop)
        server.start_indicator()
        threading.Thread(target=server.supervise, daemon=True).start()
        print(f"computer service ready: {desktop.name}, pid={os.getpid()}", flush=True)
        server.serve_forever(poll_interval=0.1)
    finally:
        controller.close()
        if server.indicator_process:
            server.indicator_process.terminate()
        server.server_close()
        discovery.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    serve()
