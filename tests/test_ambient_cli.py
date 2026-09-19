from __future__ import annotations

import time

from click.testing import CliRunner

from cli import main
from core.ambient_store import AmbientStore


def run(args, db_path, monkeypatch):
    monkeypatch.setenv("SERENA_AMBIENT_DB_PATH", str(db_path))
    return CliRunner().invoke(main, ["ambient", *args])


def test_status_reports_pause_state_and_counts(tmp_path, monkeypatch):
    db = tmp_path / "ambient.sqlite3"
    AmbientStore(db).record("window", app="code", now=time.time())

    result = run(["status"], db, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "recording" in result.output.lower()
    assert "stored: 1" in result.output
    assert "code" in result.output


def test_pause_and_resume_toggle_recording(tmp_path, monkeypatch):
    db = tmp_path / "ambient.sqlite3"

    assert run(["pause"], db, monkeypatch).exit_code == 0
    assert AmbientStore(db).paused() is True
    assert run(["resume"], db, monkeypatch).exit_code == 0
    assert AmbientStore(db).paused() is False


def test_forget_minutes_removes_recent_events(tmp_path, monkeypatch):
    db = tmp_path / "ambient.sqlite3"
    store = AmbientStore(db)
    store.record("window", app="keep", now=time.time() - 3_600.0)
    store.record("window", app="drop", now=time.time())

    result = run(["forget", "--minutes", "15"], db, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "drop" not in AmbientStore(db).dump_for_test()
    assert "keep" in AmbientStore(db).dump_for_test()


def test_forget_all_clears_everything(tmp_path, monkeypatch):
    db = tmp_path / "ambient.sqlite3"
    AmbientStore(db).record("window", app="a", now=time.time())

    result = run(["forget", "--all"], db, monkeypatch)

    assert result.exit_code == 0, result.output
    assert AmbientStore(db).dump_for_test() == ""


def test_forget_needs_minutes_or_all(tmp_path, monkeypatch):
    result = run(["forget"], tmp_path / "ambient.sqlite3", monkeypatch)

    assert result.exit_code != 0


def test_recent_lists_trailing_activity(tmp_path, monkeypatch):
    db = tmp_path / "ambient.sqlite3"
    AmbientStore(db).record("window", app="code", title="a.py", now=time.time())

    result = run(["recent"], db, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "code" in result.output


def test_visual_context_adapter_reads_the_ambient_buffer(tmp_path, monkeypatch):
    from core.visual_context import AmbientAccessibilityAdapter

    db = tmp_path / "ambient.sqlite3"
    AmbientStore(db).record("window", app="code", title="a.py", now=time.time())
    monkeypatch.setenv("SERENA_AMBIENT_DB_PATH", str(db))

    adapter = AmbientAccessibilityAdapter()
    snapshot = adapter.snapshot()

    assert adapter.name == "ambient-sensor"
    assert "code" in str(snapshot)
