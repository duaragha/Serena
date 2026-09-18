import json

import pytest


def _configure(monkeypatch, tmp_path, *, backend="telegram", chat="6354"):
    env = tmp_path / "telegram.env"
    env.write_text(f"TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID={chat}\n", encoding="utf-8")
    line = tmp_path / "phone-line.json"
    line.write_text(json.dumps({"backend": backend}), encoding="utf-8")
    monkeypatch.setenv("SERENA_TELEGRAM_ENV", str(env))
    monkeypatch.setenv("SERENA_PHONE_LINE_CONFIG", str(line))


def _update(update_id, text, *, chat="6354", is_bot=False):
    return {"update_id": update_id, "message": {
        "message_id": update_id * 10, "text": text,
        "chat": {"id": int(chat)}, "from": {"id": 1, "is_bot": is_bot}}}


def test_enabled_needs_both_the_switch_and_the_credentials(monkeypatch, tmp_path):
    from core import telegram_line

    _configure(monkeypatch, tmp_path)
    assert telegram_line.enabled() is True
    # Pointed at another transport, Telegram must keep its hands off the line.
    _configure(monkeypatch, tmp_path, backend="bluebubbles")
    assert telegram_line.enabled() is False
    monkeypatch.setenv("SERENA_TELEGRAM_ENV", str(tmp_path / "missing.env"))
    assert telegram_line.configured() is False


def test_only_his_own_chat_reaches_the_grammar(monkeypatch, tmp_path):
    from core import telegram_line

    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(telegram_line, "updates", lambda **kw: [
        _update(7, "task: fix the thing in locket"),
        _update(8, "task: drop the database", chat="999"),
        _update(9, "got it, queued as #3", is_bot=True),
    ])
    rows = telegram_line.recent_messages()
    assert [(row["created"], row["own"]) for row in rows] == [(7, False), (9, True)]


