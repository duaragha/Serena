"""She talks while she codes, but only into a gap.

Raghav asked for what he liked in ChatGPT's voice mode: "she updates as she's
coding, so there's always something going on". He then named the exact way it
could go wrong, being talked over mid-conversation, and chose full silence
during a real back-and-forth over interjections. These pin that bargain.
"""

from __future__ import annotations

import time

from core.work_narration import (
    NarrationLine,
    append,
    collapse,
    current_offset,
    read_since,
)


def _line(kind: str, text: str, age: float = 0.0) -> NarrationLine:
    return NarrationLine(
        offset=0, created_at=time.time() - age, job_id="j", kind=kind, text=text
    )


def test_a_backlog_is_collapsed_to_the_newest_update() -> None:
    """Three updates piling up while he talks must not be read out one after
    another when he finally pauses."""
    kept = collapse(
        [
            _line("milestone", "reading the click handler", 6),
            _line("milestone", "the listener was never bound", 4),
            _line("milestone", "adding the binding now", 2),
        ]
    )
    assert [line.text for line in kept] == ["adding the binding now"]


def test_a_blocker_and_a_completion_are_never_dropped() -> None:
    """Routine progress is allowed to die unheard. These two are the whole
    reason he wants to be told anything at all."""
    kept = collapse(
        [
            _line("milestone", "running the tests", 5),
            _line("blocker", "the migration needs your call", 4),
            _line("done", "shipped, tests green", 1),
        ]
    )
    assert [line.kind for line in kept] == ["blocker", "done"]


def test_stale_routine_news_is_thrown_away_not_spoken_late() -> None:
    """Saying "about to run the tests" a minute after they passed is worse
    than saying nothing."""
    assert collapse([_line("milestone", "about to run the tests", 300)]) == []
    # but a blocker that old is still his to hear
    assert len(collapse([_line("blocker", "needs your call", 300)])) == 1


def test_a_new_call_does_not_recite_an_old_job_backlog(tmp_path) -> None:
    """The offset starts at the end of the file, so picking up the phone never
    opens with what a job said an hour ago."""
    path = tmp_path / "narration.jsonl"
    append("old news from before he picked up", path=path)
    start = current_offset(path)
    lines, offset = read_since(start, path=path)
    assert lines == []
    append("this happened while he was listening", path=path)
    lines, _offset = read_since(offset, path=path)
    assert [line.text for line in lines] == ["this happened while he was listening"]


def test_a_half_written_line_is_never_spoken(tmp_path) -> None:
    """The supervisor appends from another process. Reading mid-append must
    leave the partial line for next time rather than speaking half a sentence."""
    path = tmp_path / "narration.jsonl"
    append("complete line", path=path)
    with path.open("ab") as handle:
        handle.write(b'{"created_at":1,"kind":"milestone","text":"half a sen')
    lines, offset = read_since(0, path=path)
    assert [line.text for line in lines] == ["complete line"]
    # and the offset stops before the partial line so it is re-read intact
    with path.open("ab") as handle:
        handle.write(b'tence"}\n')
    lines, _offset = read_since(offset, path=path)
    assert [line.text for line in lines] == ["half a sentence"]


def test_a_truncated_spool_rewinds_instead_of_going_deaf(tmp_path) -> None:
    """If the file is rotated or truncated under the reader, an offset past
    the end would silently stop all narration forever."""
    path = tmp_path / "narration.jsonl"
    append("first", path=path)
    _lines, offset = read_since(0, path=path)
    path.write_text("", encoding="utf-8")
    append("after rotation", path=path)
    lines, _offset = read_since(offset, path=path)
    assert [line.text for line in lines] == ["after rotation"]


def test_only_decisions_and_errors_are_offered_out_loud() -> None:
    """Reading every bash line aloud is the constant narration that makes this
    unbearable. Commands and file edits stay on the overlay."""
    from core.voice_work_supervisor import _narration_line

    assert _narration_line("Okay.") == ""
    assert _narration_line("") == ""
    spoken = _narration_line(
        "I checked the handler and the listener was never bound. So I am adding it."
    )
    # first sentence only: the agent writes for a screen, not for an ear
    assert spoken == "I checked the handler and the listener was never bound."


def _session(**overrides):
    """A CallSession with only the fields the floor rule reads."""
    from voice.call.orchestrator import CallSession

    session = CallSession.__new__(CallSession)
    session._closed = False
    session._call_started = True
    session._turn_task = None
    session._greeting_claimed = False
    session._greeting_completed = True
    session.current_generation = 4
    session._audio_ended_generations = {4}
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def test_she_stays_silent_while_a_turn_is_in_flight() -> None:
    """His condition for agreeing to this: a coding job is never more
    important than the conversation he is having. A turn in flight covers both
    him talking and her answering, because both allocate the turn task."""

    class _Running:
        def done(self):
            return False

    # a settled turn with nothing pending is the only time she may speak
    assert _session()._floor_is_free() is True
    assert _session(_turn_task=_Running())._floor_is_free() is False


def test_she_stays_silent_until_her_own_answer_has_finished_landing() -> None:
    """A generation whose audio has not ended is a reply still playing. The
    turn task can already be done while the last sentences are still on the
    wire."""
    assert _session(_audio_ended_generations=set())._floor_is_free() is False
    # a NEW generation he just started, before any audio, is also busy
    assert _session(current_generation=5)._floor_is_free() is False


def test_she_does_not_talk_over_her_own_greeting() -> None:
    assert (
        _session(_greeting_claimed=True, _greeting_completed=False)._floor_is_free()
        is False
    )


def test_a_closing_session_never_starts_speaking() -> None:
    assert _session(_closed=True)._floor_is_free() is False
    assert _session(_call_started=False)._floor_is_free() is False


def test_how_a_job_ended_always_gets_said(tmp_path, monkeypatch) -> None:
    """Completion and failure are the two outcomes that must survive a busy
    conversation. They are emitted from the snapshot rather than the live event
    stream, because a job can finish without ever writing an agent message."""
    from contextlib import suppress as _suppress

    import core.work_narration as wn
    from core.voice_work_supervisor import VoiceWorkSupervisor

    path = tmp_path / "narration.jsonl"
    monkeypatch.setattr(wn, "DEFAULT_PATH", path)

    supervisor = VoiceWorkSupervisor.__new__(VoiceWorkSupervisor)

    supervisor._narrate_completion("j1", {"state": "completed", "summary": "Fixed the click handler. Tests pass."})
    supervisor._narrate_completion("j2", {"state": "failed", "summary": "The migration blew up."})
    # he stopped it himself, so telling him it stopped is noise
    supervisor._narrate_completion("j3", {"state": "cancelled", "summary": "stopped"})

    lines, _offset = wn.read_since(0, path=path)
    assert [(line.kind, line.text) for line in lines] == [
        ("done", "Fixed the click handler."),
        ("blocker", "The migration blew up."),
    ]


def test_a_silent_job_still_reports_that_it_finished(tmp_path, monkeypatch) -> None:
    """A job that never wrote a summary must not finish in total silence."""
    import core.work_narration as wn
    from core.voice_work_supervisor import VoiceWorkSupervisor

    path = tmp_path / "narration.jsonl"
    monkeypatch.setattr(wn, "DEFAULT_PATH", path)
    supervisor = VoiceWorkSupervisor.__new__(VoiceWorkSupervisor)
    supervisor._narrate_completion("j4", {"state": "completed", "summary": ""})

    lines, _offset = wn.read_since(0, path=path)
    assert [line.text for line in lines] == ["that job's done"]
