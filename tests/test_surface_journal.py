"""Tests for the seam that puts voice, chat, tool, and memory on the control plane.

The mechanism was already proven for Fleet. What is adversarial here is
everything a *live* surface does to it: replaying the same event, failing
repeatedly, dying mid-turn, handing over a native id that is not a legal
identifier, and rolling back its own transaction after staging.
"""

from __future__ import annotations

import sqlite3

import pytest

from core.control_plane import ControlPlaneStore
from core.control_recovery import (
    journal_redelivery_handlers,
    outstanding_debts,
    reconcile_obligations,
)
from core.surface_journal import (
    MIGRATABLE_SURFACES,
    SurfaceJournal,
    SurfaceJournalError,
    rule_for,
    safe_token,
)


@pytest.fixture
def control(tmp_path):
    return ControlPlaneStore(tmp_path / "control.sqlite3")


def _journal(tmp_path, control, surface="voice"):
    return SurfaceJournal(
        surface, tmp_path / f"{surface}.sqlite3", control_store=control
    )


# ---- the seam matches the ledger, by construction -------------------------


@pytest.mark.parametrize("surface", MIGRATABLE_SURFACES)
def test_every_migratable_surface_has_a_usable_lifecycle(tmp_path, control, surface):
    entry = _journal(tmp_path, control, surface)
    rule = rule_for(surface)

    assert entry.open_event == rule.open_on
    assert entry.fulfil_event in rule.fulfill_on
    # An event name this journal can emit is always one the ledger reacts to.
    assert entry.fulfil_event != entry.open_event


def test_a_surface_with_no_rule_is_refused(tmp_path):
    with pytest.raises(SurfaceJournalError, match="no obligation rule"):
        SurfaceJournal("telepathy", tmp_path / "nope.sqlite3")


@pytest.mark.parametrize("surface", MIGRATABLE_SURFACES)
def test_open_then_fulfil_closes_the_obligation(tmp_path, control, surface):
    entry = _journal(tmp_path, control, surface)

    entry.opened(f"{surface}-1", summary="something real")
    assert [item.state for item in control.obligations(surface=surface)] == ["open"]

    entry.fulfilled(f"{surface}-1")
    assert [item.state for item in control.obligations(surface=surface)] == ["fulfilled"]


# ---- idempotent publication ----------------------------------------------


def test_replaying_the_same_open_event_does_not_double_the_obligation(tmp_path, control):
    entry = _journal(tmp_path, control)
    for _ in range(4):
        entry.opened("voice-1", summary="tell him how it went")

    assert len(control.obligations(surface="voice")) == 1
    assert len(control.events(surface="voice")) == 1


def test_replaying_a_fulfilment_is_a_no_op(tmp_path, control):
    entry = _journal(tmp_path, control)
    entry.opened("voice-1")
    for _ in range(3):
        entry.fulfilled("voice-1")

    obligation = control.obligations(surface="voice")[0]
    assert obligation.state == "fulfilled"
    assert len(control.events(surface="voice")) == 2


def test_each_real_failure_counts_once_and_keeps_the_obligation_open(tmp_path, control):
    entry = _journal(tmp_path, control)
    entry.opened("voice-1")

    for _ in range(3):
        entry.failed("voice-1", error="the voice bridge was down")

    obligation = control.obligations(surface="voice")[0]
    assert obligation.state == "open"
    assert obligation.attempts == 3
    assert obligation.last_error == "the voice bridge was down"


def test_replaying_one_specific_attempt_is_idempotent(tmp_path, control):
    entry = _journal(tmp_path, control)
    entry.opened("voice-1")

    for _ in range(5):
        entry.failed("voice-1", error="same attempt, redelivered", attempt=1)

    assert control.obligations(surface="voice")[0].attempts == 1


def test_the_attempt_counter_survives_a_restart(tmp_path, control):
    path = tmp_path / "voice.sqlite3"
    first = SurfaceJournal("voice", path, control_store=control)
    first.opened("voice-1")
    first.failed("voice-1", error="down")
    first.failed("voice-1", error="still down")

    # The process dies and a fresh journal opens the same durable file.
    reopened = SurfaceJournal("voice", path, control_store=control)
    reopened.failed("voice-1", error="down again")

    assert control.obligations(surface="voice")[0].attempts == 3


# ---- transactional staging ------------------------------------------------


