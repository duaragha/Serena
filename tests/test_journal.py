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
        async def invents(_prompt):
            return "You had dinner with Saad and Hasba at Farooj."

        monkeypatch.setattr(draft, "_ask", invents)
        text = draft.summary(self.FACTS)
        assert "Hasba" not in text and "Farooj" not in text
        assert "Saad" in text

    def test_summarising_does_not_strip_the_evidence_from_the_callers_facts(self, monkeypatch):
        async def plain(_prompt):
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

        def write_entry(*, day, title, html, entry_id):
            entries.append({"day": day, "html": html, "entry_id": entry_id})
            return entry_id or 77

        async def plain(_prompt):
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

    def test_an_answer_for_a_day_with_no_draft_is_refused(self, written):
        with pytest.raises(nightly.JournalError):
            nightly.record_answer("2026-01-01", "anything")

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

    def test_it_sends_today_after_half_past_nine(self, clock):
        calls, at, _ = clock
        store.save_day("2026-09-20", sent_at=1.0)
        at(21, 35)
        assert [d.isoformat() for d in calls["nightly"]] == ["2026-09-21"]

    def test_it_does_not_send_twice(self, clock):
        calls, at, _ = clock
        store.save_day("2026-09-21", sent_at=1.0)
        at(21, 50)
        assert calls["nightly"] == []

    def test_a_missed_day_is_texted_in_the_morning_without_a_call(self, clock):
        calls, at, _ = clock
        at(9, 0)
        assert [(d.isoformat(), call) for d, call in calls["send"]] == [("2026-09-20", False)]

    def test_nothing_goes_out_in_quiet_hours(self, clock):
        calls, at, policy = clock
        policy.quiet = True
        at(23, 0)
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
        return {"success": True, "data": {"id": 5}}

    monkeypatch.setattr(locket, "_request", fake)
    locket.day_facts("2026-09-20")
    locket.write_entry(day="2026-09-20", title="t", html="h", entry_id=None)
    locket.write_entry(day="2026-09-20", title="t", html="h", entry_id=5)
    assert all(path.split("?")[0].endswith("/") for path in seen), seen
