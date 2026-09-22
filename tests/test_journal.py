"""His journal: facts only, his words kept as his, and nothing invented."""

from __future__ import annotations

from datetime import datetime

import pytest

from core.journal import draft, nightly, store
from core.journal.people import Evidence, Person, verify
from core.journal.unified_source import ChatMessage


@pytest.fixture(autouse=True)
def journal_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_JOURNAL_DB", str(tmp_path / "journal.sqlite3"))


def _msg(chat, text, author="x"):
    return ChatMessage(id=text, chat=chat, chat_id=chat, network="wa", author=author,
                       outgoing=False, text=text, at=datetime(2026, 9, 20, 13, 0).astimezone())


class TestPeopleNeedRealEvidence:
    def test_a_quote_that_is_really_in_the_chat_keeps_the_person(self):
        people = [Person("Saad", "high", "2:30pm", [Evidence("skype", "Sun 13:51", "Where we linking")])]
        kept, dropped = verify(people, [_msg("skype", "Where we linking")])
        assert [p.name for p in kept] == ["Saad"] and dropped == []

    def test_an_invented_quote_drops_the_person(self):
        """A plausible quote the model made up is the failure this exists for."""

        people = [Person("Zayn", "high", "", [Evidence("skype", "Sun 14:00", "omw bro see u at 3")])]
        kept, dropped = verify(people, [_msg("skype", "Where we linking")])
        assert kept == [] and dropped == ["Zayn"]

    def test_a_real_quote_from_the_wrong_chat_does_not_count(self):
        people = [Person("Saad", "high", "", [Evidence("Friday", "", "Where we linking")])]
        kept, _ = verify(people, [_msg("skype", "Where we linking")])
        assert kept == []

    def test_curly_quotes_and_spacing_do_not_fail_a_real_quote(self):
        people = [Person("Rana", "high", "", [Evidence("Friday", "", "I'm just in the  parking lot")])]
        kept, _ = verify(people, [_msg("Friday", "I’m just in the parking lot near your house")])
        assert [p.name for p in kept] == ["Rana"]


class TestTheSummaryCannotInvent:
    FACTS = {"day": "2026-09-20", "people": [{"name": "Saad", "confidence": "high"},
                                             {"name": "Sohaib", "confidence": "high"}]}

    def test_names_from_the_facts_pass(self):
        assert draft.faithful("You spent the afternoon with Saad and Sohaib.", self.FACTS)

    def test_a_person_who_is_not_in_the_facts_fails(self):
        assert not draft.faithful("You spent the afternoon with Saad and Hasba.", self.FACTS)

    def test_a_place_from_a_chat_quote_is_not_a_fact(self):
        """'Farooj' sat in a quoted plan and was never a place."""

        facts = {**self.FACTS, "people": [{**p, "evidence": [{"quote": "Farooj at 2?"}]}
                                          for p in self.FACTS["people"]]}
        trimmed = draft._prompt_facts(facts)
        assert not draft.faithful("You went to Farooj with Saad.", trimmed)

    def test_an_unfaithful_model_reply_falls_back_to_the_template(self, monkeypatch):
        def invents(_prompt):
            return "You had dinner with Saad and Hasba at Farooj."

        monkeypatch.setattr(draft, "_ask", invents)
        text = draft.summary(self.FACTS)
        assert "Hasba" not in text and "Farooj" not in text
        assert "Saad" in text

    def test_summarising_does_not_strip_the_evidence_from_the_callers_facts(self, monkeypatch):
        def plain(_prompt):
            return "You saw Saad."

        monkeypatch.setattr(draft, "_ask", plain)
        facts = {"day": "2026-09-20", "people": [{"name": "Saad", "evidence": [{"quote": "q"}]}]}
        draft.summary(facts)
        assert facts["people"][0]["evidence"] == [{"quote": "q"}]


class TestQuestions:
    def test_only_he_can_say_where_when_there_is_no_location(self):
        qs = draft.questions({"people": [{"name": "Saad"}, {"name": "Sohaib"}]})
        assert qs == [{"id": "where-with", "kind": "where-with",
                       "text": "where did you go with Saad and Sohaib?"}]

    def test_a_long_unnamed_visit_is_asked_about_and_a_short_one_is_not(self):
        visit = {"place": "", "arrived": "2:10pm", "departed": "5:40pm", "arrived_ts": 1.0,
                 "lat": 43.6, "lng": -79.6}
        facts = {"visits": [{**visit, "minutes": 210}, {**visit, "arrived_ts": 2.0, "minutes": 25}]}
        assert [q["id"] for q in draft.questions(facts)] == ["where-1"]

    def test_never_more_than_three(self):
        facts = {"people_questions": [f"who is x{i}?" for i in range(5)]}
        assert len(draft.questions(facts)) == draft.MAX_QUESTIONS


