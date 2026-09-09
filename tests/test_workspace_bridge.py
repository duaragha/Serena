import asyncio
import time

import pytest
from flask import Flask

from core.workspace_host import WorkspaceHost
from core.workspace_journal import WorkspaceJournal
from ui.workspace_bridge import structured_bridge


class Owner:
    def __init__(self, *, session_id, cwd, publish):
        self.sid, self.publish = session_id, publish
        self.state, self.active_turn = "closed", None
        self.sent = []
        self.tasks = []

    async def open(self):
        self.state = "ready"

    async def submit(self, inputs):
        if self.state != "ready":
            raise RuntimeError("Session is busy")
        self.sent.append(inputs)
        self.state = "running"

        async def finish():
            await asyncio.sleep(0.08)
            for event in [
                {
                    "method": "item/agentMessage/delta",
                    "params": {"turnId": "other", "itemId": "x", "delta": "WRONG TURN"},
                },
                {
                    "method": "item/agentMessage/delta",
                    "params": {"turnId": "own", "itemId": "a", "delta": "partial"},
                },
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "own",
                        "item": {"id": "a", "type": "agentMessage", "text": "exact answer"},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "own", "status": "completed"}},
                },
            ]:
                event["params"]["threadId"] = self.sid
                await self.publish(event)
            self.state = "ready"

        self.tasks.append(asyncio.create_task(finish()))
        return {"turn": {"id": "own"}}

    async def close(self):
        await asyncio.gather(*self.tasks)
        self.state = "closed"


@pytest.fixture(params=["codex", "claude"])
def host(tmp_path, request):
    provider = request.param
    value = WorkspaceHost(
        journal=WorkspaceJournal(tmp_path / "events.db"),
        resolve=lambda sid: {"session_id": sid, "provider": provider, "cwd": str(tmp_path)},
        factories={provider: Owner},
    )
    yield value, provider
    value.shutdown()


