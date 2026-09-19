from __future__ import annotations

import os
import stat

from core.ambient_store import AmbientStore


def test_store_is_private_on_disk(tmp_path):
    path = tmp_path / "ambient.sqlite3"
    AmbientStore(path)

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_consecutive_identical_events_merge_like_a_heartbeat(tmp_path):
    store = AmbientStore(tmp_path / "ambient.sqlite3")

    first = store.record("window", app="code", title="a.py", now=100.0)
    second = store.record("window", app="code", title="a.py", now=160.0)

    assert second.event_id == first.event_id
    assert second.duration == 60.0
    assert len(store.recent(now=200.0)) == 1


def test_changed_activity_starts_a_new_event(tmp_path):
    store = AmbientStore(tmp_path / "ambient.sqlite3")

    store.record("window", app="code", title="a.py", now=100.0)
    store.record("window", app="microsoft-edge", title="docs", now=160.0)

    events = store.recent(now=200.0)
    assert len(events) == 2
    assert events[0].app == "code"
    assert events[1].app == "microsoft-edge"


def test_ring_buffer_keeps_only_the_newest_events_in_memory(tmp_path):
    store = AmbientStore(tmp_path / "ambient.sqlite3")

    for index in range(2_050):
        store.record("window", app=f"app-{index}", now=float(index))

    assert len(store.recent(now=10_000.0)) == 2_000
    assert store.recent(now=10_000.0)[0].app == "app-50"


def test_retention_sweep_drops_events_older_than_thirty_days(tmp_path):
    store = AmbientStore(tmp_path / "ambient.sqlite3")
    now = 5_000_000.0

    store.record("window", app="old", now=now - 31 * 86_400)
    store.record("window", app="new", now=now)

    removed = store.retention_sweep(now=now)

    assert removed == 1
    events = store.recent(now=now)
    assert [event.app for event in events] == ["new"]
    assert "old" not in store.dump_for_test()


def test_forget_minutes_removes_memory_and_disk(tmp_path):
    store = AmbientStore(tmp_path / "ambient.sqlite3")

    store.record("window", app="keep", now=100.0)
    store.record("window", app="drop", now=1_000.0)

    removed = store.forget(minutes=15, now=1_100.0)

    assert removed == 1
    assert [event.app for event in store.recent(now=1_100.0)] == ["keep"]
    assert "drop" not in store.dump_for_test()


def test_forget_all_empties_memory_and_disk(tmp_path):
    store = AmbientStore(tmp_path / "ambient.sqlite3")
    store.record("window", app="a", now=100.0)
    store.record("idle", now=200.0)

    removed = store.forget_all(now=300.0)

    assert removed == 2
    assert store.recent(now=300.0) == []
    assert store.dump_for_test() == ""


def test_paused_store_records_nothing(tmp_path):
    store = AmbientStore(tmp_path / "ambient.sqlite3")
    store.pause(now=100.0)

    assert store.record("window", app="a", now=150.0) is None
    assert store.recent(now=200.0) == []

    store.resume(now=200.0)
    assert store.record("window", app="a", now=250.0) is not None


def test_agent_driven_periods_are_tagged_not_counted_as_him(tmp_path):
    store = AmbientStore(tmp_path / "ambient.sqlite3")
    store.mark_agent_active(until=500.0, now=100.0)

    assert store.is_agent_driven(now=200.0) is True
    assert store.is_agent_driven(now=600.0) is False


def test_status_reports_visible_state(tmp_path):
    store = AmbientStore(tmp_path / "ambient.sqlite3")
    store.record("window", app="code", now=100.0)
    store.pause(now=150.0)

    status = store.status(now=200.0)

    assert status["paused"] is True
    assert status["buffered"] == 1
    assert status["stored"] == 1


def test_disk_reads_return_the_newest_rows_chronological(tmp_path):
    store = AmbientStore(tmp_path / "ambient.sqlite3")
    for index in range(5):
        store.record("window", app=f"app-{index}", now=100.0 + index)

    events = store.recent_from_disk(limit=2, seconds=None)

    assert [event.app for event in events] == ["app-3", "app-4"]


def test_size_cap_trims_one_bounded_batch_and_keeps_the_newest(tmp_path, monkeypatch):
    import core.ambient_store as ambient_module

    monkeypatch.setattr(ambient_module, "MAX_DB_BYTES", 1)
    monkeypatch.setattr(ambient_module, "SIZE_TRIM_KEEP_ROWS", 5)
    monkeypatch.setattr(ambient_module, "SIZE_TRIM_BATCH_ROWS", 3)
    store = AmbientStore(tmp_path / "ambient.sqlite3")
    for index in range(10):
        store.record("window", app=f"app-{index}", now=100.0 + index)

    removed = store.retention_sweep(now=200.0)

    assert removed == 3
    remaining = store.recent_from_disk(limit=20, seconds=None)
    assert [event.app for event in remaining] == [f"app-{index}" for index in range(3, 10)]


def test_size_cap_never_wipes_a_small_store(tmp_path, monkeypatch):
    import core.ambient_store as ambient_module

    monkeypatch.setattr(ambient_module, "MAX_DB_BYTES", 1)
    store = AmbientStore(tmp_path / "ambient.sqlite3")
    store.record("window", app="only", now=100.0)

    assert store.retention_sweep(now=200.0) == 0
    assert [event.app for event in store.recent_from_disk(seconds=None)] == ["only"]
