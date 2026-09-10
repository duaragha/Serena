from core.notification_authority import NotificationAuthority, NotificationPolicy
from core.control_plane import ControlPlaneStore
from fleet.attention import notify_blocked_runs
from test_fleet_ready_resume import _park
from datetime import datetime
from dataclasses import replace
import json


def _authority(tmp_path, sent, **policy):
    return NotificationAuthority(tmp_path / "notice.sqlite3",
        control_store=ControlPlaneStore(tmp_path / "control.sqlite3"),
        policy=NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0, **policy),
        senders={"voice": lambda request: sent.append(request) or True})


def test_blocked_workers_alert_once_across_repeated_polls(tmp_path):
    store, rid = _park(tmp_path)
    sent = []
    authority = _authority(tmp_path, sent)
    for _ in range(3):
        notify_blocked_runs(store, lambda *_: authority)
    assert len(sent) == 1
    assert sent[0].kind == "fleet.run.waiting_for_input"
    assert sent[0].job_id == rid
    assert "blocked work" in sent[0].summary
    assert store.get_run(rid)["state"] == "waiting_for_input"


def test_approval_is_not_bypassed_or_repeated(tmp_path):
    store, _ = _park(tmp_path)
    sent = []
    authority = _authority(tmp_path, sent,
        approval_required_kinds=("fleet.run.waiting_for_input",))
    for _ in range(3):
        notify_blocked_runs(store, lambda *_: authority)
    assert sent == []
    assert len(authority.pending_approvals()) == 1


def test_one_blocked_worker_alerts_while_siblings_continue(tmp_path):
    store, rid = _park(tmp_path)
    with store._connect() as db:
        db.execute("UPDATE fleet_runs SET state='running' WHERE run_id=?", (rid,))
    sent = []
    notify_blocked_runs(store, lambda *_: _authority(tmp_path, sent))
    assert len(sent) == 1
    assert store.get_run(rid)["state"] == "running"


def test_failed_transport_reuses_its_bounded_retry_record(tmp_path):
    store, _ = _park(tmp_path)
    calls = []
    authority = _authority(tmp_path, [])
    authority._senders = {channel: lambda request: calls.append(request.channel) or False
                          for channel in ("voice", "telegram")}
    for _ in range(6):
        notify_blocked_runs(store, lambda *_: authority)
        # Only this test's private queue is made due; no resident timing claim.
        with authority._connect() as db:
            db.execute("UPDATE notifications SET deliver_after=0 WHERE decision='failed'")
    assert calls == ["voice", "telegram", "telegram", "telegram"]
    assert len(authority.history()) == 2


def test_cancelled_work_does_not_generate_attention(tmp_path):
    store, rid = _park(tmp_path)
    store.request_cancel(rid)
    sent = []
    notify_blocked_runs(store, lambda *_: _authority(tmp_path, sent))
    assert sent == []


def test_deferred_voice_failure_falls_back_after_restart(tmp_path):
    store, _ = _park(tmp_path)
    calls = []
    authority = _authority(tmp_path, calls)
    hour = datetime.now().hour
    authority.policy = replace(authority.policy, quiet_start_hour=hour,
                               quiet_end_hour=(hour + 1) % 24)
    notify_blocked_runs(store, lambda *_: authority)
    assert calls == []
    assert len(authority.history(decision="deferred")) == 1
    # Recreate authority/store to discard all in-memory state. Only private
    # notification scheduling is advanced; this is not a real-time timer test.
    authority = _authority(tmp_path, calls)
    authority._senders = {
        "voice": lambda request: calls.append(request.channel) or False,
        "telegram": lambda request: calls.append(request.channel) or True,
    }
    with authority._connect() as db:
        db.execute("UPDATE notifications SET deliver_after=0 WHERE decision='deferred'")
    store = type(store)(store.path)
    for _ in range(3):
        notify_blocked_runs(store, lambda *_: authority)
    assert calls == ["voice", "telegram"]