class TestTheEntry:
    def test_his_words_are_escaped_not_rendered(self):
        html = draft.render_html("2026-09-20", {}, "x",
                                 [{"question_id": "", "answer": "<script>alert(1)</script>"}], [])
        assert "<script>" not in html and "&lt;script&gt;" in html

    def test_a_source_that_could_not_be_read_is_said_out_loud(self):
        html = draft.render_html("2026-09-20", {"unavailable": {"locket": "HTTP 500"}}, "x", [], [])
        assert "Couldn’t read: locket" in html

    def test_a_maybe_is_marked_as_one(self):
        html = draft.render_html("2026-09-20", {"people": [{"name": "Rachael", "confidence": "medium"}]},
                                 "x", [], [])
        assert "Rachael (probably)" in html


class TestPlacesAreLearned:
    def test_a_named_place_resolves_the_next_visit_nearby(self):
        store.name_place("MOTW Cafe", 43.59, -79.64)
        assert store.place_for(43.5901, -79.6401) == "MOTW Cafe"   # ~14 m away
        assert store.place_for(43.60, -79.64) == ""                  # ~1.1 km away

    def test_renaming_a_spot_replaces_it(self):
        store.name_place("MOTW", 43.59, -79.64)
        store.name_place("MOTW Cafe", 43.5900, -79.6400)
        assert store.place_for(43.59, -79.64) == "MOTW Cafe"


class TestHisAnswers:
    @pytest.fixture
    def written(self, monkeypatch):
        entries = []

        def write_entry(*, day, title, html, entry_id, base=None, written=None):
            entries.append({"day": day, "html": html, "entry_id": entry_id})
            return {"id": entry_id or 77, "base": "", "written": html, "skipped": False}

        def plain(_prompt):
            return "You saw Saad."

        monkeypatch.setattr(nightly.locket, "write_entry", write_entry)
        monkeypatch.setattr(draft, "_ask", plain)
        return entries

    def _seed(self):
        store.save_day("2026-09-20", facts={"day": "2026-09-20", "people": [{"name": "Saad", "confidence": "medium"}],
                                            "visits": [{"place": "", "arrived": "2:10pm", "departed": "5:40pm",
                                                        "arrived_ts": 100, "lat": 43.59, "lng": -79.64,
                                                        "minutes": 210}]},
                       questions=[{"id": "where-100", "kind": "where", "text": "where were you 2:10pm–5:40pm?",
                                   "lat": 43.59, "lng": -79.64}],
                       entry_id=77, sent_at=1.0)

    def test_his_answer_is_kept_verbatim_and_names_the_place(self, written):
        self._seed()
        result = nightly.record_answer("2026-09-20", "MOTW cafe with saad", question_id="where-100",
                                       place_name="MOTW Cafe", people=["saad"])
        assert result["still_open"] == []
        day = store.load_day("2026-09-20")
        assert day["answers"][0]["answer"] == "MOTW cafe with saad"
        assert day["facts"]["visits"][0]["place"] == "MOTW Cafe"
        assert day["facts"]["people"][0]["confidence"] == "high", "he said so"
        assert store.place_for(43.59, -79.64) == "MOTW Cafe"
        assert "MOTW cafe with saad" in written[-1]["html"]
        assert written[-1]["entry_id"] == 77, "updates her entry instead of adding a second"

    def test_an_answer_to_a_question_that_does_not_exist_is_refused(self, written):
        self._seed()
        with pytest.raises(nightly.JournalError):
            nightly.record_answer("2026-09-20", "x", question_id="where-999")
        assert written == []

    def test_open_days_are_the_ones_still_waiting_on_him(self, written):
        self._seed()
        assert [d["day"] for d in store.open_days()] == ["2026-09-20"]
        nightly.record_answer("2026-09-20", "home", question_id="where-100", place_name="Home")
        assert store.open_days() == []