def test_bridge_exact_turn_and_timeout_retry_preserve_one_owner(host):
    value, provider = host
    assert value.bridge("exact", provider, "hello", "r") is None
    assert value._loop is None
    value.attach("exact")
    result = value.bridge("exact", provider, "hello", "r", timeout=0.005)
    assert result["pending"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = value.bridge("exact", provider, "hello", "r")
        if not result.get("pending"):
            break
        time.sleep(0.02)
    assert result == {
        "ok": True,
        "response": "exact answer",
        "message": "finished (completed)",
        "session_id": "exact",
        "turn_id": "own",
    }
    assert value.bridge("exact", provider, "hello", "r") == result
    owner = value._sessions["exact"][0]
    assert len(owner.sent) == 1 and owner.state == "ready"
    with pytest.raises(ValueError, match="different content"):
        value.bridge("exact", provider, "different", "r")


def test_bridge_wrong_provider_or_busy_owner_does_not_fallback(host):
    value, provider = host
    value.attach("exact")
    owner = value._sessions["exact"][0]
    assert not value.bridge("exact", "wrong", "hello", "wrong")["ok"]
    owner.state = "running"
    result = value.bridge("exact", provider, "hello", "busy")
    assert result["queued"] and result["pending"]
    assert not owner.sent


def test_busy_bridge_queue_is_fifo_and_acknowledges_without_mutual_wait(host):
    value, provider = host
    value.attach("exact")
    owner = value._sessions["exact"][0]
    owner.state = "running"
    assert value.bridge("exact", provider, "first", "first")["queued"]
    assert value.bridge("exact", provider, "second", "second")["queued"]
    assert not owner.sent
    assert value.bridge("exact", provider, "first", "first")["pending"]

    async def answer(request_id, payload):
        owner.state = "ready"
        return {}

    owner.answer = answer
    assert value.command("exact", "approval", "answer", {"request_id": 1, "answer": {}})["ok"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        first = value.bridge("exact", provider, "first", "first")
        second = value.bridge("exact", provider, "second", "second")
        if not first.get("pending") and not second.get("pending"):
            break
        time.sleep(0.02)
    assert first["ok"] and second["ok"] and not second.get("pending")
    assert [inputs[0]["text"] for inputs in owner.sent] == ["first", "second"]
    assert not value._bridge_queues["exact"]


def test_queue_edit_preserves_receipt_and_order_and_rejects_stale_edits(host):
    value, provider = host
    value.attach("exact")
    owner = value._sessions["exact"][0]
    owner.state = "running"
    assert value.bridge("exact", provider, "original", "edit-me")["queued"]
    assert value.bridge("exact", provider, "second", "second")["queued"]
    payload = {"request_id": "edit-me", "expected_prompt": "original", "prompt": "corrected"}
    result = value.command("exact", "edit-control", "edit_queued_bridge", payload)
    assert result["result"] == {"edited": True}
    assert value.command("exact", "edit-control", "edit_queued_bridge", payload) == result
    assert not value.command("exact", "stale-edit", "edit_queued_bridge", {**payload, "prompt": "stale overwrite"})["ok"]
    assert not owner.sent and owner.state == "running"
    queued = value.events("exact")["events"][-1]["event"]["params"]["requests"]
    assert queued == [{"id": "edit-me", "prompt": "corrected"}, {"id": "second", "prompt": "second"}]
    owner.state = "ready"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        first = value.bridge("exact", provider, "original", "edit-me")
        second = value.bridge("exact", provider, "second", "second")
        if not first.get("pending") and not second.get("pending"):
            break
        time.sleep(0.02)
    assert first["ok"] and second["ok"]
    assert [inputs[0]["text"] for inputs in owner.sent] == ["corrected", "second"]
    late = value.command("exact", "late-edit", "edit_queued_bridge", {**payload, "expected_prompt": "corrected"})
    assert not late["ok"] and "no longer queued" in late["error"]
    assert len(owner.sent) == 2


def test_queue_edit_rolls_back_if_journal_publication_fails(host, monkeypatch):
    value, provider = host
    value.attach("exact")
    owner = value._sessions["exact"][0]
    owner.state = "running"
    value.bridge("exact", provider, "original", "queued")
    append = value.journal.append
    def fail_append(*args, **kwargs):
        raise OSError("Journal write failed")
    monkeypatch.setattr(value.journal, "append", fail_append)
    result = value.command("exact", "edit-failed", "edit_queued_bridge", {"request_id": "queued", "expected_prompt": "original", "prompt": "replacement"})
    assert not result["ok"] and "Journal write failed" in result["error"]
    assert value._bridge_messages[("exact", "bridge:queued")] == "original"
    assert not owner.sent
    monkeypatch.setattr(value.journal, "append", append)


def test_queued_message_cancellation_never_submits_or_interrupts(host):
    value, provider = host
    value.attach("exact")
    owner = value._sessions["exact"][0]
    owner.state = "running"
    assert value.bridge("exact", provider, "cancel me", "queued")["queued"]
    queue = value.events("exact")["events"][-1]["event"]["params"]
    assert queue["requests"] == [{"id": "queued", "prompt": "cancel me"}]
    cancelled = value.command(
        "exact", "cancel-request", "cancel_queued_bridge", {"request_id": "queued"}
    )
    assert cancelled["result"] == {"cancelled": True}
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = value.bridge("exact", provider, "cancel me", "queued")
        if not result.get("pending"):
            break
        time.sleep(0.02)
    assert not result["ok"] and "cancelled before submission" in result["message"]
    assert owner.state == "running" and not owner.sent
    assert not value.command(
        "exact", "stale-cancel", "cancel_queued_bridge", {"request_id": "queued"}
    )["ok"]
    assert not value._bridge_queues["exact"] and not value._bridge_messages


def test_cancel_queue_cannot_interrupt_already_dispatched_turn(host):
    value, provider = host
    value.attach("exact")
    value.bridge("exact", provider, "hello", "sent", timeout=0.005)
    deadline = time.monotonic() + 3
    while not value._sessions["exact"][0].sent and time.monotonic() < deadline:
        time.sleep(0.005)
    assert value._sessions["exact"][0].sent
    result = value.command("exact", "cancel-late", "cancel_queued_bridge", {"request_id": "sent"})
    assert not result["ok"] and "running turns are not cancelled" in result["error"]
    assert len(value._sessions["exact"][0].sent) == 1


def test_queued_bridge_fails_without_submission_when_owner_dies(host):
    value, provider = host
    value.attach("exact")
    owner = value._sessions["exact"][0]
    owner.state = "running"
    assert value.bridge("exact", provider, "hello", "r")["queued"]
    owner.state = "unavailable"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = value.bridge("exact", provider, "hello", "r")
        if not result.get("pending"):
            break
        time.sleep(0.02)
    assert not result["ok"] and not result.get("pending")
    assert not owner.sent


def test_saved_bridge_receipt_prevents_terminal_fallback_without_live_owner(host):
    value, provider = host
    payload = {"provider": provider, "prompt": "hello"}
    value.journal.claim_command("exact", "bridge:pending", payload)
    assert value.bridge("exact", provider, "hello", "pending")["pending"]
    value.journal.claim_command("exact", "bridge:done", payload)
    response = {"ok": True, "response": "already answered"}
    value.journal.finish_command("exact", "bridge:done", response)
    assert value.bridge("exact", provider, "hello", "done") == response
    assert value._loop is None and not value._sessions


def test_web_bridge_is_local_and_uses_receipt_without_legacy_fallback(host):
    value, provider = host
    app = Flask(__name__)
    app.extensions["workspace_host"] = value
    with app.test_request_context(
        "/",
        base_url="http://127.0.0.1",
        json={"request_id": "r"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ):
        assert structured_bridge(provider, "exact", "hello", 1) is None
        value.attach("exact")
        assert structured_bridge(provider, "exact", "hello", 1)["response"] == "exact answer"
    with app.test_request_context("/", base_url="http://evil.test", json={}):
        result = structured_bridge(provider, "exact", "hello", 1)
        assert result is not None and not result["ok"]
    assert len(value._sessions["exact"][0].sent) == 1


def test_existing_http_bridge_routes_prefer_structured_owner(host, monkeypatch):
    from core import claude_bridge, codex_bridge
    from ui.web import app

    value, provider = host
    monkeypatch.setitem(app.extensions, "workspace_host", value)

    def forbidden(*args, **kwargs):
        raise AssertionError("Existing structured owner must never fall back to a terminal")

    monkeypatch.setattr(codex_bridge, "call_codex_via_bridge", forbidden)
    monkeypatch.setattr(claude_bridge, "call_claude_via_bridge", forbidden)
    value.attach("exact")
    with app.test_client() as client:
        result = client.post(
            f"/api/{provider}-bridge",
            base_url="http://127.0.0.1",
            json={"target_sid": "exact", "prompt": "hello", "request_id": "http-proof"},
        )
        assert result.status_code == 200
        assert result.json["response"] == "exact answer"
        assert result.json["request_id"] == "http-proof"
        owner = value._sessions["exact"][0]
        owner.state = "running"
        busy = client.post(
            f"/api/{provider}-bridge",
            base_url="http://127.0.0.1",
            json={"target_sid": "exact", "prompt": "second", "request_id": "http-busy"},
        )
        assert busy.json["queued"] and busy.json["pending"]
        assert len(owner.sent) == 1
