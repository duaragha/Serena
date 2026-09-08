from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.computer_context_hook import hook_context
from core.computer_conversation import ConversationCursor, ConversationStore, origin_arguments
from core.computer_platform import ComputerError


def write_chat(home, agent, *, sid=None, count=30):
    sid = sid or str(uuid.uuid4())
    if agent == "codex":
        path = home / ".codex/sessions/2026/09/08" / f"rollout-2026-09-08-{sid}.jsonl"
        records = [{"type": "session_meta", "payload": {"id": sid}}]
    else:
        path = home / ".claude/projects/test" / f"{sid}.jsonl"
        records = []
    start = time.time() - 1000
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        text = f"prior-message-{i}: account naming decision {i}"
        stamp = datetime.fromtimestamp(start + i, timezone.utc).isoformat()
        if agent == "codex":
            records.append(
                {
                    "type": "response_item",
                    "timestamp": stamp,
                    "payload": {
                        "type": "message",
                        "role": role,
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
        else:
            records.append(
                {
                    "type": role,
                    "timestamp": stamp,
                    "message": {
                        "id": f"msg-{i}",
                        "role": role,
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return sid, path


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_all_30_chat_messages_10_coaching_updates_and_followup_survive_restart(tmp_path, agent):
    store = ConversationStore(tmp_path / "state", home=tmp_path)
    sid, path = write_chat(tmp_path, agent)
    origin = store.register(sid, agent, path)
    session = SimpleNamespace(id=uuid.uuid4().hex, request="guide me through setup")
    store.bind(session, origin)
    cursor = ConversationCursor(store, session.id)
    initial = cursor.context()
    assert cursor.message_count == 30
    assert all(f"prior-message-{i}:" in initial for i in range(30))
    assert cursor.context() == initial  # An interrupted startup must not lose unsent context.
    cursor.commit()
    assert cursor.context() == ""  # Do not resend the whole transcript each frame.
    for i in range(10):
        event = {
            "id": i + 1,
            "type": "observation",
            "session_id": session.id,
            "at": time.time() + i / 100,
            "text": f"coaching-message-{i}: useful step {i}",
        }
        store.record(event)
        store.record(event)  # Transport replays must not duplicate advice.
        assert f"coaching-message-{i}:" in cursor.context()
        cursor.commit()
    question = "explain the initial naming decision and the last coaching step"
    at = datetime.now(timezone.utc).isoformat()
    record = (
        {
            "type": "response_item",
            "timestamp": at,
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": question}],
            },
        }
        if agent == "codex"
        else {"type": "user", "timestamp": at, "message": {"role": "user", "content": question}}
    )
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    # A new process/worker has no in-memory cursor or original model thread.
    reopened = ConversationStore(store.directory, home=tmp_path)
    restored = ConversationCursor(reopened, session.id)
    context = restored.context()
    assert restored.message_count == 41
    assert all(f"prior-message-{i}:" in context for i in range(30))
    assert all(f"coaching-message-{i}:" in context for i in range(10))
    assert question in context
    hook = hook_context(
        {"session_id": sid, "transcript_path": str(path), "prompt": question}, agent, store=reopened
    )
    injected = hook["hookSpecificOutput"]["additionalContext"]
    assert all(f"coaching-message-{i}:" in injected for i in range(10))
    assert hook["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    # Another chat's hook must never receive this conversation's coaching.
    other, other_path = write_chat(tmp_path, agent)
    assert (
        hook_context(
            {"session_id": other, "transcript_path": str(other_path)}, agent, store=reopened
        )
        == {}
    )


def test_source_binding_rejects_ambiguous_ids_and_unrelated_files(tmp_path):
    store = ConversationStore(tmp_path / "state", home=tmp_path)
    sid, path = write_chat(tmp_path, "codex")
    with pytest.raises(ComputerError, match="full chat ID"):
        store.resolve(sid[:8])
    with pytest.raises(ComputerError, match="does not match"):
        store.register(str(uuid.uuid4()), "codex", path)
    outside = tmp_path / "private.jsonl"
    outside.write_bytes(path.read_bytes())
    with pytest.raises(ComputerError, match="local chat rollout"):
        store.register(sid, "codex", outside)


def test_origin_comes_from_calling_process_not_recent_chat(monkeypatch):
    sid = str(uuid.uuid4())
    monkeypatch.setattr(
        "core.session_identity.resolve_origin_session", lambda *args: (sid, "codex")
    )
    assert origin_arguments() == {"source_session_id": sid, "source_agent": "codex"}


def test_source_discovery_works_before_chat_indexing(tmp_path, monkeypatch):
    monkeypatch.setattr("core.indexer.get_session", lambda sid: None)
    sid, path = write_chat(tmp_path, "codex")
    store = ConversationStore(tmp_path / "state", home=tmp_path)
    assert store.resolve(sid, "codex")["path"] == str(path)


def test_oversized_context_is_never_silently_truncated(tmp_path):
    store = ConversationStore(tmp_path / "state", home=tmp_path)
    session = SimpleNamespace(id=uuid.uuid4().hex, request="test")
    store.bind(session)
    store.record(
        {
            "id": 1,
            "type": "observation",
            "session_id": session.id,
            "at": time.time(),
            "text": "x" * 710000,
        }
    )
    with pytest.raises(ComputerError, match="no earlier messages were silently dropped"):
        ConversationCursor(store, session.id).context()
    assert len(store.messages(session.id)[0]["text"]) == 710000


def test_hook_installation_preserves_existing_hooks_and_is_idempotent(tmp_path):
    from core.computer_hooks import install_context_hooks

    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "keep-existing"}]}
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "keep-stop"}]}],
                },
            }
        )
    )
    install_context_hooks(home=tmp_path, python="/python path/bin/python", cli="/repo path/cli.py")
    before = {p: p.read_bytes() for p in (settings, tmp_path / ".codex/hooks.json")}
    install_context_hooks(home=tmp_path, python="/python path/bin/python", cli="/repo path/cli.py")
    assert all(p.read_bytes() == content for p, content in before.items())
    claude = json.loads(settings.read_text())
    assert claude["theme"] == "dark"
    assert claude["hooks"]["Stop"][0]["hooks"][0]["command"] == "keep-stop"
    assert len(claude["hooks"]["UserPromptSubmit"]) == 2
    codex = json.loads((tmp_path / ".codex/hooks.json").read_text())
    handler = codex["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert handler["additionalContextLimit"] == 0
    assert handler["command"].startswith("'/python path/bin/python' '/repo path/cli.py'")
