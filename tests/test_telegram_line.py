import json

import pytest


def _configure(monkeypatch, tmp_path, *, backend="telegram", chat="6354"):
    env = tmp_path / "telegram.env"
    env.write_text(f"TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID={chat}\n", encoding='utf-8')
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

    def _call(method, payload=None, *, timeout=None):
        calls.append((method, payload, timeout))
        return []

    monkeypatch.setattr(telegram_line, "_call", _call)
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


def _dedicated(monkeypatch, tmp_path, rows, *, brain="down"):
    """Point the whole line at her bot, with `rows` waiting in the chat.

    `brain` stands in for the resident daemon: "down" makes every read fall
    through to `triage`, or pass ("task", brief) / ("say", reply) to script her.
    """

    from core import phone_intent, phone_line, telegram_line

    _configure(monkeypatch, tmp_path)
    state = tmp_path / "phone-line-telegram.json"
    monkeypatch.setattr(phone_line._FileStateBackend, "_state_path", lambda self: state)
    monkeypatch.setattr(telegram_line, "recent_messages", lambda offset=0: list(rows))
    sent: list[str] = []
    monkeypatch.setattr(telegram_line, "send_text", lambda text, **kw: sent.append(text) or True)
    asked: list[str] = []

    def _read(text, *, queue=""):
        asked.append(text)
        return None if brain == "down" else brain

    monkeypatch.setattr(phone_intent, "read", _read)
    return state, sent, asked


def _row(update_id, text, *, kind="text", own=False):
    return {"id": str(update_id * 10), "text": text, "created": update_id,
            "own": own, "deleted": False, "kind": kind}


def test_the_grammar_owns_commands_and_hands_prose_to_her():
    from core import phone_line

    # Typed on purpose: matched for free, no model in the path.
    assert phone_line.parse("status") == ("status", {})
    assert phone_line.parse("retry #42") == ("retry", {"task_id": 42})
    assert phone_line.parse("task: fix the journal") == ("task", {"brief": "fix the journal"})
    # Prose is not a command and is not guessed at either; she reads it.
    assert phone_line.parse("enable workouts so they affect health stats in Locket") is None
    assert phone_line.parse("what's the cue right now?") is None
    # Her own replies are still hers.
    assert phone_line.parse("serena: queued as #9") is None


def test_the_first_poll_answers_what_waits_instead_of_eating_it(monkeypatch, tmp_path, queue):
    from core import phone_line

    brief = "research enabling workouts so they affect the health stats in Locket"
    state, sent, asked = _dedicated(
        monkeypatch, tmp_path, [_row(537122438, brief)], brain=("task", brief))

    report = phone_line.poll(now=1000)

    # Adopting his message as the opening watermark is what lost it before.
    assert report.seen == 1
    assert [command["kind"] for command in report.commands] == ["task"]
    assert sent and sent[0].startswith("got it, queued as #")
    assert json.loads(state.read_text(encoding="utf-8"))["inbound_watermark"] == 537122438


def test_a_second_poll_does_not_requeue_the_same_brief(monkeypatch, tmp_path, queue):
    from core import phone_line

    brief = "fix the journal in Locket so entries before august load again"
    state, sent, asked = _dedicated(
        monkeypatch, tmp_path, [_row(11, brief)], brain=("task", brief))
    assert len(phone_line.poll(now=1000).commands) == 1
    assert phone_line.poll(now=1010).commands == []
    assert len(sent) == 1


def test_a_thin_brief_asks_him_rather_than_going_quiet(monkeypatch, tmp_path, queue):
    from core import phone_line

    state, sent, asked = _dedicated(
        monkeypatch, tmp_path, [_row(12, "the locket thing again")],
        brain=("task", "the locket thing again"))
    report = phone_line.poll(now=1000)
    assert [command["kind"] for command in report.commands] == ["task"]
    assert "what exactly should change" in sent[0]


def test_a_photo_is_answered_rather_than_dropped(monkeypatch, tmp_path, queue):
    from core import phone_line

    state, sent, asked = _dedicated(monkeypatch, tmp_path, [_row(13, "", kind="other")])
    report = phone_line.poll(now=1000)
    assert report.seen == 1 and report.commands == []
    assert sent == ["i can only read text. type what you want done."]


