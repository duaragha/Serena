import json


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
