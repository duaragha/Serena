"""The commitment store and its local, free inputs.

Two things are being defended here. A commitment must not silently change state
or lose the history of how it got wrong, and ingesting the same local source
twice must not grow a second copy of the same obligation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.commitment_sources import (
    IngestReport,
    LocalIcsAdapter,
    LocalRemindersDbAdapter,
    MemoryTaskAdapter,
    ingest,
    source_report,
)
from core.commitments import (
    PRIORITIES,
    STATES,
    Commitment,
    CommitmentError,
    CommitmentStore,
    Recurrence,
    next_occurrence,
)

NOW = datetime(2026, 8, 8, 9, 0, 0).timestamp()
HOUR = 3_600.0
DAY = 86_400.0


@pytest.fixture
def store(tmp_path):
    return CommitmentStore(tmp_path / "commitments.sqlite3")


# ---- shape ----------------------------------------------------------------


def test_states_and_priorities_are_the_declared_vocabulary():
    assert STATES == ("proposed", "accepted", "active", "completed", "abandoned")
    assert PRIORITIES == ("low", "normal", "high", "critical")


def test_a_commitment_needs_a_title_an_actor_and_a_source(store):
    for kwargs in (
        {"title": "", "actor": "raghav", "source": "voice"},
        {"title": "call mum", "actor": "", "source": "voice"},
        {"title": "call mum", "actor": "raghav", "source": ""},
    ):
        with pytest.raises(CommitmentError):
            store.propose(**kwargs)


def test_a_new_commitment_starts_proposed_and_records_who_said_so(store):
    item = store.propose(
        title="renew the passport",
        actor="serena",
        source="local_ics",
        due_at=NOW + DAY,
        now=NOW,
    )
    assert isinstance(item, Commitment)
    assert item.state == "proposed"
    assert item.owner == "raghav"
    assert item.priority == "normal"

    trail = store.corrections(item.commitment_id)
    assert len(trail) == 1
    assert trail[0].field == "state"
    assert trail[0].new_value == "proposed"
    assert trail[0].actor == "serena"


def test_a_recurring_commitment_without_a_due_date_is_refused(store):
    with pytest.raises(CommitmentError):
        store.propose(
            title="weekly review",
            actor="raghav",
            source="voice",
            recurrence="weekly",
            now=NOW,
        )


# ---- state machine --------------------------------------------------------


def test_only_declared_transitions_are_legal(store):
    item = store.propose(
        title="file the taxes", actor="raghav", source="voice", now=NOW
    )
    accepted = store.accept(item.commitment_id, actor="raghav", now=NOW)
    assert accepted.state == "accepted"

    done, _following = store.complete(
        accepted.commitment_id, actor="raghav", now=NOW + HOUR
    )
    assert done.state == "completed"
    assert done.completed_at == NOW + HOUR

    # Terminal really is terminal.
    with pytest.raises(CommitmentError):
        store.transition(done.commitment_id, "active", actor="raghav")


def test_abandoning_requires_a_reason_and_keeps_it(store):
    item = store.propose(title="sell the car", actor="raghav", source="voice", now=NOW)
    with pytest.raises(CommitmentError):
        store.abandon(item.commitment_id, actor="raghav", reason="  ")
    dropped = store.abandon(
        item.commitment_id, actor="raghav", reason="decided to keep it", now=NOW
    )
    assert dropped.state == "abandoned"
    assert dropped.abandoned_reason == "decided to keep it"


def test_activate_due_promotes_accepted_but_never_proposed(store):
    guessed = store.propose(
        title="dentist", actor="serena", source="local_ics", due_at=NOW + 60, now=NOW
    )
    agreed = store.propose(
        title="standup",
        actor="raghav",
        source="voice",
        due_at=NOW + 60,
        state="accepted",
        now=NOW,
    )
    promoted = store.activate_due(now=NOW)
    promoted_ids = {item.commitment_id for item in promoted}

    assert agreed.commitment_id in promoted_ids
    # Serena's own guess does not become an obligation on a timer.
    assert guessed.commitment_id not in promoted_ids
    assert store.require(guessed.commitment_id).state == "proposed"


# ---- snooze, dismiss, follow-up -------------------------------------------


def test_snooze_hides_it_without_changing_what_is_owed(store):
    item = store.propose(
        title="call the bank",
        actor="raghav",
        source="voice",
        due_at=NOW - HOUR,
        state="accepted",
        now=NOW,
    )
    assert item.is_overdue(NOW)

    snoozed = store.snooze(
        item.commitment_id, actor="raghav", seconds=4 * HOUR, now=NOW
    )
    assert snoozed.state == "accepted"  # still owed
    assert snoozed.is_snoozed(NOW)
    assert not snoozed.is_overdue(NOW)
    assert snoozed.commitment_id not in {
        entry.commitment_id for entry in store.due(now=NOW)
    }
    # And it comes back on its own.
    assert not store.require(item.commitment_id).is_snoozed(NOW + 5 * HOUR)

    with pytest.raises(CommitmentError):
        store.snooze(item.commitment_id, actor="raghav", until=NOW - HOUR, now=NOW)


def test_dismissing_a_repeating_commitment_rolls_to_the_next_one(store):
    item = store.propose(
        title="bin night",
        actor="raghav",
        source="voice",
        due_at=NOW,
        recurrence={"frequency": "weekly"},
        state="accepted",
        now=NOW,
    )
    dismissed, following = store.dismiss(
        item.commitment_id, actor="raghav", reason="already did it", now=NOW
    )
    assert dismissed.dismissed_at == NOW
    assert dismissed.state == "accepted"  # not a lie about completion
    assert following is not None
    assert following.due_at == pytest.approx(NOW + 7 * DAY)
    assert following.follow_up_of == item.commitment_id


def test_completing_a_repeating_commitment_leaves_history_and_schedules_the_next(store):
    item = store.propose(
        title="water the plants",
        actor="raghav",
        source="voice",
        due_at=NOW,
        recurrence={"frequency": "daily", "interval": 3},
        state="accepted",
        now=NOW,
    )
    done, following = store.complete(item.commitment_id, actor="raghav", now=NOW + HOUR)
    assert done.state == "completed"
    assert following is not None
    assert following.commitment_id != done.commitment_id
    assert following.due_at == pytest.approx(NOW + 3 * DAY)
    assert following.recurrence == Recurrence("daily", 3)


def test_a_long_missed_series_resumes_in_the_future_not_as_a_backlog(store):
    item = store.propose(
        title="weekly review",
        actor="raghav",
        source="voice",
        due_at=NOW - 30 * DAY,
        recurrence="weekly",
        state="accepted",
        now=NOW,
    )
    _done, following = store.complete(item.commitment_id, actor="raghav", now=NOW)
    assert following is not None
    assert following.due_at > NOW


def test_follow_up_links_back_to_what_produced_it(store):
    item = store.propose(
        title="call the plumber", actor="raghav", source="voice", now=NOW
    )
    nxt = store.follow_up(
        item.commitment_id,
        title="pay the plumber",
        actor="raghav",
        due_at=NOW + DAY,
        now=NOW,
    )
    assert nxt.follow_up_of == item.commitment_id
    assert nxt.state == "accepted"
    assert nxt.source == "follow_up"


# ---- recurrence maths -----------------------------------------------------


def test_recurrence_rejects_nonsense_and_parses_the_real_vocabulary():
    assert Recurrence.parse(None) is None
    assert Recurrence.parse("none") is None
    assert Recurrence.parse("weekly") == Recurrence("weekly", 1)
    with pytest.raises(CommitmentError):
        Recurrence.parse("hourly")
    with pytest.raises(CommitmentError):
        Recurrence.parse({"frequency": "daily", "interval": 0})


def test_weekdays_recurrence_skips_the_weekend():
    friday = datetime(2026, 8, 7, 9, 0).timestamp()
    following = next_occurrence(friday, Recurrence("weekdays", 1))
    assert datetime.fromtimestamp(following).weekday() == 0  # monday


def test_monthly_recurrence_clamps_to_a_real_day():
    end_of_january = datetime(2026, 1, 31, 9, 0).timestamp()
    following = next_occurrence(end_of_january, Recurrence("monthly", 1))
    landed = datetime.fromtimestamp(following)
    assert (landed.month, landed.day) == (2, 28)


# ---- corrections ----------------------------------------------------------


def test_correcting_a_due_date_keeps_what_serena_thought_before(store):
    item = store.propose(
        title="dentist",
        actor="serena",
        source="local_ics",
        due_at=NOW + DAY,
        now=NOW,
    )
    corrected = store.correct(
        item.commitment_id,
        actor="raghav",
        reason="it moved to friday",
        due_at=NOW + 3 * DAY,
        priority="high",
        now=NOW + HOUR,
    )
    assert corrected.due_at == NOW + 3 * DAY
    assert corrected.priority == "high"

    fields = {entry.field: entry for entry in store.corrections(item.commitment_id)}
    assert fields["due_at"].old_value.startswith("2026-08-09")
    assert fields["due_at"].new_value.startswith("2026-08-11")
    assert fields["due_at"].reason == "it moved to friday"
    assert fields["priority"].old_value == "normal"
    assert fields["priority"].actor == "raghav"


def test_a_correction_that_changes_nothing_writes_nothing(store):
    item = store.propose(
        title="dentist", actor="serena", source="local_ics", now=NOW
    )
    before = len(store.corrections(item.commitment_id))
    store.correct(item.commitment_id, actor="raghav", title="dentist", now=NOW)
    assert len(store.corrections(item.commitment_id)) == before


def test_unknown_fields_cannot_be_corrected(store):
    item = store.propose(title="dentist", actor="serena", source="voice", now=NOW)
    with pytest.raises(CommitmentError):
        store.correct(item.commitment_id, actor="raghav", state="completed")


def test_a_correction_cannot_create_a_recurrence_with_nothing_to_repeat_from(store):
    """`propose` refuses this pair, so `correct` has to refuse it too."""

    item = store.propose(title="water the plants", actor="serena", source="voice", now=NOW)
    assert item.due_at is None

    with pytest.raises(CommitmentError, match="needs a due date"):
        store.correct(
            item.commitment_id, actor="raghav", recurrence="weekly", now=NOW
        )
    assert store.require(item.commitment_id).recurrence is None


def test_a_correction_cannot_pull_the_due_date_out_from_under_a_recurrence(store):
    item = store.propose(
        title="water the plants",
        actor="serena",
        source="voice",
        due_at=NOW + DAY,
        recurrence="weekly",
        now=NOW,
    )

    with pytest.raises(CommitmentError, match="needs a due date"):
        store.correct(item.commitment_id, actor="raghav", due_at=None, now=NOW)

    still = store.require(item.commitment_id)
    assert still.due_at == pytest.approx(NOW + DAY)
    assert still.recurrence is not None


def test_the_pair_may_be_fixed_together_in_one_correction(store):
    item = store.propose(title="water the plants", actor="serena", source="voice", now=NOW)
    fixed = store.correct(
        item.commitment_id,
        actor="raghav",
        due_at=NOW + DAY,
        recurrence="weekly",
        now=NOW,
    )
    assert fixed.recurrence is not None
    assert fixed.due_at == pytest.approx(NOW + DAY)


def test_clearing_both_halves_together_is_allowed(store):
    item = store.propose(
        title="water the plants",
        actor="serena",
        source="voice",
        due_at=NOW + DAY,
        recurrence="weekly",
        now=NOW,
    )
    cleared = store.correct(
        item.commitment_id, actor="raghav", due_at=None, recurrence=None, now=NOW
    )
    assert cleared.recurrence is None
    assert cleared.due_at is None


def test_inspect_returns_the_commitment_and_its_whole_history(store):
    item = store.propose(title="dentist", actor="serena", source="voice", now=NOW)
    store.correct(item.commitment_id, actor="raghav", title="dentist checkup", now=NOW)
    view = store.inspect(item.commitment_id)
    assert view["commitment"]["title"] == "dentist checkup"
    assert {entry["field"] for entry in view["corrections"]} == {"state", "title"}


# ---- queries --------------------------------------------------------------


def test_due_sorts_overdue_first_and_hides_dismissed(store):
    late = store.propose(
        title="late thing",
        actor="raghav",
        source="voice",
        due_at=NOW - DAY,
        state="accepted",
        now=NOW,
    )
    soon = store.propose(
        title="soon thing",
        actor="raghav",
        source="voice",
        due_at=NOW + HOUR,
        state="accepted",
        now=NOW,
    )
    dropped = store.propose(
        title="dropped thing",
        actor="raghav",
        source="voice",
        due_at=NOW + HOUR,
        state="accepted",
        now=NOW,
    )
    store.dismiss(dropped.commitment_id, actor="raghav", now=NOW)

    ids = [item.commitment_id for item in store.due(now=NOW)]
    assert ids[0] == late.commitment_id
    assert soon.commitment_id in ids
    assert dropped.commitment_id not in ids
    assert [item.commitment_id for item in store.overdue(now=NOW)] == [late.commitment_id]


def test_migration_is_additive_on_an_existing_database(tmp_path):
    path = tmp_path / "commitments.sqlite3"
    first = CommitmentStore(path)
    item = first.propose(title="keep me", actor="raghav", source="voice", now=NOW)

    # Re-opening runs initialization again, which must not drop anything.
    second = CommitmentStore(path)
    assert second.require(item.commitment_id).title == "keep me"
    assert len(second.corrections(item.commitment_id)) == 1


# ---- local sources --------------------------------------------------------


def test_memory_task_adapter_reads_tasks_without_touching_the_real_store():
    adapter = MemoryTaskAdapter(
        lister=lambda _type: [
            {"id": 7, "content": "book the flights\nsecond line", "type": "task"},
            {"id": 8, "content": "   ", "type": "task"},
        ]
    )
    assert adapter.status().available
    candidates = adapter.fetch(now=NOW)
    assert len(candidates) == 1
    assert candidates[0].title == "book the flights"
    assert candidates[0].source_ref == "7"
    # His own todo list outranks something scraped from a calendar.
    assert candidates[0].priority == "high"


def test_memory_task_adapter_reports_a_broken_source_instead_of_raising():
    def explode(_type):
        raise RuntimeError("memory dir is gone")

    adapter = MemoryTaskAdapter(lister=explode)
    status = adapter.status()
    assert not status.available
    assert "memory dir is gone" in status.reason
    assert adapter.fetch(now=NOW) == []


def test_unconfigured_calendar_says_so_rather_than_pretending(monkeypatch):
    monkeypatch.delenv("SERENA_CALENDAR_ICS_DIR", raising=False)
    status = LocalIcsAdapter().status()
    assert not status.available
    assert "SERENA_CALENDAR_ICS_DIR" in status.reason


def _write_ics(directory, name, body):
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def test_local_ics_adapter_reads_events_folding_and_recurrence(tmp_path):
    when = datetime.fromtimestamp(NOW + 2 * DAY).strftime("%Y%m%dT%H%M%S")
    _write_ics(
        tmp_path,
        "work.ics",
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:abc-123\r\n"
        "SUMMARY:Quarterly review with\r\n  the whole team\r\n"
        f"DTSTART;TZID=America/Toronto:{when}\r\n"
        "RRULE:FREQ=WEEKLY;INTERVAL=2\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n",
    )
    adapter = LocalIcsAdapter(tmp_path)
    assert adapter.status().available

    candidates = adapter.fetch(now=NOW)
    assert len(candidates) == 1
    found = candidates[0]
    # The folded continuation line is rejoined, not dropped.
    assert found.title == "Quarterly review with the whole team"
    assert found.recurrence == Recurrence("weekly", 2)
    assert found.due_at == pytest.approx(NOW + 2 * DAY)
    assert found.source_ref == "work.ics:abc-123"


def test_local_ics_adapter_ignores_an_rrule_it_cannot_model(tmp_path):
    when = datetime.fromtimestamp(NOW + DAY).strftime("%Y%m%dT%H%M%S")
    _write_ics(
        tmp_path,
        "odd.ics",
        "BEGIN:VEVENT\r\n"
        "UID:odd\r\nSUMMARY:Standup\r\n"
        f"DTSTART:{when}\r\n"
        "RRULE:FREQ=HOURLY;INTERVAL=2\r\n"
        "END:VEVENT\r\n",
    )
    candidate = LocalIcsAdapter(tmp_path).fetch(now=NOW)[0]
    # Better to model it as a one-off than to invent a schedule.
    assert candidate.recurrence is None


def test_local_ics_adapter_skips_stale_one_off_events(tmp_path):
    old = datetime.fromtimestamp(NOW - 60 * DAY).strftime("%Y%m%dT%H%M%S")
    _write_ics(
        tmp_path,
        "old.ics",
        f"BEGIN:VEVENT\r\nUID:old\r\nSUMMARY:Last year\r\nDTSTART:{old}\r\nEND:VEVENT\r\n",
    )
    assert LocalIcsAdapter(tmp_path).fetch(now=NOW) == []


def test_local_ics_adapter_survives_a_junk_file(tmp_path):
    _write_ics(tmp_path, "junk.ics", "this is not a calendar at all\x00\r\n")
    assert LocalIcsAdapter(tmp_path).fetch(now=NOW) == []


def test_all_day_events_land_at_a_sane_hour(tmp_path):
    _write_ics(
        tmp_path,
        "allday.ics",
        "BEGIN:VEVENT\r\nUID:ad\r\nSUMMARY:Birthday\r\n"
        "DTSTART;VALUE=DATE:20260810\r\nEND:VEVENT\r\n",
    )
    candidate = LocalIcsAdapter(tmp_path).fetch(now=NOW)[0]
    assert datetime.fromtimestamp(candidate.due_at).hour == 9


def test_a_tzid_event_is_read_in_its_declared_zone_not_this_machines(tmp_path):
    """09:00 in New York is not 09:00 here, and a briefing spoken as if it were is a missed meeting."""

    _write_ics(
        tmp_path,
        "remote.ics",
        "BEGIN:VEVENT\r\nUID:tz1\r\nSUMMARY:Standup\r\n"
        "DTSTART;TZID=America/New_York:20260810T090000\r\nEND:VEVENT\r\n",
    )
    candidate = LocalIcsAdapter(tmp_path).fetch(now=NOW)[0]

    expected = datetime(2026, 8, 10, 9, 0, tzinfo=ZoneInfo("America/New_York"))
    assert candidate.due_at == pytest.approx(expected.timestamp())

    # And a different declared zone genuinely produces a different instant.
    _write_ics(
        tmp_path,
        "remote2.ics",
        "BEGIN:VEVENT\r\nUID:tz2\r\nSUMMARY:Standup\r\n"
        "DTSTART;TZID=Asia/Kolkata:20260810T090000\r\nEND:VEVENT\r\n",
    )
    both = {item.source_ref: item.due_at for item in LocalIcsAdapter(tmp_path).fetch(now=NOW)}
    assert len(set(both.values())) == 2


def test_an_unknown_or_absent_zone_falls_back_to_local_time(tmp_path):
    naive = datetime(2026, 8, 10, 9, 0)
    _write_ics(
        tmp_path,
        "outlook.ics",
        "BEGIN:VEVENT\r\nUID:tz3\r\nSUMMARY:Review\r\n"
        'DTSTART;TZID="(UTC-05:00) Eastern Time":20260810T090000\r\nEND:VEVENT\r\n',
    )
    candidate = LocalIcsAdapter(tmp_path).fetch(now=NOW)[0]
    assert candidate.due_at == pytest.approx(naive.timestamp())


def test_local_reminders_adapter_is_read_only_and_optional(tmp_path):
    import sqlite3

    missing = LocalRemindersDbAdapter(tmp_path / "nope.sqlite3")
    assert not missing.status().available

    path = tmp_path / "reminders.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE reminders (id INTEGER PRIMARY KEY, message TEXT, "
        "due_at TEXT, fired INTEGER DEFAULT 0)"
    )
    connection.execute(
        "INSERT INTO reminders(message, due_at, fired) VALUES ('take the bins out', "
        "'2026-08-09 18:00:00', 0)"
    )
    connection.execute(
        "INSERT INTO reminders(message, due_at, fired) VALUES ('already done', "
        "'2026-08-01 18:00:00', 1)"
    )
    connection.commit()
    connection.close()

    adapter = LocalRemindersDbAdapter(path)
    assert adapter.status().available
    candidates = adapter.fetch(now=NOW)
    assert [item.title for item in candidates] == ["take the bins out"]
    assert candidates[0].due_at == datetime(2026, 8, 9, 18, 0).timestamp()


# ---- ingestion ------------------------------------------------------------


class _StubSource:
    def __init__(self, name, candidates, available=True, reason="ok"):
        self.name = name
        self._candidates = candidates
        self._available = available
        self._reason = reason
        self.fetches = 0

    def status(self):
        from core.commitment_sources import SourceStatus

        return SourceStatus(self._available, self._reason)

    def fetch(self, *, now):
        self.fetches += 1
        return list(self._candidates)


def test_ingest_is_idempotent_on_source_and_reference(store):
    adapter = MemoryTaskAdapter(
        lister=lambda _t: [{"id": 3, "content": "renew insurance", "type": "task"}]
    )
    first = ingest(store, [adapter], now=NOW)
    assert len(first.created) == 1

    second = ingest(store, [adapter], now=NOW + HOUR)
    assert second.created == []
    assert len(second.unchanged) == 1
    assert len(store.list()) == 1


def test_ingest_corrects_an_upstream_change_instead_of_duplicating(store, tmp_path):
    when = datetime.fromtimestamp(NOW + DAY).strftime("%Y%m%dT%H%M%S")
    path = _write_ics(
        tmp_path,
        "cal.ics",
        f"BEGIN:VEVENT\r\nUID:u1\r\nSUMMARY:Dentist\r\nDTSTART:{when}\r\nEND:VEVENT\r\n",
    )
    adapter = LocalIcsAdapter(tmp_path)
    created = ingest(store, [adapter], now=NOW)
    assert len(created.created) == 1

    moved = datetime.fromtimestamp(NOW + 3 * DAY).strftime("%Y%m%dT%H%M%S")
    path.write_text(
        f"BEGIN:VEVENT\r\nUID:u1\r\nSUMMARY:Dentist\r\nDTSTART:{moved}\r\nEND:VEVENT\r\n",
        encoding="utf-8",
    )
    again = ingest(store, [adapter], now=NOW + HOUR)
    assert len(again.corrected) == 1
    assert len(store.list()) == 1

    only = store.list()[0]
    assert only.due_at == pytest.approx(NOW + 3 * DAY)
    assert any(entry.field == "due_at" for entry in store.corrections(only.commitment_id))


def test_ingest_corrects_every_field_the_source_owns_not_just_the_date(store):
    """A changed priority upstream has to land, or ingestion converges on a lie."""

    tasks = [{"id": 4, "content": "file taxes", "type": "task"}]
    adapter = MemoryTaskAdapter(lister=lambda _t: tasks)
    ingest(store, [adapter], now=NOW)
    item = store.list()[0]
    assert item.priority == "high"

    store.correct(item.commitment_id, actor="raghav", priority="low", now=NOW)
    again = ingest(store, [adapter], now=NOW + HOUR)

    assert again.corrected == [item.commitment_id]
    assert store.require(item.commitment_id).priority == "high"
    assert any(
        entry.field == "priority" for entry in store.corrections(item.commitment_id)
    )
    # And it settles: a third pass has nothing left to say.
    third = ingest(store, [adapter], now=NOW + 2 * HOUR)
    assert third.corrected == []
    assert len(third.unchanged) == 1


def test_ingest_corrects_a_changed_recurrence_and_detail(store, tmp_path):
    when = datetime.fromtimestamp(NOW + DAY).strftime("%Y%m%dT%H%M%S")
    path = _write_ics(
        tmp_path,
        "cal.ics",
        f"BEGIN:VEVENT\r\nUID:u7\r\nSUMMARY:Standup\r\nDTSTART:{when}\r\n"
        "RRULE:FREQ=DAILY\r\nEND:VEVENT\r\n",
    )
    adapter = LocalIcsAdapter(tmp_path)
    ingest(store, [adapter], now=NOW)
    item = store.list()[0]
    assert item.recurrence.frequency == "daily"

    path.write_text(
        f"BEGIN:VEVENT\r\nUID:u7\r\nSUMMARY:Standup\r\nDTSTART:{when}\r\n"
        "RRULE:FREQ=WEEKLY\r\nDESCRIPTION:now once a week\r\nEND:VEVENT\r\n",
        encoding="utf-8",
    )
    again = ingest(store, [adapter], now=NOW + HOUR)

    assert again.corrected == [item.commitment_id]
    updated = store.require(item.commitment_id)
    assert updated.recurrence.frequency == "weekly"
    assert "once a week" in updated.detail


def test_a_calendar_that_never_mentions_recurrence_does_not_strip_one(store, tmp_path):
    """Silence from a source is not an instruction to delete."""

    when = datetime.fromtimestamp(NOW + DAY).strftime("%Y%m%dT%H%M%S")
    _write_ics(
        tmp_path,
        "cal.ics",
        f"BEGIN:VEVENT\r\nUID:u8\r\nSUMMARY:Gym\r\nDTSTART:{when}\r\nEND:VEVENT\r\n",
    )
    adapter = LocalIcsAdapter(tmp_path)
    ingest(store, [adapter], now=NOW)
    item = store.list()[0]
    store.correct(
        item.commitment_id, actor="raghav", recurrence="weekly", now=NOW
    )

    ingest(store, [adapter], now=NOW + HOUR)
    assert store.require(item.commitment_id).recurrence is not None


def test_ingest_never_resurrects_something_he_closed(store):
    adapter = MemoryTaskAdapter(
        lister=lambda _t: [{"id": 9, "content": "cancel the gym", "type": "task"}]
    )
    ingest(store, [adapter], now=NOW)
    item = store.list()[0]
    store.accept(item.commitment_id, actor="raghav", now=NOW)
    store.complete(item.commitment_id, actor="raghav", now=NOW)

    report = ingest(store, [adapter], now=NOW + DAY)
    assert report.created == []
    assert store.require(item.commitment_id).state == "completed"


def test_one_broken_source_does_not_stop_the_others(store):
    class Exploding:
        name = "exploding"

        def status(self):
            raise RuntimeError("disk on fire")

        def fetch(self, *, now):  # pragma: no cover - never reached
            raise AssertionError("must not be fetched")

    working = MemoryTaskAdapter(
        lister=lambda _t: [{"id": 1, "content": "still works", "type": "task"}]
    )
    report = ingest(store, [Exploding(), working], now=NOW)
    assert len(report.created) == 1
    assert "disk on fire" in report.unavailable["exploding"]


def test_unavailable_sources_are_named_not_hidden(store):
    stub = _StubSource("local_ics", [], available=False, reason="no directory set")
    report = ingest(store, [stub], now=NOW)
    assert isinstance(report, IngestReport)
    assert report.total == 0
    assert report.unavailable == {"local_ics": "no directory set"}
    assert stub.fetches == 0


def test_source_report_lists_every_adapter_with_its_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("SERENA_CALENDAR_ICS_DIR", str(tmp_path))
    monkeypatch.delenv("SERENA_REMINDERS_DB_PATH", raising=False)
    report = source_report(
        [
            MemoryTaskAdapter(lister=lambda _t: []),
            LocalIcsAdapter(),
            LocalRemindersDbAdapter(),
        ]
    )
    assert report["memory_task"]["available"] is True
    assert report["local_ics"]["available"] is True
    assert report["local_reminders"]["available"] is False
    assert "SERENA_REMINDERS_DB_PATH" in report["local_reminders"]["reason"]


def test_ingested_commitments_arrive_proposed_not_accepted(store, tmp_path):
    when = datetime.fromtimestamp(NOW + DAY).strftime("%Y%m%dT%H%M%S")
    _write_ics(
        tmp_path,
        "cal.ics",
        f"BEGIN:VEVENT\r\nUID:u2\r\nSUMMARY:Lunch\r\nDTSTART:{when}\r\nEND:VEVENT\r\n",
    )
    ingest(store, [LocalIcsAdapter(tmp_path)], now=NOW)
    # Reading his calendar is not the same as him handing Serena the job.
    assert {item.state for item in store.list()} == {"proposed"}


def test_subject_entity_id_carries_a_state_graph_reference(store):
    item = store.propose(
        title="replace the laptop battery",
        actor="raghav",
        source="voice",
        subject_entity_id="device:laptop-01",
        now=NOW,
    )
    assert item.subject_entity_id == "device:laptop-01"
    corrected = store.correct(
        item.commitment_id,
        actor="raghav",
        subject_entity_id="device:laptop-02",
        now=NOW,
    )
    assert corrected.subject_entity_id == "device:laptop-02"


def test_recurrence_survives_a_round_trip_through_sqlite(store):
    item = store.propose(
        title="pay rent",
        actor="raghav",
        source="voice",
        due_at=NOW,
        recurrence={"frequency": "monthly", "interval": 1},
        now=NOW,
    )
    reloaded = store.require(item.commitment_id)
    assert reloaded.recurrence == Recurrence("monthly", 1)
    assert reloaded.to_dict()["recurrence"] == {"frequency": "monthly", "interval": 1}


def test_a_far_future_event_is_left_out_of_the_horizon(tmp_path):
    far = (datetime.fromtimestamp(NOW) + timedelta(days=200)).strftime("%Y%m%dT%H%M%S")
    _write_ics(
        tmp_path,
        "far.ics",
        f"BEGIN:VEVENT\r\nUID:far\r\nSUMMARY:Someday\r\nDTSTART:{far}\r\nEND:VEVENT\r\n",
    )
    assert LocalIcsAdapter(tmp_path).fetch(now=NOW) == []
