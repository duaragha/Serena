"""Serena's own Apple ID line: BlueBubbles transport, polling and health.

The BlueBubbles server is faked at the HTTP layer; nothing leaves the process.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest

from memory import store

BRIEF = "Fix memory/store.py so expired task claims can be reclaimed safely."


class FakeServer:
    def __init__(self):
        self.up = True
        self.chat_exists = True
        self.messages: list[dict] = []
        self.sent: list[dict] = []
        self.available = True

    def urlopen(self, request, timeout=0):
        if not self.up:
            raise urllib.error.URLError("down")
        url = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(url.query)
        assert query["password"] == ["pw"]
        path = url.path.removeprefix("/api/v1/")
        method = request.get_method()
        if path == "ping":
            return self._json({"message": "pong"})
        if path.startswith("chat/") and path.endswith("/message"):
            if not self.chat_exists:
                raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, io.BytesIO(b"{}"))
            return self._json({"data": list(reversed(self.messages))})
        if path.startswith("chat/") and method == "GET":
            if not self.chat_exists:
                raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, io.BytesIO(b"{}"))
            return self._json({"data": {}})
        if path in ("message/text", "chat/new"):
            body = json.loads(request.data)
            self.sent.append({"endpoint": path, **body})
            self.chat_exists = True
            guid = f"g{len(self.sent)}"
            self.messages.append({"guid": guid, "text": body["message"], "isFromMe": True,
                                  "dateCreated": 10_000 + len(self.messages)})
            return self._json({"status": 200, "data": {"guid": guid}})
        if path == "message/attachment":
            self.sent.append({"endpoint": path, "body": request.data})
            return self._json({"status": 200, "data": {"guid": "att"}})
        if path == "handle/availability/imessage":
            return self._json({"data": {"available": self.available}})
        raise AssertionError(path)

    @staticmethod
    def _json(data):
        return io.BytesIO(json.dumps(data).encode())


@pytest.fixture
def server(tmp_path, monkeypatch):
    import urllib.request

    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(store, "_active_v2_store", lambda: None)
    monkeypatch.setattr(store, "_source_context", lambda: ("", "", "", ""))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "pw").write_text("pw\n", encoding="utf-8")
    config = tmp_path / "phone-line.json"
    config.write_text(json.dumps({
        "backend": "bluebubbles", "url": "http://bb", "password_file": str(tmp_path / "pw"),
        "address": "him@example.com", "watch_number": "+14165550100"}), encoding="utf-8")
    monkeypatch.setenv("SERENA_PHONE_LINE_CONFIG", str(config))
    fake = FakeServer()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


def test_own_line_is_preferred_and_sends_without_prefix(server):
    from core import phone_line

    assert phone_line.backend_name() == "bluebubbles"
    assert phone_line.send("serena: hello")
    assert server.sent[-1]["message"] == "hello"
    assert server.sent[-1]["chatGuid"] == "iMessage;-;him@example.com"


def test_first_message_creates_the_chat(server):
    from core import bluebubbles_line

    server.chat_exists = False
    bluebubbles_line.send_text("hi")
    assert server.sent[0]["endpoint"] == "chat/new"
    assert server.sent[0]["addresses"] == ["him@example.com"]


def test_poll_acts_only_on_his_messages(server):
    from core import phone_line

    server.messages.append({"guid": "old", "text": "task: " + BRIEF, "isFromMe": False,
                            "dateCreated": 1})
    assert phone_line.poll(now=100).commands == []
    server.messages += [
        {"guid": "m1", "text": "task: " + BRIEF, "isFromMe": False, "dateCreated": 5},
        {"guid": "m2", "text": "task: from me, not him", "isFromMe": True, "dateCreated": 6},
    ]
    report = phone_line.poll(now=200)
    assert [c["kind"] for c in report.commands] == ["task"]
    (task,) = store.tasks_in_state("ready")
    assert task["source_id"] == "imessage:m1"
    assert server.sent[-1]["message"].startswith("got it")
    # Her own reply is in the chat now and must not be treated as a command.
    assert phone_line.poll(now=300).commands == []


def test_swapped_command_arms_the_reminder_clock(server):
    from core import phone_line

    phone_line.poll(now=100)
    server.messages.append({"guid": "s", "text": "swapped", "isFromMe": False,
                            "dateCreated": 50})
    report = phone_line.poll(now=500)
    assert report.commands[0]["kind"] == "swapped"
    health = json.loads((server_home(server) / "phone-health.json").read_text(encoding="utf-8"))
    assert health["number_swapped_at"] == 500


def server_home(_server):
    import os
    from pathlib import Path

    return Path(os.environ["HOME"]) / ".local" / "state" / "serena"


def test_health_alerts_once_through_the_fallback_when_her_server_dies(server, monkeypatch):
    from core import phone_line, scheduler_actions

    fallback = []
    monkeypatch.setattr(phone_line, "send_fallback",
                        lambda text, key="": fallback.append(text) or True)
    action = scheduler_actions.REVIEWED_ACTIONS["serena.phone.health"]
    clock = [1_000_000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    assert action({}).output["server_up"] is True
    server.up = False
    assert action({}).output["server_up"] is False
    assert fallback == []  # not yet: short blips are ignored
    clock[0] += scheduler_actions.LINE_DOWN_ALERT_SECONDS + 1
    action({})
    action({})
    assert len(fallback) == 1 and "Serena user" in fallback[0]
    server.up = True
    clock[0] += 60
    action({})
    assert server.sent[-1]["message"] == "i'm back on my own line."


def test_health_reports_a_dropped_number_and_the_swap_due(server, monkeypatch):
    from core import scheduler_actions

    action = scheduler_actions.REVIEWED_ACTIONS["serena.phone.health"]
    clock = [2_000_000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    assert action({}).output["number_registered"] is True
    server.available = False
    clock[0] += 25 * 3600
    action({})
    assert "fell off imessage" in server.sent[-1]["message"]
    count = len(server.sent)
    clock[0] += 25 * 3600
    action({})
    assert len(server.sent) == count  # asked once until it recovers

    state_file = server_home(server) / "phone-health.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state.update(number_swapped_at=clock[0] - 43 * 86400, swap_reminded=False)
    state_file.write_text(json.dumps(state), encoding="utf-8")
    action({})
    assert "sim refresh" in server.sent[-1]["message"]


def test_documents_use_her_line(server, tmp_path):
    from core.document_delivery import send_document_to_imessage

    root = tmp_path / "Serena"
    root.mkdir(mode=0o700)
    (root / "Notes.txt").write_text("x\n", encoding="utf-8")
    result = send_document_to_imessage(
        "Notes.txt", origin={"text": "send it to my phone", "protocol": "voice"},
        root=root, audit_path=tmp_path / "audit.jsonl")
    assert result.ok
    assert server.sent[-1]["endpoint"] == "message/attachment"
    assert b'filename="Notes.txt"' in server.sent[-1]["body"]
