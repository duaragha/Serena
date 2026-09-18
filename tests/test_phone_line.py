
import pytest


def test_plain_questions_about_the_queue_read_as_status():
    from core import phone_line

    for text in ("How many tasks are undone", "what tasks are running?",
                 "any prs waiting", "hows the queue"):
        assert phone_line.parse(text) == ("status", {}), text
    for text in ("how was your day", "hows the weather", "what do you think"):
        assert phone_line.parse(text) is None, text


def test_self_thread_marks_her_messages_by_prefix(monkeypatch):
    from core import bluebubbles_line

    payload = {"data": [
        {"guid": "a", "text": "serena: got it, queued as #9", "dateCreated": 2, "isFromMe": True},
        {"guid": "b", "text": "task: fix the thing in locket", "dateCreated": 1, "isFromMe": True},
    ]}
    monkeypatch.setattr(bluebubbles_line, "_request", lambda *a, **k: payload)
    monkeypatch.setattr(bluebubbles_line, "settings",
                        lambda: {"backend": "bluebubbles", "url": "http://x",
                                 "address": "+1", "self_thread": True})
    rows = bluebubbles_line.recent_messages(own_prefix="serena:")
    assert [(row["id"], row["own"]) for row in rows] == [("b", False), ("a", True)]
    # Without the prefix rule, a self-thread would look entirely like her own.
    plain = bluebubbles_line.recent_messages()
    assert all(row["own"] for row in plain)


def test_self_thread_send_keeps_the_prefix(monkeypatch):
    from core import bluebubbles_line, phone_line

    sent = []
    monkeypatch.setattr(bluebubbles_line, "send_text", lambda text, **kw: sent.append(text))
    monkeypatch.setattr(bluebubbles_line, "self_thread", lambda: True)
    assert phone_line._BlueBubblesBackend().send("queued as #9", "k") is True
    monkeypatch.setattr(bluebubbles_line, "self_thread", lambda: False)
    assert phone_line._BlueBubblesBackend().send("serena: queued as #9", "k") is True
    assert sent == ["serena: queued as #9", "queued as #9"]


def test_ping_reads_the_pong_from_data(monkeypatch):
    from core import bluebubbles_line

    monkeypatch.setattr(bluebubbles_line, "_request",
                        lambda *a, **k: {"status": 200, "message": "Ping received!",
                                         "data": "pong"})
    assert bluebubbles_line.ping() is True
    monkeypatch.setattr(bluebubbles_line, "_request", lambda *a, **k: {"message": "pong"})
    assert bluebubbles_line.ping() is True
    monkeypatch.setattr(bluebubbles_line, "_request", lambda *a, **k: {"message": "nope"})
    assert bluebubbles_line.ping() is False


def test_all_five_commands_still_parse():
    """Spec: conversation must not regress the command grammar."""

    from core import phone_line

    assert phone_line.parse("task: fix the thing in locket") == (
        "task", {"brief": "fix the thing in locket"})
    assert phone_line.parse("#1054 more detail here") == (
        "answer", {"task_id": 1054, "answer": "more detail here"})
    assert phone_line.parse("retry #1054") == ("retry", {"task_id": 1054})
    assert phone_line.parse("swapped") == ("swapped", {})
    assert phone_line.parse("status") == ("status", {})
    assert phone_line.parse("serena: queued as #9") is None
    assert phone_line.parse("   ") is None


class _FakeSharedLine:
    """A non-dedicated line (hub or BlueBubbles self-thread)."""

    name = "bluebubbles"
    initial_watermark = 0
    dedicated = False
    replays_pending_on_connect = False

    def __init__(self, messages):
        self._messages = messages
        self.state = {"inbound_watermark": 0, "inbound_handled": {}}
        self.sent = []

    def available(self):
        return True

    def load_state(self):
        return dict(self.state)

    def save_state(self, **fields):
        self.state.update(fields)

    def messages(self, state):
        return self._messages

    def send(self, text, key):
        self.sent.append((text, key))
        return True


def _row(message_id, text, created, **overrides):
    row = {"id": message_id, "text": text, "created": created,
           "own": False, "deleted": False, "kind": "text"}
    row.update(overrides)
    return row


