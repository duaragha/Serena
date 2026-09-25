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
from core.computer_platform import ComputerError, Rect, X11Desktop, create_desktop
from core.computer_use import LIVE_STATES, ComputerController

GUIDANCE = (
    "When the user requests computer use in this chat, the agent may start "
    "that scoped session directly. No manual user terminal step is required. "
    "Use computer_start with its default background=true for live coaching. "
    "The worker uses Astra medium with fast processing and the exact launching "
    "chat's history plus a bounded local task pack from Serena knowledge and project runbooks. "
    "Prompt hooks supply completed advice on follow-up questions; "
    "computer_history is the fallback when hooks are unavailable. "
    "If that tool is not loaded, execute chats computer watch --detach "
    "for live guidance or chats computer run --detach for a GUI task. "
    "begin --mode watch also starts the watcher unless --interactive is explicit. "
    "Sharing-only sessions do not produce automatic observations. Pass the user's task, mode and intended "
    "target explicitly; active may be the terminal. Watch is observation "
    "only; control needs a specific requested GUI task. "
    "Control defaults to target=isolated: Serena's own desktop with its own mouse, keyboard, browser "
    "and terminal, so the user keeps working meanwhile. Use a window:ID/display:NAME target only when "
    "the task needs the user's own open windows. Physical input pauses a control session on that "
    "desktop; computer_resume continues it from a fresh screenshot."
)


def _isolated_x11(env):
    return X11Desktop(env, role="isolated")


def _clipboard_bridge(host, nested, viewer):
    from core.computer_clipboard import ClipboardBridge

    return ClipboardBridge(host, nested, viewer=viewer).start()


class ComputerServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(
        self,
        controller,
        *,
        directory=None,
        conversations=None,
        isolated=None,
        isolated_desktop=_isolated_x11,
        clipboard=_clipboard_bridge,
    ):
        self.controller = controller
        self.directory = directory or state_dir()
        self.conversations = controller.conversations = conversations
        self.token = secrets.token_urlsafe(32)
        self.operator_token = secrets.token_urlsafe(32)
        self.indicator_seen = 0.0
        self.visible_session = ""
        self.indicator_process = None
        # Serena's own nested desktop: its lifecycle, and the controller that
        # drives it once opened. Its sessions run alongside one on his screen.
        self.isolated_runtime = isolated
        self.isolated_desktop = isolated_desktop
        self.isolated = None
        self.isolated_lock = threading.Lock()
        self.isolated_indicator_process = None
        self.isolated_indicator_seen = 0.0
        self.isolated_visible_session = ""
        # His clipboard reaches her desktop; hers reaches his only from the viewer.
        self.clipboard_bridge = clipboard
        self.clipboard = None
        self.client_slots = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", 0), Handler)

    def controllers(self):
        return [c for c in (self.controller, self.isolated) if c is not None]

    def owner(self, session_id):
        for c in self.controllers():
            if session_id and c.session and c.session.id == session_id:
                return c
        raise ComputerError("computer session not found")

    def status(self):
        result = self.controller.status()
        isolated = self.isolated.status()["session"] if self.isolated else None
        sessions = [item for item in (result["session"], isolated) if item]
        live = [item for item in sessions if item["state"] in LIVE_STATES]
        # Old clients read one session: prefer whichever is still running.
        result["session"] = (live or sessions or [None])[0]
        result["sessions"] = sessions
        result["isolated_desktop"] = (
            self.isolated_runtime.status() if self.isolated_runtime else None
        )
        return result

    def dispatch(self, method, params, operator):
        c = self.controller
        if method == "status":
            return {
                **self.status(),
                "monitors": c.desktop.monitors(),
                "session_start": {"mcp_tool": "computer_start", "guidance": GUIDANCE},
            }
        if method == "indicator" and params.get("desk") == "isolated":
            # Her desktop's HUD lives on his screen, so it is never in her
            # screenshots and needs no mask.
            self.isolated_indicator_seen = time.monotonic()
            self.isolated_visible_session = str(params.get("visible_session", ""))
            return self.isolated.status() if self.isolated else {**c.status(), "session": None}
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
            source_id, source_agent = (
                params.pop("source_session_id", ""),
                params.pop("source_agent", ""),
            )
            origin = (
                self.conversations.resolve(source_id, source_agent) if self.conversations else None
            )
            if source_id and not origin:
                raise ComputerError("conversation linking is unavailable")
            if not isinstance(interactive, bool):
                raise ComputerError("interactive must be a boolean")
            if params.get("target") == "isolated":
                c = self.isolated_controller()
                params["target"] = "desktop"
            # Old chats used begin --mode watch for coaching. Honor that intent
            # while leaving internal one-shot captures and explicit sharing alone.
            start_agent = method == "run" or (
                params.get("mode") == "watch"
                and params.get("owner", "cli") in {"cli", "mcp"}
                and not interactive
            )
            c.begin(
                **params,
                operator_confirmed=True,
                source_session_id=origin["id"] if origin else "",
                source_agent=origin["agent"] if origin else "",
            )
            try:
                if self.conversations:
                    self.conversations.bind(c.session, origin)
                    c.session.context_message_count = len(self.conversations.messages(c.session.id))
            except Exception:
                c.stop("conversation context could not be loaded")
                raise
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
        if method in {"observe", "act", "next_frame"}:
            return getattr(self.owner(params.get("session_id")), method)(**params)
        if method == "events":
            # Every desk writes the same ordered stream; events carry session_id.
            return c.read_events(**params)
        if method == "stop":
            for item in self.controllers():
                item.stop(**params)
            return self.status()
        if method == "resume":
            if not operator:
                raise ComputerError("resume from the local operator surface")
            session_id = params.get("session_id") or ""
            if session_id:
                target = self.owner(session_id)
            else:
                paused = [
                    item
                    for item in self.controllers()
                    if item.session and item.session.state in {"paused", "resuming"}
                ]
                if not paused:
                    raise ComputerError("no paused computer session to resume")
                if len(paused) > 1:
                    raise ComputerError("two sessions are paused; pass session_id")
                target = paused[0]
            target.resume(session_id or None)
            return self.status()
        if method == "steer":
            session_id = params.get("session_id")
            running = (
                [self.owner(session_id)]
                if session_id
                else [
                    item
                    for item in self.controllers()
                    if item.agent and item.agent.thread and item.agent.thread.is_alive()
                ]
            )
            if not running or not running[0].agent:
                raise ComputerError("no running visual task to steer")
            if len(running) > 1:
                raise ComputerError("two visual tasks are running; pass session_id")
            return running[0].agent.steer(params["message"])
        if method == "history":
            if not self.conversations:
                raise ComputerError("conversation history is unavailable")
            return {"messages": self.conversations.messages(params["session_id"])}
        if method == "desktop":
            if not operator:
                raise ComputerError("manage serena's desktop from the local operator surface")
            return self.desktop_operation(**params)
        raise ComputerError("unknown computer operation")

    # -- Serena's own desktop ------------------------------------------------
    def desktop_operation(self, action="status", app="", url=None):
        runtime = self.isolated_runtime
        if runtime is None:
            raise ComputerError("serena's own desktop is unavailable in this helper")
        if action == "status":
            return runtime.status()
        if action == "open":
            self.isolated_controller()
            return runtime.status()
        if action == "close":
            with self.isolated_lock:
                self._drop_isolated("serena's desktop closed")
            return runtime.stop()
        if action == "show":
            self.isolated_controller()
            return runtime.show()
        if action == "hide":
            return runtime.hide()
        if action == "launch":
            if url is not None and not (
                isinstance(url, str) and url.startswith(("http://", "https://")) and len(url) <= 2048
            ):
                raise ComputerError("launch url must be one http(s) URL")
            self.isolated_controller()
            return runtime.launch(app, url)
        raise ComputerError("desktop action must be status, open, close, show, hide, or launch")

    def isolated_controller(self):
        """The controller for her own desktop, starting or adopting it on demand."""
        with self.isolated_lock:
            runtime = self.isolated_runtime
            if runtime is None:
                raise ComputerError("serena's own desktop is unavailable in this helper")
            if self.isolated is not None and runtime.running():
                return self.isolated
            self._drop_isolated("serena's desktop restarted")
            info = runtime.start()
            desktop = self.isolated_desktop(runtime.env(info))
            c = ComputerController(
                desktop,
                authority=self.controller.authority,
                clock=self.controller.clock,
                publish=self.controller.publish,
                bus=self.controller.bus,
                desk="isolated",
                launcher=runtime.launch,
            )
            c.conversations = self.conversations
            try:
                # Metacity there takes his GNOME keybindings, including this
                # stop chord; the host grab stops her sessions too.
                desktop.start_input_monitor(c.physical_input, c.stop, motion=False, shortcut=False)
                self.start_isolated_indicator(c)
            except Exception:
                with contextlib.suppress(Exception):
                    desktop.close()
                raise
            self.isolated = c
            self._start_clipboard(runtime, info)
            return c

    def _start_clipboard(self, runtime, info):
        if self.clipboard_bridge is None:
            return
        try:
            self.clipboard = self.clipboard_bridge(
                self.controller.desktop.env["DISPLAY"],
                info["display"],
                runtime.viewer_window,
            )
        except Exception as exc:
            # Pasting is a convenience; her desktop still works without it.
            self.clipboard = None
            self.controller.event("clipboard_unavailable", error=str(exc)[:200])

    def _drop_isolated(self, reason):
        bridge, self.clipboard = self.clipboard, None
        if bridge is not None:
            with contextlib.suppress(Exception):
                bridge.stop()
        c, self.isolated = self.isolated, None
        process, self.isolated_indicator_process = self.isolated_indicator_process, None
        if c is not None:
            with contextlib.suppress(Exception):
                c.stop(reason)
            c.shutdown.set()
            with contextlib.suppress(Exception):
                c.desktop.close()
        if process is not None:
            with contextlib.suppress(Exception):
                process.terminate()

    def close_isolated(self):
        """Release her controller but leave her desktop running for the next helper."""
        with self.isolated_lock:
            self._drop_isolated("computer service stopped")

    def start_isolated_indicator(self, controller):
        self.isolated_indicator_seen = 0.0
        self.isolated_visible_session = ""
        self.isolated_indicator_process = subprocess.Popen(
            [*child_command("indicator"), "--desk", "isolated"],
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
                    self.isolated_visible_session == _session.id
                    and time.monotonic() - self.isolated_indicator_seen < 1
                ):
                    return
                time.sleep(0.05)
            raise ComputerError("visible indicator for serena's desktop did not start")

        controller.indicator = require_indicator

    def start_indicator(self):
        # Match the compact HUD's initial footprint before its first heartbeat.
        self.controller.indicator_rect = Rect(20, 20, 500, 118)
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

    # A live HUD whose heartbeat is late is a stalled X server or session bus,
    # not a disconnected indicator; only a dead process ends the session at once.
    INDICATOR_STALE_SECONDS = 10.0
    # The lock probe is one gdbus call with a 1 s timeout every 0.5 s. A single
    # slow answer must not end a healthy session; a probe that keeps failing does.
    LOCK_PROBE_GRACE_SECONDS = 3.0

    def supervise(self):
        last_lock_check = {}
        lock_probe_failing_since = {}
        runtime_checked = 0.0
        while not self.controller.shutdown.wait(0.05):
            for desk, c in (("host", self.controller), ("isolated", self.isolated)):
                if c is not None:
                    self._supervise(desk, c, last_lock_check, lock_probe_failing_since)
            if self.isolated is not None and time.monotonic() - runtime_checked >= 1:
                runtime_checked = time.monotonic()
                if not self.isolated_runtime.running():
                    # Closing the viewer window closes her desktop.
                    with self.isolated_lock:
                        self._drop_isolated("serena's desktop was closed")

    def _supervise(self, desk, c, last_lock_check, lock_probe_failing_since):
        s = c.session
        if not s or s.state not in LIVE_STATES:
            lock_probe_failing_since[desk] = None
            return
        with c.lock:
            for identifier, frame in list(s.frames.items()):
                if time.time() >= frame["expires_at"]:
                    s.frames.pop(identifier, None)
        if desk == "host":
            process, seen = self.indicator_process, self.indicator_seen
        else:
            process, seen = self.isolated_indicator_process, self.isolated_indicator_seen
        if time.time() >= s.expires_at:
            c.stop("session expired")
        elif process and (
            process.poll() is not None
            # begin waits for its first acknowledgement before capture.
            # An initial zero heartbeat is not a disconnected indicator.
            or (seen and time.monotonic() - seen > self.INDICATOR_STALE_SECONDS)
        ):
            c.stop("visible indicator disconnected")
        elif c.authority.lock_state()["engaged"]:
            c.stop("Serena emergency stop")
        elif time.monotonic() - last_lock_check.get(desk, 0.0) >= 0.5:
            last_lock_check[desk] = time.monotonic()
            try:
                if c.desktop.locked():
                    c.stop("desktop locked")
                lock_probe_failing_since[desk] = None
            except ComputerError:
                since = lock_probe_failing_since.get(desk) or time.monotonic()
                lock_probe_failing_since[desk] = since
                if time.monotonic() - since >= self.LOCK_PROBE_GRACE_SECONDS:
                    c.stop("desktop lock state unavailable")
        c.settle_resume()


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
            "timing",
            "by_agent",
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
    from core.computer_conversation import ConversationStore
    from core.computer_nested import IsolatedDesktop

    server = ComputerServer(
        controller,
        directory=directory,
        conversations=ConversationStore(directory),
        isolated=IsolatedDesktop(
            directory / "isolated", host_env=desktop.env, host_monitors=desktop.monitors
        ),
    )
    info = {
        "port": server.server_port,
        "pid": os.getpid(),
        "token": server.token,
        "operator_token": server.operator_token,
        "version": 1,
    }
    discovery = directory / "service.json"
    temporary = directory / "service.json.tmp"
    temporary.write_text(json.dumps(info), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(discovery)

    def stopping(_signum, _frame):
        for item in server.controllers():
            item.stop("computer service shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stopping)
    try:
        desktop.start_input_monitor(
            controller.physical_input,
            controller.stop,
            # The stop chord is global: it also ends a task on her own desktop.
            on_shortcut=lambda reason: [item.stop(reason) for item in server.controllers()],
        )
        server.start_indicator()
        threading.Thread(target=server.supervise, daemon=True).start()
        print(f"computer service ready: {desktop.name}, pid={os.getpid()}", flush=True)
        server.serve_forever(poll_interval=0.1)
    finally:
        server.close_isolated()
        controller.close()
        if server.indicator_process:
            server.indicator_process.terminate()
        server.server_close()
        discovery.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    serve()
