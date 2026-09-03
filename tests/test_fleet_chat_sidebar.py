from __future__ import annotations

from core import metadata
from ui import web


def test_fleet_marker_is_exposed_to_the_sidebar(monkeypatch) -> None:
    marker = {
        "run_id": "fleet-run-1",
        "leg_id": "leg-1",
        "phase": "execute",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
    }
    monkeypatch.setattr(metadata, "get_all_meta", lambda: {"worker-1": {"fleet_worker": marker}})
    monkeypatch.setattr(web, "_ambiguous_shorts", lambda: set())
    monkeypatch.setattr(web, "_external_runtime_active", lambda _sid: False)

    rows = web._decorate_sessions(
        [{"session_id": "worker-1", "project_dir": "", "agent": "codex"}]
    )

    assert rows[0]["fleet_worker"] == marker


def test_fleet_chats_have_one_collapsible_home_outside_normal_buckets() -> None:
    html = web.HTML

    assert "function _isFleetSession(session)" in html
    assert "marker.run_id" in html
    assert "rowMembers(s).some(_isFleetSession)" in html
    assert "visibleTop = visibleTop.filter(s => !fleetSet.has(s.session_id))" in html
    assert 'data-testid="fleet-chats-header"' in html
    assert 'data-testid="fleet-chats-section"' in html
    assert "let _collapsedState = { fleetChats: true" in html
    assert "fetch('/api/ui-state')" in html
    assert "body: JSON.stringify({ collapsed: _collapsedState })" in html
    assert "function toggleFleetChatsCollapsed()" in html
    assert "const readOnly = externallyRunning || serenaVoice || fleetWorker" in html
    assert "_isSerenaVoiceSession(localSession || sid) || _isFleetSession(localSession)" in html

    partition = html.index("const fleetChats = visibleTop.filter")
    active_bucket = html.index("const active = _activeTerms.size")
    done_bucket = html.index("const doneList = visibleTop.filter")
    starred_bucket = html.index("const starred = remaining.filter")
    assert partition < active_bucket < done_bucket < starred_bucket

    active_section = html.index("if (active.length)")
    fleet_section = html.index("if (fleetChats.length)")
    starred_section = html.index("if (starred.length)")
    assert active_section < fleet_section < starred_section


def test_finished_fleet_workers_are_never_resumable(monkeypatch) -> None:
    session = {
        "session_id": "fleet-worker-1",
        "agent": "codex",
        "project_dir": "-home-raghav-Documents-Projects-serena",
        "last_cwd": "/home/raghav/Documents/Projects/serena",
    }
    marker = {"run_id": "fleet-run-1"}
    monkeypatch.setattr(web, "get_session", lambda _sid: session)
    monkeypatch.setattr(web, "_fleet_worker_marker", lambda _sid: marker)
    client = web.app.test_client()

    resume = client.post("/api/resume/fleet-worker-1")
    spawn = client.post(
        "/api/spawn-terminal",
        json={"session_id": "fleet-worker-1"},
    )

    assert resume.status_code == 409
    assert resume.get_json()["error"] == "Fleet worker chats are read-only"
    assert spawn.status_code == 409
    assert spawn.get_json()["error"] == "Fleet worker chats are read-only"

    deleted = client.delete("/api/session/fleet-worker-1")
    assert deleted.status_code == 409
    assert "durable run history" in deleted.get_json()["error"]