def test_free_form_text_gets_a_brain_answer(monkeypatch):
    from core import phone_line, text_conversation

    line = _FakeSharedLine([_row("g1", "how was your day", 1)])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    calls = []
    monkeypatch.setattr(
        text_conversation, "answer",
        lambda texts, **kw: calls.append(texts) or ["pretty good.", "you?"])
    report = phone_line.poll(now=1000.0)
    assert calls == [["how was your day"]]
    assert [text for text, _ in line.sent] == ["pretty good.", "you?"]
    assert [c["kind"] for c in report.commands] == ["chat"]
    assert report.seen == 1


def test_three_bubbles_in_a_row_produce_one_turn(monkeypatch):
    from core import phone_line, text_conversation

    line = _FakeSharedLine([
        _row("g1", "first", 1), _row("g2", "second", 2), _row("g3", "third", 3),
    ])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    calls = []
    monkeypatch.setattr(
        text_conversation, "answer",
        lambda texts, **kw: calls.append(texts) or ["one answer"])
    phone_line.poll(now=1000.0)
    assert calls == [["first", "second", "third"]]
    assert [text for text, _ in line.sent] == ["one answer"]


def test_command_on_a_shared_thread_never_becomes_chat(monkeypatch):
    from core import phone_line, text_conversation

    line = _FakeSharedLine([_row("g1", "status", 1)])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    monkeypatch.setattr(phone_line, "_status_text", lambda: "nothing queued")
    monkeypatch.setattr(
        text_conversation, "answer",
        lambda texts, **kw: (_ for _ in ()).throw(AssertionError("chat!")))
    report = phone_line.poll(now=1000.0)
    assert [c["kind"] for c in report.commands] == ["status"]
    assert [text for text, _ in line.sent] == ["nothing queued"]


def test_chat_answer_respects_the_duplicate_window(monkeypatch):
    from core import phone_line, text_conversation

    first = _FakeSharedLine([_row("g1", "hello again", 1)])
    monkeypatch.setattr(phone_line, "_backend", lambda: first)
    calls = []
    monkeypatch.setattr(
        text_conversation, "answer",
        lambda texts, **kw: calls.append(texts) or ["hi"])
    phone_line.poll(now=1000.0)
    assert len(calls) == 1
    # The same text surfacing twice (sent + received copies) answers once.
    second = _FakeSharedLine([_row("g2", "hello again", 2)])
    second.state = dict(first.state)
    monkeypatch.setattr(phone_line, "_backend", lambda: second)
    phone_line.poll(now=1010.0)
    assert len(calls) == 1
    assert second.sent == []


def test_brain_outage_sends_one_fallback_not_silence(monkeypatch):
    from core import phone_line, text_conversation

    line = _FakeSharedLine([_row("g1", "are you there", 1)])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)

    def _down(texts, **kw):
        raise text_conversation.ConversationError("brain is down")

    monkeypatch.setattr(text_conversation, "answer", _down)
    report = phone_line.poll(now=1000.0)
    assert len(line.sent) == 1
    assert [c["kind"] for c in report.commands] == ["chat"]
    assert "error" in report.commands[0]


@pytest.fixture(autouse=True)
def no_live_bluebubbles_features(monkeypatch):
    """Phase 1/2 tests never touch Private API features, whatever the host
    has configured. Phase 3 tests opt back in per test."""

    from core import bluebubbles_line

    monkeypatch.setattr(bluebubbles_line, "enabled", lambda *a, **k: False)


def test_typing_indicator_posts_to_start_and_deletes_to_stop(monkeypatch):
    from core import bluebubbles_line

    calls = []
    monkeypatch.setattr(
        bluebubbles_line, "_request",
        lambda method, endpoint, **kw: calls.append((method, endpoint)) or {})
    assert bluebubbles_line.typing("chat-g", True) is True
    assert bluebubbles_line.typing("chat-g", False) is True
    assert calls[0] == ("POST", "chat/chat-g/typing")
    assert calls[1] == ("DELETE", "chat/chat-g/typing")


def test_private_api_failures_degrade_to_false(monkeypatch):
    from core import bluebubbles_line

    def _down(*args, **kwargs):
        raise bluebubbles_line.BlueBubblesLineError("no private api")

    monkeypatch.setattr(bluebubbles_line, "_request", _down)
    assert bluebubbles_line.typing("chat-g", True) is False
    assert bluebubbles_line.mark_read("chat-g") is False
    assert bluebubbles_line.react("chat-g", "m-1", "like") is False