class TestWhenItRuns:
    """Once a day, before quiet hours, and a missed day is caught up in the morning."""

    @pytest.fixture
    def clock(self, monkeypatch):
        from core import scheduler_actions

        calls = {"nightly": [], "build": [], "send": []}
        monkeypatch.setattr(nightly, "run_nightly", lambda day: calls["nightly"].append(day) or {"ok": 1})
        monkeypatch.setattr(nightly, "build", lambda day: calls["build"].append(day))
        monkeypatch.setattr(nightly, "send", lambda day, call=True: calls["send"].append((day, call)) or {})

        class Policy:
            quiet = False

            def in_quiet_hours(self, _now):
                return self.quiet

        policy = Policy()

        class Authority:
            pass

        Authority.policy = policy
        monkeypatch.setattr("core.notification_senders.default_authority", lambda: Authority)

        def at(hour, minute=0):
            real = datetime

            class Frozen(real):
                @classmethod
                def now(cls, tz=None):
                    return real(2026, 9, 21, hour, minute, tzinfo=tz)

            monkeypatch.setattr("datetime.datetime", Frozen)
            return scheduler_actions.journal_nightly({})

        return calls, at, policy

    def test_it_sends_today_at_quarter_to_midnight_even_in_quiet_hours(self, clock):
        """He picked 11:45pm; quiet hours start at 10 and must not hold it."""

        calls, at, policy = clock
        policy.quiet = True
        at(23, 47)
        assert [d.isoformat() for d in calls["nightly"]] == ["2026-09-21"]

    def test_not_before_quarter_to_midnight(self, clock):
        calls, at, _ = clock
        store.save_day("2026-09-20", sent_at=1.0)
        at(23, 40)
        assert calls["nightly"] == []

    def test_a_tick_just_after_midnight_still_sends_the_day_that_ended(self, clock):
        calls, at, policy = clock
        policy.quiet = True
        at(0, 10)
        assert [d.isoformat() for d in calls["nightly"]] == ["2026-09-20"]

    def test_it_does_not_send_twice(self, clock):
        calls, at, _ = clock
        store.save_day("2026-09-21", sent_at=1.0)
        at(23, 55)
        assert calls["nightly"] == []

    def test_a_missed_day_is_texted_in_the_morning_without_a_call(self, clock):
        calls, at, _ = clock
        at(9, 0)
        assert [(d.isoformat(), call) for d, call in calls["send"]] == [("2026-09-20", False)]

    def test_yesterday_is_refreshed_once_in_the_morning_without_a_text(self, clock):
        """The 11:45pm draft predates his getting home; the morning fills it in."""

        calls, at, _ = clock
        store.save_day("2026-09-20", sent_at=1.0)
        at(10, 5)
        assert [d.isoformat() for d in calls["build"]] == ["2026-09-20"]
        assert calls["send"] == []
        at(10, 10)
        assert len(calls["build"]) == 1, "only once"

    def test_the_morning_catch_up_waits_for_quiet_hours_to_end(self, clock):
        calls, at, policy = clock
        policy.quiet = True
        at(7, 0)
        assert calls == {"nightly": [], "build": [], "send": []}


def test_the_brain_mounts_and_permits_the_journal_tools():
    """Under dontAsk a tool missing from allowed_tools is denied silently."""

    from core.brain_daemon import _build_agent_options
    from core.brain_journal_tools import JOURNAL_TOOL_NAMES

    captured = {}

    def options(**kwargs):
        captured.update(kwargs)
        return type("O", (), kwargs)

    _build_agent_options(options, object(), [], journal_tools=object(),
                         journal_tool_names=JOURNAL_TOOL_NAMES)
    assert "serena-journal" in captured["mcp_servers"]
    for name in JOURNAL_TOOL_NAMES:
        assert name in captured["allowed_tools"]


def test_the_commit_scan_uses_the_projects_tree_not_the_runtime_parent(tmp_path, monkeypatch):
    """On the PC the runtime checkout's parent is the whole home directory."""

    from core.journal import facts

    (tmp_path / "Projects" / "serena").mkdir(parents=True)
    (tmp_path / "serena-runtime").mkdir()
    monkeypatch.delenv("SERENA_PROJECTS_ROOT", raising=False)
    monkeypatch.setattr(facts.Path, "home", classmethod(lambda cls: tmp_path))
    assert facts._projects_root() == tmp_path / "Projects"