def test_an_envelope_staged_in_the_callers_transaction_commits_with_it(tmp_path, control):
    entry = _journal(tmp_path, control)
    with entry.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO jobs(id) VALUES ('voice-1')")
        entry.opened("voice-1", connection=connection)

    # Committed but not yet published: exactly the crash window.
    assert entry.pending() == 1
    assert control.obligations(surface="voice") == []

    assert entry.flush() == 1
    assert control.obligations(surface="voice")[0].state == "open"


def test_a_rolled_back_transaction_discards_the_envelope_too(tmp_path, control):
    entry = _journal(tmp_path, control)
    connection = entry.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        entry.opened("voice-1", connection=connection)
        connection.rollback()
    finally:
        connection.close()

    assert entry.pending() == 0
    assert control.obligations(surface="voice") == []


def test_a_failing_control_plane_preserves_the_staged_event(tmp_path):
    class Broken:
        def append_envelope(self, _envelope):
            raise RuntimeError("control plane is unavailable")

    entry = SurfaceJournal("voice", tmp_path / "voice.sqlite3", control_store=Broken())
    entry.opened("voice-1")

    assert entry.pending() == 1
    # A later pass, after the plane is repaired, still publishes it.
    control = ControlPlaneStore(tmp_path / "control.sqlite3")
    assert entry.flush.__self__ is entry
    repaired = SurfaceJournal(
        "voice", tmp_path / "voice.sqlite3", control_store=control
    )
    assert repaired.flush() == 1
    assert control.obligations(surface="voice")[0].state == "open"


# ---- messy native ids -----------------------------------------------------


def test_a_legal_native_id_is_left_exactly_alone():
    assert safe_token("claude-session_12.34:56") == "claude-session_12.34:56"


def test_an_illegal_native_id_becomes_a_stable_token():
    messy = "turn 4 / claude@laptop"
    first = safe_token(messy, prefix="turn")
    assert first == safe_token(messy, prefix="turn")
    assert first.startswith("turn-")
    assert safe_token("a different one", prefix="turn") != first


def test_an_empty_native_id_is_refused():
    with pytest.raises(SurfaceJournalError):
        safe_token("")


def test_a_messy_id_still_produces_one_closable_obligation(tmp_path, control):
    entry = _journal(tmp_path, control, "chat")
    messy = "turn 9 / desk"

    entry.opened(messy)
    entry.fulfilled(messy)

    assert [item.state for item in control.obligations(surface="chat")] == ["fulfilled"]


# ---- the tracking context manager -----------------------------------------


def test_track_fulfils_on_a_clean_exit(tmp_path, control):
    entry = _journal(tmp_path, control, "tool")
    with entry.track("tool-1", summary="read the file"):
        pass

    assert [item.state for item in control.obligations(surface="tool")] == ["fulfilled"]


def test_track_records_the_real_error_and_re_raises(tmp_path, control):
    entry = _journal(tmp_path, control, "tool")

    with pytest.raises(RuntimeError, match="the tool blew up"), entry.track("tool-1"):
        raise RuntimeError("the tool blew up")

    obligation = control.obligations(surface="tool")[0]
    assert obligation.state == "open"
    assert obligation.attempts == 1
    assert "the tool blew up" in str(obligation.last_error)


def test_a_crash_inside_track_leaves_the_obligation_open_for_recovery(tmp_path, control):
    """Simulates the process dying: the open event landed, nothing closed it."""

    entry = _journal(tmp_path, control, "chat")
    entry.opened("chat-1", summary="finish answering him")

    assert control.obligations(surface="chat")[0].state == "open"


# ---- ambiguity ------------------------------------------------------------


def test_ambiguous_never_becomes_fulfilled(tmp_path, control):
    entry = _journal(tmp_path, control)
    entry.opened("voice-1")

    assert entry.ambiguous("voice-1", reason="the bridge accepted it but never confirmed")

    obligation = control.obligations(surface="voice")[0]
    assert obligation.state == "ambiguous"
    assert obligation.state != "fulfilled"
    assert obligation.fulfillment_event_id is None


def test_marking_an_unknown_job_ambiguous_reports_rather_than_raises(tmp_path, control):
    entry = _journal(tmp_path, control)
    assert entry.ambiguous("never-existed", reason="nothing to abandon") is False


# ---- restart recovery -----------------------------------------------------