def test_mark_read_and_react_hit_their_endpoints(monkeypatch):
    from core import bluebubbles_line

    calls = []

    def _record(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs.get("body")))
        return {}

    monkeypatch.setattr(bluebubbles_line, "_request", _record)
    assert bluebubbles_line.mark_read("chat-g") is True
    assert bluebubbles_line.react("chat-g", "m-1", "like") is True
    assert calls[0][:2] == ("POST", "chat/chat-g/read")
    assert calls[1][:2] == ("POST", "message/react")
    import json

    body = json.loads(calls[1][2].decode("utf-8"))
    assert body == {"chatGuid": "chat-g", "selectedMessageGuid": "m-1",
                    "reaction": "like"}


def test_typing_keepalive_shows_immediately_refreshes_and_stops(monkeypatch):
    import time

    from core import bluebubbles_line

    calls = []
    monkeypatch.setattr(
        bluebubbles_line, "typing",
        lambda guid, on: calls.append((guid, on)) or True)
    with bluebubbles_line.typing_keepalive("chat-g", interval=0.02):
        assert calls == [("chat-g", True)]
        time.sleep(0.05)
    assert calls[0] == ("chat-g", True)
    assert calls[-1] == ("chat-g", False)
    assert sum(1 for _, on in calls if on) >= 2


def test_recent_messages_expose_sender_service_and_attachments(monkeypatch):
    from core import bluebubbles_line

    payload = {"data": [
        {"guid": "a", "text": "", "dateCreated": 2, "isFromMe": False,
         "handle": {"address": "+1555", "service": "iMessage"},
         "attachments": [{"guid": "att-1", "mimeType": "image/png"}]},
        {"guid": "b", "text": "", "dateCreated": 1, "isFromMe": False,
         "handle": {"address": "+1555", "service": "iMessage"},
         "attachments": [{"guid": "att-2", "mimeType": "audio/x-caf"}]},
        {"guid": "c", "text": "hi", "dateCreated": 3, "isFromMe": False,
         "handle": {"address": "+1555", "service": "iMessage"}},
    ]}
    monkeypatch.setattr(bluebubbles_line, "_request", lambda *a, **k: payload)
    monkeypatch.setattr(bluebubbles_line, "settings",
                        lambda: {"backend": "bluebubbles", "url": "http://x",
                                 "address": "+1555"})
    rows = {row["id"]: row for row in bluebubbles_line.recent_messages()}
    assert rows["a"]["kind"] == "image"
    assert rows["a"]["attachments"] == [{"guid": "att-1", "mime": "image/png"}]
    assert rows["a"]["handle"] == "+1555"
    assert rows["a"]["service"] == "iMessage"
    assert rows["b"]["kind"] == "audio"
    assert rows["c"]["kind"] == "text"
    assert rows["c"]["attachments"] == []


def test_download_attachment_fetches_binary(monkeypatch):
    from core import bluebubbles_line

    calls = []
    monkeypatch.setattr(
        bluebubbles_line, "_request_bytes",
        lambda method, endpoint, **kw: calls.append((method, endpoint))
        or b"\x89PNG\r\n\x1a\nrest")
    monkeypatch.setattr(bluebubbles_line, "settings",
                        lambda: {"backend": "bluebubbles", "url": "http://x",
                                 "address": "+1555"})
    assert bluebubbles_line.download_attachment("att-1").startswith(b"\x89PNG")
    assert calls == [("GET", "attachment/att-1/download")]


def _live_bluebubbles(monkeypatch, calls):
    """Opt one test back into Private API features, all recorded."""

    import contextlib

    from core import bluebubbles_line

    monkeypatch.setattr(bluebubbles_line, "enabled", lambda *a, **k: True)
    monkeypatch.setattr(bluebubbles_line, "chat_guid", lambda *a, **k: "chat-g")
    monkeypatch.setattr(
        bluebubbles_line, "mark_read",
        lambda guid: calls.append(("read", guid)) or True)
    monkeypatch.setattr(
        bluebubbles_line, "react",
        lambda chat, message, reaction: calls.append(
            ("react", chat, message, reaction)) or True)

    @contextlib.contextmanager
    def _keepalive(guid, **kwargs):
        calls.append(("typing-on", guid))
        try:
            yield
        finally:
            calls.append(("typing-off", guid))

    monkeypatch.setattr(bluebubbles_line, "typing_keepalive", _keepalive)


