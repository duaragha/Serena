from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core import brain_daemon, brain_document_tools
from core.document_delivery import (
    authority_denial,
    create_document,
    send_document_to_beeper,
    send_document_to_telegram,
)
from core.work_authority import authority_denial as work_authority_denial
from voice.call.tasking import parse_live_work_intent


def _turn(text: str, protocol: str = "voice") -> dict:
    return {
        "text": text,
        "protocol": protocol,
        "call_id": "desk-1",
        "turn_id": "desk-1:4",
    }


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.payload if limit < 0 else self.payload[:limit]


class _Options:
    def __init__(self, **values):
        self.__dict__.update(values)


@pytest.mark.parametrize(
    "action,text",
    [
        ("create_document", "tell me about that memory"),
        ("send_telegram", "send the file to me"),
        ("send_beeper", "send the file to Telegram"),
    ],
)
def test_document_actions_require_explicit_current_spoken_authority(
    action: str, text: str
) -> None:
    assert authority_denial(action, _turn(text)) is not None
    assert authority_denial(action, _turn(text, protocol="frontdoor")) is not None


def test_text_document_is_private_visible_and_never_overwrites(tmp_path: Path) -> None:
    root = tmp_path / "Documents" / "Serena"
    audit = tmp_path / "authority.jsonl"
    secret_content = "memory one\nmemory two private-marker-917"
    first = create_document(
        "Memory List.txt",
        secret_content,
        "txt",
        origin=_turn("put those memories in a Notepad text file"),
        root=root,
        audit_path=audit,
    )
    second = create_document(
        "Memory List.txt",
        "another list",
        "txt",
        origin=_turn("create another memory list in a text file"),
        root=root,
        audit_path=audit,
    )

    assert first.ok and second.ok
    assert Path(first.path).parent == root.resolve()
    assert Path(first.path).read_text(encoding="utf-8") == secret_content + "\n"
    assert first.filename == "Memory List.txt"
    assert second.filename == "Memory List (2).txt"
    assert Path(first.path).stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700
    assert "private-marker-917" not in audit.read_text(encoding="utf-8")


def test_word_document_is_a_valid_docx_package(tmp_path: Path) -> None:
    result = create_document(
        "Memories.docx",
        "Memory & one\nMemory <two>",
        "docx",
        origin=_turn("create a Word document with that list of memories"),
        root=tmp_path / "Serena",
        audit_path=tmp_path / "audit.jsonl",
    )

    assert result.ok
    assert result.format == "docx"
    with zipfile.ZipFile(result.path) as package:
        assert set(package.namelist()) == {
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
        }
        document = package.read("word/document.xml").decode("utf-8")
    assert "Memory &amp; one" in document
    assert "Memory &lt;two&gt;" in document


def test_telegram_sends_attachment_only_after_bot_accepts_it(tmp_path: Path) -> None:
    root = tmp_path / "Serena"
    root.mkdir()
    attachment = root / "Memories.txt"
    attachment.write_text("one\ntwo\n", encoding="utf-8")
    env_path = tmp_path / "telegram.env"
    env_path.write_text(
        "TELEGRAM_BOT_TOKEN=token-secret\nTELEGRAM_CHAT_ID=chat-pinned\n",
        encoding="utf-8",
    )
    requests = []

    def fake_open(request, timeout):
        requests.append((request, timeout))
        return _Response({"ok": True, "result": {"message_id": 12}})

    result = send_document_to_telegram(
        attachment.name,
        origin=_turn("send that document to me on my Telegram bot"),
        root=root,
        env_path=env_path,
        audit_path=tmp_path / "audit.jsonl",
        urlopen=fake_open,
    )

    assert result.ok
    assert result.channel == "telegram"
    assert len(requests) == 1
    request = requests[0][0]
    assert request.full_url.endswith("/sendDocument")
    assert b'name="document"' in request.data
    assert b"one\ntwo" in request.data


def test_telegram_rejects_paths_outside_document_root_without_http(tmp_path: Path) -> None:
    called = False

    def fake_open(_request, _timeout):
        nonlocal called
        called = True
        raise AssertionError("HTTP must not run")

    result = send_document_to_telegram(
        "../outside.txt",
        origin=_turn("send that document to Telegram"),
        root=tmp_path / "Serena",
        env_path=tmp_path / "telegram.env",
        audit_path=tmp_path / "audit.jsonl",
        urlopen=fake_open,
    )

    assert not result.ok
    assert not called


