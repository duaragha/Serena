"""A schedule that switches itself off has to say so.

On 2026-09-18 five consecutive "duplicate task id requires reconciliation"
ticks pushed `serena.fleet.start` and `serena.fleet.reconcile` past
MAX_CONSECUTIVE_FAILURES. Both went to `disabled`, nothing dispatched for the
next half hour, and the queue looked merely empty: a task sat `ready` while no
scheduler would ever claim it again. The disable itself was never reported
anywhere but the schedule row.
"""

from core.serena_scheduler import (
    ActionOutcome,
    MAX_CONSECUTIVE_FAILURES,
    SerenaScheduler,
)


class _Notifier:
    def __init__(self):
        self.sent = []

    def request(self, request):
        self.sent.append(request)
        return request


def _scheduler(tmp_path, handler, notifier):
    scheduler = SerenaScheduler(
        path=tmp_path / "scheduler.sqlite3",
        handlers={"serena.probe": handler},
        notifier=notifier,
    )
    record = scheduler.add_schedule(
        action="serena.probe",
        interval_seconds=60,
        actor="test",
        requires_approval=False,
        first_run_at=1_000_000.0,
    )
    return scheduler, record["schedule_id"]


def _drive(scheduler, schedule_id, ticks):
    """Run `ticks` due passes, walking the clock past each interval."""

    now = 1_000_000.0
    for _ in range(ticks):
        scheduler.tick(now=now)
        now += 61.0
    return scheduler.require(schedule_id)


def test_the_disable_is_announced_with_the_failure_that_caused_it(tmp_path):
    notifier = _Notifier()
    failing = lambda payload: ActionOutcome(False, "duplicate task id requires reconciliation")
    scheduler, schedule_id = _scheduler(tmp_path, failing, notifier)

    row = _drive(scheduler, schedule_id, MAX_CONSECUTIVE_FAILURES)

    assert row["state"] == "disabled"
    assert row["consecutive_failures"] == MAX_CONSECUTIVE_FAILURES
    disabled = [r for r in notifier.sent if r.kind == "schedule.disabled"]
    assert len(disabled) == 1, [r.kind for r in notifier.sent]
    summary = disabled[0].summary
    assert "serena.probe" in summary
    assert "switched off" in summary
    assert "duplicate task id" in summary, summary
    # It has to reach his phone, not the voice bridge he is not sitting at.
    assert disabled[0].channel == "imessage"


def test_a_schedule_short_of_the_cap_stays_quiet(tmp_path):
    notifier = _Notifier()
    failing = lambda payload: ActionOutcome(False, "transient")
    scheduler, schedule_id = _scheduler(tmp_path, failing, notifier)

    row = _drive(scheduler, schedule_id, MAX_CONSECUTIVE_FAILURES - 1)

    assert row["state"] == "active"
    assert [r for r in notifier.sent if r.kind == "schedule.disabled"] == []


def test_the_notice_fires_once_not_on_every_later_tick(tmp_path):
    notifier = _Notifier()
    failing = lambda payload: ActionOutcome(False, "duplicate task id requires reconciliation")
    scheduler, schedule_id = _scheduler(tmp_path, failing, notifier)

    _drive(scheduler, schedule_id, MAX_CONSECUTIVE_FAILURES + 4)

    assert scheduler.require(schedule_id)["state"] == "disabled"
    assert len([r for r in notifier.sent if r.kind == "schedule.disabled"]) == 1


def test_a_healthy_schedule_never_announces_anything(tmp_path):
    notifier = _Notifier()
    scheduler, schedule_id = _scheduler(
        tmp_path, lambda payload: ActionOutcome(True, "fine"), notifier
    )

    row = _drive(scheduler, schedule_id, MAX_CONSECUTIVE_FAILURES + 2)

    assert row["state"] == "active"
    assert row["consecutive_failures"] == 0
    assert [r for r in notifier.sent if r.kind == "schedule.disabled"] == []