def test_resident_poll_reports_blocker_without_operator_call(tmp_path, monkeypatch):
    import threading
    from fleet import supervisor
    store, rid = _park(tmp_path)
    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH", str(tmp_path / "control.sqlite3"))
    monkeypatch.setattr(supervisor, "_store", lambda: store)
    for name in ("recover_stale_runs", "_reconcile_recent_runs", "_retry_recent_terminal_notices",
                 "_recover_outstanding_obligations", "resume_ready_capacity_waits"):
        monkeypatch.setattr(supervisor, name, lambda *args, **kwargs: None)
    for name in ("fleet.resources.resume_ready_resource_waits",
                 "fleet.ready_resume.resume_ready_input_runs",
                 "fleet.integration_recovery.resume_saved_integrations"):
        monkeypatch.setattr(name, lambda *args: [])
    stop = threading.Event()
    sent = []
    authority = _authority(tmp_path, sent)
    def sender(request):
        sent.append(request)
        stop.set()
        return True
    authority._senders = {"voice": sender}
    monkeypatch.setattr(supervisor, "_terminal_notification_authority", lambda *_: authority)
    timer = threading.Timer(5, stop.set)
    timer.start()
    try:
        supervisor.serve_forever(poll_interval=.01, stop_event=stop)
    finally:
        timer.cancel()
    assert len(sent) == 1
    assert sent[0].job_id == rid


def test_resolved_blocker_is_not_retried_by_fleet(tmp_path):
    store, rid = _park(tmp_path)
    calls = []
    authority = _authority(tmp_path, [])
    authority._senders = {channel: lambda request: calls.append(request.channel) or False
                          for channel in ("voice", "telegram")}
    notify_blocked_runs(store, lambda *_: authority)
    with authority._connect() as db:
        db.execute("UPDATE notifications SET deliver_after=0 WHERE decision='failed'")
    with store._connect() as db:
        db.execute("UPDATE fleet_legs SET state='queued' WHERE run_id=? AND state='waiting_for_input'", (rid,))
    notify_blocked_runs(store, lambda *_: authority)
    assert calls == ["voice", "telegram"]


def test_approved_but_failed_notice_can_retry_without_bypassing_approval(tmp_path):
    store, _ = _park(tmp_path)
    calls = []
    authority = _authority(tmp_path, [], approval_required_kinds=("fleet.run.waiting_for_input",))
    authority._senders = {"voice": lambda request: calls.append(request.channel) or False}
    notify_blocked_runs(store, lambda *_: authority)
    notification_id = authority.pending_approvals()[0]["notification_id"]
    authority.approve(notification_id)
    with authority._connect() as db:
        db.execute("UPDATE notifications SET deliver_after=0 WHERE decision='failed'")
    notify_blocked_runs(store, lambda *_: authority)
    assert calls == ["voice", "voice"]
    # The alternate channel still requests approval; it cannot reuse voice's.
    pending = authority.pending_approvals()
    assert len(pending) == 1 and pending[0]["channel"] == "telegram"


def test_voice_bridge_accepts_blocked_notice_and_preserves_delivery_identity(tmp_path, monkeypatch):
    from voice import brain_bridge
    store, rid = _park(tmp_path)
    monkeypatch.setattr("core.fleet_store.FleetStore", lambda: store)
    notice = {"type": "fleet_notice", "run_id": rid, "state": "waiting_for_input",
              "token": "attention:private-probe", "text": "Worker reported blocked work."}
    assert json.loads(brain_bridge.parse_local_event(json.dumps(notice).encode())) == notice
    brain_bridge._record_fleet_notice(notice, "run.notification.delivered")
    assert store.terminal_notice_delivered(rid, notice["token"], channel="voice")
    notice["state"] = "running"
    assert brain_bridge.parse_local_event(json.dumps(notice).encode()) is None


def test_voice_bridge_fallback_respects_approval_and_cancel(tmp_path, monkeypatch):
    from voice import brain_bridge
    from fleet import supervisor
    store, rid = _park(tmp_path)
    monkeypatch.setattr("core.fleet_store.FleetStore", lambda: store)
    sent = []
    authority = _authority(tmp_path, sent, approval_required_kinds=("fleet.run.waiting_for_input",))
    authority._senders = {"telegram": lambda request: sent.append(request) or True}
    monkeypatch.setattr(supervisor, "_terminal_notification_authority", lambda *_: authority)
    notice = {"run_id": rid, "state": "waiting_for_input", "token": "attention:bridge-fallback",
              "text": "Worker reported blocked work."}
    brain_bridge._fallback_fleet_notice(notice)
    assert sent == []
    assert len(authority.pending_approvals()) == 1
    store.request_cancel(rid)
    count = len(authority.history())
    brain_bridge._fallback_fleet_notice(notice)
    assert len(authority.history()) == count