def test_her_own_messages_never_come_back_as_work(monkeypatch, tmp_path, queue):
    from core import phone_line

    state, sent, asked = _dedicated(monkeypatch, tmp_path, [_row(14, "got it, queued as #9", own=True)])
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


# ---- she reads prose herself; a regex never guesses again -----------------


def test_a_question_is_answered_not_filed_as_work(monkeypatch, tmp_path, queue):
    """The exact text that broke this: voice-to-text turned "queue" into "cue"."""

    from core import phone_line
    from memory import store

    state, sent, asked = _dedicated(
        monkeypatch, tmp_path, [_row(15, "What's the cue right now?")],
        brain=("say", "#1054 running, nothing else queued."))

    report = phone_line.poll(now=1000)

    assert [command["kind"] for command in report.commands] == ["say"]
    assert sent == ["#1054 running, nothing else queued."]
    assert store.tasks_in_state("ready") == []
    assert store.tasks_in_state("needs_triage") == []


def test_she_sees_the_text_and_the_live_queue(monkeypatch, tmp_path, queue):
    from core import phone_line

    state, sent, asked = _dedicated(
        monkeypatch, tmp_path, [_row(16, "hows it going in there")],
        brain=("say", "still chewing on #1054."))
    phone_line.poll(now=1000)
    assert asked == ["hows it going in there"]


def test_her_brain_being_down_still_queues_plain_work(monkeypatch, tmp_path, queue):
    from core import phone_line

    brief = "fix the journal in Locket so entries before august load again"
    state, sent, asked = _dedicated(monkeypatch, tmp_path, [_row(17, brief)], brain="down")
    report = phone_line.poll(now=1000)
    assert [command["kind"] for command in report.commands] == ["task"]
    assert sent and sent[0].startswith("got it, queued as #")


def test_her_brain_being_down_asks_rather_than_misfiling(monkeypatch, tmp_path, queue):
    from core import phone_line
    from memory import store

    state, sent, asked = _dedicated(
        monkeypatch, tmp_path, [_row(18, "What's the cue right now?")], brain="down")
    report = phone_line.poll(now=1000)
    assert [command["kind"] for command in report.commands] == ["say"]
    assert "task:" in sent[0]
    assert store.tasks_in_state("ready") == []


def test_a_command_never_reaches_her_brain(monkeypatch, tmp_path, queue):
    """`status` and `retry` are free and deterministic; don't pay for a turn."""

    from core import phone_line

    state, sent, asked = _dedicated(
        monkeypatch, tmp_path, [_row(19, "status")], brain=("task", "nope"))
    report = phone_line.poll(now=1000)
    assert [command["kind"] for command in report.commands] == ["status"]
    assert asked == []


def test_she_marks_work_with_a_line_prefix(monkeypatch):
    """The wire contract, without a daemon: prefix means queue, prose means reply."""

    from core import phone_intent

    monkeypatch.setattr(phone_intent, "_endpoint", lambda: ("http://x/turn", "t"))
    calls = {}

    def _post(url, payload, token):
        calls["payload"] = payload
        return {"ok": True, "say": calls["reply"]}

    monkeypatch.setattr(phone_intent, "_post", _post)

    calls["reply"] = "queue: enable workouts in locket so they write to health stats"
    assert phone_intent.read("the workout thing", queue="#1 running") == (
        "task", "enable workouts in locket so they write to health stats")
    assert "#1 running" in calls["payload"]["text"]

    calls["reply"] = "  QUEUE:  fix the journal  "
    assert phone_intent.read("x") == ("task", "fix the journal")

    calls["reply"] = "#1054 running, nothing else."
    assert phone_intent.read("what's the cue") == ("say", "#1054 running, nothing else.")

    # A bare prefix is worse than the words he actually sent.
    calls["reply"] = "queue:"
    assert phone_intent.read("do the locket thing") == ("task", "do the locket thing")

    calls["reply"] = ""
    assert phone_intent.read("anything") is None


