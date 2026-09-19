from __future__ import annotations

from core.ambient_context import (
    MAX_CONTEXT_CHARS,
    ambient_context_block,
    summarize,
)
from core.ambient_store import AmbientEvent, AmbientStore


def event(kind, at, app="", title="", url="", duration=0.0):
    return AmbientEvent(
        event_id=f"{at}-{kind}-{app}-{title}",
        kind=kind, app=app, title=title, url=url,
        started_at=at, ended_at=at + duration,
    )


def test_summarize_names_apps_switches_class_and_last_error():
    now = 100_000.0
    events = [
        event("window", now - 900.0, app="code", title="a.py", duration=600.0),
        event("tab", now - 200.0, app="microsoft-edge", title="docs",
              url="https://example.com/docs", duration=100.0),
        event("window", now - 60.0, app="org.gnome.Terminal",
              title="pytest - 2 FAILED", duration=30.0),
    ]

    summary = summarize(events, now=now)

    assert "code" in summary
    assert "switches: 2" in summary
    assert "focused" in summary
    assert "2 FAILED" in summary


def test_empty_buffer_summarizes_to_nothing():
    assert summarize([], now=100.0) == ""


def test_summary_never_exceeds_its_own_budget():
    now = 100_000.0
    events = [
        event("window", now - 60.0 - index, app=f"app-{index}",
              title="x" * 500, duration=1.0)
        for index in range(100)
    ]

    summary = summarize(events, now=now)

    assert len(summary) <= MAX_CONTEXT_CHARS
    assert len(summarize(events, now=now, max_chars=100)) <= 100


def test_agent_driven_activity_is_excluded():
    now = 100_000.0
    mine = AmbientEvent(
        event_id="m", kind="window", app="code", title="a.py",
        started_at=now - 60.0, ended_at=now - 50.0, agent_driven=True)

    assert summarize([mine], now=now) == ""


def test_per_app_dwell_never_exceeds_the_window():
    import re

    now = 100_000.0
    events = [
        event("window", now - 1_100.0 + index * 5.0, app="code",
              title=f"edit {index}")
        for index in range(100)
    ]

    summary = summarize(events, now=now)

    minutes = [int(item) for item in re.findall(r"\((\d+)m\)", summary)]
    assert minutes
    assert all(item <= 20 for item in minutes)


def test_block_reads_the_disk_buffer_and_is_fail_soft(tmp_path, monkeypatch):
    import time as time_module

    db = tmp_path / "ambient.sqlite3"
    monkeypatch.setenv("SERENA_AMBIENT_DB_PATH", str(db))
    now = time_module.time()
    AmbientStore(db).record("window", app="code", title="a.py", now=now - 60.0)

    block = ambient_context_block(now=now)

    assert "code" in block


def test_block_is_empty_when_nothing_was_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_AMBIENT_DB_PATH", str(tmp_path / "ambient.sqlite3"))

    assert ambient_context_block(now=100.0) == ""


def test_brain_turn_keeps_memory_context_and_gains_ambient(tmp_path, monkeypatch):
    import time as time_module

    import core.brain_daemon as brain

    calls = []
    db = tmp_path / "ambient.sqlite3"
    monkeypatch.setenv("SERENA_AMBIENT_DB_PATH", str(db))

    def fake_memory(*args, **kwargs):
        calls.append((args, kwargs))
        return "MEMORY-BYTES"

    monkeypatch.setattr(brain, "_memory_context_block", fake_memory)
    monkeypatch.setattr(brain, "_supportive_context_block", lambda text: "")

    without_ambient = brain._compose_message({"text": "hi", "protocol": "plain"})
    assert "MEMORY-BYTES" in without_ambient
    assert "ambient-context" not in without_ambient

    AmbientStore(db).record("window", app="code", title="a.py",
                            now=time_module.time() - 60.0)
    with_ambient = brain._compose_message({"text": "hi", "protocol": "plain"})

    assert "MEMORY-BYTES" in with_ambient
    assert calls[0] == calls[1]
    assert "ambient-context" in with_ambient
    assert "code" in with_ambient
