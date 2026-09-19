from __future__ import annotations

import pytest

from core.ambient_sensor import (
    AmbientSensor,
    BadWindowError,
    SensorEvent,
    WindowInfo,
    X11EventSource,
)
from core.ambient_denylist import AmbientDenylist
from core.ambient_store import AmbientStore


class FakeXDisplay:
    """Scripted X connection: queued raw events, canned window info."""

    def __init__(self):
        self.raw_events: list = []
        self.windows: dict[int, WindowInfo] = {}
        self.bad_windows: set[int] = set()
        self.watched: set[int] = set()
        self.unwatched: list[int] = []
        self.idle_value = 0
        self.closed = False

    def queue(self, *events):
        self.raw_events.extend(events)

    def pending_events(self):
        queued, self.raw_events = self.raw_events, []
        return queued

    def window_info(self, window):
        if window in self.bad_windows:
            raise BadWindowError(window)
        return self.windows.get(window)

    def watch_window(self, window):
        self.watched.add(window)

    def unwatch_window(self, window):
        self.watched.discard(window)
        self.unwatched.append(window)

    def idle_ms(self):
        return self.idle_value

    def close(self):
        self.closed = True


def make_sensor(tmp_path, display, **kwargs):
    clock = kwargs.pop("clock", None)
    store = AmbientStore(tmp_path / "ambient.sqlite3")
    if clock is not None:
        kwargs["now"] = lambda: clock[0]
    source = X11EventSource(display, **kwargs)
    sensor = AmbientSensor(store=store, denylist=AmbientDenylist.default(), source=source)
    if clock is not None:
        sensor._now = lambda: clock[0]
    return sensor, store


def test_window_switch_records_new_event(tmp_path):
    display = FakeXDisplay()
    display.windows = {
        11: WindowInfo(app="code", title="a.py", pid=100),
        22: WindowInfo(app="microsoft-edge", title="docs", pid=200),
    }
    display.queue(("active", 11), ("active", 22))
    sensor, store = make_sensor(tmp_path, display)

    sensor.run_once(timeout=0)
    sensor.run_once(timeout=0)

    apps = [event.app for event in store.recent(now=1_000.0)]
    assert apps == ["code", "microsoft-edge"]


def test_title_change_on_same_window_records_without_resubscribe_storm(tmp_path):
    display = FakeXDisplay()
    display.windows = {11: WindowInfo(app="microsoft-edge", title="tab one", pid=100)}
    display.queue(("active", 11))
    clock = [100.0]
    sensor, store = make_sensor(tmp_path, display, clock=clock)
    sensor.run_once(timeout=0)

    clock[0] += 5.0
    display.windows[11] = WindowInfo(app="microsoft-edge", title="tab two", pid=100)
    display.queue(("title", 11))
    sensor.run_once(timeout=0)

    titles = [event.title for event in store.recent(now=1_000.0)]
    assert titles == ["tab one", "tab two"]
    assert display.unwatched == []


def test_noisy_title_flaps_are_debounced(tmp_path):
    display = FakeXDisplay()
    display.windows = {11: WindowInfo(app="microsoft-edge", title="tab one", pid=100)}
    display.queue(("active", 11))
    clock = [100.0]
    sensor, store = make_sensor(tmp_path, display, clock=clock)
    sensor.run_once(timeout=0)

    clock[0] += 5.0
    display.windows[11] = WindowInfo(app="microsoft-edge", title="loading…", pid=100)
    display.queue(("title", 11))
    sensor.run_once(timeout=0)
    clock[0] += 0.1
    display.windows[11] = WindowInfo(app="microsoft-edge", title="loading… 2", pid=100)
    display.queue(("title", 11))
    sensor.run_once(timeout=0)

    titles = [event.title for event in store.recent(now=1_000.0)]
    assert titles == ["tab one", "loading…"]


def test_switch_unsubscribes_old_window_and_swallows_bad_window(tmp_path):
    display = FakeXDisplay()
    display.windows = {
        11: WindowInfo(app="code", title="a.py", pid=100),
        22: WindowInfo(app="code", title="b.py", pid=100),
    }
    display.queue(("active", 11))
    sensor, store = make_sensor(tmp_path, display)
    sensor.run_once(timeout=0)

    # The old window dies mid-flight: title query raises BadWindow, the
    # destroy arrives, then the new window activates. Nothing crashes.
    display.bad_windows.add(11)
    display.queue(("title", 11), ("destroy", 11), ("active", 22))
    sensor.run_once(timeout=0)
    sensor.run_once(timeout=0)
    sensor.run_once(timeout=0)

    assert 11 in display.unwatched
    assert [event.title for event in store.recent(now=1_000.0)] == ["a.py", "b.py"]