def test_triage_is_the_floor_when_her_brain_is_unreachable(monkeypatch):
    from core import phone_intent

    monkeypatch.setattr(phone_intent, "brain_file", lambda: __import__("pathlib").Path("/nope"))
    assert phone_intent.read("enable workouts in locket so they hit health stats") is None

    kind, body = phone_intent.triage("fix the journal in locket so old entries load again")
    assert kind == "task" and body.startswith("fix the journal")
    kind, body = phone_intent.triage("What's the cue right now?")
    assert kind == "say" and "task:" in body


# ---- a status has to carry why, or it is a riddle -------------------------


RUN = "8ba9ee7a-f1fa-416d-b7d0-6d02e893d3bd"


def _blocked_task(brief="fix the routines list in locket so it shows this week"):
    """Walk a task the way the dispatcher does, ending on a failed run."""

    from memory import store

    task = store.enqueue_task(brief)
    claimed = store.claim_next_task("test-dispatcher")
    assert claimed and claimed["id"] == task["id"]
    assert store.mark_task_running(task["id"], "test-dispatcher", claimed["lease_token"], RUN)
    assert store.finish_task_run(
        task["id"], RUN, "blocked",
        "failed: execute: test gate failed after integration; changes were rolled back")
    return task["id"]


def test_status_names_the_failure_not_just_the_state(queue):
    from core import phone_line

    task_id = _blocked_task()
    text = phone_line._status_text()

    assert f"#{task_id} blocked" in text
    assert "test gate failed after integration" in text
    # The word "failed:" is already carried by "blocked"; don't say it twice.
    assert "blocked — failed:" not in text


def test_her_turn_is_grounded_in_the_run_not_a_state_word(queue):
    """"blocked: #1054" is what made her hedge; she gets the run and the brief."""

    from core import phone_line

    task_id = _blocked_task()
    grounding = phone_line._grounding()

    assert f"#{task_id} blocked" in grounding
    assert "fleet 8ba9ee7a" in grounding
    assert "test gate failed after integration" in grounding
    assert "he asked for: fix the routines list in locket" in grounding


def test_the_envelope_forbids_the_menu_and_names_the_grammar():
    from core import phone_intent

    envelope = phone_intent.envelope("hows 1054?", queue="#1054 blocked | fleet 8ba9ee7a")
    # The two failures she actually shipped: hedging, then asking him to pick.
    assert "cannot find a record" in envelope
    assert "offering" in envelope and "menu" in envelope
    # She can only point at a command that exists.
    assert "retry #<id>" in envelope
    assert "#1054 blocked | fleet 8ba9ee7a" in envelope


def test_nothing_queued_still_reads_as_a_sentence(queue):
    from core import phone_line

    assert phone_line._status_text() == "nothing queued"
    assert phone_line._grounding() == "nothing queued"


# ---- the wait is where the latency went ----------------------------------


def test_the_poll_waits_on_telegram_instead_of_sleeping(monkeypatch, tmp_path):
    """His text used to sit until the next 60s tick; now a poll is listening."""

    from core import telegram_line
    from core.serena_scheduler import ACTION_LEASE_SECONDS, MIN_INTERVAL_SECONDS

    _configure(monkeypatch, tmp_path)
    calls = []

    def _call(method, payload=None, *, timeout=None):
        calls.append((method, payload, timeout))
        return []

    monkeypatch.setattr(telegram_line, "_call", _call)
    telegram_line.updates()

    method, payload, timeout = calls[0]
    assert method == "getUpdates"
    assert payload["timeout"] == telegram_line.LONG_POLL_SECONDS
    # The socket has to outlive the server-side wait, or urlopen raises just
    # before Telegram would have answered and every poll looks like a fault.
    assert timeout > telegram_line.LONG_POLL_SECONDS

    # Two invariants the number itself depends on.
    assert telegram_line.LONG_POLL_SECONDS < MIN_INTERVAL_SECONDS
    assert telegram_line.LONG_POLL_SECONDS < ACTION_LEASE_SECONDS