def test_a_date_in_the_summary_is_not_an_invention():
    facts = {"day": "2026-09-20", "people": [{"name": "Saad"}]}
    assert draft.faithful("On September 20th you saw Saad.", facts)


def test_windows_gets_a_zone_database_and_current_tls_roots():
    """Windows has no zone database; the dependency must be declared for it."""

    from pathlib import Path

    import tomllib

    deps = tomllib.loads(Path("pyproject.toml").read_text())["project"]["dependencies"]
    assert any(d.startswith("tzdata") and "win32" in d for d in deps)
    assert any(d.startswith("certifi") for d in deps)


def test_every_locket_path_carries_its_trailing_slash(monkeypatch):
    """Locket 308s bare paths, and urllib will not follow that for a POST."""

    from core.journal import locket

    seen = []

    def fake(method, path, body=None):
        seen.append(path)
        return {"success": True, "data": [] if method == "GET" and "dateFrom" in path else {"id": 5}}

    monkeypatch.setattr(locket, "_request", fake)
    locket.day_facts("2026-09-20")
    locket.write_entry(day="2026-09-20", title="t", html="h", entry_id=None)
    assert all(path.split("?")[0].endswith("/") for path in seen), seen


class TestOneEntryPerDay:
    """Locket already makes the day's entry; she writes into it, never beside it."""

    @pytest.fixture
    def locket_api(self, monkeypatch):
        from core.journal import locket

        state = {"entries": [], "calls": [], "stored": {}}

        def fake(method, path, body=None):
            state["calls"].append((method, path, body))
            if method == "GET" and "dateFrom" in path:
                return {"success": True, "data": state["entries"]}
            if method == "GET":
                entry_id = int(path.rstrip("/").split("/")[-1])
                return {"success": True, "data": {"content": state["stored"].get(entry_id, "")}}
            if method == "POST":
                state["stored"][99] = body["content"]
                return {"success": True, "data": {"id": 99}}
            state["stored"][int(path.rstrip("/").split("/")[-1])] = body["content"]
            return {"success": True}

        monkeypatch.setattr(locket, "_request", fake)
        return locket, state

    def _patches(self, state):
        return [c for c in state["calls"] if c[0] == "PATCH"]

    def test_the_auto_logged_entry_is_used_instead_of_adding_a_second(self, locket_api):
        locket, state = locket_api
        state["entries"] = [{"id": 2187, "entryDate": "2026-09-20", "title": "Auto-logged",
                             "content": "", "tags": []}]
        out = locket.write_entry(day="2026-09-20", title="Sunday, September 20",
                                 html="<p>You saw Saad.</p>", entry_id=None)
        assert out["id"] == 2187 and not out["skipped"]
        assert "POST" not in [m for m, *_ in state["calls"]]
        _, path, body = self._patches(state)[-1]
        assert path == "/api/v1/journal/2187/"
        assert body["content"] == "<p>You saw Saad.</p>"
        assert body["title"] == "Sunday, September 20", "a placeholder title is replaced"
        assert out["written"] == "<p>You saw Saad.</p>"

    def test_there_is_no_byline_on_his_journal(self):
        html = draft.render_html("2026-09-20", {"people": [{"name": "Saad"}]}, "You saw Saad.", [], [])
        assert "Serena" not in html and "Drafted" not in html

    def test_what_he_wrote_before_her_stays_first(self, locket_api):
        locket, state = locket_api
        state["entries"] = [{"id": 7, "entryDate": "2026-09-20", "title": "good day",
                             "content": "<p>i wrote this</p>", "tags": [{"name": "friends"}]}]
        out = locket.write_entry(day="2026-09-20", title="Sunday", html="<p>draft</p>", entry_id=7)
        body = self._patches(state)[-1][2]
        assert body["content"] == "<p>i wrote this</p><p>draft</p>"
        assert "title" not in body
        assert body["tagNames"] == ["friends", "serena"]
        assert out["base"] == "<p>i wrote this</p>"

    def test_her_later_drafts_replace_only_her_own_part(self, locket_api):
        locket, state = locket_api
        state["stored"][7] = "<p>mine</p><p>draft 1</p>"
        state["entries"] = [{"id": 7, "entryDate": "2026-09-20", "title": "x",
                             "content": "<p>mine</p><p>draft 1</p>", "tags": [{"name": "serena"}]}]
        out = locket.write_entry(day="2026-09-20", title="t", html="<p>draft 2</p>", entry_id=7,
                                 base="<p>mine</p>", written="<p>mine</p><p>draft 1</p>")
        assert self._patches(state)[-1][2]["content"] == "<p>mine</p><p>draft 2</p>"
        assert out["written"] == "<p>mine</p><p>draft 2</p>"

    def test_once_he_has_edited_it_she_leaves_it_alone(self, locket_api):
        """No visible boundary means an edit of his could be anywhere; never overwrite it."""

        locket, state = locket_api
        state["entries"] = [{"id": 7, "entryDate": "2026-09-20", "title": "x",
                             "content": "<p>draft 1, but he fixed a name</p>", "tags": []}]
        out = locket.write_entry(day="2026-09-20", title="t", html="<p>draft 2</p>", entry_id=7,
                                 base="", written="<p>draft 1</p>")
        assert out["skipped"] is True
        assert self._patches(state) == []

    def test_an_entry_from_the_first_version_loses_its_byline(self, locket_api):
        locket, state = locket_api
        state["entries"] = [{"id": 2187, "entryDate": "2026-09-20", "title": "Sunday",
                             "content": "<p><em>Drafted by Serena from your chats.</em></p><p>old</p>",
                             "tags": [{"name": "serena"}]}]
        locket.write_entry(day="2026-09-20", title="t", html="<p>new</p>", entry_id=2187)
        assert self._patches(state)[-1][2]["content"] == "<p>new</p>"

    def test_a_day_with_no_entry_gets_one(self, locket_api):
        locket, state = locket_api
        out = locket.write_entry(day="2026-09-20", title="t", html="<p>h</p>", entry_id=None)
        assert out["id"] == 99 and out["written"] == "<p>h</p>"


