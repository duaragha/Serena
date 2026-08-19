from __future__ import annotations

import json
import sqlite3

import pytest

from core import voice_inbox
from core.voice_inbox import VoiceInboxStore


def test_voice_inbox_is_idempotent_and_delivers_once(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    first = store.enqueue(
        "fix the spoken response pacing",
        call_id="call-1",
        turn_id="call-1:3",
    )
    repeated = store.enqueue(
        "this duplicate must not replace the original",
        call_id="call-1",
        turn_id="call-1:3",
    )

    assert repeated.item_id == first.item_id
    assert repeated.request == "fix the spoken response pacing"
    claimed = store.claim_next("headless-voice-codex-session")
    assert claimed is not None
    assert claimed.item_id == first.item_id
    assert "Treat it exactly as a message he typed" in claimed.prompt
    assert store.claim_next("headless-voice-other-session") is None
    assert store.acknowledge(claimed.item_id, target_sid="headless-voice-codex-session")
    assert store.pending_count() == 0
    assert store.claim_next("headless-voice-codex-session") is None


def test_failed_delivery_returns_to_the_queue(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    queued = store.enqueue(
        "build the voice inbox",
        call_id="call-2",
        turn_id="call-2:1",
    )

    claimed = store.claim_next("headless-voice-claude-session")
    assert claimed is not None
    assert not store.release(queued.item_id, target_sid="wrong-session")
    assert store.release(
        queued.item_id,
        target_sid="headless-voice-claude-session",
        error="terminal closed",
    )
    retried = store.claim_next("headless-voice-codex-session")
    assert retried is not None
    assert retried.item_id == queued.item_id


def test_claim_exclusion_canonicalizes_the_stored_checkout_root(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    shared = tmp_path / "shared"
    shared.mkdir()
    blocked = _accepted_brief("blocked-job", f"{shared}/.")
    eligible_root = tmp_path / "eligible"
    eligible_root.mkdir()
    eligible = _accepted_brief("eligible-job", str(eligible_root))
    store.enqueue_accepted(blocked, call_id="blocked", turn_id="blocked:1")
    store.enqueue_accepted(eligible, call_id="eligible", turn_id="eligible:1")

    claimed = store.claim_next(
        "headless-voice-canonical",
        excluded_project_roots={str(shared.resolve())},
    )

    assert claimed is not None
    assert claimed.item_id == "eligible-job"


def test_spawned_work_tracks_migration_completion_and_marker(tmp_path) -> None:
    marker = tmp_path / "voice_working"
    store = VoiceInboxStore(
        tmp_path / "voice.sqlite3",
        work_marker_path=marker,
    )
    queued = store.enqueue(
        "open a dedicated coding pane",
        call_id="call-work",
        turn_id="call-work:1",
    )
    claimed = store.claim_next("headless-voice-worker")

    assert claimed is not None
    assert store.acknowledge_started(
        queued.item_id,
        target_sid="headless-voice-worker",
        cwd="/tmp/project",
    )
    assert store.working_count() == 1
    assert marker.is_file()
    assert store.migrate_work_target("headless-voice-worker", "real-codex-session") == 1
    assert store.finish_work_target("real-codex-session") == 1
    assert store.working_count() == 0
    assert not marker.exists()


def test_resident_work_tracks_session_summary_and_completion(tmp_path) -> None:
    database = tmp_path / "voice.sqlite3"
    marker = tmp_path / "voice_working"
    store = VoiceInboxStore(database, work_marker_path=marker)
    queued = store.enqueue(
        "fix the resident worker",
        call_id="call-resident",
        turn_id="call-resident:1",
    )
    target = "headless-voice-test"
    claimed = store.claim_next(target)

    assert claimed is not None
    assert store.acknowledge_started(
        queued.item_id,
        target_sid=target,
        cwd="/tmp/project",
    )
    assert store.set_work_session(queued.item_id, "codex-session-id")
    assert store.finish_work_item(queued.item_id, summary="finished cleanly")

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state, session_id, summary FROM voice_work WHERE item_id=?",
            (queued.item_id,),
        ).fetchone()
    assert row == ("completed", "codex-session-id", "finished cleanly")
    assert not marker.exists()


def test_interrupted_resident_work_returns_to_queue(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    queued = store.enqueue(
        "continue after a restart",
        call_id="call-restart",
        turn_id="call-restart:1",
    )
    target = "headless-voice-before-restart"
    claimed = store.claim_next(target)

    assert claimed is not None
    assert store.acknowledge_started(queued.item_id, target_sid=target)
    assert store.requeue_work_item(queued.item_id, error="service restarted")
    retried = store.claim_next("headless-voice-after-restart")
    assert retried is not None
    assert retried.item_id == queued.item_id


def test_restart_recovery_closes_orphan_running_attempt(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    queued = store.enqueue(
        "recover the exact committed turn",
        call_id="call-orphan",
        turn_id="call-orphan:1",
    )
    target = "headless-voice-orphan"
    claimed = store.claim_next(target)
    assert claimed is not None
    assert store.acknowledge_started(queued.item_id, target_sid=target)
    assert store.set_work_session(queued.item_id, "persisted-codex-session")
    attempt_id, _attempt_no = store.start_attempt(
        queued.item_id,
        provider="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        resume_session_id="persisted-codex-session",
    )

    assert store.recover_headless_work() == 1

    snapshot = store.job_snapshot(queued.item_id)
    attempt = next(row for row in snapshot["attempts"] if row["attempt_id"] == attempt_id)
    assert attempt["state"] == "failed"
    assert attempt["finished_at"] is not None
    assert attempt["last_error"] == "resident worker restarted"
    assert snapshot["work"]["state"] == "resume_queued"
    assert snapshot["queue"]["state"] == "queued"


def test_resident_lease_prevents_desktop_from_stealing_queue(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    queued = store.enqueue(
        "resident only",
        call_id="call-owner",
        turn_id="call-owner:1",
    )
    store.renew_resident_lease("worker-1", pid=1234)

    assert store.resident_lease_active()
    assert store.claim_next("new-voice-old-desktop") is None
    claimed = store.claim_next("headless-voice-worker")
    assert claimed is not None
    assert claimed.item_id == queued.item_id


def test_stale_resident_lease_never_enables_desktop_fallback(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    store.enqueue(
        "fallback safely",
        call_id="call-fallback",
        turn_id="call-fallback:1",
    )
    store.renew_resident_lease("dead-worker", pid=1234, heartbeat=1.0)

    assert not store.resident_lease_active(now=10.0)
    claimed = store.claim_next("new-voice-desktop")
    assert claimed is None
    claimed = store.claim_next("headless-voice-resident")
    assert claimed is not None
    assert store.clear_resident_lease("wrong-worker") is False
    assert store.clear_resident_lease("dead-worker") is True


def _accepted_brief(item_id: str, root: str = "/tmp/project") -> dict:
    return {
        "schema_version": 1,
        "item_id": item_id,
        "exact_request": "fix the project",
        "triggering_request": "fix the project",
        "project_root": root,
        "relevant_conversation": [],
        "project_context": [],
        "memory_guidance": [],
        "ledger_guidance": [],
        "handoff_guidance": [],
        "requested_outcome": "fix the project",
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


def test_status_exposes_durable_structured_brief_and_model_policy(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    brief = _accepted_brief("accepted-job")
    item = store.enqueue_accepted(brief, call_id="call-brief", turn_id="turn-brief")

    snapshot = store.job_snapshot(item.item_id)

    assert snapshot is not None
    assert snapshot["brief"] == brief
    assert snapshot["queue"]["state"] == "queued"


def test_incomplete_accepted_brief_cannot_enter_the_durable_queue(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")

    with pytest.raises(ValueError, match="incomplete"):
        store.enqueue_accepted(
            {
                "schema_version": 1,
                "item_id": "thin-job",
                "exact_request": "fix it",
                "project_root": "/tmp/project",
            },
            call_id="call-thin",
            turn_id="turn-thin",
        )

    assert store.pending_count() == 0


def test_cancellation_and_steering_are_durable_controls(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue_accepted(
        _accepted_brief("controlled-job"), call_id="call-control", turn_id="turn-control"
    )
    claimed = store.claim_next("headless-voice-control")
    assert claimed is not None
    assert store.acknowledge_started(item.item_id, target_sid="headless-voice-control")
    assert store.set_work_session(item.item_id, "persisted-codex-session")

    cancel_id = store.request_cancel(item.item_id)
    steer_id = store.add_steering(item.item_id, "keep the public API compatible")
    pending = store.pending_controls(item.item_id)

    assert [control["action"] for control in pending] == ["cancel", "steer"]
    assert store.finish_control(cancel_id)
    assert store.finish_control(steer_id)
    assert store.pending_controls(item.item_id) == []


def test_resume_requeues_the_same_persisted_codex_session(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue_accepted(
        _accepted_brief("resume-job"), call_id="call-resume", turn_id="turn-resume"
    )
    claimed = store.claim_next("headless-voice-first")
    assert claimed is not None
    assert store.acknowledge_started(item.item_id, target_sid="headless-voice-first")
    assert store.set_work_session(item.item_id, "persisted-codex-session")
    assert store.finish_work_item(item.item_id, error="interrupted")

    store.request_resume(item.item_id)
    resumed = store.claim_next("headless-voice-resume")

    assert resumed is not None
    assert resumed.brief is not None
    assert store.work_record(item.item_id)["session_id"] == "persisted-codex-session"


def test_cancel_that_stops_a_job_that_has_not_reached_codex_yet(tmp_path) -> None:
    """"cancel that" has to reach a job that is queued and about to run."""

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue_accepted(
        _accepted_brief("queued-cancel-job"), call_id="call-q", turn_id="turn-q"
    )

    store.request_cancel(item.item_id)

    assert store.work_record(item.item_id)["state"] == "cancelled"
    assert store.pending_controls(item.item_id) == []
    assert store.claim_next("headless-voice-late") is None
    assert store.overlay_snapshot(item.item_id)["state"] == "cancelled"


def test_cancel_reaches_a_resume_queued_job_between_attempts(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue_accepted(
        _accepted_brief("resume-cancel-job"), call_id="call-rc", turn_id="turn-rc"
    )
    assert store.claim_next("headless-voice-rc") is not None
    assert store.acknowledge_started(item.item_id, target_sid="headless-voice-rc")
    assert store.set_work_session(item.item_id, "persisted-codex-session")
    assert store.finish_work_item(item.item_id, error="interrupted")
    store.request_resume(item.item_id)
    assert store.work_record(item.item_id)["state"] == "resume_queued"

    store.request_cancel(item.item_id)

    assert store.work_record(item.item_id)["state"] == "cancelled"
    assert store.claim_next("headless-voice-rc-late") is None


def test_a_finished_job_refuses_a_control_instead_of_reviving(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue_accepted(
        _accepted_brief("finished-job"), call_id="call-f", turn_id="turn-f"
    )
    assert store.claim_next("headless-voice-f") is not None
    assert store.acknowledge_started(item.item_id, target_sid="headless-voice-f")
    store.record_evidence(item.item_id, {"complete": True, "captured_at": 12.0})
    assert store.finish_work_item(item.item_id, require_evidence=True)

    with pytest.raises(ValueError, match="not running"):
        store.request_cancel(item.item_id)
    assert store.work_record(item.item_id)["state"] == "completed"


def test_overlay_snapshot_stays_small_enough_for_the_bridge(tmp_path) -> None:
    """An oversized snapshot is dropped in transit, so the panel goes blank."""

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue_accepted(
        _accepted_brief("huge-job"), call_id="call-huge", turn_id="turn-huge"
    )
    store.record_evidence(
        item.item_id,
        {
            "complete": True,
            "captured_at": 12.0,
            "changed_files": [f"src/module_{index}.py" for index in range(900)],
            "errors": [f"error {index}" for index in range(100)],
        },
    )

    snapshot = store.overlay_snapshot(item.item_id)

    assert len(snapshot["changes"]) == 200
    assert snapshot["changes_truncated"] == 700
    assert len(snapshot["evidence"]["errors"]) == 40
    assert len(json.dumps(snapshot).encode("utf-8")) < 55_000


def test_a_thousand_long_paths_shrink_instead_of_blanking_the_panel(tmp_path) -> None:
    """The bridge drops an oversized event whole, so fit it before sending."""

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue_accepted(
        _accepted_brief("enormous-job"), call_id="call-big", turn_id="turn-big"
    )
    store.record_evidence(
        item.item_id,
        {
            "complete": True,
            "captured_at": 12.0,
            "changed_files": [f"src/{'deep/' * 40}module_{index}.py" for index in range(1_200)],
        },
    )

    snapshot = store.overlay_snapshot(item.item_id)

    assert len(json.dumps(snapshot).encode("utf-8")) < 45_000
    assert snapshot["changes"], "some changed files must still be shown"
    assert snapshot["changes_truncated"] == 1_200 - len(snapshot["changes"])
    assert snapshot["state"] == "queued"


def test_completed_state_requires_mechanical_evidence_when_enforced(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue_accepted(
        _accepted_brief("evidence-job"), call_id="call-evidence", turn_id="turn-evidence"
    )
    claimed = store.claim_next("headless-voice-evidence")
    assert claimed is not None
    assert store.acknowledge_started(item.item_id, target_sid="headless-voice-evidence")

    with pytest.raises(ValueError, match="mechanical evidence"):
        store.finish_work_item(item.item_id, require_evidence=True)
    store.record_evidence(item.item_id, {"complete": True, "captured_at": 12.0})
    assert store.finish_work_item(item.item_id, require_evidence=True)


def test_reused_chat_route_and_delivery_receipt_are_durable(tmp_path) -> None:
    """A restart must reconcile a committed prompt instead of sending it twice."""

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    brief = _accepted_brief("reuse-route-job")
    brief["work_route"] = {
        "mode": "reuse",
        "preference": "auto",
        "project_root": "/tmp/project",
        "session_id": "019fcaaa-1111-7222-8333-123456789abc",
        "group_id": "g_exact",
        "bridge_port": 45678,
        "title": "Tightening Serena",
        "reason": "focused exact project chat",
        "bound_focus": True,
    }
    item = store.enqueue_accepted(
        brief,
        call_id="call-route",
        turn_id="turn-route",
    )

    assert store.prepare_route_dispatch(item.item_id, "digest-one")
    assert store.mark_route_dispatch(
        item.item_id,
        "committed",
        start_offset=91,
        prompt_sha256="digest-one",
    )
    reopened = VoiceInboxStore(store.path)

    assert reopened.route_record(item.item_id) == {
        "item_id": item.item_id,
        "mode": "reuse",
        "preference": "auto",
        "project_root": "/tmp/project",
        "session_id": "019fcaaa-1111-7222-8333-123456789abc",
        "group_id": "g_exact",
        "bridge_port": 45678,
        "title": "Tightening Serena",
        "reason": "focused exact project chat",
        "bound_focus": 1,
        "state": "committed",
        "start_offset": 91,
        "end_offset": None,
        "prompt_sha256": "digest-one",
        "updated_at": reopened.route_record(item.item_id)["updated_at"],
    }


def test_busy_uncommitted_reuse_route_recovers_once_on_a_fresh_private_session(
    tmp_path, monkeypatch
) -> None:
    clock = [10_000.0]
    monkeypatch.setattr(voice_inbox.time, "time", lambda: clock[0])
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    brief = _accepted_brief("busy-route-job")
    brief["work_route"] = {
        "mode": "reuse",
        "preference": "auto",
        "project_root": "/tmp/project",
        "session_id": "019fcaaa-1111-7222-8333-busy00000001",
        "bridge_port": 45678,
    }
    item = store.enqueue_accepted(brief, call_id="call-busy", turn_id="turn-busy")
    target = "headless-voice-busy"
    assert store.claim_next(target) is not None
    assert store.acknowledge_started(item.item_id, target_sid=target)
    assert store.set_work_session(item.item_id, brief["work_route"]["session_id"])

    recovery = store.queue_automatic_recovery(
        item.item_id,
        error="runtime has an active turn",
        kind="route",
        max_recoveries=3,
        force_private_route=True,
        drop_uncommitted_session=True,
    )
    duplicate = store.queue_automatic_recovery(
        item.item_id,
        error="runtime has an active turn",
        kind="route",
        max_recoveries=3,
        force_private_route=True,
        drop_uncommitted_session=True,
    )

    assert recovery == {
        "queued": True,
        "reason": "bounded automatic recovery queued",
        "recoveries": 1,
        "budget": 3,
        "private_route": True,
        "resumed_session": False,
    }
    assert duplicate["queued"] is False
    assert "already queued" in duplicate["reason"]
    assert store.route_record(item.item_id)["mode"] == "private"
    assert store.route_record(item.item_id)["bridge_port"] is None
    assert store.work_record(item.item_id)["state"] == "failed"
    assert store.work_record(item.item_id)["session_id"] == ""
    durable = store.latest_automatic_recovery(item.item_id)
    assert durable["state"] == "scheduled"
    assert durable["private_route"] is True
    assert durable["resumed_session"] is False
    assert store.claim_next("headless-voice-private-too-early") is None
    clock[0] += 2.0
    assert store.claim_next("headless-voice-private") is not None


def test_committed_dead_reuse_route_recovers_privately_in_the_same_session(
    tmp_path,
) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    brief = _accepted_brief("dead-route-job")
    session_id = "019fcaaa-1111-7222-8333-dead00000001"
    brief["work_route"] = {
        "mode": "reuse",
        "preference": "auto",
        "project_root": "/tmp/project",
        "session_id": session_id,
        "bridge_port": 45678,
    }
    item = store.enqueue_accepted(brief, call_id="call-dead", turn_id="turn-dead")
    target = "headless-voice-dead"
    assert store.claim_next(target) is not None
    assert store.acknowledge_started(item.item_id, target_sid=target)
    assert store.set_work_session(item.item_id, session_id)
    assert store.prepare_route_dispatch(item.item_id, "digest")
    assert store.mark_route_dispatch(item.item_id, "committed", start_offset=12)

    recovery = store.queue_automatic_recovery(
        item.item_id,
        error="the selected existing-chat Codex runtime is no longer present",
        kind="route",
        max_recoveries=3,
        force_private_route=True,
        drop_uncommitted_session=True,
    )

    assert recovery["queued"] is True
    assert recovery["private_route"] is True
    assert recovery["resumed_session"] is True
    assert store.route_record(item.item_id)["mode"] == "private"
    assert store.route_record(item.item_id)["state"] == "committed"
    assert store.work_record(item.item_id)["state"] == "resume_queued"
    assert store.work_record(item.item_id)["session_id"] == session_id
    durable = store.latest_automatic_recovery(item.item_id)
    assert durable["private_route"] is True
    assert durable["resumed_session"] is True


def test_prestart_failure_becomes_terminal_instead_of_reclaiming_forever(tmp_path) -> None:
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    item = store.enqueue_accepted(
        _accepted_brief("terminal-prestart"),
        call_id="call-prestart",
        turn_id="turn-prestart",
    )
    target = "headless-voice-prestart"
    assert store.claim_next(target) is not None

    assert store.fail_claimed_item(
        item.item_id,
        target_sid=target,
        error="accepted coding job has an invalid frozen policy",
        cwd="/tmp/project",
    )

    assert store.claim_next("headless-voice-again") is None
    snapshot = store.job_snapshot(item.item_id)
    assert snapshot["queue"]["state"] == "delivered"
    assert snapshot["work"]["state"] == "failed"
    assert "invalid frozen policy" in snapshot["work"]["last_error"]


def test_next_reused_turn_cannot_erase_an_unresolved_delivery(tmp_path) -> None:
    """Steering must wait until the prior prompt has a final delivery state."""

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    brief = _accepted_brief("reuse-serial-job")
    brief["work_route"] = {
        "mode": "reuse",
        "preference": "auto",
        "project_root": "/tmp/project",
        "session_id": "019fcaaa-1111-7222-8333-abcdef123456",
        "bridge_port": 45678,
    }
    item = store.enqueue_accepted(brief, call_id="call-serial", turn_id="turn-serial")
    assert store.prepare_route_dispatch(item.item_id, "first")
    assert store.mark_route_dispatch(item.item_id, "committed", start_offset=12)

    assert store.prepare_route_dispatch(item.item_id, "second") is False
    route = store.route_record(item.item_id)
    assert route["prompt_sha256"] == "first"
    assert route["start_offset"] == 12


def _completed_attempt(
    store: VoiceInboxStore,
    item_id: str,
    *,
    root: str,
    provider: str,
    session_id: str,
    state: str = "completed",
    exit_code: int = 0,
    work_state: str = "completed",
) -> None:
    queued = store.enqueue_accepted(
        _accepted_brief(item_id, root),
        call_id=f"call-{item_id}",
        turn_id=f"call-{item_id}:1",
    )
    target = f"headless-voice-{item_id}"
    claimed = store.claim_next(target)
    assert claimed is not None and claimed.item_id == queued.item_id
    assert store.acknowledge_started(item_id, target_sid=target, cwd=root)
    attempt_id, _number = store.start_attempt(
        item_id, provider=provider, model="m", effort="high"
    )
    store.set_attempt_session(attempt_id, session_id)
    store.finish_attempt(attempt_id, state=state, exit_code=exit_code)
    if work_state == "completed":
        store.record_evidence(item_id, {"complete": True})
    store.finish_work_item(
        item_id,
        state=work_state,
        error="final gate failed" if work_state == "failed" else "",
    )


def test_a_warm_session_is_found_only_for_the_same_repo_and_provider(tmp_path) -> None:
    """Orientation is repaid on every cold start, so a warm session is worth
    finding. It is only worth reusing when it cannot leak across a boundary."""

    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    _completed_attempt(
        store, "old-job", root="/tmp/project", provider="claude", session_id="warm-old"
    )
    _completed_attempt(
        store, "new-job", root="/tmp/project", provider="claude", session_id="warm-new"
    )
    _completed_attempt(
        store, "other-repo", root="/tmp/other", provider="claude", session_id="elsewhere"
    )
    _completed_attempt(
        store, "codex-job", root="/tmp/project", provider="codex", session_id="codex-thread"
    )
    _completed_attempt(
        store,
        "failed-job",
        root="/tmp/project",
        provider="claude",
        session_id="half-written",
        state="failed",
    )
    _completed_attempt(
        store,
        "nonzero-job",
        root="/tmp/project",
        provider="claude",
        session_id="nonzero",
        exit_code=1,
    )
    _completed_attempt(
        store,
        "failed-after-attempt",
        root="/tmp/project",
        provider="claude",
        session_id="passed-worker-but-failed-gate",
        work_state="failed",
    )

    warm = store.warm_session_for_project(
        "/tmp/project", provider="claude", max_age_seconds=3_600
    )
    assert warm is not None
    assert warm["session_id"] == "warm-new"
    assert store.attempt_provider_for_session("new-job", "warm-new") == "claude"
    assert store.attempt_provider_for_session("new-job", "codex-thread") == ""

    # Never across repositories, and never another provider's session id.
    assert (
        store.warm_session_for_project(
            "/tmp/nothing-here", provider="claude", max_age_seconds=3_600
        )
        is None
    )
    codex_warm = store.warm_session_for_project(
        "/tmp/project", provider="codex", max_age_seconds=3_600
    )
    assert codex_warm["session_id"] == "codex-thread"

    # A stale session's picture of the tree is worse than none.
    assert (
        store.warm_session_for_project(
            "/tmp/project", provider="claude", max_age_seconds=0
        )
        is None
    )

    # And a job never warms itself from its own earlier attempt here; that is
    # the resume path, which carries its own session id.
    self_excluded = store.warm_session_for_project(
        "/tmp/project",
        provider="claude",
        max_age_seconds=3_600,
        exclude_item_id="new-job",
    )
    assert self_excluded["session_id"] == "warm-old"
