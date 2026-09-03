"""He talks to a running coding job. These are the sentences he actually says.

The audit's fourth cause was that a job, once started, was uncontrollable: no
way to ask what it was doing, correct it, call it off, or pick it back up. So
these are named after the utterance, not the function, and each one asserts
the durable consequence rather than what Serena said about it.
"""

from __future__ import annotations

import json
import uuid

import pytest

from core.coding_job_controls import (
    JobResolutionError,
    control_job,
    job_status,
    resolve_job,
    spoken_status,
)
from core.voice_inbox import VoiceInboxStore

VOICE = {"text": "cancel that", "protocol": "voice", "call_id": "c1", "turn_id": "c1:9"}


def _brief(item_id: str, *, root: str = "/tmp/serena", request: str = "fix the dot overlay") -> dict:
    return {
        "schema_version": 1,
        "item_id": item_id,
        "exact_request": request,
        "triggering_request": request,
        "project_root": root,
        "relevant_conversation": [],
        "project_context": [],
        "memory_guidance": [],
        "ledger_guidance": [],
        "handoff_guidance": [],
        "requested_outcome": request,
        "codex_model": "gpt-5.6-sol",
        "codex_effort": "high",
        "review_model": "claude-opus-5",
        "review_effort": "xhigh",
        "accepted_at": 10.0,
        "acceptance_criteria": ["tests pass"],
        "authority_boundaries": ["do not commit"],
        "commit_authorized": False,
        "initial_git": {"tree": "abc", "dirty_paths": []},
    }


def _running(store: VoiceInboxStore, name: str, *, root: str, request: str, session: str = "codex-session"):
    # Real job ids are uuids, and he reads the first eight characters off the
    # overlay, so the fixtures have to be shaped the same way.
    item = store.enqueue_accepted(
        _brief(str(uuid.uuid4()), root=root, request=request),
        call_id=f"call-{name}",
        turn_id=f"turn-{name}",
    )
    assert store.claim_next(f"headless-voice-{name}") is not None
    assert store.acknowledge_started(item.item_id, target_sid=f"headless-voice-{name}")
    if session:
        assert store.set_work_session(item.item_id, session)
    return item


def _store(tmp_path, name: str = "voice.sqlite3") -> VoiceInboxStore:
    return VoiceInboxStore(tmp_path / name)


def _queued(store: VoiceInboxStore, name: str, *, root: str = "/tmp/serena"):
    return store.enqueue_accepted(
        _brief(str(uuid.uuid4()), root=root, request="fix the dot overlay"),
        call_id=f"call-{name}",
        turn_id=f"turn-{name}",
    )


def test_how_is_it_going_reads_the_real_job_not_her_own_earlier_words(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "status", root="/tmp/serena", request="fix the dot overlay")
    store.record_evidence(
        item.item_id,
        {
            "complete": True,
            "changed_files": ["voice/brain_bridge.py", "ui/web.py"],
            "tests": [{"command": "pytest tests/test_voice_inbox.py", "exit_code": 0}],
            "live_proof": [{"command": "systemctl --user status serena-brain", "exit_code": 0}],
            "errors": [],
        },
    )

    result = job_status("", inbox=store)

    assert result.ok
    assert result.item_id == item.item_id
    assert "serena" in result.spoken
    assert "changed 2 files" in result.spoken
    assert "voice/brain_bridge.py" in result.spoken
    assert "1 test command exited clean" in result.spoken
    assert "1 live proof command recorded" in result.spoken
    assert result.job["model"]["requested"] == "gpt-5.6-sol"


def test_cancel_that_stops_the_running_job_durably(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "cancel", root="/tmp/serena", request="fix the dot overlay")

    result = control_job("cancel", origin=VOICE, inbox=store, audit_path=tmp_path / "audit.jsonl")

    assert result.ok
    assert result.item_id == item.item_id
    pending = store.pending_controls(item.item_id)
    assert [control["action"] for control in pending] == ["cancel"]


def test_cancel_that_stops_queued_work_before_a_worker_can_claim_it(tmp_path) -> None:
    store = _store(tmp_path)
    item = _queued(store, "queued-cancel")

    result = control_job("cancel", origin=VOICE, inbox=store, audit_path=tmp_path / "a.jsonl")

    assert result.ok
    assert store.recent_jobs()[0]["state"] == "cancelled"
    assert store.claim_next("headless-voice-too-late") is None
    assert store.pending_controls(item.item_id) == []


