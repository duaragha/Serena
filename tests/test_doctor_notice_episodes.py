"""Continuing health failures must not become hourly texts."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import doctor, scheduler_actions
from core.control_plane import ControlPlaneStore
from core.notification_authority import (
    NotificationAuthority,
    NotificationPolicy,
    NotificationRequest,
)
from core.serena_scheduler import SerenaScheduler


@pytest.fixture
def health(tmp_path, monkeypatch):
    clock = [1_000_000.0]
    findings = [doctor.Finding("schedules.disabled", False, "two disabled: start, reconcile")]
    delivered = []
    path = tmp_path / "notifications.sqlite3"
    monkeypatch.setenv("SERENA_NOTIFICATION_DB_PATH", str(path))
    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH", str(tmp_path / "control.sqlite3"))
    monkeypatch.setattr("core.machine_context.machine_name", lambda: "PC")
    monkeypatch.setattr(doctor, "run", lambda: doctor.Report(findings=list(findings)))
    monkeypatch.setattr("core.notification_authority.time.time", lambda: clock[0])
    policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0, hourly_limit=100)
    authority = NotificationAuthority(
        path, policy=policy,
        senders={"telegram": lambda notice: delivered.append(notice) or True},
        control_store=ControlPlaneStore(tmp_path / "control.sqlite3"),
    )

    def scheduler():
        return SerenaScheduler(
            tmp_path / "scheduler.sqlite3",
            handlers={"serena.doctor": scheduler_actions.run_doctor}, notifier=authority,
        )

    instance = scheduler()
    instance.add_schedule(action="serena.doctor", interval_seconds=60, actor="test",
                          requires_approval=False, first_run_at=clock[0])
    return clock, findings, delivered, authority, scheduler


def _check(health, advance=61):
    clock, _, _, _, scheduler = health
    result = scheduler().tick(now=clock[0])
    assert len(result) == 1 and result[0].ok
    clock[0] += advance


def test_unchanged_failure_stays_quiet_after_restart_and_hourly_dedupe_expires(health):
    clock, _, delivered, _, scheduler = health
    _check(health, advance=7201)
    _check(health, advance=86401)
    _check(health)
    assert len(delivered) == 1
    row = scheduler().list()[0]
    assert row["last_output"]["failures"] == ["schedules.disabled"]
    assert row["consecutive_failures"] == 0
    assert row["state"] == "active"


def test_changed_failure_content_is_announced_within_the_same_hour(health):
    _, findings, delivered, _, _ = health
    _check(health)
    findings[0].detail = "one disabled: start"
    _check(health)
    assert len(delivered) == 2
    assert delivered[0].dedupe_key != delivered[1].dedupe_key


def test_successful_check_clears_episode_and_recurrence_is_announced(health):
    _, findings, delivered, _, _ = health
    original = findings[:]
    _check(health)
    findings.clear()
    _check(health)
    findings[:] = original
    _check(health)
    assert len(delivered) == 2
    assert delivered[0].dedupe_key != delivered[1].dedupe_key


def test_initial_healthy_check_and_warning_changes_do_not_text(health):
    _, findings, delivered, _, _ = health
    original = findings[:]
    findings.clear()
    _check(health)
    assert delivered == []
    findings[:] = original
    _check(health)
    findings.append(doctor.Finding("repo.stale_fetch", False, "14 hours", severity="warn"))
    _check(health, advance=7201)
    findings[-1].detail = "16 hours"
    _check(health)
    assert len(delivered) == 1


def test_concurrent_doctors_share_the_same_durable_episode(health):
    _, _, delivered, authority, _ = health

    def run():
        return scheduler_actions.run_doctor({})

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: run(), range(8)))
    keys = {outcome.notify["dedupe_key"] for outcome in outcomes}
    assert len(keys) == 1
    for outcome in outcomes:
        authority.request(NotificationRequest(**outcome.notify))
    assert len(delivered) == 1
    assert run().notify is None


def test_exhausted_delivery_failure_does_not_silence_the_outage(health):
    _, _, delivered, authority, _ = health
    authority.policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0, max_attempts=1)
    authority._senders["telegram"] = lambda notice: False
    _check(health)
    authority._senders["telegram"] = lambda notice: delivered.append(notice) or True
    _check(health)
    _check(health, advance=7201)
    _check(health)
    assert len(delivered) == 1


def test_hourly_transport_limit_does_not_count_as_an_announced_episode(health):
    _, _, delivered, authority, _ = health
    authority.policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0, hourly_limit=0)
    _check(health)
    assert delivered == []
    authority.policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0, hourly_limit=100)
    _check(health)
    _check(health)
    assert len(delivered) == 1


def test_exhausted_attempts_are_retried_even_if_a_legacy_deadline_remains(health):
    _, _, delivered, authority, _ = health
    authority._senders["telegram"] = lambda notice: False
    _check(health)
    with authority._connect() as db:
        db.execute("UPDATE notifications SET attempts = ?", (authority.policy.max_attempts,))
    authority._senders["telegram"] = lambda notice: delivered.append(notice) or True
    _check(health)
    assert len(delivered) == 1


def test_retryable_failure_uses_the_authoritys_existing_delivery_retry(health):
    clock, _, delivered, authority, _ = health
    authority._senders["telegram"] = lambda notice: False
    _check(health)
    # It has not arrived, but the authority still owns a durable retry.
    assert scheduler_actions.run_doctor({}).notify is None
    authority._senders["telegram"] = lambda notice: delivered.append(notice) or True
    results = authority.deliver_due(now=clock[0])
    assert len(results) == 1 and results[0].sent
    _check(health, advance=7201)
    _check(health)
    assert len(delivered) == 1


def test_quiet_hours_queue_a_single_notice_even_after_a_day(health):
    _, _, delivered, authority, _ = health
    authority.policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=23)
    _check(health, advance=86401)
    _check(health)
    assert delivered == []
    with authority._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1


def test_recovery_withdraws_its_queued_notice_before_quiet_hours_end(health):
    clock, findings, delivered, authority, _ = health
    authority.policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=23)
    _check(health)
    findings.clear()
    _check(health)
    assert authority.deliver_due(now=clock[0] + 86400) == []
    assert delivered == []
    assert authority.history()[0]["decision"] == "suppressed"
    assert authority._control_store.obligations(surface="notification")[0].state == "cancelled"


def test_changed_failure_retires_only_its_old_queued_episode(health):
    clock, findings, delivered, authority, _ = health
    authority.policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=23)
    _check(health)
    unrelated = authority.request(NotificationRequest(
        kind="task.update", summary="unrelated task result", channel="telegram", dedupe_key="task:1"))
    findings[0].detail = "one disabled: start"
    _check(health)
    results = authority.deliver_due(now=clock[0] + 86400)
    assert len(results) == 2 and all(result.sent for result in results)
    assert {notice.summary for notice in delivered} == {
        "PC: schedules.disabled: one disabled: start", "unrelated task result"}
    assert authority.withdraw(unrelated.notification_id, reason="already delivered").sent


def test_recovery_withdraws_a_failed_notices_transport_retry(health):
    clock, findings, delivered, authority, _ = health
    authority._senders["telegram"] = lambda notice: False
    _check(health)
    findings.clear()
    _check(health)
    authority._senders["telegram"] = lambda notice: delivered.append(notice) or True
    assert authority.deliver_due(now=clock[0] + 86400) == []
    assert delivered == []


def test_recovery_keeps_previous_episode_until_an_inflight_send_finishes(health, monkeypatch):
    clock, findings, delivered, authority, _ = health
    authority.policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=23)
    _check(health)
    notice_id = authority.history()[0]["notification_id"]
    authority.policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0)
    with authority._connect() as db:
        db.execute("UPDATE notifications SET deliver_after = 0")
    entered, release = threading.Event(), threading.Event()

    def send(notice):
        entered.set()
        assert release.wait(5)
        delivered.append(notice)
        return True

    authority._senders["telegram"] = send
    from core import notification_authority

    original_lock = notification_authority.exclusive_lock
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(authority.redeliver, notice_id, now=clock[0])
        try:
            assert entered.wait(5)
            monkeypatch.setattr(notification_authority, "exclusive_lock",
                                lambda handle, timeout=None: original_lock(handle, timeout=0))
            findings.clear()
            _check(health)
            state_path = authority.path.with_name(authority.path.stem + "-doctor.sqlite3")
            with sqlite3.connect(state_path) as db:
                assert db.execute("SELECT COUNT(*) FROM doctor_notices").fetchone()[0] == 1
        finally:
            release.set()
        receipt = running.result(timeout=5)
    assert receipt.sent
    _check(health)
    with sqlite3.connect(state_path) as db:
        assert db.execute("SELECT COUNT(*) FROM doctor_notices").fetchone()[0] == 0
    assert len(delivered) == 1
    row = authority.history()[0]
    assert row["decision"] == "sent" and row["delivered_at"] is not None


def test_pending_approval_can_be_withdrawn_without_delivery(health):
    clock, _, delivered, authority, _ = health
    authority.policy = NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0,
                                         approval_required_kinds=("test.pending",))
    notice = authority.request(NotificationRequest(
        kind="test.pending", summary="obsolete approval", channel="telegram", dedupe_key="obsolete"))
    assert notice.decision == "pending_approval"
    result = authority.withdraw(notice.notification_id, reason="condition cleared")
    assert result.decision == "suppressed"
    assert authority.approve(notice.notification_id, now=clock[0]).decision == "suppressed"
    assert authority.deliver_due(now=clock[0] + 86400) == []
    assert delivered == []


def test_elapsed_brain_downtime_does_not_create_a_new_failure_episode(health):
    _, findings, delivered, _, _ = health
    findings[:] = [doctor.Finding("brain.down", False, "her brain is down for 2 hours: crash")]
    _check(health, advance=7201)
    findings[0].detail = "her brain is down for 4 hours: crash"
    _check(health)
    assert len(delivered) == 1
    findings[0].detail = "her brain is down for 4 hours: policy failed"
    _check(health)
    assert len(delivered) == 2