def test_updates_confirm_the_offset_server_side(monkeypatch, tmp_path):
    from core import telegram_line

    _configure(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(telegram_line, "_call",
                        lambda method, payload=None: calls.append((method, payload)) or [])
    telegram_line.updates(offset=41)
    telegram_line.updates(offset=0)
    assert calls[0][1]["offset"] == 42
    # A fresh line has nothing to confirm, so no offset is sent at all.
    assert "offset" not in calls[1][1]
    assert [call[0] for call in calls] == ["getUpdates", "getUpdates"]


def test_a_rejected_call_never_leaks_the_token(monkeypatch, tmp_path):
    import urllib.request

    from core import telegram_line

    _configure(monkeypatch, tmp_path)

    class _Response:
        def read(self):
            return json.dumps({"ok": False, "description": "Unauthorized"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response())
    try:
        telegram_line.identity()
    except telegram_line.TelegramLineError as error:
        assert "123:abc" not in str(error)
        assert "Unauthorized" in str(error)
    else:
        raise AssertionError("a rejected call must raise")
    assert telegram_line.ping() is False


def test_the_bot_sends_without_the_self_thread_prefix(monkeypatch, tmp_path):
    from core import phone_line, telegram_line

    _configure(monkeypatch, tmp_path)
    sent = []

    def _call(method, payload=None):
        sent.append((method, payload))
        return {"message_id": 5}

    monkeypatch.setattr(telegram_line, "_call", _call)
    assert phone_line._TelegramBackend().send("serena: queued as #9", "k") is True
    assert sent[0][0] == "sendMessage"
    assert sent[0][1]["text"] == "queued as #9"
    assert sent[0][1]["chat_id"] == "6354"


def test_the_line_prefers_telegram_when_it_owns_the_switch(monkeypatch, tmp_path):
    from core import bluebubbles_line, phone_line, telegram_line

    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(bluebubbles_line, "enabled", lambda: True)
    assert phone_line.backend_name() == "telegram"
    monkeypatch.setattr(telegram_line, "enabled", lambda: False)
    assert phone_line.backend_name() == "bluebubbles"


def test_the_watermark_becomes_the_next_offset(monkeypatch, tmp_path):
    from core import phone_line, telegram_line

    _configure(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(telegram_line, "recent_messages",
                        lambda offset=0: seen.append(offset) or [])
    backend = phone_line._TelegramBackend()
    backend.messages({"inbound_watermark": 12})
    backend.messages({"inbound_watermark": ""})
    backend.messages({})
    assert seen == [12, 0, 0]
    # Its state must not share a file with the BlueBubbles watermark.
    assert backend._state_path() != phone_line._BlueBubblesBackend()._state_path()


# ---- her own chat is a dedicated line, not a thread to overhear -----------


@pytest.fixture
def queue(tmp_path, monkeypatch):
    from memory import store

    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(store, "_active_v2_store", lambda: None)
    monkeypatch.setattr(store, "_source_context", lambda: ("", "", "", ""))
    import memory.locket_mirror as mirror
    for name in ("mirror_add", "mirror_update", "mirror_delete", "mirror_archive"):
        monkeypatch.setattr(mirror, name, lambda *a, **kw: None)
    return tmp_path / "memory"


def _dedicated(monkeypatch, tmp_path, rows):
    """Point the whole line at her bot, with `rows` waiting in the chat."""

    from core import phone_line, telegram_line

    _configure(monkeypatch, tmp_path)
    state = tmp_path / "phone-line-telegram.json"
    monkeypatch.setattr(phone_line._FileStateBackend, "_state_path", lambda self: state)
    monkeypatch.setattr(telegram_line, "recent_messages", lambda offset=0: list(rows))
    sent: list[str] = []
    monkeypatch.setattr(telegram_line, "send_text", lambda text: sent.append(text) or True)
    return state, sent


def _row(update_id, text, *, kind="text", own=False):
    return {"id": str(update_id * 10), "text": text, "created": update_id,
            "own": own, "deleted": False, "kind": kind}


def test_plain_text_on_her_own_chat_is_a_brief():
    from core import phone_line

    text = "enable workouts so they affect the health stats in Locket"
    # He is not writing a command line; on her bot's chat he never has to.
    assert phone_line.parse(text) is None
    assert phone_line.parse(text, plain_is_brief=True) == ("task", {"brief": text})
    # The grammar still wins where it matches, so `status` never queues work.
    assert phone_line.parse("status", plain_is_brief=True) == ("status", {})
    assert phone_line.parse("retry #42", plain_is_brief=True) == ("retry", {"task_id": 42})
    # Her own replies are still hers, prefix or not.
    assert phone_line.parse("serena: queued as #9", plain_is_brief=True) is None


def test_the_first_poll_answers_what_waits_instead_of_eating_it(monkeypatch, tmp_path, queue):
    from core import phone_line

    brief = "research enabling workouts so they affect the health stats in Locket"
    state, sent = _dedicated(monkeypatch, tmp_path, [_row(537122438, brief)])

    report = phone_line.poll(now=1000)

    # Adopting his message as the opening watermark is what lost it before.
    assert report.seen == 1
    assert [command["kind"] for command in report.commands] == ["task"]
    assert sent and sent[0].startswith("got it, queued as #")
    assert json.loads(state.read_text(encoding="utf-8"))["inbound_watermark"] == 537122438


def test_a_second_poll_does_not_requeue_the_same_brief(monkeypatch, tmp_path, queue):
    from core import phone_line

    brief = "fix the journal in Locket so entries before august load again"
    state, sent = _dedicated(monkeypatch, tmp_path, [_row(11, brief)])
    assert len(phone_line.poll(now=1000).commands) == 1
    assert phone_line.poll(now=1010).commands == []
    assert len(sent) == 1


def test_a_thin_brief_asks_him_rather_than_going_quiet(monkeypatch, tmp_path, queue):
    from core import phone_line

    state, sent = _dedicated(monkeypatch, tmp_path, [_row(12, "the locket thing again")])
    report = phone_line.poll(now=1000)
    assert [command["kind"] for command in report.commands] == ["task"]
    assert "what exactly should change" in sent[0]


def test_a_photo_is_answered_rather_than_dropped(monkeypatch, tmp_path, queue):
    from core import phone_line

    state, sent = _dedicated(monkeypatch, tmp_path, [_row(13, "", kind="other")])
    report = phone_line.poll(now=1000)
    assert report.seen == 1 and report.commands == []
    assert sent == ["i can only read text. type what you want done."]


def test_her_own_messages_never_come_back_as_work(monkeypatch, tmp_path, queue):
    from core import phone_line

    state, sent = _dedicated(monkeypatch, tmp_path, [_row(14, "got it, queued as #9", own=True)])
    assert phone_line.poll(now=1000).commands == []
    assert sent == []


def test_a_shared_thread_still_ignores_plain_conversation(monkeypatch, tmp_path):
    from core import bluebubbles_line, phone_line, telegram_line

    _configure(monkeypatch, tmp_path, backend="bluebubbles")
    monkeypatch.setattr(telegram_line, "enabled", lambda: False)
    monkeypatch.setattr(bluebubbles_line, "enabled", lambda: True)
    line = phone_line._backend()
    assert line.name == "bluebubbles"
    # His own thread carries notes to himself; only the grammar queues work,
    # and connecting must not replay whatever is already sitting in it.
    assert line.dedicated is False
    assert line.replays_pending_on_connect is False
    assert phone_line._TelegramBackend().dedicated is True
