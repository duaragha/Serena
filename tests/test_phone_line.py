

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