def test_a_caller_can_still_ask_without_waiting(monkeypatch, tmp_path):
    from core import telegram_line

    _configure(monkeypatch, tmp_path)
    calls = []

    def _call(method, payload=None, *, timeout=None):
        calls.append((method, payload, timeout))
        return []

    monkeypatch.setattr(telegram_line, "_call", _call)
    telegram_line.updates(wait_seconds=0)
    assert calls[0][1]["timeout"] == 0
    # A no-wait poll must not inherit a long socket timeout either.
    assert calls[0][2] == telegram_line.TIMEOUT_SECONDS


def test_button_taps_come_through_only_from_his_chat(monkeypatch, tmp_path):
    from core import telegram_line

    _configure(monkeypatch, tmp_path)
    mine = telegram_line.chat_id()
    monkeypatch.setattr(telegram_line, "updates", lambda **kw: [
        {"update_id": 7, "callback_query": {"id": "cb1", "data": "retry:9",
         "from": {"id": int(mine)}, "message": {"chat": {"id": int(mine)}, "message_thread_id": 18669}}},
        {"update_id": 8, "callback_query": {"id": "cb2", "data": "retry:9",
         "from": {"id": 1}, "message": {"chat": {"id": 1}}}},
    ])
    rows = telegram_line.recent_messages()
    assert [(r["kind"], r["data"], r["callback_id"], r["thread_id"]) for r in rows] == [
        ("callback", "retry:9", "cb1", 18669)]


def test_send_text_goes_into_its_topic_and_survives_a_deleted_one(monkeypatch, tmp_path):
    from core import telegram_line

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("SERENA_TELEGRAM_TOPICS", str(tmp_path / "topics.json"))
    (tmp_path / "topics.json").write_text(
        '{"chat_id": "%s", "threads": {"jobs": 11, "health": 12, "journal": 13, "chat": 14}}'
        % telegram_line.chat_id(), encoding="utf-8")
    payloads = []

    def call(method, payload=None, **kw):
        payloads.append((method, dict(payload or {})))
        if method == "sendMessage" and payload.get("message_thread_id") == 12:
            raise telegram_line.TelegramLineError("telegram sendMessage failed: message thread not found")
        return {"message_id": 55}

    monkeypatch.setattr(telegram_line, "_call", call)
    assert telegram_line.send_text("<b>hi</b>", topic="jobs", html=True, silent=True,
                                   buttons=[[{"text": "x", "url": "https://x.y"}]]) == 55
    method, sent = payloads[0]
    assert sent["message_thread_id"] == 11 and sent["parse_mode"] == "HTML"
    assert sent["disable_notification"] is True
    assert sent["reply_markup"] == {"inline_keyboard": [[{"text": "x", "url": "https://x.y"}]]}
    assert telegram_line.send_text("alert", topic="health") == 55
    assert "message_thread_id" not in payloads[-1][1]  # fell back to the plain chat


def test_topics_are_created_once_when_threaded_mode_is_on(monkeypatch, tmp_path):
    from core import telegram_line

    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("SERENA_TELEGRAM_TOPICS", str(tmp_path / "topics.json"))
    created = []

    def call(method, payload=None, **kw):
        if method == "getMe":
            return {"id": 1, "has_topics_enabled": True}
        created.append(payload["name"])
        return {"message_thread_id": 100 + len(created)}

    monkeypatch.setattr(telegram_line, "_call", call)
    assert telegram_line.ensure_topics() == {"jobs": 101, "health": 102, "journal": 103, "chat": 104}
    assert telegram_line.ensure_topics()["chat"] == 104
    assert created == ["🛠 Jobs", "🩺 Health", "📓 Journal", "💬 Chat"]


def test_the_retry_button_reruns_the_job_and_answers_the_tap(monkeypatch, tmp_path):
    from core import phone_line, telegram_line

    retried, toasts = [], []
    monkeypatch.setattr(phone_line, "_retry", lambda task_id: retried.append(task_id) or "retrying #9.")
    monkeypatch.setattr(telegram_line, "answer_callback",
                        lambda cid, text="": toasts.append((cid, text)) or True)
    outcome = phone_line._handle_button({"kind": "callback", "data": "retry:9", "callback_id": "cb1"})
    assert retried == [9] and toasts == [("cb1", "retrying #9.")]
    assert outcome["task_id"] == 9