def test_nothing_already_auto_logged_is_written_again():
    """Drives, workouts and shows are in Locket's Auto-logged panel already."""

    facts = {"day": "2026-09-20", "people": [{"name": "Saad"}],
             "drives": [{"start": "1:59pm", "km": 36.7, "minutes": 78, "from": "", "to": ""}],
             "workouts": [{"name": "Pull — Hypertrophy", "start": "10:45pm", "sets": 15}],
             "activity": [{"kind": "tv", "summary": "Malcolm in the Middle"}]}
    html = draft.render_html("2026-09-20", facts, draft.template_summary(facts), [], [])
    for repeated in ("Pull", "Malcolm", "36.7", "drive"):
        assert repeated not in html
    assert set(draft._prompt_facts(facts)) <= {"day", "people", "visits", "commits"}


def test_the_timeline_only_shows_places_with_names():
    facts = {"visits": [{"place": "", "arrived": "2:10pm", "departed": "5:40pm"},
                        {"place": "MOTW Cafe", "arrived": "6:00pm", "departed": "8:00pm"}]}
    assert [line for _, line in draft._timeline(facts)] == ["6:00pm–8:00pm · MOTW Cafe"]


def test_the_codex_fallback_sends_the_prompt_on_stdin(monkeypatch):
    """A day of chats is far past Windows' 32K command-line limit."""

    import subprocess

    from core.journal import model

    seen = {}

    class Done:
        returncode = 0
        stderr = ""
        stdout = '{"item": {"type": "agent_message", "text": "ok"}}'

    def run(args, **kwargs):
        seen["args"], seen["input"] = args, kwargs.get("input")
        return Done()

    monkeypatch.setattr(model.shutil, "which", lambda name: "codex")
    monkeypatch.setattr(subprocess, "run", run)
    huge = "x" * 100_000
    assert model._codex(huge, "sys") == "ok"
    assert all(len(a) < 1000 for a in seen["args"])
    assert huge in seen["input"]


def test_he_can_add_to_a_day_before_she_has_drafted_it(monkeypatch):
    """'put in today's journal that...' at 2pm, hours before the nightly draft."""

    writes = []
    monkeypatch.setattr(nightly.locket, "write_entry",
                        lambda **kw: writes.append(kw) or {"id": 5, "base": "", "written": kw["html"],
                                                           "skipped": False})
    monkeypatch.setattr(draft, "_ask", lambda _p: "")
    result = nightly.record_answer("2026-09-21", "got the dogs today")
    assert result["saved"] and result["entry_id"] == 5
    assert "got the dogs today" in writes[-1]["html"]
    assert store.load_day("2026-09-21")["answers"][0]["answer"] == "got the dogs today"


