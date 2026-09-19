"""Reviewed-action wiring: serena.support.checkin and serena.proactive.scan."""

from __future__ import annotations

import time

import core.notification_senders as senders
import core.supportive_mode as supportive_module
from core.ambient_store import AmbientStore
from core.control_plane import ControlPlaneStore
from core.notification_authority import (
    NotificationAuthority,
    NotificationPolicy,
    NotificationRequest,
    PresenceState,
)
from core.scheduler_actions import REVIEWED_ACTIONS


def install_authority(tmp_path, monkeypatch, **kwargs):
    kwargs.setdefault("policy", NotificationPolicy(quiet_start_hour=0, quiet_end_hour=0))
    kwargs.setdefault("senders", {"telegram": lambda request: True})
    authority = NotificationAuthority(
        tmp_path / "notices.sqlite3",
        control_store=ControlPlaneStore(tmp_path / "control.sqlite3"),
        **kwargs,
    )
    monkeypatch.setattr(senders, "_AUTHORITY", authority)
    monkeypatch.setenv("SERENA_POLICY_DB_PATH", str(tmp_path / "policy.sqlite3"))
    monkeypatch.setenv("SERENA_AMBIENT_DB_PATH", str(tmp_path / "ambient.sqlite3"))
    monkeypatch.setattr(
        supportive_module, "DEFAULT_SUPPORT_DB", tmp_path / "support.sqlite3")
    return authority


def test_actions_are_registered_under_reviewed_names():
    assert "serena.support.checkin" in REVIEWED_ACTIONS
    assert "serena.proactive.scan" in REVIEWED_ACTIONS


def test_checkin_delivers_when_due_and_records_it(tmp_path, monkeypatch):
    install_authority(tmp_path, monkeypatch)
    support = supportive_module.SupportiveModeStore()
    support.configure(enabled=True, allow_checkins=True, now=time.time() - 100_000.0)
    assert support.checkin_due() is True

    outcome = REVIEWED_ACTIONS["serena.support.checkin"]({})

    assert outcome.ok is True
    assert outcome.output["delivered"] is True
    assert support.checkin_due() is False


def test_checkin_is_quiet_when_none_is_due(tmp_path, monkeypatch):
    install_authority(tmp_path, monkeypatch)

    outcome = REVIEWED_ACTIONS["serena.support.checkin"]({})

    assert outcome.ok is True
    assert outcome.output["delivered"] is False


def test_checkin_never_fires_during_quiet_hours(tmp_path, monkeypatch):
    from datetime import datetime

    now = time.time()
    current_hour = datetime.now().hour
    install_authority(
        tmp_path, monkeypatch,
        policy=NotificationPolicy(quiet_start_hour=current_hour,
                                  quiet_end_hour=(current_hour + 2) % 24),
    )
    support = supportive_module.SupportiveModeStore()
    support.configure(enabled=True, allow_checkins=True, now=now - 100_000.0)

    outcome = REVIEWED_ACTIONS["serena.support.checkin"]({})

    assert outcome.ok is True
    assert outcome.output["delivered"] is False
    assert support.checkin_due(now=now) is True


def test_checkin_waits_out_typing(tmp_path, monkeypatch):
    install_authority(
        tmp_path, monkeypatch, presence=lambda: PresenceState(typing=True))
    support = supportive_module.SupportiveModeStore()
    support.configure(enabled=True, allow_checkins=True, now=time.time() - 100_000.0)

    outcome = REVIEWED_ACTIONS["serena.support.checkin"]({})

    assert outcome.ok is True
    assert outcome.output["delivered"] is False
    assert outcome.output["action"] == "hold"


def test_scan_releases_held_at_a_breakpoint(tmp_path, monkeypatch):
    sent = []
    authority = install_authority(
        tmp_path, monkeypatch,
        presence=lambda: PresenceState(typing=True),
        senders={"telegram": lambda request: sent.append(request.summary) or True},
    )
    now = time.time()
    authority.request(
        NotificationRequest(kind="stuck.nudge", summary="held nudge",
                            channel="telegram", proactive=True, dedupe_key="scan-1"),
        now=now - 30.0,
    )
    ambient = AmbientStore()
    ambient.record("window", app="code", title="a.py", now=now - 60.0)
    ambient.record("unlock", now=now - 5.0)
    authority._presence = lambda: PresenceState()

    outcome = REVIEWED_ACTIONS["serena.proactive.scan"]({})

    assert outcome.ok is True
    assert sent == ["held nudge"]
    assert len(outcome.output["released"]) == 1


def test_scan_without_a_breakpoint_releases_nothing(tmp_path, monkeypatch):
    authority = install_authority(tmp_path, monkeypatch)
    now = time.time()
    AmbientStore().record("window", app="code", title="a.py", now=now - 60.0)

    outcome = REVIEWED_ACTIONS["serena.proactive.scan"]({})

    assert outcome.ok is True
    assert outcome.output["released"] == []
    assert authority.held_items() == []


def test_actions_reject_schedule_payloads(tmp_path, monkeypatch):
    install_authority(tmp_path, monkeypatch)

    assert REVIEWED_ACTIONS["serena.support.checkin"]({"x": 1}).ok is False
    assert REVIEWED_ACTIONS["serena.proactive.scan"]({"x": 1}).ok is False
