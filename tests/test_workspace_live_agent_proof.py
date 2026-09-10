"""Guard the opt-in proof against wasting time after a terminal provider failure."""

import runpy
from pathlib import Path

import pytest


@pytest.fixture
def reject_finished_parent():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify-workspace-live-agent.py"))["reject_finished_parent"]


@pytest.mark.parametrize("status", ["failed", "completed", "interrupted"])
def test_terminal_parent_without_child_fails_immediately(reject_finished_parent, status):
    events = [{"method": "turn/completed", "params": {"turn": {
        "id": "parent-turn", "status": status, "error": {"message": "Authentication expired"}}}}]
    with pytest.raises(RuntimeError, match="Parent finished without a child.*Authentication expired"):
        reject_finished_parent(events, "parent-turn")


def test_child_or_unfinished_events_do_not_end_proof(reject_finished_parent):
    events = [
        {"method": "turn/started", "params": {"turn": {"id": "parent-turn"}}},
        {"method": "turn/completed", "params": {"turn": {"id": "other"}}},
        {"method": "workspace/agentEvent", "params": {"event": {"method": "turn/completed"}}},
    ]
    assert reject_finished_parent(events, "parent-turn") is None
