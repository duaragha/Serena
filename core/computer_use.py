"""One desktop owner, bounded observation sessions, and audited input batches."""

from __future__ import annotations

import base64
import contextlib
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
from core.computer_claude import computer_model
from core.computer_platform import ComputerError, ComputerPaused, ComputerTransientError, Rect
from core.visual_context import VisualPolicy

MAX_ACTIONS = 12
MAX_SESSION_SECONDS = 1800
MAX_FRAME_AGE = 45
# After resume, input waits until his hands have been off for this long: the
# click on the resume button is itself physical input.
RESUME_QUIET_SECONDS = 0.8
# A post-action screenshot waits for the UI to react and stop painting, so the
# model rarely spends a whole turn discovering that nothing had rendered yet.
POST_ACTION_SETTLE_SECONDS = 1.0
POST_ACTION_QUIET_SECONDS = 0.45
LIVE_STATES = frozenset({"active", "paused", "resuming"})


class EventBus:
    """One ordered event stream shared by every desktop the helper owns."""

    def __init__(self):
        self.condition = threading.Condition(threading.RLock())
        self.events = deque(maxlen=256)
        self.sequence = 0


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
    observation_preview: str = ""
    inspection_started_at: float | None = None
    last_model_ms: int | None = None
    driver: str = "connected_chat"
    observation_state: str = "ready"
    source_session_id: str = ""
    source_agent: str = ""
    context_message_count: int = 0
    action_deadline: float = 0
    browser_checks: dict | None = None
    desk: str = "host"
    paused_reason: str = ""
    paused_by_agent: bool = False
    pauses: int = 0
    resume_requested_at: float | None = None
    last_physical_at: float = 0
    input_hold: threading.Event = field(default_factory=threading.Event)
    last_timing: dict | None = None
    frame_delivered_at: float = 0
    # The visual worker's model sees frames at this width; see computer_claude.
    frame_width: int = 1920
    worker_model: str = ""
    worker_effort: str = ""


def number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ComputerError(f"{name} must be a finite number")
    if not minimum <= value <= maximum:
        raise ComputerError(f"{name} must be between {minimum} and {maximum}")
    return value


