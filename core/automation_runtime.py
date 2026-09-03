"""The resident loop that actually makes Serena's automation run.

Everything underneath this was already built and tested: a leased scheduler, a
notification authority with quiet hours and retries, surface journals, an
obligation ledger. None of it moved on its own, because nothing called it on a
clock. This is that clock.

Bounded is the whole design:

- One pass does a fixed, small amount of work and returns. There is no queue it
  can fall behind on and no unbounded catch-up.
- Providers are checked before scheduled work runs. When both Claude and Codex
  are out of capacity, scheduled actions are held rather than burned, and the
  hold lifts by itself when capacity comes back.
- Every user-facing message goes through the notification authority, so a
  runaway schedule still cannot get past quiet hours or the hourly limit.
- On boot it reconciles what died mid-delivery, once, before starting.

It owns no state of its own. Kill it at any point and the next start picks up
from the durable stores.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_POLL_SECONDS = 30.0
MIN_POLL_SECONDS = 1.0
# A capacity check costs a subprocess or a file read, so it is not worth doing
# every pass at a 30 second cadence.
CAPACITY_PROBE_SECONDS = 300.0
NOTIFICATION_BATCH = 20


@dataclass
class PassReport:
    """What one pass of the loop actually did."""

    ran: list[str] = field(default_factory=list)
    notifications: int = 0
    published: dict[str, int] = field(default_factory=dict)
    capacity_held: bool = False
    capacity_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def providers_exhausted() -> tuple[bool, str]:
    """True when neither provider has capacity for scheduled work.

    Read-only and best-effort. If capacity cannot be determined, the answer is
    "not exhausted": holding all automation because a probe failed would be a
    worse failure than running one action against a tired provider.
    """

    try:
        from fleet.capacity import read_fleet_capacity

        capacity = read_fleet_capacity()
    except Exception:
        return False, ""
    if not capacity:
        return False, ""
    available: list[str] = []
    reasons: list[str] = []
    for name, entry in capacity.items():
        # `usable` is false only for a positive, unexpired exhaustion signal,
        # so an unknown provider counts as available. That is the fail-open
        # direction we want here.
        if bool(getattr(entry, "usable", True)):
            available.append(str(name))
        else:
            detail = str(getattr(entry, "reason", "") or "usage limit")
            reasons.append(f"{name}: {detail}")
    if available:
        return False, ""
    return True, "; ".join(reasons) or "no provider has capacity"


class AutomationRuntime:
    """One bounded pass, repeated. Nothing clever, on purpose."""

    def __init__(
        self,
        *,
        scheduler: Any | None = None,
        authority: Any | None = None,
        control_store: Any | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        capacity_probe_seconds: float = CAPACITY_PROBE_SECONDS,
        capacity_reader: Any | None = None,
        publish_journals: bool = True,
    ) -> None:
        self._scheduler = scheduler
        self._authority = authority
        self._control_store = control_store
        self.poll_seconds = max(MIN_POLL_SECONDS, float(poll_seconds))
        self.capacity_probe_seconds = max(0.0, float(capacity_probe_seconds))
        self._capacity_reader = capacity_reader or providers_exhausted
        self._publish_journals = publish_journals
        self._next_capacity_probe = 0.0
        self._capacity_held = False
        self._capacity_reason = ""

    # -- wiring -------------------------------------------------------------

    def scheduler(self) -> Any:
        if self._scheduler is None:
            from core.scheduler_actions import register_all
            from core.serena_scheduler import SerenaScheduler

            self._scheduler = register_all(
                SerenaScheduler(notifier=self.authority())
            )
        return self._scheduler

    def authority(self) -> Any:
        if self._authority is None:
            from core.notification_senders import default_authority

            self._authority = default_authority()
        return self._authority

    # -- startup ------------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """Pick up what died mid-delivery, exactly once, before the loop runs."""

        try:
            from core.control_plane import ControlPlaneStore
            from core.control_recovery import (
                journal_redelivery_handlers,
                reconcile_obligations,
            )

            control = self._control_store or ControlPlaneStore()
            handlers = dict(journal_redelivery_handlers(control))
            authority = self.authority()

            def redeliver_notification(obligation: Any) -> bool:
                result = authority.redeliver(str(obligation.job_id or ""))
                return bool(result and result.sent)

            handlers["notification"] = redeliver_notification
            return reconcile_obligations(control, handlers=handlers).to_dict()
        except Exception as error:
            return {"recovered": [], "abandoned": [], "skipped": [], "error": str(error)}

    # -- one pass -----------------------------------------------------------

    def run_pass(self, *, now: float | None = None) -> PassReport:
        moment = float(time.time() if now is None else now)
        report = PassReport()

        held, reason = self._capacity(moment)
        report.capacity_held = held
        report.capacity_reason = reason

        if not held:
            try:
                # `drain` rather than `tick` so a fan-out finishes in this pass
                # instead of one link per poll interval.
                runs = self.scheduler().drain(now=moment)
                report.ran = [f"{run.action}:{'ok' if run.ok else 'failed'}" for run in runs]
            except Exception as error:
                report.errors.append(f"scheduler: {error}")

        try:
            delivered = self.authority().deliver_due(now=moment, limit=NOTIFICATION_BATCH)
            report.notifications = len([item for item in delivered if item.sent])
        except Exception as error:
            report.errors.append(f"notifications: {error}")

        if self._publish_journals:
            try:
                from core.surface_journal import flush_all

                report.published = flush_all()
            except Exception as error:
                report.errors.append(f"journals: {error}")

        return report

    def _capacity(self, moment: float) -> tuple[bool, str]:
        """Hold scheduled work while both providers are out, and let go after."""

        if self.capacity_probe_seconds <= 0:
            return False, ""
        if moment < self._next_capacity_probe:
            return self._capacity_held, self._capacity_reason
        self._next_capacity_probe = moment + self.capacity_probe_seconds
        try:
            held, reason = self._capacity_reader()
        except Exception:
            held, reason = False, ""
        self._capacity_held = bool(held)
        self._capacity_reason = str(reason or "")
        return self._capacity_held, self._capacity_reason

    # -- resident loop ------------------------------------------------------

    def serve_forever(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_passes: int = 0,
    ) -> int:
        """Run passes until asked to stop. `max_passes` bounds it for tests."""

        stopper = stop_event or threading.Event()
        self.recover()
        passes = 0
        while not stopper.is_set():
            self.run_pass()
            passes += 1
            if max_passes and passes >= max_passes:
                break
            if stopper.wait(self.poll_seconds):
                break
        return passes


def serve_forever(
    poll_interval: float = DEFAULT_POLL_SECONDS,
    stop_event: threading.Event | None = None,
) -> None:
    """Entry point for ``serena-automation.service``."""

    AutomationRuntime(poll_seconds=poll_interval).serve_forever(stop_event=stop_event)
