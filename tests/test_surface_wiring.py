"""Proof that the live surfaces are actually on the control plane.

`test_surface_journal.py` proves the seam works. This proves the seam is
plugged in: it drives the real voice supervisor method, the real chat turn
wrapper, and the real laptop tool handler, and checks the obligation ledger
afterwards. Without these, the migration would be a well-tested module nothing
calls.
"""

from __future__ import annotations

import asyncio

import pytest

from core.control_plane import ControlPlaneStore


@pytest.fixture
def plane(tmp_path, monkeypatch):
    """Point every journal and the control plane at a temporary directory."""

    import core.surface_journal as surface_journal

    monkeypatch.setenv("SERENA_CONTROL_PLANE_DB_PATH", str(tmp_path / "control.sqlite3"))
    monkeypatch.setattr(surface_journal, "DEFAULT_JOURNAL_DIR", tmp_path)
    surface_journal.reset_journals()
    yield ControlPlaneStore(tmp_path / "control.sqlite3")
    surface_journal.reset_journals()


# ---- voice ----------------------------------------------------------------


def _supervisor(notifier):
    from core.voice_work_supervisor import VoiceWorkSupervisor

    return VoiceWorkSupervisor.__new__(VoiceWorkSupervisor)  # no live inbox needed


def test_taking_a_spoken_job_makes_serena_owe_him_the_outcome(plane):
    from core.voice_work_supervisor import VoiceWorkSupervisor

    supervisor = _supervisor(None)
    VoiceWorkSupervisor._owe_outcome(supervisor, "job-1", "fix the parser")

    obligations = plane.obligations(surface="voice")
    assert [item.state for item in obligations] == ["open"]
    assert obligations[0].summary == "fix the parser"


def test_telling_him_how_it_ended_discharges_the_obligation(plane):
    from core.voice_work_supervisor import VoiceWorkSupervisor

    said: list = []
    supervisor = _supervisor(None)
    supervisor.notifier = said.append

    VoiceWorkSupervisor._owe_outcome(supervisor, "job-1", "fix the parser")
    VoiceWorkSupervisor._deliver_outcome(supervisor, "job-1", "done. it's fixed")

    assert said == ["done. it's fixed"]
    assert plane.obligations(surface="voice")[0].state == "fulfilled"


def test_a_failed_job_he_was_told_about_is_still_discharged(plane):
    """The promise was to tell him how it ended, not that it ended well."""

    from core.voice_work_supervisor import VoiceWorkSupervisor

    said: list = []
    supervisor = _supervisor(None)
    supervisor.notifier = said.append

    VoiceWorkSupervisor._owe_outcome(supervisor, "job-1", "fix the parser")
    VoiceWorkSupervisor._deliver_outcome(supervisor, "job-1", "the coding job stopped: boom")

    assert plane.obligations(surface="voice")[0].state == "fulfilled"


def test_a_notifier_that_fails_leaves_serena_still_owing_him(plane):
    from core.voice_work_supervisor import VoiceWorkSupervisor

    def broken(_message):
        raise RuntimeError("the telegram bot is down")

    supervisor = _supervisor(None)
    supervisor.notifier = broken

    VoiceWorkSupervisor._owe_outcome(supervisor, "job-1", "fix the parser")
    with pytest.raises(RuntimeError):
        VoiceWorkSupervisor._deliver_outcome(supervisor, "job-1", "done.")

    obligation = plane.obligations(surface="voice")[0]
    assert obligation.state == "open"
    assert obligation.attempts == 1
    assert "telegram bot is down" in str(obligation.last_error)


