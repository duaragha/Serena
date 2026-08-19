"""Fleet's terminal alert must reach Raghav exactly once.

Regression coverage for a real defect: the idempotency key for a terminal
notice was stored in an event payload field named `token`, which Fleet's
secret filter correctly redacts on write. The dedupe lookup then compared the
live key against `[redacted:sensitive_field]`, never matched, and re-sent the
same alert on every pass, including on every service restart.

Nagging is the failure mode here, so this stays covered.
"""

from __future__ import annotations

import os

import pytest

from core import fleet_supervisor as supervisor
from core.fleet_workers import WorkerResult


@pytest.fixture
def notice_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_DB_PATH", str(tmp_path / "fleet.sqlite3"))
    monkeypatch.setenv("SERENA_FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SERENA_FLEET_NO_AUTOSTART", "1")
    monkeypatch.setenv("SERENA_FLEET_ISOLATION", "off")
    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH", str(tmp_path / "control.sqlite3"))
    monkeypatch.setenv("SERENA_QUIET_START_HOUR", "0")
    monkeypatch.setenv("SERENA_QUIET_END_HOUR", "0")
    monkeypatch.delenv("SERENA_FLEET_INLINE", raising=False)
    monkeypatch.delenv("SERENA_FLEET_WORKER", raising=False)
    monkeypatch.setattr(supervisor, "_STORE_INSTANCE", None)
    monkeypatch.setattr(supervisor, "_surface_session", lambda *_a, **_k: None)
    monkeypatch.setattr(supervisor, "_release_session", lambda *_a, **_k: None)
    monkeypatch.setattr(supervisor, "_refresh_index", lambda: None)
    monkeypatch.setattr(supervisor, "_completion_verdict", lambda *_a, **_k: None)
    return tmp_path


def _worker(request, *, cancel_requested, on_event):
    del cancel_requested
    sid = f"session-{request.leg_id}"
    on_event("process.started", {"pid": os.getpid(), "event_log_path": "/event"})
    on_event("session.started", {"session_id": sid})
    return WorkerResult(True, "worker transcript", sid, request.model, request.effort, 0)


def _finished_run(env, monkeypatch):
    monkeypatch.setattr(supervisor, "run_worker", _worker)
    run = supervisor.start_run("a harmless alert marker", activity="coding", cwd=str(env))
    return supervisor.run_supervisor(run["run_id"])


def test_a_repeated_terminal_pass_does_not_text_him_twice(notice_env, monkeypatch):
    texts: list[str] = []
    monkeypatch.setattr(supervisor, "_send_spoken_notice", lambda _run, _token: False)
    monkeypatch.setattr(
        supervisor, "_send_raghav_text", lambda message: texts.append(message) or True
    )

    completed = _finished_run(notice_env, monkeypatch)
    assert completed["state"] == "completed"
    assert len(texts) == 1

    # Whatever calls this again (a boot-time retry sweep, a second pass) must
    # find the delivery already recorded.
    for _ in range(3):
        supervisor._terminal_outcome(supervisor._store(), completed)
    assert len(texts) == 1, "Serena must not re-text the same finished run"
    assert supervisor._retry_recent_terminal_notices(supervisor._store()) is False
    hooks = [
        event
        for event in supervisor._store().events(completed["run_id"], limit=2_000)
        if event["type"] == "run.plugin_hook.dispatched"
    ]
    assert len(hooks) == 1
    assert hooks[0]["payload"]["hook"] == "fleet.run.completed"


def test_the_delivery_receipt_is_recorded_exactly_once(notice_env, monkeypatch):
    monkeypatch.setattr(supervisor, "_send_spoken_notice", lambda _run, _token: False)
    monkeypatch.setattr(supervisor, "_send_raghav_text", lambda _message: True)

    completed = _finished_run(notice_env, monkeypatch)
    supervisor._terminal_outcome(supervisor._store(), completed)

    events = supervisor._store().events(completed["run_id"], limit=2_000)
    delivered = [
        event for event in events if event["type"] == "run.notification.delivered"
    ]
    assert len(delivered) == 1


def test_the_idempotency_key_survives_secret_filtering(notice_env, monkeypatch):
    """The key must not be stored under a name the secret filter redacts."""

    monkeypatch.setattr(supervisor, "_send_spoken_notice", lambda _run, _token: False)
    monkeypatch.setattr(supervisor, "_send_raghav_text", lambda _message: True)

    completed = _finished_run(notice_env, monkeypatch)
    events = supervisor._store().events(completed["run_id"], limit=2_000)
    notices = [
        event for event in events if str(event["type"]).startswith("run.notification")
    ]
    assert notices
    for event in notices:
        recorded = str(event["payload"].get("notice_id") or "")
        assert recorded, "the notice payload must carry a usable idempotency key"
        assert "redacted" not in recorded

    store = supervisor._store()
    assert store.terminal_notice_delivered(
        completed["run_id"], supervisor._notice_token(completed), channel="telegram"
    )


def test_a_spoken_notice_suppresses_the_telegram_fallback(notice_env, monkeypatch):
    """Telegram stays the fallback, not a second channel for the same event."""

    texts: list[str] = []
    monkeypatch.setattr(supervisor, "_send_spoken_notice", lambda _run, _token: True)
    monkeypatch.setattr(
        supervisor, "_send_raghav_text", lambda message: texts.append(message) or True
    )

    completed = _finished_run(notice_env, monkeypatch)
    supervisor._terminal_outcome(supervisor._store(), completed)
    assert texts == []
