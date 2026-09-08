"""One desktop owner, bounded observation sessions, and audited input batches."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from core.action_authority import BASIS_GRANT, build_request, default_authority
from core.computer_platform import ComputerError, ComputerTransientError, Rect
from core.visual_context import VisualPolicy

MAX_ACTIONS = 12
MAX_SESSION_SECONDS = 1800
MAX_FRAME_AGE = 45


@dataclass
class Session:
    id: str
    mode: str
    target: str
    request: str
    expires_at: float
    owner: str
    grant_id: str = ""
    state: str = "active"
    reason: str = ""
    frames: OrderedDict = field(default_factory=OrderedDict)
    results: OrderedDict = field(default_factory=OrderedDict)
    cancelled: threading.Event = field(default_factory=threading.Event)
    last_signature: str = ""
    last_inspected_at: float | None = None
    observation: str = ""
    driver: str = "connected_chat"
    observation_state: str = "ready"
    source_session_id: str = ""
    source_agent: str = ""
    context_message_count: int = 0
    action_deadline: float = 0


def number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ComputerError(f"{name} must be a finite number")
    if not minimum <= value <= maximum:
        raise ComputerError(f"{name} must be between {minimum} and {maximum}")
    return value


class ComputerController:
    def __init__(self, desktop, *, authority=None, clock=time.time, indicator=None, publish=None):
        self.desktop = desktop
        self.authority = authority or default_authority()
        self.clock = clock
        self.indicator = indicator
        self.publish = publish
        self.lock = threading.RLock()
        self.capture_lock = threading.Lock()
        self.action_lock = threading.Lock()
        self.session = None
        self.events = deque(maxlen=256)
        self.sequence = 0
        self.condition = threading.Condition(self.lock)
        self.shutdown = threading.Event()
        self.policy = VisualPolicy()
        self.agent = None
        self.conversations = None
        self.indicator_rect = None

    def event(self, kind, **data):
        with self.condition:
            self.sequence += 1
            event = {"id": self.sequence, "type": kind, "at": self.clock(), **data}
            if self.conversations:
                self.conversations.record(event)
            self.events.append(event)
            self.condition.notify_all()
        if self.publish and kind not in {"frame", "delta"}:
            self.publish(event)
        return event

    def status(self):
        with self.lock:
            s = self.session
            info = (
                None
                if s is None
                else {
                    "id": s.id,
                    "request": s.request,
                    "owner": s.owner,
                    "mode": s.mode,
                    "target": s.target,
                    "state": s.state,
                    "reason": s.reason,
                    "expires_at": s.expires_at,
                    "last_inspected_at": s.last_inspected_at,
                    "observation": s.observation,
                    "driver": s.driver,
                    "observation_state": s.observation_state,
                    "source_session_id": s.source_session_id or None,
                    "source_agent": s.source_agent or None,
                    "context_message_count": s.context_message_count,
                    "service_tier": "fast" if s.driver == "astra" else None,
                    "latest_frame": next(reversed(s.frames), None),
                }
            )
        return {
            "ok": True,
            "backend": self.desktop.name,
            "session": info,
            "model": "gpt-6-astra",
            "stop_shortcut": "Ctrl+Alt+Shift+Escape",
            "event_id": self.sequence,
        }

    def begin(
        self,
        *,
        mode,
        target,
        request,
        seconds=300,
        owner="cli",
        operator_confirmed=False,
        source_session_id="",
        source_agent="",
    ):
        if not operator_confirmed:
            raise ComputerError(
                "start a scoped session from the CLI or a verified Serena user turn"
            )
        if mode not in {"watch", "control"}:
            raise ComputerError("mode must be watch or control")
        number(seconds, "seconds", 1, MAX_SESSION_SECONDS)
        if not isinstance(request, str) or not request.strip() or len(request) > 4000:
            raise ComputerError("a session needs the user's bounded task description")
        if self.authority.lock_state()["engaged"]:
            raise ComputerError("Serena's emergency stop is engaged")
        if self.desktop.locked():
            raise ComputerError("desktop is locked")
        if target == "active":
            window_id = self.desktop.active_window()
            if not window_id:
                raise ComputerError("no active window; choose a display explicitly")
            target = "window:" + window_id
        self.geometry(target)
        with self.lock:
            if self.session and self.session.state == "active":
                raise ComputerError("a computer session already owns this desktop; stop it first")
            if self.action_lock.locked() or (
                self.agent and self.agent.thread and self.agent.thread.is_alive()
            ):
                raise ComputerError("the previous session is still releasing input; retry shortly")
            grant = None
            if mode == "control":
                grant = self.authority.issue_grant(
                    capabilities=["computer.input"],
                    reason=request,
                    max_tier=2,
                    ttl_seconds=seconds,
                    uses=50,
                    operator_confirmed=True,
                )
            s = Session(
                uuid.uuid4().hex,
                mode,
                target,
                request.strip(),
                self.clock() + seconds,
                owner,
                grant_id=grant.grant_id if grant else "",
                source_session_id=source_session_id,
                source_agent=source_agent,
            )
            self.session = s
        try:
            if self.indicator:
                self.indicator(s)
            for attempt in range(3):
                try:
                    self.observe(s.id)
                    break
                except ComputerTransientError:
                    if attempt == 2:
                        raise
                    if s.cancelled.wait(0.1):
                        raise ComputerError("session stopped") from None
        except Exception:
            self.stop("session startup failed")
            raise
        self.event("started", session_id=s.id, mode=mode, target=target, expires_at=s.expires_at)
        return self.status()

    def current(self, session_id):
        s = self.session
        if not s or s.id != session_id:
            raise ComputerError("computer session not found")
        if s.state != "active" or s.cancelled.is_set():
            raise ComputerError(f"computer session {s.state}: {s.reason}")
        if self.clock() >= s.expires_at:
            self.stop("session expired")
            raise ComputerError("computer session expired")
        return s

    def stop(self, reason="stopped by user", session_id=None):
        with self.lock:
            s = self.session
            if session_id and (not s or s.id != session_id):
                return self.status()
            if not s or s.state != "active":
                return self.status()
            # Cancellation is visible to the executor before waiting for any I/O.
            s.cancelled.set()
            s.state = "stopped"
            s.reason = str(reason)[:300]
            s.frames.clear()
        self.desktop.release()
        if s.grant_id:
            self.authority.revoke_grant(s.grant_id, reason=reason)
        self.event("stopped", session_id=s.id, reason=s.reason)
        if self.agent:
            self.agent.cancel()
        return self.status()

    def physical_input(self):
        s = self.session
        if s and s.state == "active" and s.mode == "control":
            self.stop("you took over with the mouse or keyboard")

    def geometry(self, target):
        monitors = self.desktop.monitors()
        rects = [Rect(**item["rect"]) for item in monitors]
        if target == "desktop":
            x, y = min(r.x for r in rects), min(r.y for r in rects)
            rect = Rect(
                x, y, max(r.x + r.width for r in rects) - x, max(r.y + r.height for r in rects) - y
            )
        elif target.startswith("display:"):
            found = next((m for m in monitors if m["name"] == target[8:]), None)
            if not found:
                raise ComputerError("selected display is disconnected")
            rect = Rect(**found["rect"])
        elif target.startswith("window:") and target[7:].isdigit():
            info = self.desktop.window_info(target[7:])
            active = self.desktop.active_window()
            if active != target[7:] and self._window_allowed(active, target[7:]):
                info = self.desktop.window_info(active)
            if not info["visible"]:
                raise ComputerError("selected window is not visible")
            rect = Rect(**info["rect"])
        else:
            raise ComputerError("target must be active, desktop, display:NAME, or window:ID")
        signature = hashlib.sha256(
            json.dumps([monitors, rect.to_dict()], sort_keys=True).encode()
        ).hexdigest()
        return rect, rects, signature

    def _window_allowed(self, active, target):
        if active == target:
            return True
        check = getattr(self.desktop, "window_in_scope", None)
        return bool(check and check(active, target))

    def _assert_public(self, context):
        app = context.get("app", "").casefold()
        title = context.get("title", "")
        if any(name in app for name in self.policy.private_apps) or any(
            re.search(pattern, title, re.IGNORECASE)
            for pattern in self.policy.private_title_patterns
        ):
            raise ComputerError("a private application is visible; capture is paused")

    def _assert_public_region(self, rect):
        for window in self.desktop.visible_windows():
            other = Rect(**window["rect"])
            if (
                rect.x < other.x + other.width
                and other.x < rect.x + rect.width
                and rect.y < other.y + other.height
                and other.y < rect.y + rect.height
            ):
                self._assert_public(window)

    def observe(self, session_id, *, max_width=1920):
        from PIL import Image, ImageDraw

        number(max_width, "max_width", 640, 2560)
        with self.capture_lock:
            s = self.current(session_id)
            if self.desktop.locked():
                self.stop("desktop locked")
                raise ComputerError("desktop locked")
            rect, _, geometry = self.geometry(s.target)
            context = self.desktop.context()
            self._assert_public(context)
            self._assert_public_region(rect)
            if s.target.startswith("window:"):
                self._assert_public(self.desktop.window_info(s.target[7:]))
                if not self._window_allowed(context.get("id"), s.target[7:]):
                    raise ComputerError(
                        "selected window is not foreground; bring it forward before capture"
                    )
            started = time.perf_counter()
            image = self.desktop.capture(rect)
            try:
                captured_at = self.clock()
                capture_ms = round((time.perf_counter() - started) * 1000, 2)
                # Recheck before pixels can leave the helper after focus/geometry changes.
                if (
                    self.desktop.context().get("id") != context.get("id")
                    or self.geometry(s.target)[2] != geometry
                ):
                    raise ComputerTransientError(
                        "desktop changed while capturing; request a fresh frame"
                    )
                self._assert_public_region(rect)
                if self.indicator_rect:
                    masked = self.indicator_rect
                    ImageDraw.Draw(image).rectangle(
                        (
                            masked.x - rect.x,
                            masked.y - rect.y,
                            masked.x + masked.width - rect.x,
                            masked.y + masked.height - rect.y,
                        ),
                        fill=(33, 27, 41),
                    )
                image.thumbnail((int(max_width), 1600), Image.Resampling.LANCZOS)
                if image.mode != "RGB":
                    image = image.convert("RGB")
                tiny = image.resize((320, 200)).convert("L")
                signature = hashlib.sha256(tiny.tobytes()).hexdigest()
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=90, subsampling=0)
                data = buffer.getvalue()
                frame = {
                    "frame_id": uuid.uuid4().hex,
                    "session_id": s.id,
                    "captured_at": captured_at,
                    "expires_at": min(captured_at + MAX_FRAME_AGE, s.expires_at),
                    "rect": rect.to_dict(),
                    "width": image.width,
                    "height": image.height,
                    "geometry": geometry,
                    "context": context,
                    "indicator_rect": self.indicator_rect.to_dict()
                    if self.indicator_rect
                    else None,
                    "capture_ms": capture_ms,
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(data).decode(),
                    "signature": signature,
                }
            finally:
                image.close()
            with self.lock:
                self.current(session_id)
                s.frames[frame["frame_id"]] = frame
                while len(s.frames) > 4:
                    s.frames.popitem(last=False)
            return frame

    def frame(self, session_id, frame_id):
        s = self.current(session_id)
        value = s.frames.get(frame_id)
        if not value or self.clock() >= value["expires_at"]:
            raise ComputerError("frame is expired; observe again before acting")
        return value

    def next_frame(self, session_id, after_signature="", timeout=10):
        number(timeout, "timeout", 0, 20)
        deadline = time.monotonic() + timeout
        while True:
            frame = self.observe(session_id)
            if frame["signature"] != after_signature:
                return frame
            if time.monotonic() >= deadline:
                return {"unchanged": True, "session_id": session_id, "signature": after_signature}
            if self.current(session_id).cancelled.wait(0.5):
                raise ComputerError("session stopped")

    def read_events(self, after=0, timeout=0):
        number(timeout, "timeout", 0, 20)
        with self.condition:
            if self.sequence <= after and timeout:
                self.condition.wait(timeout)
            return {"events": [e for e in self.events if e["id"] > after], "last_id": self.sequence}

    def _point(self, frame, x, y, monitors):
        number(x, "x", 0, frame["width"] - 1)
        number(y, "y", 0, frame["height"] - 1)
        rect = Rect(**frame["rect"])
        px = rect.x + round(x * rect.width / frame["width"])
        py = rect.y + round(y * rect.height / frame["height"])
        if not rect.contains(px, py) or not any(r.contains(px, py) for r in monitors):
            raise ComputerError("point falls outside the selected display/window")
        return px, py

    def _validate_actions(self, actions, frame, monitors):
        if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
            raise ComputerError(f"an action batch must contain 1–{MAX_ACTIONS} actions")
        allowed = {
            "move",
            "click",
            "double_click",
            "drag",
            "scroll",
            "keypress",
            "type",
            "wait",
            "screenshot",
        }
        for a in actions:
            if not isinstance(a, dict) or a.get("type") not in allowed:
                raise ComputerError("unknown computer action")
            kind = a["type"]
            fields = {
                "type",
                "x",
                "y",
                "button",
                "keys",
                "path",
                "scroll_x",
                "scroll_y",
                "text",
                "seconds",
            }
            if set(a) - fields:
                raise ComputerError("unexpected action fields")
            if kind in {"move", "click", "double_click", "scroll"}:
                self._point(frame, a.get("x"), a.get("y"), monitors)
            if kind in {"click", "double_click", "drag"} and a.get("button", "left") not in {
                "left",
                "right",
                "middle",
            }:
                raise ComputerError("unsupported mouse button")
            if kind == "drag":
                path = a.get("path")
                if not isinstance(path, list) or not 2 <= len(path) <= 60:
                    raise ComputerError("drag needs 2–60 points")
                for p in path:
                    if not isinstance(p, dict) or set(p) != {"x", "y"}:
                        raise ComputerError("invalid drag point")
                    self._point(frame, p["x"], p["y"], monitors)
            if kind == "keypress":
                keys = a.get("keys")
                if (
                    not isinstance(keys, list)
                    or not 1 <= len(keys) <= 5
                    or any(
                        not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,24}", k)
                        for k in keys
                    )
                ):
                    raise ComputerError("keypress needs 1–5 named keys")
            if kind == "type" and (
                not isinstance(a.get("text"), str) or not 1 <= len(a["text"]) <= 2000
            ):
                raise ComputerError("type needs 1–2000 characters")
            if kind == "wait":
                number(a.get("seconds", 0.5), "seconds", 0, 3)
            if kind == "scroll":
                for axis in ("scroll_x", "scroll_y"):
                    number(a.get(axis, 0), axis, -1200, 1200)
        text = "".join(a.get("text", "") for a in actions if a["type"] == "type")
        if len(text) > 500 or sum(ord(char) > 127 for char in text) > 100 or "\x00" in text:
            raise ComputerError(
                "split text into batches of at most 500 characters / 100 non-ASCII characters; NUL is unsupported"
            )

    def act(self, session_id, frame_id, actions, *, request_id, intent, confirmation_id=""):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
            raise ComputerError("supply a unique request_id (8–80 letters/digits/dashes)")
        if not isinstance(intent, str) or not intent.strip() or len(intent) > 500:
            raise ComputerError("describe the intended effect of this batch")
        if not self.action_lock.acquire(blocking=False):
            raise ComputerError("another action batch is running")
        try:
            s = self.current(session_id)
            digest = hashlib.sha256(
                json.dumps([frame_id, actions, intent], sort_keys=True).encode()
            ).hexdigest()
            if request_id in s.results:
                old = s.results[request_id]
                if old["digest"] != digest:
                    raise ComputerError("request_id was reused with different actions")
                return {**old["result"], "replayed": True}
            if s.mode != "control":
                raise ComputerError("this session can watch but cannot send input")
            frame = self.frame(session_id, frame_id)
            _, monitors, geometry = self.geometry(s.target)
            if geometry != frame["geometry"]:
                raise ComputerError("display/window geometry changed; observe again")
            if self.desktop.context().get("id") != frame["context"].get("id"):
                raise ComputerError("foreground changed; observe again")
            self._validate_actions(actions, frame, monitors)
            authorization = build_request(
                capability="computer.input",
                intent=f"{s.request}; {intent}",
                source="automation",
                target=s.target,
                effect="external",
                session_id=s.id,
                authorization_basis=BASIS_GRANT,
                grant_id=s.grant_id,
                confirmation_id=confirmation_id,
            )
            decision = self.authority.authorize(authorization)
            if not decision.allowed:
                raise ComputerError(decision.reason)
            receipt = {
                "ok": False,
                "status": "running",
                "actions_executed": 0,
                "request_id": request_id,
                "action_receipt_id": authorization.request_id,
            }
            s.results[request_id] = {"digest": digest, "result": receipt}
            while len(s.results) > 100:
                s.results.popitem(last=False)
            self.event("action_started", session_id=s.id, request_id=request_id, intent=intent)
            s.action_deadline = time.monotonic() + 15
            try:
                for action in actions:
                    self.current(session_id)
                    if self._input_cancelled(s):
                        raise ComputerError(
                            "input batch deadline reached; inspect the partial result"
                        )
                    if self.authority.lock_state()["engaged"] or self.desktop.locked():
                        raise ComputerError("input stopped by lock")
                    if self.geometry(s.target)[2] != geometry:
                        raise ComputerError("target moved during the batch")
                    if self.desktop.context().get("id") != frame["context"].get("id"):
                        raise ComputerError(
                            "foreground changed during the batch; inspect before continuing"
                        )
                    self._execute(s, action, frame, monitors)
                    receipt["actions_executed"] += 1
                receipt.update(ok=True, status="executed")
            except Exception as exc:
                receipt.update(
                    status="partial" if receipt["actions_executed"] else "failed",
                    error=str(exc),
                    outcome_uncertain=True,
                )
            finally:
                self.desktop.release()
                self.authority.record_outcome(
                    authorization.request_id,
                    status="completed" if receipt["ok"] else "failed",
                    detail="input dispatched; visual outcome still requires inspection"
                    if receipt["ok"]
                    else receipt["error"],
                    receipt={k: v for k, v in receipt.items() if k != "data"},
                )
            if not s.cancelled.is_set():
                try:
                    post_frame = self.observe(session_id)
                except Exception as exc:
                    receipt["verification_error"] = str(exc)
                    post_frame = None
            else:
                post_frame = None
            self.event(
                "action_finished",
                session_id=s.id,
                **{k: v for k, v in receipt.items() if k != "frame"},
            )
            return {**receipt, **({"frame": post_frame} if post_frame else {})}
        finally:
            self.action_lock.release()

    def _execute(self, s, a, frame, monitors):
        kind = a["type"]
        if kind in {"move", "click", "double_click", "scroll"}:
            self.desktop.move(*self._point(frame, a["x"], a["y"], monitors))
        if kind in {"click", "double_click"}:
            button = {"left": 1, "middle": 2, "right": 3}[a.get("button", "left")]
            for _ in range(2 if kind == "double_click" else 1):
                if s.cancelled.is_set():
                    raise ComputerError("input cancelled")
                self.desktop.button(button, True)
                self.desktop.button(button, False)
                if kind == "double_click":
                    s.cancelled.wait(0.07)
        elif kind == "scroll":
            for axis, negative, positive in (("scroll_y", 4, 5), ("scroll_x", 6, 7)):
                value = a.get(axis, 0)
                for _ in range(math.ceil(abs(value) / 100)):
                    if s.cancelled.is_set():
                        raise ComputerError("input cancelled")
                    button = positive if value > 0 else negative
                    self.desktop.button(button, True)
                    self.desktop.button(button, False)
        elif kind == "drag":
            path = a["path"]
            self.desktop.move(*self._point(frame, **path[0], monitors=monitors))
            button = {"left": 1, "middle": 2, "right": 3}[a.get("button", "left")]
            self.desktop.button(button, True)
            try:
                for p in path[1:]:
                    if s.cancelled.wait(0.02) or self._input_cancelled(s):
                        raise ComputerError("drag cancelled")
                    self.desktop.move(*self._point(frame, **p, monitors=monitors))
            finally:
                self.desktop.button(button, False)
        elif kind == "keypress":
            try:
                for key in a["keys"]:
                    if s.cancelled.is_set():
                        raise ComputerError("keypress cancelled")
                    self.desktop.key(key, True)
            finally:
                self.desktop.release()
        elif kind == "type":
            self.desktop.type_text(a["text"], lambda: self._input_cancelled(s))
        elif kind == "wait" and s.cancelled.wait(a.get("seconds", 0.5)):
            raise ComputerError("wait cancelled")

    @staticmethod
    def _input_cancelled(s):
        return s.cancelled.is_set() or (
            s.action_deadline > 0 and time.monotonic() >= s.action_deadline
        )

    def close(self):
        self.stop("computer service stopped")
        self.shutdown.set()
        self.desktop.close()


def frame_content(frame):
    """MCP images remain images through the resident Codex adapter."""
    metadata = {k: v for k, v in frame.items() if k != "data"}
    content = [{"type": "text", "text": json.dumps(metadata, separators=(",", ":"))}]
    if frame.get("data"):
        content.append({"type": "image", "mimeType": frame["media_type"], "data": frame["data"]})
    return {"content": content}