def test_an_answer_to_a_question_still_needs_the_draft_that_asked_it():
    with pytest.raises(nightly.JournalError):
        nightly.record_answer("2026-09-21", "x", question_id="where-1")


class TestFromAnyMachine:
    """The laptop's orb reaches the one journal, on the PC."""

    def test_the_pc_runs_it_locally_and_everything_else_proxies(self, monkeypatch, tmp_path):
        from core.journal import remote

        monkeypatch.setenv("SERENA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(remote.sys, "platform", "win32")
        assert remote.home() == "local"
        monkeypatch.setattr(remote.sys, "platform", "linux")
        assert remote.home() == "pc"
        (tmp_path / "journal.json").write_text('{"home": "local"}')
        assert remote.home() == "local"

    def test_off_the_pc_the_operation_runs_there_over_ssh(self, monkeypatch, tmp_path):
        import subprocess

        from core.journal import remote

        monkeypatch.setenv("SERENA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(remote.sys, "platform", "linux")
        seen = {}

        class Done:
            returncode = 0
            stderr = ""
            stdout = 'noise\nJOURNAL-RESULT {"saved": true, "day": "2026-09-21"}\n'

        def run(args, **kwargs):
            seen["args"], seen["input"] = args, kwargs["input"]
            return Done()

        monkeypatch.setattr(subprocess, "run", run)
        result = remote.call("answer", {"day": "2026-09-21", "answer": 'got "the" dogs'})
        assert result == {"saved": True, "day": "2026-09-21"}
        assert seen["args"][:4] == ["ssh", "-o", "BatchMode=yes", "-o"] and seen["args"][-1] == "-"
        assert "got" in seen["input"], "the request rides in the script, not on argv"
        compile(seen["input"], "<runner>", "exec")

    def test_an_unreachable_pc_is_an_error_not_a_silent_save(self, monkeypatch, tmp_path):
        import subprocess

        from core.journal import remote

        monkeypatch.setenv("SERENA_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(remote.sys, "platform", "linux")

        class Done:
            returncode = 255
            stderr = "ssh: connect to host pc port 22: Connection timed out"
            stdout = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
        assert "error" in remote.call("answer", {"answer": "x"})



def test_agent_commits_are_not_his():
    """They go out under his git name; 'you made 16 commits' was all agents."""

    from core.journal.facts import _is_agents

    assert _is_agents("fix(journal): x", "Claude Opus 5 (1M context) <noreply@anthropic.com>")
    assert _is_agents("serena: In Locket, add a scanner", "")
    assert _is_agents("feat: y", "Codex <codex@openai.com>")
    assert not _is_agents("fix typo in readme", "")


def test_visit_spans_read_the_way_his_day_does():
    assert draft.span({"arrived": "2:10pm", "departed": "5:40pm"}) == "2:10pm–5:40pm"
    assert draft.span({"arrived": "10:19pm", "departed": "~12:27am"}) == "10:19pm–~12:27am"
    assert draft.span({"arrived": "12:27am", "departed": "7:20am", "ends_after": True}) == "from 12:27am"
    assert draft.span({"arrived": "11pm", "departed": "7:20am", "started_before": True}) == "until 7:20am"


def test_home_and_work_are_never_asked_about():
    visit = {"arrived": "9:17am", "departed": "5:09pm", "arrived_ts": 1.0, "minutes": 470,
             "lat": 43.718, "lng": -79.469}
    facts = {"visits": [{**visit, "place": "work"}, {**visit, "arrived_ts": 2.0, "place": "home"}]}
    assert draft.questions(facts) == []


def test_the_end_of_the_night_sorts_last():
    facts = {"visits": [{"place": "home", "arrived": "12:27am", "departed": "7:20am", "ends_after": True},
                        {"place": "work", "arrived": "9:17am", "departed": "5:09pm"}]}
    assert [line for _, line in draft._timeline(facts)] == ["9:17am–5:09pm · work", "from 12:27am · home"]


def test_a_drive_that_ends_where_he_parks_still_names_the_place(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_JOURNAL_DB", str(tmp_path / "journal.sqlite3"))
    store.name_place("work", 43.718, -79.4691)
    assert store.place_for(43.722591, -79.463628) == ""                    # ~630 m: a visit here is elsewhere
    assert store.place_for(43.722591, -79.463628, radius_m=1000) == "work"  # a drive ending here is work
