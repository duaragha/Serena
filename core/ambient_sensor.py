"""Event-driven X11 ambient sensor: metadata, not pixels.

The source blocks on the X connection and only wakes for real changes:
`PropertyNotify` on the root window's `_NET_ACTIVE_WINDOW`, plus `_NET_WM_NAME`
on the currently active window (a tab switch changes only the title). The old
window is unwatched on every switch and `BadWindow` on a closed window is
swallowed. Idle comes from the XScreenSaver extension, lock/power from a
sampled system source, clipboard as hash/size/owner only.

Everything passes the denylist *before* the store sees it, and anything
recorded while `mark_agent_active` covers the moment is tagged agent-driven
instead of counted as him.
"""

from __future__ import annotations

import hashlib
import select
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from core.ambient_denylist import AmbientDenylist
from core.ambient_store import AmbientEvent, AmbientStore

SENSOR_EVENT_TYPES = frozenset(
    {
        "active_window",
        "title",
        "window_closed",
        "idle",
        "active",
        "lock",
        "unlock",
        "sleep",
        "power",
        "clipboard",
    }
)
DEFAULT_IDLE_AFTER_MS = 60_000
DEFAULT_TITLE_DEBOUNCE_MS = 2_000


class BadWindowError(Exception):
    """A window id the X server no longer knows. Always swallowed."""


@dataclass(frozen=True, slots=True)
class WindowInfo:
    app: str = ""
    title: str = ""
    pid: int = 0


@dataclass(frozen=True, slots=True)
class SensorEvent:
    type: str
    app: str = ""
    title: str = ""
    url: str = ""
    pid: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in SENSOR_EVENT_TYPES:
            raise TypeError(f"unknown sensor event type {self.type!r}")


class XDisplay(Protocol):
    """The narrow X surface the source needs. Faked in tests."""

    def pending_events(self) -> list[tuple]: ...
    def window_info(self, window: int) -> WindowInfo | None: ...
    def watch_window(self, window: int) -> None: ...
    def unwatch_window(self, window: int) -> None: ...
    def idle_ms(self) -> int: ...
    def close(self) -> None: ...


class SystemSource(Protocol):
    """Sampled lock/power transitions. Faked in tests."""

    def sample(self, now: float) -> list[SensorEvent]: ...


