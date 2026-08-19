"""Owner-safe dispatch coverage for accepted voice coding work."""

import hashlib
import json

from core import codex_bridge


def _record(kind, **payload):
    return json.dumps({"type": "event_msg", "payload": {"type": kind, **payload}})


def test_work_dispatch_persists_commit_before_waiting_for_response(
    monkeypatch, tmp_path
):
    """A spoken job must have a durable receipt before Codex can finish it."""
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(_record("task_complete") + "\n", encoding="utf-8")
    owner = {"kind": "pty", "tid": "term-1", "sid": "codex-1"}
    sequence = []

    monkeypatch.setattr(codex_bridge, "find_codex_jsonl", lambda _sid: transcript)
    monkeypatch.setattr(
        codex_bridge,
        "_acquire_work_owner",
        lambda sid, item: (owner, "reserved"),
    )
    monkeypatch.setattr(
        codex_bridge,
        "_feed_work_owner",
        lambda current, prompt, item: sequence.append(("feed", prompt, item))
        or (True, "queued", True),
    )

    def persist(item_id, state, **fields):
        sequence.append((state, item_id, fields))

    monkeypatch.setattr(codex_bridge, "_mark_route_dispatch", persist)

    def collect(path, start, prompt_hash, timeout, current):
        assert sequence[1][0] == "committed"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_record("user_message", message="implement safely") + "\n")
            handle.write(_record("agent_message", message="done") + "\n")
            handle.write(_record("task_complete") + "\n")
        return {"ok": True, "response": "done", "message": "finished", "finished": True}

    monkeypatch.setattr(codex_bridge, "_collect_work_response", collect)
    monkeypatch.setattr(
        codex_bridge,
        "_finish_work_owner_turn",
        lambda current: sequence.append(("turn-finished", current["sid"])),
    )
    monkeypatch.setattr(
        codex_bridge,
        "_release_work_owner",
        lambda current, item: sequence.append(("released", item)) or True,
    )

    result = codex_bridge.call_codex_work_via_bridge(
        "codex-1", "implement safely", "job-1", timeout=2
    )

    assert result == {
        "ok": True,
        "response": "done",
        "message": "finished",
        "committed": True,
        "start_offset": 1,
        "end_offset": 4,
    }
    assert [entry[0] for entry in sequence] == [
        "feed",
        "committed",
        "completed",
        "turn-finished",
        "released",
    ]
    committed = sequence[1][2]
    assert committed["start_offset"] == 1
    assert committed["end_offset"] is None
    assert committed["prompt_sha256"] == hashlib.sha256(
        b"implement safely"
    ).hexdigest()


def test_uncertain_committed_dispatch_keeps_the_exact_reservation(
    monkeypatch, tmp_path
):
    """A timed-out committed prompt must not be replayed into a second owner."""
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("", encoding="utf-8")
    owner = {"kind": "pty", "tid": "term-1", "sid": "codex-1"}
    states = []
    releases = []

    monkeypatch.setattr(codex_bridge, "find_codex_jsonl", lambda _sid: transcript)
    monkeypatch.setattr(
        codex_bridge, "_acquire_work_owner", lambda *_args: (owner, "reserved")
    )
    monkeypatch.setattr(
        codex_bridge, "_feed_work_owner", lambda *_args: (True, "queued", True)
    )
    monkeypatch.setattr(
        codex_bridge,
        "_mark_route_dispatch",
        lambda _item, state, **_fields: states.append(state),
    )
    monkeypatch.setattr(
        codex_bridge,
        "_collect_work_response",
        lambda *_args: {
            "ok": False,
            "response": "partial",
            "message": "timed out",
            "finished": False,
        },
    )
    monkeypatch.setattr(
        codex_bridge,
        "_release_work_owner",
        lambda *_args: releases.append(True) or True,
    )

    result = codex_bridge.call_codex_work_via_bridge(
        "codex-1", "implement safely", "job-1", timeout=1
    )

    assert result["ok"] is False
    assert result["committed"] is True
    assert result["reserved"] is True
    assert states == ["committed", "uncertain"]
    assert releases == []


def test_exact_response_collector_rejects_an_unexpected_user_turn(
    monkeypatch, tmp_path
):
    """A manual prompt in the reserved slice must never be credited to the job."""
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        _record("user_message", message="different prompt")
        + "\n"
        + _record("task_complete")
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_bridge, "_owner_alive", lambda _owner: True)

    result = codex_bridge._collect_work_response(
        transcript,
        0,
        hashlib.sha256(b"accepted prompt").hexdigest(),
        1,
        {"kind": "pty"},
    )

    assert result["ok"] is False
    assert result["finished"] is False
    assert "another prompt" in result["message"]