def test_sms_service_is_refused_on_the_poll_path(monkeypatch):
    from core import phone_line, text_conversation

    line = _FakeSharedLine([
        _row("g1", "task: forged over sms", 1, service="SMS"),
        _row("g2", "hello over sms", 2, service="SMS"),
    ])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    monkeypatch.setattr(
        text_conversation, "answer",
        lambda texts, **kw: (_ for _ in ()).throw(AssertionError("chat!")))
    report = phone_line.poll(now=1000.0)
    assert report.seen == 2
    assert report.commands == []
    assert line.sent == []
    assert line.state["inbound_watermark"] == 2


def test_chat_marks_read_and_shows_typing_while_composing(monkeypatch):
    from core import phone_line, text_conversation

    calls = []
    _live_bluebubbles(monkeypatch, calls)
    line = _FakeSharedLine([_row("g1", "how are you", 1)])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    monkeypatch.setattr(
        text_conversation, "answer", lambda texts, **kw: ["good."])
    phone_line.poll(now=1000.0)
    assert ("read", "chat-g") in calls
    assert calls.index(("typing-on", "chat-g")) < calls.index(
        ("typing-off", "chat-g"))


def test_command_gets_a_tapback(monkeypatch):
    from core import phone_line

    calls = []
    _live_bluebubbles(monkeypatch, calls)
    line = _FakeSharedLine([_row("g1", "status", 1)])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    monkeypatch.setattr(phone_line, "_status_text", lambda: "nothing queued")
    phone_line.poll(now=1000.0)
    assert ("react", "chat-g", "g1", "like") in calls