def test_a_broken_control_plane_never_costs_him_the_notification(plane, monkeypatch):
    """The native journal is authoritative; the ledger is not load-bearing."""

    import core.surface_journal as surface_journal
    from core.voice_work_supervisor import VoiceWorkSupervisor

    monkeypatch.setattr(
        surface_journal, "journal", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    said: list = []
    supervisor = _supervisor(None)
    supervisor.notifier = said.append

    VoiceWorkSupervisor._owe_outcome(supervisor, "job-1", "fix the parser")
    VoiceWorkSupervisor._deliver_outcome(supervisor, "job-1", "done.")

    assert said == ["done."], "he still gets told even with the ledger broken"


# ---- chat -----------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_a_finished_turn_opens_and_closes_one_chat_obligation(plane, monkeypatch):
    from core import brain_daemon

    async def fake_turn(_client, _payload, on_delta=None):
        return {"ok": True, "text": "here you go"}

    monkeypatch.setattr(brain_daemon, "_run_turn_answered", fake_turn)
    out = _run(brain_daemon._run_turn(None, {"text": "hey", "turn_id": "turn-1"}))

    assert out["ok"] is True
    assert [item.state for item in plane.obligations(surface="chat")] == ["fulfilled"]


def test_an_error_answer_still_counts_as_answering_him(plane, monkeypatch):
    from core import brain_daemon

    async def fake_turn(_client, _payload, on_delta=None):
        return {"ok": False, "error": "text or image required"}

    monkeypatch.setattr(brain_daemon, "_run_turn_answered", fake_turn)
    _run(brain_daemon._run_turn(None, {"text": "hey", "turn_id": "turn-1"}))

    # He asked, the daemon replied. The turn is over.
    assert plane.obligations(surface="chat")[0].state == "fulfilled"


def test_a_turn_that_raises_leaves_the_obligation_open(plane, monkeypatch):
    from core import brain_daemon

    async def fake_turn(_client, _payload, on_delta=None):
        raise RuntimeError("the provider died mid-turn")

    monkeypatch.setattr(brain_daemon, "_run_turn_answered", fake_turn)
    with pytest.raises(RuntimeError):
        _run(brain_daemon._run_turn(None, {"text": "hey", "turn_id": "turn-1"}))

    obligation = plane.obligations(surface="chat")[0]
    assert obligation.state == "open"
    assert "provider died mid-turn" in str(obligation.last_error)


def test_a_cancelled_turn_closes_without_claiming_an_answer(plane, monkeypatch):
    from core import brain_daemon

    async def fake_turn(_client, _payload, on_delta=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(brain_daemon, "_run_turn_answered", fake_turn)
    with pytest.raises(asyncio.CancelledError):
        _run(brain_daemon._run_turn(None, {"text": "hey", "turn_id": "turn-1"}))

    obligation = plane.obligations(surface="chat")[0]
    assert obligation.state == "cancelled"
    assert obligation.fulfillment_event_id is None


def test_two_turns_are_two_separate_obligations(plane, monkeypatch):
    from core import brain_daemon

    async def fake_turn(_client, _payload, on_delta=None):
        return {"ok": True}

    monkeypatch.setattr(brain_daemon, "_run_turn_answered", fake_turn)
    _run(brain_daemon._run_turn(None, {"text": "one", "turn_id": "turn-1"}))
    _run(brain_daemon._run_turn(None, {"text": "two", "turn_id": "turn-2"}))

    assert len(plane.obligations(surface="chat")) == 2


def test_a_turn_with_no_id_still_gets_its_own_obligation(plane, monkeypatch):
    from core import brain_daemon

    async def fake_turn(_client, _payload, on_delta=None):
        return {"ok": True}

    monkeypatch.setattr(brain_daemon, "_run_turn_answered", fake_turn)
    _run(brain_daemon._run_turn(None, {"text": "one"}))
    _run(brain_daemon._run_turn(None, {"text": "two"}))

    assert len(plane.obligations(surface="chat")) == 2


# ---- tool -----------------------------------------------------------------


def test_a_laptop_tool_call_records_and_closes_its_obligation(plane, monkeypatch):
    from core import brain_laptop_tools

    monkeypatch.setattr(
        brain_laptop_tools, "execute_laptop_action", lambda *_a, **_k: "volume raised"
    )
    result = _run(
        brain_laptop_tools.laptop_action.handler({"action": "volume_up", "target": ""})
    )

    assert "volume raised" in result["content"][0]["text"]
    obligations = plane.obligations(surface="tool")
    assert [item.state for item in obligations] == ["fulfilled"]
    assert "volume_up" in obligations[0].summary


def test_a_failing_tool_call_leaves_evidence_and_re_raises(plane, monkeypatch):
    from core import brain_laptop_tools

    def explode(*_args, **_kwargs):
        raise RuntimeError("the desk broker refused")

    monkeypatch.setattr(brain_laptop_tools, "execute_laptop_action", explode)
    with pytest.raises(RuntimeError, match="the desk broker refused"):
        _run(
            brain_laptop_tools.laptop_action.handler({"action": "open_app", "target": "x"})
        )

    obligation = plane.obligations(surface="tool")[0]
    assert obligation.state == "open"
    assert "the desk broker refused" in str(obligation.last_error)


def test_each_tool_call_is_its_own_obligation(plane, monkeypatch):
    from core import brain_laptop_tools

    monkeypatch.setattr(brain_laptop_tools, "execute_laptop_action", lambda *_a, **_k: "ok")
    for _ in range(3):
        _run(brain_laptop_tools.laptop_action.handler({"action": "mute", "target": ""}))

    assert len(plane.obligations(surface="tool")) == 3


# ---- all four surfaces, one ledger ----------------------------------------


def test_what_serena_owes_spans_every_migrated_surface(plane, monkeypatch):
    from core import brain_daemon
    from core.control_recovery import outstanding_debts
    from core.surface_journal import journal
    from core.voice_work_supervisor import VoiceWorkSupervisor

    # voice: taken, never delivered
    VoiceWorkSupervisor._owe_outcome(_supervisor(None), "job-1", "fix the parser")

    # chat: died mid-turn
    async def dying(_client, _payload, on_delta=None):
        raise RuntimeError("died")

    monkeypatch.setattr(brain_daemon, "_run_turn_answered", dying)
    with pytest.raises(RuntimeError):
        _run(brain_daemon._run_turn(None, {"text": "hey", "turn_id": "turn-1"}))

    # tool: started, never returned
    journal("tool").opened("call-1", summary="run the open_app action")

    # memory: a proposal still waiting on him
    journal("memory").opened("proposal-1", summary="update where he works")

    debts = outstanding_debts(plane)

    assert debts["open"] == 4
    assert {item["surface"] for item in debts["items"]} == {
        "voice",
        "chat",
        "tool",
        "memory",
    }
    assert "4 things still open" in debts["spoken"]
