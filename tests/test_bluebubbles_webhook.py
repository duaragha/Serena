"""Spec: iMessage conversation, Phase 2 — instant delivery.

BlueBubbles pushes `new-message` / `updated-message` events to the signed
ingress. The route authenticates with its own URL token (BlueBubbles cannot
HMAC-sign), refuses anything that is not his iMessage handle, and triggers
one fast poll per message GUID — so the two events BlueBubbles fires for one
message answer once, and the 60 s poll stays a no-op safety net.
"""

import pytest

from core import webhook_ingress as webhooks

SECRET = "test-ingress-secret"
BB_TOKEN = "test-bluebubbles-token"
HIS_ADDRESS = "raghav@example.com"


@pytest.fixture
def ingress(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_BLUEBUBBLES_WEBHOOK_TOKEN", BB_TOKEN)
    from core.webhook_signing import WebhookReplayStore

    return webhooks.default_ingress(
        path=tmp_path / "ingress.sqlite3", secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "replays.sqlite3"),
    )


@pytest.fixture(autouse=True)
def fresh_guid_claims():
    webhooks._BLUEBUBBLES_SEEN.clear()
    yield
    webhooks._BLUEBUBBLES_SEEN.clear()


@pytest.fixture
def bluebubbles_settings(monkeypatch):
    from core import bluebubbles_line

    monkeypatch.setattr(
        bluebubbles_line, "settings",
        lambda: {"backend": "bluebubbles", "url": "http://x",
                 "address": HIS_ADDRESS, "self_thread": True})


def _payload(**overrides):
    data = {"guid": "m-1", "text": "hello there", "isFromMe": True,
            "handle": {"address": HIS_ADDRESS, "service": "iMessage"},
            "chats": [{"guid": f"iMessage;-;{HIS_ADDRESS}"}]}
    if "data" in overrides:
        replacement = overrides.pop("data")
        data = replacement if isinstance(replacement, dict) else replacement
        if isinstance(data, dict):
            merged = {"guid": "m-1", "text": "hello there", "isFromMe": True,
                      "handle": {"address": HIS_ADDRESS,
                                 "service": "iMessage"},
                      "chats": [{"guid": f"iMessage;-;{HIS_ADDRESS}"}]}
            merged.update(replacement)
            data = merged
    event = {"type": "new-message", "data": data}
    event.update(overrides)
    return event


def _post(ingress, payload, token=BB_TOKEN):
    import json

    return ingress.handle(
        "bluebubbles", json.dumps(payload).encode("utf-8"), {},
        query={"token": token} if token else {}, now=1000.0)


def test_bluebubbles_route_is_registered(ingress):
    assert "bluebubbles" in ingress.routes


def test_wrong_or_missing_token_is_refused(ingress):
    assert _post(ingress, _payload(), token="wrong").status == 401
    assert _post(ingress, _payload(), token="").status == 401


def test_missing_token_config_closes_the_route(tmp_path, monkeypatch):
    from core.webhook_signing import WebhookReplayStore

    monkeypatch.delenv("SERENA_BLUEBUBBLES_WEBHOOK_TOKEN", raising=False)
    monkeypatch.setenv("SERENA_BLUEBUBBLES_WEBHOOK_TOKEN_FILE",
                       str(tmp_path / "missing-token"))
    ingress = webhooks.default_ingress(
        path=tmp_path / "ingress.sqlite3", secret=SECRET,
        replay_store=WebhookReplayStore(tmp_path / "replays.sqlite3"),
    )
    assert _post(ingress, _payload()).status == 401


@pytest.mark.parametrize("payload", [
    _payload(type="typing-indicator"),
    _payload(data={"guid": ""}),
    _payload(data={"handle": {"address": HIS_ADDRESS,
                              "service": "SMS"}}),
    _payload(data={"handle": {"service": "iMessage"}}),
    _payload(data="not-a-dict"),
    {"type": "new-message"},
])
def test_malformed_or_sms_payload_is_refused(ingress, payload):
    assert _post(ingress, payload).status == 400


def test_his_message_triggers_one_fast_poll(ingress, bluebubbles_settings,
                                            monkeypatch):
    from core import phone_line

    polls = []
    monkeypatch.setattr(
        phone_line, "poll",
        lambda **kw: polls.append(kw) or phone_line.PollReport(seen=1))
    monkeypatch.setattr(webhooks.time, "sleep", lambda seconds: None)
    result = _post(ingress, _payload())
    assert result.accepted
    assert len(polls) == 1