def test_denylisted_app_produces_zero_rows(tmp_path):
    display = FakeXDisplay()
    display.windows = {11: WindowInfo(app="1password", title="vault", pid=100)}
    display.queue(("active", 11))
    sensor, store = make_sensor(tmp_path, display)

    assert sensor.run_once(timeout=0) is None
    assert store.recent(now=1_000.0) == []
    assert store.dump_for_test() == ""


def test_idle_transition_is_recorded_and_activity_returns(tmp_path):
    display = FakeXDisplay()
    display.idle_value = 300_000
    sensor, store = make_sensor(tmp_path, display, idle_after_ms=60_000)

    sensor.run_once(timeout=0)
    assert [event.kind for event in store.recent(now=1_000.0)] == ["idle"]

    display.idle_value = 0
    sensor.run_once(timeout=0)
    assert [event.kind for event in store.recent(now=1_000.0)] == ["idle", "active"]


def test_agent_driven_input_is_tagged_not_counted_as_him(tmp_path):
    display = FakeXDisplay()
    display.idle_value = 0
    display.windows = {11: WindowInfo(app="code", title="a.py", pid=100)}
    display.queue(("active", 11))
    sensor, store = make_sensor(tmp_path, display, idle_after_ms=60_000, clock=[100.0])
    store.mark_agent_active(until=10_000.0, now=100.0)

    sensor.run_once(timeout=0)

    events = store.recent(now=1_000.0)
    assert len(events) == 1
    assert events[0].agent_driven is True


def test_clipboard_event_stores_shape_only(tmp_path):
    display = FakeXDisplay()
    display.queue(("clipboard", {"owner": "code", "sha256": "abc", "size": 12}))
    sensor, store = make_sensor(tmp_path, display)

    sensor.run_once(timeout=0)

    events = store.recent(now=1_000.0)
    assert len(events) == 1
    assert events[0].kind == "clipboard"
    assert events[0].meta == {"owner": "code", "sha256": "abc", "size": "12"}


def test_password_manager_clipboard_owner_is_skipped(tmp_path):
    display = FakeXDisplay()
    display.queue(("clipboard", {"owner": "1password", "sha256": "abc", "size": 12}))
    sensor, store = make_sensor(tmp_path, display)

    assert sensor.run_once(timeout=0) is None
    assert store.recent(now=1_000.0) == []


def test_sensor_event_source_protocol_is_explicit():
    event = SensorEvent(type="active_window", app="code", title="a.py")
    assert event.type == "active_window"
    with pytest.raises(TypeError):
        SensorEvent(type="bogus-type", app="x")


def test_first_system_sample_seeds_without_emitting():
    from core.ambient_sensor import PollingSystemSource

    source = PollingSystemSource(lock_interval=15.0, power_interval=60.0)
    source._read_locked = lambda: False
    source._read_on_battery = lambda: False

    assert source.sample(100.0) == []

    source._read_locked = lambda: True
    events = source.sample(120.0)

    assert [event.type for event in events] == ["lock"]
    assert source.sample(140.0) == []


def test_window_pid_is_kept_in_event_meta(tmp_path):
    display = FakeXDisplay()
    display.idle_value = 0
    display.windows = {11: WindowInfo(app="code", title="a.py", pid=4242)}
    display.queue(("active", 11))
    sensor, store = make_sensor(tmp_path, display, idle_after_ms=60_000, clock=[100.0])

    sensor.run_once(timeout=0)

    events = store.recent(now=1_000.0)
    assert len(events) == 1
    assert events[0].meta.get("pid") == "4242"


def test_stashed_events_replay_before_the_live_queue():
    from collections import deque

    from core.ambient_sensor import XDisplayAdapter

    adapter = object.__new__(XDisplayAdapter)
    adapter._stashed = deque(["stashed-first"])

    class QuietDisplay:
        def pending_events(self):
            return False

        def next_event(self):  # pragma: no cover - never reached
            raise AssertionError("queue must not be touched")

    adapter._display = QuietDisplay()

    assert adapter._next_drain_event() == "stashed-first"
    assert adapter._next_drain_event() is None