def test_also_handle_the_empty_case_steers_the_same_codex_session(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "steer", root="/tmp/serena", request="fix the dot overlay")
    before = len(store.recent_jobs())

    result = control_job(
        "steer",
        text="also handle the empty case",
        origin={**VOICE, "text": "also make it handle the empty case"},
        inbox=store,
        audit_path=tmp_path / "audit.jsonl",
    )

    assert result.ok
    pending = store.pending_controls(item.item_id)
    assert [(control["action"], control["text"]) for control in pending] == [
        ("steer", "also handle the empty case")
    ]
    # Steering must never fork a second blind job.
    assert len(store.recent_jobs()) == before
    assert store.work_record(item.item_id)["session_id"] == "codex-session"


def test_steering_without_the_correction_in_words_is_refused(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "empty", root="/tmp/serena", request="fix the dot overlay")

    result = control_job("steer", text="   ", origin=VOICE, inbox=store, audit_path=tmp_path / "a.jsonl")

    assert not result.ok
    assert "in words" in result.reason
    assert store.pending_controls(item.item_id) == []


def test_pick_that_back_up_resumes_the_persisted_codex_session(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "resume", root="/tmp/serena", request="fix the dot overlay")
    assert store.finish_work_item(item.item_id, error="interrupted")

    result = control_job("resume", origin={**VOICE, "text": "pick that back up"}, inbox=store, audit_path=tmp_path / "a.jsonl")

    assert result.ok
    assert store.work_record(item.item_id)["state"] == "resume_queued"
    requeued = store.claim_next("headless-voice-resume-again")
    assert requeued is not None and requeued.item_id == item.item_id
    assert store.work_record(item.item_id)["session_id"] == "codex-session"


def test_a_job_with_no_persisted_session_is_not_claimed_resumable(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "nosession", root="/tmp/serena", request="fix the dot overlay", session="")
    assert store.finish_work_item(item.item_id, error="died early")

    result = control_job("resume", origin=VOICE, inbox=store, audit_path=tmp_path / "a.jsonl")

    assert not result.ok
    assert "picked back up" in result.reason or "nothing stopped" in result.reason
    assert store.work_record(item.item_id)["state"] == "failed"


def test_cancel_with_nothing_running_says_so_instead_of_faking_it(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "done", root="/tmp/serena", request="fix the dot overlay")
    store.record_evidence(item.item_id, {"complete": True, "changed_files": ["ui/web.py"]})
    assert store.finish_work_item(item.item_id, summary="finished")

    result = control_job("cancel", origin=VOICE, inbox=store, audit_path=tmp_path / "a.jsonl")

    assert not result.ok
    assert "nothing is running" in result.reason
    assert store.pending_controls(item.item_id) == []


def test_two_running_projects_ask_which_one_instead_of_guessing(tmp_path) -> None:
    store = _store(tmp_path)
    _running(store, "serena", root="/tmp/serena", request="fix the dot overlay")
    _running(store, "tracker", root="/tmp/full_tracker", request="fix the charge notification")

    ambiguous = control_job("cancel", origin=VOICE, inbox=store, audit_path=tmp_path / "a.jsonl")
    named = control_job(
        "cancel", reference="full_tracker", origin=VOICE, inbox=store, audit_path=tmp_path / "a.jsonl"
    )

    assert not ambiguous.ok
    assert "which coding job" in ambiguous.reason
    assert "serena" in ambiguous.reason and "full_tracker" in ambiguous.reason
    assert named.ok
    assert store.job_snapshot(named.item_id)["brief"]["project_root"] == "/tmp/full_tracker"


def test_a_typed_front_door_turn_cannot_control_a_coding_job(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "typed", root="/tmp/serena", request="fix the dot overlay")

    result = control_job(
        "cancel",
        origin={"text": "cancel that", "protocol": "web"},
        inbox=store,
        audit_path=tmp_path / "a.jsonl",
    )

    assert not result.ok
    assert "live spoken turn" in result.reason
    assert store.pending_controls(item.item_id) == []


def test_she_cannot_control_a_job_with_no_spoken_turn_behind_it(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "noturn", root="/tmp/serena", request="fix the dot overlay")

    result = control_job("cancel", origin={}, inbox=store, audit_path=tmp_path / "a.jsonl")

    assert not result.ok
    assert "no originating spoken turn" in result.reason
    assert store.pending_controls(item.item_id) == []


def test_every_control_decision_including_refusals_is_audited(tmp_path) -> None:
    store = _store(tmp_path)
    _running(store, "audit", root="/tmp/serena", request="fix the dot overlay")
    audit = tmp_path / "audit.jsonl"

    control_job("cancel", origin=VOICE, inbox=store, audit_path=audit)
    control_job("steer", text="", origin=VOICE, inbox=store, audit_path=audit)

    records = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
    assert [record["allowed"] for record in records] == [True, False]
    assert all("cancel" in record["reason"] or "steer" in record["reason"] for record in records)
    # Raw speech never lands in the ledger, only its digest.
    assert all("cancel that" not in json.dumps(record) for record in records)