def test_beeper_uses_only_pinned_local_api_and_waits_for_success(tmp_path: Path) -> None:
    root = tmp_path / "Serena"
    root.mkdir()
    attachment = root / "Memories.docx"
    attachment.write_bytes(b"docx bytes")
    env_path = tmp_path / "beeper.env"
    env_path.write_text(
        "BEEPER_ACCESS_TOKEN=beeper-secret\nBEEPER_CHAT_ID=!pinned:beeper.local\n",
        encoding="utf-8",
    )
    responses = iter(
        [
            {"uploadID": "upload-1", "fileName": attachment.name},
            {"chatID": "!pinned:beeper.local", "pendingMessageID": "pending-1"},
            {"sendStatus": {"status": "PENDING"}},
            {"sendStatus": {"status": "SUCCESS"}},
        ]
    )
    requests = []

    def fake_open(request, timeout):
        requests.append((request, timeout))
        return _Response(next(responses))

    result = send_document_to_beeper(
        attachment.name,
        origin=_turn("send that Word document to me on Beeper"),
        root=root,
        env_path=env_path,
        audit_path=tmp_path / "audit.jsonl",
        urlopen=fake_open,
        sleep=lambda _seconds: None,
    )

    assert result.ok
    assert result.channel == "beeper"
    assert [request.method for request, _timeout in requests] == [
        "POST",
        "POST",
        "GET",
        "GET",
    ]
    assert requests[0][0].full_url == "http://127.0.0.1:23373/v1/assets/upload/base64"
    assert "/v1/chats/%21pinned%3Abeeper.local/messages" in requests[1][0].full_url
    sent_payload = json.loads(requests[1][0].data)
    assert sent_payload == {
        "attachment": {
            "uploadID": "upload-1",
            "fileName": attachment.name,
            "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "type": "file",
        }
    }


def test_unconfigured_beeper_fails_before_any_http(tmp_path: Path) -> None:
    root = tmp_path / "Serena"
    root.mkdir()
    (root / "Memories.txt").write_text("memory", encoding="utf-8")

    def forbidden_http(*_args, **_kwargs):
        raise AssertionError("HTTP must not run without pinned config")

    result = send_document_to_beeper(
        "Memories.txt",
        origin=_turn("send the file to Beeper"),
        root=root,
        env_path=tmp_path / "missing.env",
        audit_path=tmp_path / "audit.jsonl",
        urlopen=forbidden_http,
    )
    assert not result.ok
    assert result.reason == "Beeper delivery is not configured"


@pytest.mark.parametrize(
    "spoken",
    [
        "create a list of those memories in a Word document",
        "write those memories in Notepad so I can see them",
        "make a document and send it to my Telegram bot",
        "put that list in a text file and send it on Beeper",
    ],
)
def test_personal_document_requests_never_become_legacy_coding_jobs(spoken: str) -> None:
    assert parse_live_work_intent(spoken) is None
    denial = work_authority_denial(spoken, _turn(spoken))
    assert denial == "the spoken turn asked for a personal document, not coding work"


def test_real_document_related_software_request_can_still_start_coding() -> None:
    spoken = "implement DOCX export in the Serena repo"
    assert parse_live_work_intent(spoken) is None
    assert work_authority_denial(spoken, _turn(spoken)) is None


def test_document_tools_are_explicitly_allowed_by_the_brain(
    monkeypatch, tmp_path: Path
) -> None:
    prompt_path = tmp_path / "brain-system-prompt.md"
    monkeypatch.setattr(brain_daemon, "_persona_context", lambda: "persona")
    monkeypatch.setattr(brain_daemon, "BRAIN_SYSTEM_PROMPT_FILE", prompt_path)
    server = brain_document_tools.document_tools_server()
    options = brain_daemon._build_agent_options(
        _Options,
        {},
        [],
        document_tools=server,
        document_tool_names=brain_document_tools.DOCUMENT_TOOL_NAMES,
    )

    assert set(options.mcp_servers) == {"serena-ro", "serena-documents"}
    assert options.allowed_tools == brain_document_tools.DOCUMENT_TOOL_NAMES
    assert set(name.rsplit("__", 1)[-1] for name in options.allowed_tools) == {
        "create_document",
        "send_document_to_telegram",
        "send_document_to_beeper",
    }


def test_prompt_requires_one_truthful_post_tool_outcome() -> None:
    prompt = brain_daemon._persona_context()
    assert "personal document task, never coding" in prompt
    assert "Do the tool calls before speaking about the outcome" in prompt
    assert "Never send through both channels unless he names both" in prompt
    assert "Never first promise you can send and then reverse yourself" in prompt