def test_number_spelling_of_his_handle_polls(ingress, monkeypatch):
    from core import bluebubbles_line, phone_line

    monkeypatch.setattr(
        bluebubbles_line, "settings",
        lambda: {"backend": "bluebubbles", "url": "http://x",
                 "address": "+15550101234", "self_thread": True})
    polls = []
    monkeypatch.setattr(
        phone_line, "poll",
        lambda **kw: polls.append(kw) or phone_line.PollReport(seen=1))
    monkeypatch.setattr(webhooks.time, "sleep", lambda seconds: None)
    payload = _payload(data={"guid": "m-9",
                             "handle": {"address": "5550101234",
                                        "service": "iMessage"}})
    assert _post(ingress, payload).accepted
    assert len(polls) == 1


def test_chat_waits_out_the_burst_before_polling(ingress,
                                                 bluebubbles_settings,
                                                 monkeypatch):
    """One turn, not three: chat lets the burst land, then polls once."""

    from core import phone_line
    from core.text_conversation import DEBOUNCE_SECONDS

    slept = []
    monkeypatch.setattr(webhooks.time, "sleep", slept.append)
    monkeypatch.setattr(
        phone_line, "poll", lambda **kw: phone_line.PollReport(seen=3))
    result = _post(ingress, _payload(data={"text": "and one more thing"}))
    assert result.accepted
    assert slept == [DEBOUNCE_SECONDS]


def test_command_skips_the_debounce_wait(ingress, bluebubbles_settings,
                                         monkeypatch):
    """Commands stay on the fast path: no waiting for `status`."""

    from core import phone_line

    monkeypatch.setattr(
        webhooks.time, "sleep",
        lambda seconds: (_ for _ in ()).throw(AssertionError("slept!")))
    monkeypatch.setattr(
        phone_line, "poll", lambda **kw: phone_line.PollReport(seen=1))
    result = _post(ingress, _payload(data={"text": "status"}))
    assert result.accepted


def test_new_message_plus_updated_message_replies_once(
        ingress, bluebubbles_settings, monkeypatch):
    from core import phone_line

    polls = []
    monkeypatch.setattr(
        phone_line, "poll",
        lambda **kw: polls.append(kw) or phone_line.PollReport(seen=1))
    monkeypatch.setattr(webhooks.time, "sleep", lambda seconds: None)
    assert _post(ingress, _payload()).accepted
    updated = _payload(type="updated-message")
    assert _post(ingress, updated).accepted
    assert len(polls) == 1
    assert ingress.history(route="bluebubbles")[0]["decision"] == "accepted"


def test_stranger_handle_is_ignored_not_polled(ingress, bluebubbles_settings,
                                               monkeypatch):
    from core import phone_line

    monkeypatch.setattr(
        phone_line, "poll",
        lambda **kw: (_ for _ in ()).throw(AssertionError("polled!")))
    result = _post(ingress, _payload(
        data={"handle": {"address": "stranger@example.com",
                         "service": "iMessage"}}))
    assert result.accepted
    assert "ignored" in result.reason


def test_group_chat_is_ignored(ingress, bluebubbles_settings, monkeypatch):
    from core import phone_line

    monkeypatch.setattr(
        phone_line, "poll",
        lambda **kw: (_ for _ in ()).throw(AssertionError("polled!")))
    result = _post(ingress, _payload(data={
        "participants": [HIS_ADDRESS, "friend@example.com",
                         "other@example.com"]}))
    assert result.accepted
    assert "ignored" in result.reason


def test_her_own_message_is_ignored(ingress, bluebubbles_settings, monkeypatch):
    from core import phone_line

    monkeypatch.setattr(
        phone_line, "poll",
        lambda **kw: (_ for _ in ()).throw(AssertionError("polled!")))
    # Self-thread: hers carry the prefix. Dedicated line: isFromMe decides.
    prefixed = _payload(data={"text": "serena: got it"})
    assert "ignored" in _post(ingress, prefixed).reason
    reaction = _payload(data={"guid": "m-2", "associatedMessageGuid": "m-0"})
    assert "ignored" in _post(ingress, reaction).reason


def test_poll_is_a_clean_noop_after_the_webhook_answered(monkeypatch):
    """The 60 s sweep answers when the webhook is down, else stays quiet."""

    import tests.test_phone_line as phone_line_tests
    from core import phone_line, text_conversation

    line = phone_line_tests._FakeSharedLine(
        [phone_line_tests._row("g1", "hello", 1)])
    monkeypatch.setattr(phone_line, "_backend", lambda: line)
    monkeypatch.setattr(text_conversation, "answer", lambda texts, **kw: ["hi"])
    first = phone_line.poll(now=1000.0)
    assert first.seen == 1
    assert len(line.sent) == 1
    second = phone_line.poll(now=1060.0)
    assert second.seen == 0
    assert second.commands == []
    assert len(line.sent) == 1