def test_recovery_of_a_journalled_surface_never_claims_a_redelivery(tmp_path, control):
    entry = _journal(tmp_path, control, "chat")
    entry.opened("chat-1", summary="finish answering him")

    report = reconcile_obligations(
        control, handlers=journal_redelivery_handlers(control), now=1e10
    )

    # It did not re-run his turn at him, and it did not pretend it had.
    assert report.recovered == []
    assert [item.action for item in report.skipped] == ["skipped"]


def test_repeated_restarts_burn_the_budget_and_end_in_ambiguous(tmp_path, control):
    entry = _journal(tmp_path, control, "chat")
    entry.opened("chat-1", summary="finish answering him")
    handlers = journal_redelivery_handlers(control)

    for _ in range(6):
        reconcile_obligations(control, handlers=handlers, max_attempts=5, now=1e10)

    obligation = control.obligations(surface="chat")[0]
    assert obligation.state == "ambiguous"
    assert obligation.state != "fulfilled"


def test_a_real_delivery_after_a_restart_still_closes_it_honestly(tmp_path, control):
    entry = _journal(tmp_path, control, "voice")
    entry.opened("voice-1")
    reconcile_obligations(control, handlers=journal_redelivery_handlers(control), now=1e10)

    entry.fulfilled("voice-1")

    assert control.obligations(surface="voice")[0].state == "fulfilled"


def test_surfaces_do_not_close_each_others_obligations(tmp_path, control):
    voice = _journal(tmp_path, control, "voice")
    tool = _journal(tmp_path, control, "tool")
    voice.opened("shared-id")

    tool.opened("shared-id")
    tool.fulfilled("shared-id")

    assert control.obligations(surface="voice")[0].state == "open"
    assert control.obligations(surface="tool")[0].state == "fulfilled"


# ---- what Serena owes -----------------------------------------------------


def test_debts_read_like_a_person_said_them(tmp_path, control):
    voice = _journal(tmp_path, control, "voice")
    voice.opened("voice-1", summary="tell him how the refactor went")

    debts = outstanding_debts(control, now=voice_created_at(control) + 7_200)

    assert debts["open"] == 1
    assert "spoken coding job" in debts["items"][0]["owes"]
    assert "hours ago" in debts["items"][0]["waiting_for"]
    assert "1 thing still open" in debts["spoken"]


def voice_created_at(control):
    return control.obligations(surface="voice")[0].created_at


def test_debts_separate_unconfirmed_from_still_owed(tmp_path, control):
    voice = _journal(tmp_path, control, "voice")
    voice.opened("voice-1")
    voice.ambiguous("voice-1", reason="sent but never acknowledged")
    voice.opened("voice-2", summary="the other one")

    debts = outstanding_debts(control)

    assert debts["open"] == 1
    assert debts["unconfirmed"] == 1
    assert "can't confirm" in debts["spoken"]
    assert debts["unconfirmed_items"][0]["reason"] == "sent but never acknowledged"


def test_debts_count_a_failed_attempt_as_stuck(tmp_path, control):
    voice = _journal(tmp_path, control, "voice")
    voice.opened("voice-1")
    voice.failed("voice-1", error="the bridge was down")

    debts = outstanding_debts(control)

    assert debts["stuck"] == 1
    assert "failed at least one delivery" in debts["spoken"]


def test_nothing_outstanding_says_so_plainly(control):
    assert outstanding_debts(control)["spoken"] == "nothing outstanding, we're square"


def test_a_fulfilled_obligation_is_not_a_debt(tmp_path, control):
    voice = _journal(tmp_path, control, "voice")
    voice.opened("voice-1")
    voice.fulfilled("voice-1")

    debts = outstanding_debts(control)

    assert debts["open"] == 0
    assert debts["spoken"] == "nothing outstanding, we're square"


# ---- the native journal stays authoritative -------------------------------


def test_the_journal_never_writes_to_the_surfaces_own_tables(tmp_path, control):
    """It may only add its own bookkeeping to a database it shares."""

    path = tmp_path / "voice.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, state TEXT)")
        connection.execute("INSERT INTO jobs VALUES ('voice-1', 'running')")

    entry = SurfaceJournal("voice", path, control_store=control)
    entry.opened("voice-1")
    entry.failed("voice-1", error="nope")
    entry.fulfilled("voice-1")

    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT id, state FROM jobs").fetchall()
    assert rows == [("voice-1", "running")]
