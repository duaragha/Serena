from core.notification_authority import NotificationAuthority, NotificationPolicy
from core.control_plane import ControlPlaneStore
from fleet.attention import notify_blocked_runs
from test_fleet_ready_resume import _park
from datetime import datetime
from dataclasses import replace


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