def test_a_short_job_id_names_the_job_he_read_off_the_overlay(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "byid", root="/tmp/serena", request="fix the dot overlay")

    result = control_job(
        "cancel", reference=item.item_id[:8], origin=VOICE, inbox=store, audit_path=tmp_path / "a.jsonl"
    )

    assert result.ok
    assert result.item_id == item.item_id


def test_unknown_reference_is_refused_rather_than_resolved_to_the_nearest_job(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "unknown", root="/tmp/serena", request="fix the dot overlay")

    result = control_job(
        "cancel", reference="the locket webhook", origin=VOICE, inbox=store, audit_path=tmp_path / "a.jsonl"
    )

    assert not result.ok
    assert store.pending_controls(item.item_id) == []
    # It must not answer "nothing is running" while a job is running; that is a
    # lie he would act on by starting a second one.
    assert "nothing is running" not in result.reason
    assert "serena" in result.reason


@pytest.mark.parametrize("reference", ["", "that", "it", "the job", "the last one", "latest"])
def test_pronouns_and_last_resolve_the_one_live_job(tmp_path, reference: str) -> None:
    store = _store(tmp_path)
    item = _running(store, "pronoun", root="/tmp/serena", request="fix the dot overlay")

    job = resolve_job(reference, action="cancel", jobs=store.recent_jobs())

    assert job["item_id"] == item.item_id


def test_blank_status_prefers_the_only_active_job_over_completed_history(tmp_path) -> None:
    store = _store(tmp_path)
    completed = _running(store, "old", root="/tmp/old", request="old job")
    store.record_evidence(completed.item_id, {"complete": True, "changed_files": []})
    assert store.finish_work_item(completed.item_id, summary="done")
    active = _running(store, "active", root="/tmp/serena", request="current job")

    result = job_status("", inbox=store)

    assert result.ok
    assert result.item_id == active.item_id


def test_local_cli_control_is_audited_as_cli_not_fake_desk_voice(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "cli", root="/tmp/serena", request="fix the dot overlay")
    audit = tmp_path / "audit.jsonl"

    result = control_job(
        "cancel",
        origin={"text": "coding job control from the local terminal", "protocol": "cli"},
        inbox=store,
        audit_path=audit,
    )

    assert result.ok
    assert store.pending_controls(item.item_id)
    assert json.loads(audit.read_text().splitlines()[-1])["protocol"] == "cli"


def test_status_reports_the_model_that_actually_ran_not_the_one_requested() -> None:
    spoken = spoken_status(
        {
            "item_id": "abcdef123456",
            "state": "working",
            "project": "serena",
            "project_root": "/home/raghav/Documents/Projects/serena",
            "model": {
                "requested": "gpt-5.6-sol",
                "effort": "xhigh",
                "reported": "gpt-5.6-sol",
                "reported_effort": "xhigh",
            },
            "progress": {"attempt": 2},
            "changes": [],
            "tests": [],
            "live_proof": [],
            "evidence": {},
            "review": {},
        }
    )

    assert "attempt 2 on gpt-5.6-sol at xhigh" in spoken


def test_a_job_with_no_validated_root_is_never_given_a_project_name() -> None:
    """Pre-redesign rows have no brief. Saying a name would invent certainty."""

    spoken = spoken_status({"item_id": "abcdef123456", "state": "completed", "project": "project"})

    assert "an unresolved project" in spoken
    assert "in project is" not in spoken


def test_status_never_calls_an_incomplete_job_done(tmp_path) -> None:
    store = _store(tmp_path)
    item = _running(store, "incomplete", root="/tmp/serena", request="fix the dot overlay")
    store.record_evidence(
        item.item_id,
        {
            "complete": False,
            "changed_files": ["core/voice_inbox.py"],
            "tests": [],
            "errors": ["code changed without a recorded test command and exit code"],
        },
    )

    result = job_status(item.item_id[:8], inbox=store)

    assert result.ok
    assert "evidence is incomplete" in result.spoken
    assert "recorded test command" in result.spoken


def test_status_on_an_empty_queue_refuses_instead_of_inventing_one(tmp_path) -> None:
    store = _store(tmp_path)

    result = job_status("", inbox=store)

    assert not result.ok
    assert "no coding jobs" in result.reason


def test_resolve_rejects_an_unsupported_control(tmp_path) -> None:
    store = _store(tmp_path)
    _running(store, "bad", root="/tmp/serena", request="fix the dot overlay")

    with pytest.raises(JobResolutionError):
        resolve_job("", action="delete", jobs=store.recent_jobs())
    assert not control_job("delete", origin=VOICE, inbox=store, audit_path=tmp_path / "a.jsonl").ok
