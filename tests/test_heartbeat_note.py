from __future__ import annotations

from core.heartbeat_note import heartbeat_note


def test_heartbeat_note_reports_alive() -> None:
    assert heartbeat_note() == "alive"