def test_texted_photo_reaches_the_brain_as_an_image(monkeypatch):
    from core import bluebubbles_line, phone_line, text_conversation

    calls = []
    _live_bluebubbles(monkeypatch, calls)
    monkeypatch.setattr(
        bluebubbles_line, "download_attachment", lambda guid: b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    line = _FakeSharedLine([
        _row("g1", "look at this", 1, kind="image",
             attachments=[{"guid": "att-1", "mime": "image/png"}]),
    ])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    seen = {}
    monkeypatch.setattr(
        text_conversation, "answer",
        lambda texts, **kw: seen.update(texts=texts, **kw) or ["nice shot."])
    phone_line.poll(now=1000.0)
    assert seen["texts"] == ["look at this"]
    assert len(seen["images"]) == 1
    assert seen["images"][0]["media_type"] == "image/png"
    assert [text for text, _ in line.sent] == ["nice shot."]


def test_voice_note_is_transcribed_and_answered(monkeypatch):
    from core import bluebubbles_line, phone_line, text_conversation

    calls = []
    _live_bluebubbles(monkeypatch, calls)
    monkeypatch.setattr(
        bluebubbles_line, "download_attachment", lambda guid: b"fake-caf-bytes")
    monkeypatch.setattr(
        text_conversation, "transcribe_audio", lambda path: "call mom later")
    line = _FakeSharedLine([
        _row("g1", "", 1, kind="audio",
             attachments=[{"guid": "att-9", "mime": "audio/x-caf"}]),
    ])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    seen = {}
    monkeypatch.setattr(
        text_conversation, "answer",
        lambda texts, **kw: seen.update(texts=texts) or ["noted."])
    phone_line.poll(now=1000.0)
    assert seen["texts"] == ["[voice note] call mom later"]
    assert [text for text, _ in line.sent] == ["noted."]


def test_second_photo_is_not_swallowed_as_duplicate(monkeypatch):
    from core import bluebubbles_line, phone_line, text_conversation

    calls = []
    _live_bluebubbles(monkeypatch, calls)
    monkeypatch.setattr(
        bluebubbles_line, "download_attachment",
        lambda guid: b"\x89PNG\r\n\x1a\n" + guid.encode() + b"\x00" * 64)
    first = _FakeSharedLine([
        _row("g1", "", 1, kind="image",
             attachments=[{"guid": "att-1", "mime": "image/png"}]),
    ])
    monkeypatch.setattr(phone_line, "_backend", lambda: first)
    answers = []
    monkeypatch.setattr(
        text_conversation, "answer",
        lambda texts, **kw: answers.append(texts) or ["nice."])
    phone_line.poll(now=1000.0)
    assert len(answers) == 1
    second = _FakeSharedLine([
        _row("g2", "", 2, kind="image",
             attachments=[{"guid": "att-2", "mime": "image/png"}]),
    ])
    second.state = dict(first.state)
    monkeypatch.setattr(phone_line, "_backend", lambda: second)
    phone_line.poll(now=1010.0)
    assert len(answers) == 2
    assert len(second.sent) == 1


def test_stranger_handle_is_refused_on_the_poll_path(monkeypatch):
    from core import bluebubbles_line, phone_line, text_conversation

    monkeypatch.setattr(
        bluebubbles_line, "settings",
        lambda: {"backend": "bluebubbles", "address": "him@example.com"})
    line = _FakeSharedLine([
        _row("g1", "task: forged by a stranger", 1,
             service="iMessage", handle="stranger@example.com"),
        _row("g2", "hello from a stranger", 2,
             service="iMessage", handle="stranger@example.com"),
    ])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    monkeypatch.setattr(
        text_conversation, "answer",
        lambda texts, **kw: (_ for _ in ()).throw(AssertionError("chat!")))
    report = phone_line.poll(now=1000.0)
    assert report.seen == 2
    assert report.commands == []
    assert line.sent == []
    mine = _FakeSharedLine([
        _row("g3", "hello from him", 3,
             service="iMessage", handle="him@example.com"),
    ])
    mine.state = dict(line.state)
    monkeypatch.setattr(phone_line, "_backend", lambda: mine)
    monkeypatch.setattr(text_conversation, "answer", lambda texts, **kw: ["hi."])
    mine_report = phone_line.poll(now=1010.0)
    assert [c["kind"] for c in mine_report.commands] == ["chat"]
    assert len(mine.sent) == 1


@pytest.mark.parametrize("first,second,expected", [
    ("him@example.com", "him@example.com", True),
    ("  Him@Example.COM ", "him@example.com", True),
    ("+15550101234", "5550101234", True),
    ("+1 (555) 010-1234", "5550101234", True),
    ("+15550101234", "+15550101234", True),
    ("+15550101234", "5550101999", False),
    ("stranger@example.com", "him@example.com", False),
    ("him@example.com", "+15550101234", False),
    ("", "him@example.com", False),
    ("him@example.com", "", False),
    ("911", "911", True),
    ("911", "1911", False),
])
def test_same_handle_matches_spellings_not_strings(first, second, expected):
    from core import bluebubbles_line

    assert bluebubbles_line.same_handle(first, second) is expected


def test_number_spelling_of_his_handle_is_answered(monkeypatch):
    from core import bluebubbles_line, phone_line, text_conversation

    monkeypatch.setattr(
        bluebubbles_line, "settings",
        lambda: {"backend": "bluebubbles", "address": "+15550101234"})
    line = _FakeSharedLine([
        _row("g1", "hello from his other spelling", 1,
             service="iMessage", handle="(555) 010-1234"),
        _row("g2", "hello from a stranger", 2,
             service="iMessage", handle="+15550101999"),
    ])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    monkeypatch.setattr(text_conversation, "answer", lambda texts, **kw: ["hi."])
    report = phone_line.poll(now=1000.0)
    assert report.seen == 2
    assert [c["kind"] for c in report.commands] == ["chat"]
    assert report.commands[0]["message_ids"] == ["g1"]
    assert len(line.sent) == 1


def test_broken_voice_note_says_so_instead_of_silence(monkeypatch):
    from core import bluebubbles_line, phone_line, text_conversation

    calls = []
    _live_bluebubbles(monkeypatch, calls)
    monkeypatch.setattr(
        bluebubbles_line, "download_attachment",
        lambda guid: (_ for _ in ()).throw(
            bluebubbles_line.BlueBubblesLineError("gone")))
    line = _FakeSharedLine([
        _row("g1", "", 1, kind="audio",
             attachments=[{"guid": "att-9", "mime": "audio/x-caf"}]),
    ])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    monkeypatch.setattr(
        text_conversation, "answer",
        lambda texts, **kw: (_ for _ in ()).throw(AssertionError("chat!")))
    report = phone_line.poll(now=1000.0)
    assert [c["kind"] for c in report.commands] == ["chat"]
    assert "error" in report.commands[0]
    assert len(line.sent) == 1
    assert "couldn't open" in line.sent[0][0]


def test_transcribe_audio_reports_a_missing_model(tmp_path, monkeypatch):
    from core import text_conversation

    monkeypatch.setenv("SERENA_VOICE_NOTE_MODEL", str(tmp_path / "no-model"))
    monkeypatch.delenv("SERENA_CALL_WHISPER_MODEL", raising=False)
    with pytest.raises(text_conversation.ConversationError):
        text_conversation.transcribe_audio(tmp_path / "note.caf")


@pytest.mark.parametrize("text,expected", [
    ("yes 4821", ("approve", {"nonce": "4821"})),
    ("no 4821", ("deny", {"nonce": "4821"})),
    ("YES 0007", ("approve", {"nonce": "0007"})),
    ("yes", None),
    ("no", None),
    ("yes 4821 please", None),
    ("approve 4821", None),
    ("my code is 4821 yes", None),
    ("yes 48210", None),
    ("yes 482", None),
])
def test_approval_grammar_is_anchored(text, expected):
    """Spec: `yes <nonce>` / `no <nonce>` and nothing else is an approval."""

    from core import phone_line

    assert phone_line.parse(text) == expected


@pytest.fixture()
def approval_broker(tmp_path, monkeypatch):
    from core import approvals
    from core.action_authority import ActionAuthority
    from core.notification_authority import NotificationAuthority

    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH",
                       str(tmp_path / "control.sqlite3"))
    authority = ActionAuthority(
        tmp_path / "action.sqlite3", audit_path=tmp_path / "action.jsonl",
        publish_events=False)
    notifications = NotificationAuthority(
        path=tmp_path / "notifications.sqlite3",
        senders={"voice": lambda r: True, "imessage": lambda r: True,
                 "telegram": lambda r: True, "desktop": lambda r: True})
    broker = approvals.ApprovalBroker(
        path=tmp_path / "approvals.sqlite3", authority=authority,
        notifications=notifications, in_call=lambda: False)
    monkeypatch.setattr(approvals, "ApprovalBroker", lambda *a, **k: broker)
    return broker