class X11EventSource:
    """Turns raw X happenings into sensor events, with title debounce."""

    def __init__(
        self,
        display: XDisplay,
        *,
        idle_after_ms: int = DEFAULT_IDLE_AFTER_MS,
        title_debounce_ms: int = DEFAULT_TITLE_DEBOUNCE_MS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._display = display
        self._idle_after_ms = idle_after_ms
        self._title_debounce_ms = title_debounce_ms
        self._now = now
        self._active: int | None = None
        self._idle = False
        self._last_title_at = 0.0
        self._pending: deque[tuple] = deque()

    def poll(self, timeout: float = 1.0) -> SensorEvent | None:
        if timeout > 0:
            waiter = getattr(self._display, "wait_for_events", None)
            if waiter is not None:
                waiter(timeout)
        self._pending.extend(self._display.pending_events())
        while self._pending:
            event = self._translate(self._pending.popleft())
            if event is not None:
                return event
        return self._idle_transition()

    def close(self) -> None:
        self._display.close()

    def _translate(self, raw: tuple) -> SensorEvent | None:
        kind = raw[0] if raw else ""
        if kind == "active":
            return self._switched(raw[1])
        if kind == "title":
            return self._retitled(raw[1])
        if kind == "destroy":
            return self._destroyed(raw[1])
        if kind == "clipboard":
            shape = raw[1] if len(raw) > 1 and isinstance(raw[1], dict) else {}
            return SensorEvent(
                type="clipboard",
                app=str(shape.get("owner") or ""),
                meta={
                    "owner": str(shape.get("owner") or ""),
                    "sha256": str(shape.get("sha256") or ""),
                    "size": str(shape.get("size") or ""),
                },
            )
        return None

    def _switched(self, window: int) -> SensorEvent | None:
        try:
            info = self._display.window_info(window)
        except BadWindowError:
            return None
        if self._active is not None and self._active != window:
            self._display.unwatch_window(self._active)
        self._active = window
        self._display.watch_window(window)
        if info is None:
            return None
        return SensorEvent(
            type="active_window", app=info.app, title=info.title, pid=info.pid
        )

    def _retitled(self, window: int) -> SensorEvent | None:
        if window != self._active:
            # A background tab finishing a load. Foreground only.
            return None
        try:
            info = self._display.window_info(window)
        except BadWindowError:
            return None
        if info is None:
            return None
        moment = self._now()
        if (moment * 1000.0) - self._last_title_at < self._title_debounce_ms:
            return None
        self._last_title_at = moment * 1000.0
        return SensorEvent(type="title", app=info.app, title=info.title, pid=info.pid)

    def _destroyed(self, window: int) -> SensorEvent | None:
        if window == self._active:
            self._display.unwatch_window(window)
            self._active = None
        return SensorEvent(type="window_closed")

    def _idle_transition(self) -> SensorEvent | None:
        try:
            idle = self._display.idle_ms()
        except BadWindowError:
            return None
        if not self._idle and idle >= self._idle_after_ms:
            self._idle = True
            return SensorEvent(type="idle", meta={"idle_ms": str(idle)})
        if self._idle and idle < self._idle_after_ms:
            self._idle = False
            return SensorEvent(type="active")
        return None


class PollingSystemSource:
    """Lock from logind, power from UPower, sampled, degrading gracefully.

    One `loginctl` fork every 15 s and one UPower probe a minute is negligible
    next to an event-driven X loop. When logind is unusable the screensaver
    signal (if wired) stands in for the lock; when UPower is missing there are
    simply no power events rather than a broken daemon.
    """

    def __init__(
        self,
        *,
        lock_interval: float = 15.0,
        power_interval: float = 60.0,
        screensaver_active: Callable[[], bool | None] | None = None,
    ) -> None:
        self._lock_interval = lock_interval
        self._power_interval = power_interval
        self._screensaver_active = screensaver_active
        self._locked: bool | None = None
        self._on_battery: bool | None = None
        self._next_lock = 0.0
        self._next_power = 0.0
        self._loginctl_ok = True
        self._upower_ok = True
        # The first successful read seeds the baseline without emitting: a
        # daemon restart is not an unlock, and must not fake a breakpoint.
        self._seeded_lock = False
        self._seeded_power = False

    def sample(self, now: float) -> list[SensorEvent]:
        events: list[SensorEvent] = []
        if now >= self._next_lock:
            self._next_lock = now + self._lock_interval
            locked = self._read_locked()
            if locked is not None and locked != self._locked:
                self._locked = locked
                if self._seeded_lock:
                    events.append(SensorEvent(type="lock" if locked else "unlock"))
                self._seeded_lock = True
        if now >= self._next_power:
            self._next_power = now + self._power_interval
            on_battery = self._read_on_battery()
            if on_battery is not None and on_battery != self._on_battery:
                self._on_battery = on_battery
                if self._seeded_power:
                    events.append(
                        SensorEvent(
                            type="power", meta={"on_battery": str(bool(on_battery))}
                        )
                    )
                self._seeded_power = True
        return events

    def _read_locked(self) -> bool | None:
        if self._loginctl_ok:
            try:
                probe = subprocess.run(
                    ["loginctl", "show-session", "self",
                     "--property=LockedHint", "--value"],
                    capture_output=True, text=True, timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                self._loginctl_ok = False
            else:
                if probe.returncode == 0:
                    return probe.stdout.strip().lower() == "yes"
                self._loginctl_ok = False
        if self._screensaver_active is not None:
            try:
                return self._screensaver_active()
            except Exception:
                return None
        return None

    def _read_on_battery(self) -> bool | None:
        if not self._upower_ok:
            return None
        try:
            probe = subprocess.run(
                ["gdbus", "call", "--system",
                 "--dest", "org.freedesktop.UPower",
                 "--object-path", "/org/freedesktop/UPower",
                 "--method", "org.freedesktop.DBus.Properties.Get",
                 "org.freedesktop.UPower", "OnBattery"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            self._upower_ok = False
            return None
        if probe.returncode != 0:
            self._upower_ok = False
            return None
        return "true" in probe.stdout.lower()


class AmbientSensor:
    """Denylist, then store. The only path from X into the buffer."""

    def __init__(
        self,
        *,
        store: AmbientStore,
        denylist: AmbientDenylist,
        source: X11EventSource,
        system: SystemSource | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._denylist = denylist
        self._source = source
        self._system = system
        self._now = now

    def run_once(self, timeout: float = 1.0) -> AmbientEvent | None:
        moment = self._now()
        try:
            event = self._source.poll(timeout)
        except BadWindowError:
            event = None
        if event is not None:
            return self._record_event(event, moment)
        if self._system is not None:
            recorded: AmbientEvent | None = None
            for system_event in self._system.sample(moment):
                recorded = self._record_event(system_event, moment) or recorded
            return recorded
        return None

    def run(self, stop: threading.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            try:
                self.run_once(timeout=1.0)
            except Exception:
                time.sleep(1.0)

    def _record_event(self, event: SensorEvent, moment: float) -> AmbientEvent | None:
        if event.type == "window_closed":
            return None
        if self._denylist.denies(app=event.app, title=event.title, url=event.url):
            return None
        kind = {"active_window": "window", "title": "window"}.get(event.type, event.type)
        if kind == "clipboard":
            meta = {
                key: str(event.meta.get(key) or "")
                for key in ("owner", "sha256", "size")
            }
        else:
            meta = {str(k): str(v) for k, v in event.meta.items()}
            if event.pid:
                meta.setdefault("pid", str(event.pid))
        return self._store.record(
            kind,
            app=event.app,
            title=event.title,
            url=event.url,
            meta=meta,
            agent_driven=self._store.is_agent_driven(now=moment),
            now=moment,
        )


_XRES_PROBED: bool | None = None


def _xres_available() -> bool:
    """Whether this python-xlib has an XRes binding. Probed once, not per window."""

    global _XRES_PROBED
    if _XRES_PROBED is None:
        try:
            from Xlib.ext import xres  # noqa: F401
        except ImportError:
            _XRES_PROBED = False
        else:
            _XRES_PROBED = True
    return _XRES_PROBED


class XDisplayAdapter:
    """The real X connection. Built lazily so imports never need an X server."""

    def __init__(self) -> None:
        from Xlib import X, display as xdisplay

        self._X = X
        self._display = xdisplay.Display()
        self._root = self._display.screen().root
        self._atoms = {
            name: self._display.intern_atom(name)
            for name in (
                "_NET_ACTIVE_WINDOW",
                "_NET_WM_NAME",
                "WM_NAME",
                "WM_CLASS",
                "_NET_WM_PID",
                "UTF8_STRING",
                "CLIPBOARD",
            )
        }
        self._root.change_attributes(event_mask=X.PropertyChangeMask)
        try:
            from Xlib.ext import xfixes

            xfixes.query_version(self._display)
            xfixes.select_selection_input(
                self._display, self._root, self._atoms["CLIPBOARD"],
                xfixes.XFixesSetSelectionOwnerNotifyMask
                | xfixes.XFixesSelectionWindowDestroyNotifyMask
                | xfixes.XFixesSelectionClientCloseNotifyMask,
            )
            self._xfixes = xfixes
        except Exception:
            self._xfixes = None
        # Events the clipboard fetch borrowed from the queue mid-round-trip.
        # python-xlib has no put-back, so the drain replays them first.
        self._stashed: deque = deque()

    def _next_drain_event(self):
        if self._stashed:
            return self._stashed.popleft()
        if self._display.pending_events():
            return self._display.next_event()
        return None

    def wait_for_events(self, timeout: float) -> bool:
        readable, _, _ = select.select([self._display], [], [], max(0.0, timeout))
        return bool(readable)

    def pending_events(self) -> list[tuple]:
        from Xlib import X

        raw: list[tuple] = []
        while True:
            event = self._next_drain_event()
            if event is None:
                break
            if event.type == X.PropertyNotify:
                if (
                    event.window == self._root
                    and event.atom == self._atoms["_NET_ACTIVE_WINDOW"]
                ):
                    active = self._active_window_id()
                    if active:
                        raw.append(("active", active))
                elif event.atom in (
                    self._atoms["_NET_WM_NAME"], self._atoms["WM_NAME"]
                ):
                    raw.append(("title", int(event.window.id)))
            elif event.type == X.DestroyNotify:
                raw.append(("destroy", int(event.window.id)))
            # Anything else (XFixes selection notices, RANDR, XInput hotplug)
            # is none of ours: ignore it here. Clipboard detection is the
            # owner check below, never a fetch inside this drain loop.
        # XFixes selection-owner changes arrive as extension events; the cheap
        # readable check is the owner itself changing between drains.
        shape = self._selection_owner_changed()
        if shape is not None:
            raw.append(("clipboard", shape))
        return raw

    def window_info(self, window: int) -> WindowInfo | None:
        from Xlib import X
        from Xlib.error import BadWindow

        try:
            obj = self._display.create_resource_object("window", window)
            title = self._window_title(obj)
            wm_class = obj.get_wm_class() or ("", "")
            app = str(wm_class[1] or wm_class[0] or "")
            pid = self._window_pid(obj)
        except BadWindow:
            raise BadWindowError(window)
        except Exception:
            return None
        _ = X  # event constants live with the drain, not the query.
        return WindowInfo(app=app, title=title, pid=pid)

    def watch_window(self, window: int) -> None:
        from Xlib import X
        from Xlib.error import BadWindow

        try:
            obj = self._display.create_resource_object("window", window)
            obj.change_attributes(event_mask=X.PropertyChangeMask)
        except BadWindow:
            raise BadWindowError(window)
        except Exception:
            pass

    def unwatch_window(self, window: int) -> None:
        from Xlib import X
        from Xlib.error import BadWindow

        try:
            obj = self._display.create_resource_object("window", window)
            obj.change_attributes(event_mask=X.NoEventMask)
        except BadWindow:
            pass
        except Exception:
            pass

    def idle_ms(self) -> int:
        try:
            from Xlib.ext import screensaver

            info = screensaver.query_info(self._display, self._root)
            return int(info.idle)
        except Exception:
            return 0

    def screensaver_active(self) -> bool | None:
        try:
            from Xlib.ext import screensaver

            info = screensaver.query_info(self._display, self._root)
            return bool(info.state)
        except Exception:
            return None

    def close(self) -> None:
        try:
            self._display.close()
        except Exception:
            pass

    def _active_window_id(self) -> int:
        try:
            prop = self._root.get_full_property(
                self._atoms["_NET_ACTIVE_WINDOW"], self._X.AnyPropertyType
            )
            if prop and prop.value:
                return int(prop.value[0])
        except Exception:
            pass
        return 0

    def _window_title(self, obj) -> str:
        for atom in (self._atoms["_NET_WM_NAME"], self._atoms["WM_NAME"]):
            try:
                prop = obj.get_full_property(atom, self._X.AnyPropertyType)
            except Exception:
                continue
            if prop and prop.value:
                value = prop.value
                if isinstance(value, bytes):
                    with_ = value.decode("utf-8", "replace")
                else:
                    with_ = "".join(value) if isinstance(value, list) else str(value)
                if with_.strip():
                    return with_
        return ""

    def _window_pid(self, obj) -> int:
        # XRes would be authoritative, but this python-xlib has no XRes
        # binding, so read the hint the window manager maintains instead.
        if _xres_available():
            try:
                return int(self._xres_pid(obj))
            except Exception:
                pass
        try:
            prop = obj.get_full_property(
                self._atoms["_NET_WM_PID"], self._X.AnyPropertyType
            )
            if prop and prop.value:
                return int(prop.value[0])
        except Exception:
            pass
        return 0

    def _xres_pid(self, obj) -> int:  # pragma: no cover - binding absent
        raise NotImplementedError("XRes query needs a newer python-xlib")

    _last_owner: int | None = None

    def _selection_owner_changed(self) -> dict[str, str] | None:
        try:
            owner = self._display.get_selection_owner(self._atoms["CLIPBOARD"])
        except Exception:
            return None
        owner_id = int(owner.id) if owner else 0
        if owner_id == (self._last_owner or 0):
            return None
        self._last_owner = owner_id
        if not owner_id:
            return None
        return self._clipboard_shape(owner_id)

    def _clipboard_shape(self, owner_id: int = 0) -> dict[str, str] | None:
        try:
            owner_class = ""
            if owner_id:
                obj = self._display.create_resource_object("window", owner_id)
                wm_class = obj.get_wm_class() or ("", "")
                owner_class = str(wm_class[1] or wm_class[0] or "")
            content = self._fetch_clipboard_text()
        except Exception:
            return None
        if content is None:
            return None
        digest = hashlib.sha256(content).hexdigest()
        return {"owner": owner_class, "sha256": digest, "size": str(len(content))}

    def _fetch_clipboard_text(self) -> bytes | None:
        from Xlib import X

        try:
            window = self._root.create_window(
                0, 0, 1, 1, 0, self._display.screen().root_depth,
                window_class=X.InputOnly,
            )
            prop_atom = self._display.intern_atom("SERENA_CLIPBOARD")
            window.convert_selection(
                self._atoms["CLIPBOARD"], self._atoms["UTF8_STRING"],
                prop_atom, self._X.CurrentTime,
            )
            self._display.flush()
            deadline = time.time() + 0.5
            while time.time() < deadline:
                if self._display.pending_events():
                    event = self._display.next_event()
                    if event.type != X.SelectionNotify:
                        # A window event that arrived mid-fetch belongs to the
                        # drain loop, not to us: stash it for pending_events().
                        self._stashed.append(event)
                        continue
                    if event.property == X.NONE:
                        return None
                    data = window.get_full_property(prop_atom, self._X.AnyPropertyType)
                    if data and data.value:
                        value = data.value
                        return value if isinstance(value, bytes) else str(value).encode()
                    return None
                else:
                    time.sleep(0.02)
        except Exception:
            return None
        finally:
            try:
                window.destroy()
            except Exception:
                pass
        return None


def open_sensor(*, system: bool = True) -> AmbientSensor:
    """Wire the real X connection, system probes, store, and denylist."""

    display = XDisplayAdapter()
    source = X11EventSource(display)
    probes: SystemSource | None = None
    if system:
        probes = PollingSystemSource(screensaver_active=display.screensaver_active)
    return AmbientSensor(
        store=AmbientStore(),
        denylist=AmbientDenylist.default(),
        source=source,
        system=probes,
    )


def main() -> int:
    stop = threading.Event()

    def _halt(*_args: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _halt)
    signal.signal(signal.SIGINT, _halt)
    sensor = open_sensor()
    try:
        sensor.run(stop)
    finally:
        sensor._source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
