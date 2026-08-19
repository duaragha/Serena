"""The reviewed actions a Serena schedule is allowed to name.

This registry is the entire extensibility story for automation. A schedule
names one of these strings; it cannot supply a command, a path, or a callable.
Adding an action means adding a function here, with a test, in a reviewed
change. A plugin can ask for a schedule against an existing action; it cannot
become one.

Every action here is safe to run unattended: none of them write to his memory,
none of them start a coding job, and none of them send anything except through
the notification authority.
"""

from __future__ import annotations

from typing import Any

from core.serena_scheduler import ActionOutcome

# Below this, telling him about outstanding work is noise rather than useful.
OWED_NOTICE_THRESHOLD = 1
# Only complain about something that has genuinely been sitting.
OWED_STALE_SECONDS = 3_600.0


def _number(value: object, fallback: float) -> float:
    """A payload number, where an explicit 0 means 0 and not "unset"."""

    if value is None or isinstance(value, bool):
        return float(fallback)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(fallback)


def sweep_obligations(payload: dict[str, Any]) -> ActionOutcome:
    """Retry what died mid-delivery and let hopeless things resolve honestly.

    This is the recurring counterpart to the sweep the resident loop does once
    at boot. Without it, an obligation that went stale hours after the last
    restart would sit open forever.
    """

    from core.control_plane import ControlPlaneStore
    from core.control_recovery import (
        DEFAULT_MAX_ATTEMPTS,
        DEFAULT_STALE_SECONDS,
        journal_redelivery_handlers,
        reconcile_obligations,
    )

    control = ControlPlaneStore()
    handlers = dict(journal_redelivery_handlers(control))
    stale_seconds = _number(payload.get("stale_seconds"), DEFAULT_STALE_SECONDS)
    max_attempts = int(_number(payload.get("max_attempts"), DEFAULT_MAX_ATTEMPTS))
    # If the notification authority cannot be reached, the sweep still runs for
    # every other surface, but it must not quietly claim it covered
    # notifications. The degradation is reported in the outcome rather than
    # swallowed, so "swept N outstanding" never overstates what was attempted.
    notifications_degraded = ""
    try:
        from core.notification_senders import default_authority

        authority = default_authority()

        def redeliver_notification(obligation: Any) -> bool:
            result = authority.redeliver(str(obligation.job_id or ""))
            return bool(result and result.sent)

        handlers["notification"] = redeliver_notification
    except Exception as error:
        notifications_degraded = f"{type(error).__name__}: {error}"
        handlers.pop("notification", None)

    report = reconcile_obligations(
        control,
        handlers=handlers,
        stale_seconds=stale_seconds,
        max_attempts=max_attempts,
    )
    detail = (
        f"swept {report.total} outstanding: {len(report.recovered)} retried, "
        f"{len(report.abandoned)} given up on"
    )
    if notifications_degraded:
        detail += f"; could not redeliver notifications ({notifications_degraded})"
    return ActionOutcome(
        True,
        detail,
        output={
            "recovered": len(report.recovered),
            "abandoned": len(report.abandoned),
            "skipped": len(report.skipped),
            "notifications_degraded": notifications_degraded,
        },
    )


def report_outstanding(payload: dict[str, Any]) -> ActionOutcome:
    """Tell him what Serena still owes, but only when it is worth saying.

    Silence is the common case and the correct one. This speaks only when
    something has actually been sitting unresolved, so it stays a signal rather
    than a daily status ping he learns to ignore.
    """

    import time

    from core.control_recovery import outstanding_debts

    # `or` would treat an explicit 0 as "not supplied", which is exactly the
    # value a caller uses to mean "report everything".
    threshold = int(_number(payload.get("threshold"), OWED_NOTICE_THRESHOLD))
    stale_after = _number(payload.get("stale_seconds"), OWED_STALE_SECONDS)
    now = time.time()

    debts = outstanding_debts()
    stale = [
        item
        for item in debts["items"]
        if now - float(item.get("created_at") or now) >= stale_after
    ]
    if len(stale) < threshold and not debts["unconfirmed"]:
        return ActionOutcome(True, "nothing outstanding worth interrupting him for")

    return ActionOutcome(
        True,
        debts["spoken"],
        notify={
            "kind": "serena.owed",
            "summary": debts["spoken"],
            "channel": str(payload.get("channel") or "voice"),
            "urgency": "low",
            # One notice per distinct shape of debt, so a persistent backlog
            # does not become a recurring nag.
            "dedupe_key": f"owed:{debts['open']}:{debts['unconfirmed']}",
        },
        output={"open": debts["open"], "unconfirmed": debts["unconfirmed"]},
    )


def flush_notifications(payload: dict[str, Any]) -> ActionOutcome:
    """Deliver notices whose quiet-hours or retry deadline has passed."""

    from core.notification_senders import default_authority

    results = default_authority().deliver_due()
    delivered = len([item for item in results if item.sent])
    return ActionOutcome(
        True,
        f"delivered {delivered} of {len(results)} due notices",
        output={"delivered": delivered, "attempted": len(results)},
    )


def publish_surface_events(payload: dict[str, Any]) -> ActionOutcome:
    """Push any committed-but-unpublished surface events onto the control plane."""

    from core.surface_journal import flush_all

    published = flush_all()
    return ActionOutcome(
        True, f"published {sum(published.values())} staged events", output=published
    )


# The whole registry. A schedule may name exactly one of these keys.
REVIEWED_ACTIONS = {
    "serena.obligations.sweep": sweep_obligations,
    "serena.obligations.report": report_outstanding,
    "serena.notifications.flush": flush_notifications,
    "serena.surfaces.publish": publish_surface_events,
}


def register_all(scheduler: Any) -> Any:
    """Attach every reviewed action to a scheduler."""

    for name, handler in REVIEWED_ACTIONS.items():
        scheduler.register_action(name, handler)
    return scheduler