def test_work_interrupt_requires_the_exact_reservation(monkeypatch):
    """A stale cancel request must not send Ctrl+C to somebody else's chat."""
    monkeypatch.setattr(codex_bridge, "work_reservation", lambda _sid: "job-2")

    result = codex_bridge.interrupt_codex_work("codex-1", "job-1")

    assert result == {
        "ok": False,
        "message": "work item does not own this runtime",
    }


def test_runtime_context_reports_exact_owner_state_on_loopback(monkeypatch):
    """Routing must see focused and split owners instead of guessing by recency."""
    from core import metadata
    from ui import web

    monkeypatch.setattr(
        web,
        "_native_runtime_context",
        lambda: {
            "focused_sid": "codex-1",
            "split_pair": ["claude-1", "codex-1"],
            "runtimes": [
                {
                    "sid": "codex-1",
                    "agent": "codex",
                    "cwd": "/repo",
                    "alive": True,
                    "state": "live",
                    "busy": False,
                    "draft": False,
                    "reserved": True,
                    "reservation_item_id": "job-1",
                    "owner": "gtk",
                }
            ],
        },
    )
    monkeypatch.setattr(
        web.pty_terminal,
        "runtime_context_snapshot",
        lambda: {"focused_sid": None, "split_pair": [], "runtimes": []},
    )
    monkeypatch.setattr(metadata, "get_meta", lambda _sid: {"group": "group-1"})
    monkeypatch.setattr(metadata, "external_runtime_active", lambda _sid: False)

    response = web.app.test_client().get(
        "/api/runtime-context",
        base_url="http://127.0.0.1:46747",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["focused_sid"] == "codex-1"
    assert payload["focused_session_id"] == "codex-1"
    assert payload["split_pair"] == ["claude-1", "codex-1"]
    assert payload["split_session_ids"] == ["claude-1", "codex-1"]
    assert payload["port"] == 46747
    assert payload["bridge_port"] == 46747
    assert payload["sessions"] == payload["runtimes"]
    assert payload["runtimes"][0] == {
        "sid": "codex-1",
        "agent": "codex",
        "cwd": "/repo",
        "alive": True,
        "state": "live",
        "busy": False,
        "draft": False,
        "reserved": True,
        "reservation_item_id": "job-1",
        "owner": "gtk",
        "group": "group-1",
        "external": False,
        "fleet": False,
    }


def test_runtime_and_work_bridge_endpoints_are_loopback_only(monkeypatch):
    """Tailnet callers must not inspect or drive laptop-owned coding terminals."""
    from ui import web

    client = web.app.test_client()
    context = client.get(
        "/api/runtime-context", environ_base={"REMOTE_ADDR": "100.64.0.8"}
    )
    dispatch = client.post(
        "/api/codex-work-bridge",
        json={"target_sid": "codex-1", "prompt": "work", "item_id": "job-1"},
        environ_base={"REMOTE_ADDR": "100.64.0.8"},
    )
    interrupt = client.post(
        "/api/codex-work-interrupt",
        json={"target_sid": "codex-1", "item_id": "job-1"},
        environ_base={"REMOTE_ADDR": "100.64.0.8"},
    )

    assert context.status_code == 403
    assert dispatch.status_code == 403
    assert interrupt.status_code == 403


def test_work_bridge_endpoint_forwards_only_the_explicit_target(monkeypatch):
    """Dispatch must never resolve or spawn a different chat inside the endpoint."""
    from ui import web

    calls = []
    monkeypatch.setattr(
        codex_bridge,
        "call_codex_work_via_bridge",
        lambda sid, prompt, item, timeout: calls.append((sid, prompt, item, timeout))
        or {
            "ok": True,
            "response": "done",
            "message": "finished",
            "committed": True,
            "start_offset": 4,
            "end_offset": 9,
        },
    )

    response = web.app.test_client().post(
        "/api/codex-work-bridge",
        json={
            "target_sid": "codex-1",
            "prompt": "implement safely",
            "item_id": "job-1",
            "timeout": 12,
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert calls == [("codex-1", "implement safely", "job-1", 12.0)]
    assert response.get_json()["committed"] is True
