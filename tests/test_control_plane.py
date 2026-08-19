from __future__ import annotations

from pathlib import Path

import pytest

from core.control_plane import ControlPlaneStore
from core.fleet_policy import build_policy, builtin_config
from core.fleet_store import FleetStore


def test_event_envelope_is_typed_bounded_and_idempotent(tmp_path):
    store = ControlPlaneStore(tmp_path / "control.sqlite3")
    first = store.append_event(
        event_id="event:one",
        surface="tool",
        event_type="tool.completed",
        lifecycle_state="completed",
        delivery_state="not_applicable",
        session_id="session-1",
        request_id="request-1",
        job_id="job-1",
        provider="codex",
        authority="user_delegated",
        payload={"result": "observed"},
        occurred_at=100.0,
    )
    duplicate = store.append_event(
        event_id="event:one",
        surface="tool",
        event_type="tool.failed",
        lifecycle_state="failed",
        payload={"result": "must not replace the committed event"},
        occurred_at=200.0,
    )

    assert duplicate == first
    assert first.to_dict() == {
        "event_id": "event:one",
        "surface": "tool",
        "event_type": "tool.completed",
        "session_id": "session-1",
        "turn_id": None,
        "request_id": "request-1",
        "job_id": "job-1",
        "provider": "codex",
        "authority": "user_delegated",
        "lifecycle_state": "completed",
        "delivery_state": "not_applicable",
        "payload": {"result": "observed"},
        "occurred_at": 100.0,
    }
    assert len(store.events(job_id="job-1")) == 1
    with pytest.raises(ValueError, match="surface"):
        store.append_event(
            surface="internet",
            event_type="bad.event",
            lifecycle_state="bad",
        )


def test_obligation_ledger_preserves_open_and_ambiguous_work(tmp_path):
    store = ControlPlaneStore(tmp_path / "control.sqlite3")
    obligation = store.create_obligation(
        obligation_id="obligation:one",
        surface="chat",
        kind="final_response",
        summary="reply after the background task",
        session_id="session-1",
        request_id="request-1",
    )
    assert obligation.state == "open"
    assert store.obligations(state="open") == [obligation]

    ambiguous = store.resolve_obligation(
        obligation.obligation_id,
        state="ambiguous",
        error="the process restarted after generation but before delivery confirmation",
    )
    assert ambiguous.state == "ambiguous"
    assert ambiguous.resolved_at is not None
    assert "before delivery" in str(ambiguous.last_error)
    assert store.obligations(state="open") == []


def _fleet_run(store: FleetStore, root: Path, *, dry_run: bool = False):
    return store.create_run(
        task="implement the bounded parser fix",
        activity="coding",
        cwd=str(root),
        origin_session_id="origin-session",
        origin_agent="codex",
        dry_run=dry_run,
        policy=build_policy(
            "coding",
            "implement the bounded parser fix",
            config=builtin_config(),
            provider_mode="codex",
            worker_count=1,
        ).to_dict(),
    )


def test_fleet_outbox_survives_restart_and_drives_final_delivery_obligation(tmp_path):
    fleet_path = tmp_path / "fleet.sqlite3"
    control = ControlPlaneStore(tmp_path / "control-plane.sqlite3")
    store = FleetStore(fleet_path)
    run = _fleet_run(store, tmp_path)
    assert store.pending_control_events() == 1

    reopened = FleetStore(fleet_path)
    assert reopened.flush_control_outbox(control) == 1
    assert reopened.pending_control_events() == 0
    created = control.events(surface="fleet", job_id=run["run_id"])
    assert [event.event_type for event in created] == ["run.created"]
    assert created[0].session_id == "origin-session"
    obligation = control.obligations(state="open", surface="fleet")
    assert len(obligation) == 1
    assert obligation[0].job_id == run["run_id"]
    assert obligation[0].kind == "final_result_delivery"

    reopened.append_event(
        run["run_id"],
        "run.notification.failed",
        {"state": "failed", "channel": "telegram", "error": "offline"},
    )
    reopened.flush_control_outbox(control)
    attempted = control.obligations(state="open", surface="fleet")[0]
    assert attempted.attempts == 1
    assert attempted.last_error == "offline"

    reopened.append_event(
        run["run_id"],
        "run.notification.delivered",
        {"state": "completed", "channel": "voice"},
    )
    reopened.flush_control_outbox(control)
    fulfilled = control.obligations(state="fulfilled", surface="fleet")
    assert len(fulfilled) == 1
    assert fulfilled[0].fulfillment_event_id is not None
    assert control.obligations(state="open", surface="fleet") == []


def test_dry_run_creates_events_but_no_delivery_obligation(tmp_path):
    control = ControlPlaneStore(tmp_path / "control.sqlite3")
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _fleet_run(store, tmp_path, dry_run=True)

    assert store.flush_control_outbox(control) == 1
    assert control.events(surface="fleet", job_id=run["run_id"])
    assert control.obligations(surface="fleet") == []