class ComputerController:
    def __init__(
        self,
        desktop,
        *,
        authority=None,
        clock=time.time,
        indicator=None,
        publish=None,
        bus=None,
        desk="host",
        launcher=None,
    ):
        self.desktop = desktop
        self.authority = authority or default_authority()
        self.clock = clock
        self.indicator = indicator
        self.publish = publish
        self.bus = bus or EventBus()
        # "host" is his screen; "isolated" is Serena's own nested desktop.
        self.desk = desk
        # Opens an allow-listed app on her own desktop; None on his screen.
        self.launcher = launcher
        # Her browser read as text and driven by element, and her shell; both
        # exist only on her own desktop (see computer_web / computer_shell).
        self.web = None
        self.terminal = None
        self.lock = threading.RLock()
        self.capture_lock = threading.Lock()
        self.action_lock = threading.Lock()
        self.session = None
        self.shutdown = threading.Event()
        self.policy = VisualPolicy()
        self.agent = None
        self.conversations = None
        self.indicator_rect = None

    @property
    def events(self):
        return self.bus.events

    @property
    def sequence(self):
        return self.bus.sequence

    @property
    def condition(self):
        return self.bus.condition

    def event(self, kind, **data):
        bus = self.bus
        with bus.condition:
            bus.sequence += 1
            event = {"id": bus.sequence, "type": kind, "at": self.clock(), **data}
            if self.conversations:
                self.conversations.record(event)
            bus.events.append(event)
            bus.condition.notify_all()
        if self.publish and kind not in {"frame", "delta"}:
            self.publish(event)
        return event

    def status(self):
        with self.lock:
            s = self.session
            latest = next(reversed(s.frames.values()), None) if s else None
            focused = None
            if latest and s.state == "active" and self.clock() < latest["expires_at"]:
                context = latest["context"]
                window, capture = context.get("rect"), latest["rect"]
                if window and (
                    max(window["x"], capture["x"])
                    < min(window["x"] + window["width"], capture["x"] + capture["width"])
                    and max(window["y"], capture["y"])
                    < min(window["y"] + window["height"], capture["y"] + capture["height"])
                ):
                    focused = {key: context.get(key, "") for key in ("id", "app", "title")}
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
                    "observation_preview": s.observation_preview,
                    "inspection_started_at": s.inspection_started_at,
                    "last_model_ms": s.last_model_ms,
                    "driver": s.driver,
                    "observation_state": s.observation_state,
                    "source_session_id": s.source_session_id or None,
                    "source_agent": s.source_agent or None,
                    "context_message_count": s.context_message_count,
                    "worker_model": s.worker_model or None,
                    "worker_effort": s.worker_effort or None,
                    "latest_frame": next(reversed(s.frames), None),
                    "focused_window": focused,
                    "desk": self.desk,
                    "paused_reason": s.paused_reason or None,
                    "paused_by_agent": s.paused_by_agent,
                    "pauses": s.pauses,
                    "last_timing": s.last_timing,
                }
            )
        return {
            "ok": True,
            "backend": self.desktop.name,
            "session": info,
            "model": computer_model(),
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
        browser_checks=None,
    ):
        if not operator_confirmed:
            raise ComputerError(
                "start a scoped session from the CLI or a verified Serena user turn"
            )
        if mode not in {"watch", "control"}:
            raise ComputerError("mode must be watch or control")
        from core.computer_browser import validate_plan

        browser_checks = validate_plan(browser_checks)
        if browser_checks is not None and mode != "watch":
            raise ComputerError("scripted browser conditions currently require watch mode")
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
            if self.session and self.session.state in LIVE_STATES:
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
                browser_checks=browser_checks,
                desk=self.desk,
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
        self.event(
            "started",
            session_id=s.id,
            mode=mode,
            target=target,
            desk=self.desk,
            expires_at=s.expires_at,
        )
        return self.status()

    def current(self, session_id):
        s = self.session
        if not s or s.id != session_id:
            raise ComputerError("computer session not found")
        if s.state in {"paused", "resuming"} and not s.cancelled.is_set():
            raise ComputerPaused(
                "paused: Raghav has the mouse and keyboard; after resume, observe before acting"
            )
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
            if not s or s.state not in LIVE_STATES:
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
        """His real input pauses a control session; watch sessions ignore it."""
        s = self.session
        if not s or s.mode != "control" or s.state not in LIVE_STATES:
            return
        s.last_physical_at = self.clock()
        if s.state == "active":
            self.pause(s.id, "you took over with the mouse or keyboard")

    def pause(self, session_id, reason, *, by_agent=False):
        """Hold input without ending the task; its lease and context survive."""
        with self.lock:
            s = self.session
            if not s or s.id != session_id or s.state != "active" or s.mode != "control":
                return self.status()
            s.state = "paused"
            s.paused_reason = str(reason)[:300]
            s.paused_by_agent = by_agent
            s.pauses += 1
            s.resume_requested_at = None
            # An in-flight batch sees the hold before the next key or click.
            s.input_hold.set()
            # Anything captured before the takeover no longer describes the screen.
            s.frames.clear()
        self.desktop.release()
        self.event("paused", session_id=s.id, reason=s.paused_reason, by_agent=by_agent)
        return self.status()

    def resume(self, session_id=None):
        with self.lock:
            s = self.session
            if not s or (session_id and s.id != session_id):
                raise ComputerError("computer session not found")
            if s.state in {"active", "resuming"}:
                return self.status()
            if s.state != "paused":
                raise ComputerError(f"computer session {s.state}: {s.reason}")
            if self.clock() >= s.expires_at:
                raise ComputerError("computer session expired")
            if self.authority.lock_state()["engaged"]:
                raise ComputerError("Serena's emergency stop is engaged")
            s.state = "resuming"
            s.resume_requested_at = self.clock()
        self.event("resuming", session_id=s.id)
        return self.status()

    def settle_resume(self):
        """Hand input back once his hands have been off for a moment."""
        s = self.session
        if not s or s.state != "resuming":
            return False
        quiet_since = max(s.resume_requested_at or 0, s.last_physical_at or 0)
        if self.clock() - quiet_since < RESUME_QUIET_SECONDS:
            return False
        with self.lock:
            if self.session is not s or s.state != "resuming":
                return False
            s.state = "active"
            s.paused_reason = ""
            s.paused_by_agent = False
            s.input_hold.clear()
        self.event("resumed", session_id=s.id)
        return True

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

    def observe(self, session_id, *, max_width=None):
        from PIL import Image, ImageDraw

        if max_width is not None:
            number(max_width, "max_width", 640, 2560)
        with self.capture_lock:
            s = self.current(session_id)
            max_width = max_width or s.frame_width
            if self.desktop.locked():
                self.stop("desktop locked")
                raise ComputerError("desktop locked")
            rect, _, geometry = self.geometry(s.target)
            foreground = self.desktop.context()
            context = foreground
            if s.mode == "watch" and s.target.startswith("display:"):
                # The user may type in a chat on another monitor. Describe the
                # selected display's visible app, not that unrelated foreground.
                window = foreground.get("rect")
                overlaps = window and (
                    rect.x < window["x"] + window["width"]
                    and window["x"] < rect.x + rect.width
                    and rect.y < window["y"] + window["height"]
                    and window["y"] < rect.y + rect.height
                )
                if not overlaps:
                    context = next(
                        (
                            item
                            for item in reversed(self.desktop.visible_windows())
                            if Rect(**item["rect"]).contains(
                                rect.x + rect.width // 2, rect.y + rect.height // 2
                            )
                        ),
                        {"id": "", "app": "", "title": ""},
                    )
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
                    self.desktop.context().get("id") != foreground.get("id")
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
                s.frame_delivered_at = time.monotonic()
            return frame

    def frame(self, session_id, frame_id):
        s = self.current(session_id)
        value = s.frames.get(frame_id)
        if not value or self.clock() >= value["expires_at"]:
            raise ComputerError("frame is expired; observe again before acting")
        return value

    def zoom(self, session_id, frame_id, x, y, width, height, *, max_width=1280):
        """A full-resolution crop of part of a frame, for reading small text.

        Coordinates are in the frame's pixels. The crop is not a frame: it has
        no id, so input can never be aimed with its coordinates. Privacy checks
        and the indicator mask apply exactly as they do to observe.
        """
        from PIL import Image, ImageDraw

        with self.capture_lock:
            s = self.current(session_id)
            frame = self.frame(session_id, frame_id)
            for name, value, limit in (
                ("x", x, frame["width"] - 1),
                ("y", y, frame["height"] - 1),
                ("width", width, frame["width"]),
                ("height", height, frame["height"]),
            ):
                number(value, name, 1 if name in {"width", "height"} else 0, limit)
            if x + width > frame["width"] or y + height > frame["height"]:
                raise ComputerError("zoom region extends past the screenshot")
            base = Rect(**frame["rect"])
            sx, sy = base.width / frame["width"], base.height / frame["height"]
            rect = Rect(
                base.x + round(x * sx),
                base.y + round(y * sy),
                max(1, round(width * sx)),
                max(1, round(height * sy)),
            )
            if self.geometry(s.target)[2] != frame["geometry"]:
                raise ComputerError("display/window geometry changed; observe again")
            if self.desktop.context().get("id") != frame["context"].get("id"):
                raise ComputerError("foreground changed; observe again")
            self._assert_public_region(rect)
            image = self.desktop.capture(rect)
            try:
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
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=90, subsampling=0)
                return {
                    "zoom_of": frame_id,
                    "region": {"x": x, "y": y, "width": width, "height": height},
                    "width": image.width,
                    "height": image.height,
                    "note": "for reading only; aim input with the full screenshot's coordinates",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(buffer.getvalue()).decode(),
                }
            finally:
                image.close()

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
            "launch",
            "handoff",
        }
        for index, a in enumerate(actions):
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
                "app",
                "url",
                "reason",
            }
            if set(a) - fields:
                raise ComputerError("unexpected action fields")
            if kind in {"launch", "handoff"} and index != len(actions) - 1:
                # Both change who or what is in front; inspect before anything else.
                raise ComputerError(f"{kind} must be the last action in its batch")
            if kind == "launch":
                if not self.launcher:
                    raise ComputerError("launch works only on serena's own desktop")
                if a.get("app") not in {"browser", "terminal"}:
                    raise ComputerError("launch app must be browser or terminal")
                url = a.get("url")
                if url is not None and (
                    a["app"] != "browser"
                    or not isinstance(url, str)
                    or not re.fullmatch(r"https?://[^\s]{1,2040}", url)
                ):
                    raise ComputerError("launch url must be one http(s) URL for the browser")
            if kind == "handoff" and (
                not isinstance(a.get("reason"), str) or not 1 <= len(a["reason"].strip()) <= 300
            ):
                raise ComputerError("handoff needs a 1–300 character reason for Raghav")
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
            arrived = time.monotonic()
            s = self.current(session_id)
            # Time since the model last received a screenshot: its decision time.
            decision_ms = (
                round((arrived - s.frame_delivered_at) * 1000) if s.frame_delivered_at else None
            )
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
            # A fresh baseline tells the settle loop what "the UI reacted" means,
            # and re-checks privacy just before input.
            baseline = frame["signature"]
            with contextlib.suppress(ComputerTransientError):
                baseline = self.observe(session_id)["signature"]
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
            input_started = time.monotonic()
            preflight_ms = round((input_started - arrived) * 1000)
            input_ms = 0
            expected_focus = frame["context"].get("id")
            try:
                for action in actions:
                    self.current(session_id)
                    if self._input_cancelled(s):
                        raise ComputerError(self._cancel_reason(s))
                    if self.authority.lock_state()["engaged"] or self.desktop.locked():
                        raise ComputerError("input stopped by lock")
                    if self.geometry(s.target)[2] != geometry:
                        raise ComputerError("target moved during the batch")
                    if self.desktop.context().get("id") != expected_focus:
                        raise ComputerError(
                            "foreground changed during the batch; inspect before continuing"
                        )
                    self._execute(s, action, frame, monitors)
                    receipt["actions_executed"] += 1
                    expected_focus = self._clicked_focus(s, action, frame, monitors, expected_focus)
                receipt.update(ok=True, status="executed")
            except Exception as exc:
                receipt.update(
                    status="partial" if receipt["actions_executed"] else "failed",
                    error=str(exc),
                    outcome_uncertain=True,
                )
            finally:
                input_ms = round((time.monotonic() - input_started) * 1000)
                self.desktop.release()
                self.authority.record_outcome(
                    authorization.request_id,
                    status="completed" if receipt["ok"] else "failed",
                    detail="input dispatched; visual outcome still requires inspection"
                    if receipt["ok"]
                    else receipt["error"],
                    receipt={k: v for k, v in receipt.items() if k != "data"},
                )
            if receipt["ok"] and actions[-1]["type"] == "handoff":
                self.pause(
                    s.id, "serena needs you: " + actions[-1]["reason"].strip(), by_agent=True
                )
                receipt.update(
                    status="handed_off",
                    next=(
                        "Raghav has the input now. End this turn; you continue from a fresh "
                        "screenshot after he resumes."
                    ),
                )
            post_frame = None
            settle_ms = None
            if not s.cancelled.is_set() and not s.input_hold.is_set():
                settle_started = time.monotonic()
                try:
                    post_frame = self._post_action_frame(
                        session_id,
                        baseline,
                        settle=actions[-1]["type"] not in {"wait", "screenshot"},
                    )
                except Exception as exc:
                    receipt["verification_error"] = str(exc)
                settle_ms = round((time.monotonic() - settle_started) * 1000)
            receipt["timing"] = s.last_timing = {
                "decision_ms": decision_ms,
                "preflight_ms": preflight_ms,
                "input_ms": input_ms,
                "settle_ms": settle_ms,
                "capture_ms": post_frame["capture_ms"] if post_frame else None,
            }
            self.event(
                "action_finished",
                session_id=s.id,
                **{k: v for k, v in receipt.items() if k != "frame"},
            )
            return {**receipt, **({"frame": post_frame} if post_frame else {})}
        finally:
            self.action_lock.release()

    # -- structured input on her own desktop ---------------------------------
    def browser_snapshot(self, session_id):
        """Her current page as an accessibility snapshot with element refs."""
        s = self.current(session_id)
        if self.web is None:
            raise ComputerError("page snapshots exist only on serena's own desktop")
        result = self.web.snapshot()
        s.frame_delivered_at = time.monotonic()
        return result

    def browser(self, session_id, steps, *, request_id, intent, confirmation_id=""):
        """Run a whole sequence of page steps in one call; stop at the first failure."""
        if self.web is None:
            raise ComputerError("browser steps exist only on serena's own desktop")
        from core.computer_web import MAX_STEP_TIMEOUT, validate_steps

        validate_steps(steps)
        budget = sum(float(step.get("timeout", 5)) for step in steps) + 10
        return self._structured_batch(
            session_id,
            request_id=request_id,
            intent=intent,
            payload=["browser", steps],
            budget=min(budget, len(steps) * MAX_STEP_TIMEOUT + 10),
            execute=lambda s: self.web.run(steps, cancelled=lambda: self._input_cancelled(s)),
            confirmation_id=confirmation_id,
        )

    def shell(
        self,
        session_id,
        *,
        request_id="",
        intent="",
        command=None,
        send=None,
        enter=True,
        read=False,
        timeout=30,
        confirmation_id="",
    ):
        """Her terminal as text: run a command, answer a prompt, or read output."""
        if self.terminal is None:
            raise ComputerError("the shell exists only on serena's own desktop")
        if sum(bool(item) for item in (command, send is not None, read)) != 1:
            raise ComputerError("shell takes exactly one of command, send, or read")
        s = self.current(session_id)
        if read:
            result = self.terminal.read()
            s.frame_delivered_at = time.monotonic()
            return result
        number(timeout, "timeout", 1, 600)
        if command is not None:
            payload = ["shell", command, timeout]

            def execute(session):
                return self.terminal.run(
                    command, timeout=timeout, cancelled=lambda: self._input_cancelled(session)
                )
        else:
            payload = ["send", send, enter]

            def execute(_session):
                return self.terminal.send(send, enter=enter)

        return self._structured_batch(
            session_id,
            request_id=request_id,
            intent=intent,
            payload=payload,
            budget=timeout + 10,
            execute=execute,
            confirmation_id=confirmation_id,
        )

    def _structured_batch(self, session_id, *, request_id, intent, payload, budget, execute, confirmation_id):
        """Authority, receipts, idempotency and timing for non-pixel input."""
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
            raise ComputerError("supply a unique request_id (8–80 letters/digits/dashes)")
        if not isinstance(intent, str) or not intent.strip() or len(intent) > 500:
            raise ComputerError("describe the intended effect of this batch")
        if not self.action_lock.acquire(blocking=False):
            raise ComputerError("another action batch is running")
        try:
            arrived = time.monotonic()
            s = self.current(session_id)
            decision_ms = (
                round((arrived - s.frame_delivered_at) * 1000) if s.frame_delivered_at else None
            )
            digest = hashlib.sha256(json.dumps([payload, intent], sort_keys=True).encode()).hexdigest()
            if request_id in s.results:
                old = s.results[request_id]
                if old["digest"] != digest:
                    raise ComputerError("request_id was reused with different actions")
                return {**old["result"], "replayed": True}
            if s.mode != "control":
                raise ComputerError("this session can watch but cannot send input")
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
                "request_id": request_id,
                "action_receipt_id": authorization.request_id,
            }
            s.results[request_id] = {"digest": digest, "result": receipt}
            while len(s.results) > 100:
                s.results.popitem(last=False)
            self.event("action_started", session_id=s.id, request_id=request_id, intent=intent)
            # A leftover pixel-batch deadline must not cancel this one.
            s.action_deadline = time.monotonic() + budget
            started = time.monotonic()
            try:
                result = execute(s)
                ok = bool(result.get("ok", result.get("status") != "interrupted"))
                receipt.update(result)
                receipt.update(ok=ok, status=result.get("status", "executed" if ok else "failed"))
                failed = next((item for item in result.get("steps", []) if not item.get("ok")), None)
                if failed:
                    # Which of the model's own steps broke, never page text.
                    receipt["failed_step"] = {
                        "step": failed["step"],
                        "detail": str(failed.get("detail", ""))[:240],
                    }
            except Exception as exc:
                receipt.update(status="failed", error=str(exc)[:500], outcome_uncertain=True)
            finally:
                run_ms = round((time.monotonic() - started) * 1000)
                self.authority.record_outcome(
                    authorization.request_id,
                    status="completed" if receipt["ok"] else "failed",
                    detail="structured input dispatched"
                    if receipt["ok"]
                    else str(receipt.get("error") or receipt.get("status")),
                    receipt={k: receipt.get(k) for k in ("request_id", "status", "ok")},
                )
            receipt["timing"] = s.last_timing = {"decision_ms": decision_ms, "run_ms": run_ms}
            s.frame_delivered_at = time.monotonic()
            # Events stay small: page text and command output never enter them.
            self.event(
                "action_finished",
                session_id=s.id,
                **{
                    k: receipt.get(k)
                    for k in ("request_id", "ok", "status", "error", "timing", "failed_step")
                    if k in receipt
                },
            )
            return receipt
        finally:
            self.action_lock.release()

    def _post_action_frame(self, session_id, baseline, *, settle=True):
        """The screen once the UI has reacted and stopped painting, within a bound.

        Signatures are hashes of a small grayscale thumbnail, so this compares
        frames without decoding them. A batch with no visible effect returns
        after the quiet window; a continuous animation returns at the cap.
        """
        if not settle:
            return self.observe(session_id)
        s = self.session
        started = time.monotonic()
        moved = False
        last = None
        while True:
            frame = None
            with contextlib.suppress(ComputerTransientError):
                frame = self.observe(session_id)
            if frame is not None:
                if moved and last is not None and frame["signature"] == last["signature"]:
                    return frame
                moved = moved or frame["signature"] != baseline
                last = frame
            elapsed = time.monotonic() - started
            if last is not None and (
                elapsed >= POST_ACTION_SETTLE_SECONDS
                or (not moved and elapsed >= POST_ACTION_QUIET_SECONDS)
            ):
                return last
            if elapsed >= POST_ACTION_SETTLE_SECONDS + 1:
                return self.observe(session_id)
            if s.cancelled.wait(0.05) or s.input_hold.is_set():
                raise ComputerError("input stopped before the result was captured")

    def _execute(self, s, a, frame, monitors):
        kind = a["type"]
        if kind in {"move", "click", "double_click", "scroll"}:
            self.desktop.move(*self._point(frame, a["x"], a["y"], monitors))
        if kind in {"click", "double_click"}:
            button = {"left": 1, "middle": 2, "right": 3}[a.get("button", "left")]
            for _ in range(2 if kind == "double_click" else 1):
                if self._input_cancelled(s):
                    raise ComputerError(self._cancel_reason(s))
                self.desktop.button(button, True)
                self.desktop.button(button, False)
                if kind == "double_click" and self._hold_wait(s, 0.07):
                    raise ComputerError(self._cancel_reason(s))
        elif kind == "scroll":
            for axis, negative, positive in (("scroll_y", 4, 5), ("scroll_x", 6, 7)):
                value = a.get(axis, 0)
                for _ in range(math.ceil(abs(value) / 100)):
                    if self._input_cancelled(s):
                        raise ComputerError(self._cancel_reason(s))
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
                    if self._hold_wait(s, 0.02):
                        raise ComputerError("drag cancelled")
                    self.desktop.move(*self._point(frame, **p, monitors=monitors))
            finally:
                self.desktop.button(button, False)
        elif kind == "keypress":
            try:
                for key in a["keys"]:
                    if self._input_cancelled(s):
                        raise ComputerError(self._cancel_reason(s))
                    self.desktop.key(key, True)
            finally:
                self.desktop.release()
        elif kind == "type":
            self.desktop.type_text(a["text"], lambda: self._input_cancelled(s))
        elif kind == "wait" and self._hold_wait(s, a.get("seconds", 0.5)):
            raise ComputerError("wait cancelled")
        elif kind == "launch":
            self.launcher(a["app"], a.get("url"))

    def _clicked_focus(self, s, action, frame, monitors, expected):
        """On her own desktop, a click that raised the window it hit is intended.

        "Click the browser, type the URL" then stays one batch instead of
        costing a model round trip. Anything else taking focus, such as a popup
        or a window elsewhere, still stops the batch. His screen stays strict.
        """
        if self.desk != "isolated" or action["type"] not in {"click", "double_click"}:
            return expected
        with contextlib.suppress(ComputerError):
            current = self.desktop.context()
            if current.get("id") == expected or not current.get("rect"):
                return expected
            point = self._point(frame, action["x"], action["y"], monitors)
            if Rect(**current["rect"]).contains(*point):
                return current.get("id")
        return expected

    def _hold_wait(self, s, seconds):
        """Sleep, returning True as soon as the batch must stop."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            if s.cancelled.wait(min(left, 0.02)) or self._input_cancelled(s):
                return True
        return self._input_cancelled(s)

    @staticmethod
    def _input_cancelled(s):
        return (
            s.cancelled.is_set()
            or s.input_hold.is_set()
            or (s.action_deadline > 0 and time.monotonic() >= s.action_deadline)
        )

    @staticmethod
    def _cancel_reason(s):
        if s.cancelled.is_set():
            return "input cancelled"
        if s.input_hold.is_set():
            return "Raghav took over; input stopped. After resume, observe before continuing"
        return "input batch deadline reached; inspect the partial result"

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
