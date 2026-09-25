from __future__ import annotations

import asyncio
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
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
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


def test_oversized_protected_coaching_still_has_a_safety_guard(tmp_path):
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
    with pytest.raises(ComputerError, match="700 KB computer-context safety limit"):
        ConversationCursor(store, session.id).context()
    assert len(store.messages(session.id)[0]["text"]) == 710000


def context_records(text):
    return json.loads(text.split("New context records:\n", 1)[1]) if text else []


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_long_history_tail_preserves_order_storage_and_rotation(tmp_path, monkeypatch, agent):
    monkeypatch.delenv("SERENA_COMPUTER_CONTEXT_CHARS", raising=False)
    store = ConversationStore(tmp_path / "state", home=tmp_path)
    sid, path = write_chat(tmp_path, agent, count=1000)
    session = SimpleNamespace(id=uuid.uuid4().hex, request="test")
    store.bind(session, store.register(sid, agent, path))
    original = store.messages(session.id)
    cursor = ConversationCursor(store, session.id)
    text = cursor.context()
    records = context_records(text)
    assert 0 < len(records) < len(original)
    assert sum(len(m["text"]) for m in records) <= 20000
    assert records == original[-len(records):]
    assert original[-2] in records  # Newest user followed by the newest assistant.
    assert "Earlier linked history omitted" in text
    assert cursor.message_count == 1000
    assert store.messages(session.id) == original
    assert cursor.context() == text
    cursor.commit()
    assert cursor.context() == ""
    cursor.reset()
    assert cursor.context() == text


def fake_cursor(messages):
    return ConversationCursor(SimpleNamespace(messages=lambda _: messages), "current")


def chat_record(index, role, text, *, kind="chat", session_id=""):
    return dict(id=str(index), role=role, text=text, kind=kind, session_id=session_id)


@pytest.mark.parametrize("budget", ["20", "0", "-1", "invalid"])
def test_context_budget_override_and_invalid_values(monkeypatch, budget):
    monkeypatch.setenv("SERENA_COMPUTER_CONTEXT_CHARS", budget)
    messages = [chat_record(i, "user" if i % 2 == 0 else "assistant", "界" * 10)
                for i in range(8)]
    cursor = fake_cursor(messages)
    expected = [] if budget == "0" else messages[-2:] if budget == "20" else messages
    assert context_records(cursor.context()) == expected
    assert cursor.message_count == 8


def test_newest_user_and_current_coaching_survive_tiny_budget(monkeypatch):
    monkeypatch.setenv("SERENA_COMPUTER_CONTEXT_CHARS", "5")
    messages = [
        chat_record(0, "assistant", "old coaching", kind="coaching", session_id="previous"),
        chat_record(1, "assistant", "first completed advice", kind="coaching", session_id="current"),
        chat_record(2, "user", "newest user message exceeds budget"),
        chat_record(3, "assistant", "a long assistant followup"),
        chat_record(4, "assistant", "second completed advice", kind="coaching", session_id="current"),
    ]
    cursor = fake_cursor(messages)
    text = cursor.context()
    assert context_records(text) == [messages[1], messages[2], messages[4]]
    assert "Earlier linked history omitted (2 records)" in text
    cursor.commit()
    assert cursor.context() == ""
    messages[2]["text"] += " edited"
    assert context_records(cursor.context()) == [messages[2]]
    cursor.commit()
    cursor.reset()
    assert context_records(cursor.context()) == [messages[1], messages[2], messages[4]]
    monkeypatch.setenv("SERENA_COMPUTER_CONTEXT_CHARS", "0")
    cursor.reset()
    assert context_records(cursor.context()) == [messages[1], messages[4]]


def test_trimming_does_not_change_parent_hook_or_completed_coaching(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_COMPUTER_CONTEXT_CHARS", "0")
    store = ConversationStore(tmp_path / "state", home=tmp_path)
    sid, path = write_chat(tmp_path, "codex")
    origin = store.register(sid, "codex", path)
    for session_id in ("previous", "current"):
        store.bind(SimpleNamespace(id=session_id, request="test"), origin)
        for event_id, kind in enumerate(("delta", "observation")):
            store.record(dict(id=event_id, type=kind, session_id=session_id,
                              at=time.time(), text=f"{session_id}-{kind}"))
    cursor = ConversationCursor(store, "current")
    assert [m["text"] for m in context_records(cursor.context())] == ["current-observation"]
    assert len(store.messages("current")) == 32
    injected = hook_context({"session_id": sid}, "codex", store=store)
    text = injected["hookSpecificOutput"]["additionalContext"]
    assert "previous-observation" in text and "current-observation" in text
    assert "delta" not in text


def test_tail_without_user_and_oversized_old_record(monkeypatch):
    monkeypatch.setenv("SERENA_COMPUTER_CONTEXT_CHARS", "20")
    messages = [chat_record(0, "assistant", "x" * 710000),
                chat_record(1, "assistant", "recent")]
    assert context_records(fake_cursor(messages).context()) == messages[-1:]
    assert fake_cursor([]).context() == ""
    assert ConversationCursor(None, "current").context() == ""


def test_agent_context_and_thread_reset_use_the_bounded_tail(monkeypatch):
    from core.computer_agent import ComputerAgent

    monkeypatch.setenv("SERENA_COMPUTER_CONTEXT_CHARS", "20")
    messages = [chat_record(i, "user", str(i) * 10) for i in range(6)]
    controller = SimpleNamespace(
        session=SimpleNamespace(id="current"),
        conversations=SimpleNamespace(messages=lambda _: messages),
    )
    agent = ComputerAgent(controller)
    resets = []

    async def inline_thread(function, *args):
        return function(*args)

    # Exercise prompt integration without depending on sandbox thread wakeup sockets.
    monkeypatch.setattr("core.computer_agent.asyncio.to_thread", inline_thread)

    async def reset_thread():
        resets.append(True)

    async def check():
        initial = await agent.context()
        assert context_records(initial) == messages[-2:]
        assert controller.session.context_message_count == 6
        agent.conversation.commit()
        assert await agent.context() == ""
        messages.append(chat_record(6, "user", "followup"))
        assert context_records(await agent.context()) == messages[-1:]
        agent.conversation.commit()
        await agent._reset_model_thread(SimpleNamespace(reset_thread=reset_thread))
        assert context_records(await agent.context()) == messages[-2:]
        assert controller.session.context_message_count == 7
        assert resets == [True]

    asyncio.run(check())


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
    , encoding="utf-8")
    install_context_hooks(home=tmp_path, python="/python path/bin/python", cli="/repo path/cli.py")
    before = {p: p.read_bytes() for p in (settings, tmp_path / ".codex/hooks.json")}
    install_context_hooks(home=tmp_path, python="/python path/bin/python", cli="/repo path/cli.py")
    assert all(p.read_bytes() == content for p, content in before.items())
    claude = json.loads(settings.read_text(encoding="utf-8"))
    assert claude["theme"] == "dark"
    assert claude["hooks"]["Stop"][0]["hooks"][0]["command"] == "keep-stop"
    assert len(claude["hooks"]["UserPromptSubmit"]) == 2
    codex = json.loads((tmp_path / ".codex/hooks.json").read_text(encoding="utf-8"))
    handler = codex["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert handler["additionalContextLimit"] == 0
    assert handler["command"].startswith("'/python path/bin/python' '/repo path/cli.py'")