def test_yes_nonce_by_text_approves(approval_broker, monkeypatch):
    import time

    from core import phone_line

    pending = approval_broker.authority.request_confirmation(
        capability="data.delete_one", target="old-cache",
        tier=3, ttl_seconds=600, now=time.time())
    (entry,) = approval_broker.scan()
    line = _FakeSharedLine([_row("g1", f"yes {entry['nonce']}", 1)])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    report = phone_line.poll(now=time.time())
    assert [c["kind"] for c in report.commands] == ["approve"]
    assert approval_broker.authority.confirmation(
        pending.confirmation_id).state == "approved"
    assert "approv" in line.sent[0][0]


def test_no_nonce_by_text_denies(approval_broker, monkeypatch):
    import time

    from core import phone_line

    pending = approval_broker.authority.request_confirmation(
        capability="data.delete_one", target="old-cache",
        tier=3, ttl_seconds=600, now=time.time())
    (entry,) = approval_broker.scan()
    line = _FakeSharedLine([_row("g1", f"no {entry['nonce']}", 1)])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    report = phone_line.poll(now=time.time())
    assert [c["kind"] for c in report.commands] == ["deny"]
    assert approval_broker.authority.confirmation(
        pending.confirmation_id).state == "denied"


def test_wrong_nonce_by_text_is_refused_and_stays_pending(
        approval_broker, monkeypatch):
    import time

    from core import phone_line

    pending = approval_broker.authority.request_confirmation(
        capability="data.delete_one", target="old-cache",
        tier=3, ttl_seconds=600, now=time.time())
    approval_broker.scan()
    line = _FakeSharedLine([_row("g1", "yes 0000", 1)])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    report = phone_line.poll(now=time.time())
    assert [c["kind"] for c in report.commands] == ["approve"]
    assert "error" in report.commands[0]
    assert approval_broker.authority.confirmation(
        pending.confirmation_id).state == "pending"
    assert line.sent
