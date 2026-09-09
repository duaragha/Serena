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
    assert not result["ok"] and "busy" in result["message"]
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
        assert not busy.json["ok"]
        assert len(owner.sent) == 1
