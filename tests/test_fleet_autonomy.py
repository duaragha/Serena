"""Deterministic fault injection against real durable Fleet stores and schedulers."""
import os
import json
import pytest
from test_fleet_peers import team  # noqa: F401
from fleet.collaboration import worker_key
from fleet.learning import FleetLearning
from fleet.lesson_review import review_final_lessons
from fleet.workers import WorkerResult

from fleet.store import FleetStore
from fleet.supervision import FleetSupervisionStore
from fleet.workers import _event_summary
from test_fleet_supervision import _run


def test_dead_thread_recovers_without_replaying_completed_work(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    rid = run["run_id"]
    assert store.claim_run(rid)
    first = store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    store.finish_attempt(first["attempt_id"], state="completed", output_text="preserved")
    leg = run["phases"][1]["legs"][0]
    attempt = store.begin_attempt(leg["leg_id"])
    supervision = FleetSupervisionStore(store.path)
    lease = supervision.acquire(attempt["attempt_id"])
    assert store.recover_stale_runs() == []  # current process is alive
    assert store.recover_stale_runs(dead_threads={rid}) == [rid]
    recovered = store.get_run(rid)
    assert recovered["state"] == "queued"
    assert recovered["phases"][0]["legs"][0]["state"] == "completed"
    assert recovered["phases"][1]["legs"][0]["state"] == "queued"
    assert not supervision.heartbeat(attempt["attempt_id"], lease.lease_token)
    assert store.recover_stale_runs(dead_threads={rid}) == []
    for expected in ("queued", "failed"):
        assert store.claim_run(rid)
        assert store.recover_stale_runs(dead_threads={rid}) == [rid]
        assert store.get_run(rid)["state"] == expected


def test_orphan_recovery_preserves_cancel_and_refuses_unowned_process(tmp_path, monkeypatch):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    rid = run["run_id"]
    store.claim_run(rid)
    attempt = store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    store.mark_attempt_process(attempt["attempt_id"], os.getpid(), "")
    monkeypatch.setattr("fleet.store._terminate_owned_process", lambda *args: False)
    assert store.recover_stale_runs(dead_threads={rid}) == []
    assert store.get_run(rid)["state"] == "running"
    store.request_cancel(rid)
    monkeypatch.setattr("fleet.store._terminate_owned_process", lambda *args: True)
    assert store.recover_stale_runs(dead_threads={rid}) == [rid]
    assert store.get_run(rid)["state"] == "cancelled"


def test_staged_progress_and_hard_turn_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SERENA_FLEET_TURN_SECONDS", "70")
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    attempt = store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    s = FleetSupervisionStore(store.path)
    lease = s.acquire(attempt["attempt_id"], now=100, stall_seconds=60)
    for now, stage in ((121, "warning"), (141, "suspect")):
        assert not s.mark_stalled(attempt["attempt_id"], lease.lease_token, now=now)
        assert s.project_run(run["run_id"], now=now)["workers"][0]["progress_stage"] == stage
    assert s.heartbeat(attempt["attempt_id"], lease.lease_token, progress=True, now=150)
    assert s.mark_stalled(attempt["attempt_id"], lease.lease_token, now=171)
    assert "turn budget" in s.project_run(run["run_id"])["workers"][0]["recovery_reason"]
    events = [e["type"] for e in store.events(run["run_id"])]
    assert events.count("worker.progress.warning") == 1
    assert events.count("worker.progress.suspect") == 1
    assert events.count("worker.progress.recovery") == 1


def test_native_chatter_does_not_reset_progress():
    assert not _event_summary({"type": "item.updated", "item": {"type": "reasoning"}})["progress"]
    assert not _event_summary({"type": "assistant", "message": {"content": [{"type": "thinking"}]}})["progress"]
    assert _event_summary({"type": "item.completed", "item": {"type": "command_execution"}})["progress"]
    assert _event_summary({"type": "user", "message": {"content": [{"type": "tool_result"}]}})["progress"]


def test_reply_is_not_resolution_and_only_requester_can_close(team):
    store, run, peers, legs, attempts, tokens = team
    job = peers.request_help(tokens[0], worker_key(legs[1]), "Check the parser failure", dedupe="failure")
    mid = job["message_id"]
    peers.send(tokens[1], worker_key(legs[0]), "Check the empty input case", dedupe="reply", reply_to=mid)
    assert peers.projection(run["run_id"])["messages"][0]["outcome"] == "answered"
    peers.inbox(tokens[1], acknowledge=[mid])
    with pytest.raises(PermissionError):
        peers.resolve_request(tokens[1], mid, resolved=True, reason="I answered so it must be fixed")
    peers.resolve_request(tokens[0], mid, resolved=True, reason="Applied suggestion and parser tests pass")
    peers.reconcile_outcomes(run["run_id"], terminal=True)
    assert peers.projection(run["run_id"])["messages"][0]["outcome"] == "resolved"


def test_unavailable_peer_escalates_once_and_late_reply_cannot_claim_success(team):
    store, run, peers, legs, attempts, tokens = team
    job = peers.request_help(tokens[0], worker_key(legs[1]), "Check this failure please", dedupe="timeout")
    for _ in range(2):
        peers.reconcile_outcomes(run["run_id"], now=job["deadline"] + 1)
    peers.send(tokens[1], worker_key(legs[0]), "Late answer", dedupe="late", reply_to=job["message_id"])
    assert peers.projection(run["run_id"])["messages"][0]["outcome"] == "escalated"
    assert len([e for e in store.events(run["run_id"]) if e["type"] == "peer.request.escalated"]) == 1


def final_lesson(team, tmp_path):
    store, run, peers, legs, attempts, tokens = team
    (tmp_path / "rules.txt").write_text("empty input returns an empty list\n")
    learning = FleetLearning(store)
    # Create the candidate during Fix, AFTER all normal Review workers completed.
    for phase in run["phases"][:-1]:
        for leg in phase["legs"]:
            a = next((a for a, l in zip(attempts, legs) if l["leg_id"] == leg["leg_id"]), None)
            a = a or store.begin_attempt(leg["leg_id"])
            store.finish_attempt(a["attempt_id"], state="completed", output_text="fixture completed")
    for leg in run["phases"][-1]["legs"]:
        a = store.begin_attempt(leg["leg_id"])
        if leg == run["phases"][-1]["legs"][0]:
            token = peers.issue(run["run_id"], leg, a["attempt_id"])
            lesson = learning.propose(peers.identity(token), "Empty input must return an empty list before indexing.", ["rules.txt"])
        store.finish_attempt(a["attempt_id"], state="completed", output_text="fixture fixed")
    return store, store.get_run(run["run_id"]), learning, lesson


@pytest.mark.parametrize("outcome", ["approve", "reject", "malformed", "capacity", "changed", "cancelled", "wrong_model"])
def test_post_fix_independent_review_and_promotion(team, tmp_path, outcome):
    store, run, learning, lesson = final_lesson(team, tmp_path)
    calls = []
    def reviewer(request, *, cancel_requested, on_event):
        calls.append(request)
        assert request.access_mode == "read" and request.resume_session_id is None
        assert request.worker_key.startswith("lesson-reviewer:") and not request.peer_token
        if outcome == "cancelled":
            store.request_cancel(run["run_id"])
        text = '<serena-lesson-review>' + json.dumps({"reviews": [{"id": lesson["lesson_id"],
                 "approve": outcome != "reject", "reason": "rules.txt:1 confirms the empty input guard."}]}) + '</serena-lesson-review>'
        return WorkerResult(True, "bad" if outcome == "malformed" else text, "review-session",
                            "wrong" if outcome == "wrong_model" else request.model, request.effort, 0)
    if outcome == "changed":
        (tmp_path / "rules.txt").write_text("different evidence")
    capacity = {"codex": {"usable": outcome != "capacity"}, "claude": {"usable": outcome != "capacity"}}
    review_final_lessons(store, run["run_id"], runner=reviewer, capacity=capacity)
    review_final_lessons(store, run["run_id"], runner=reviewer, capacity=capacity)
    assert len(calls) <= 1  # never rerun a finished optional review
    terminal = {**store.get_run(run["run_id"]), "state": "completed"}
    learning.finish(terminal)
    # Endorsement alone cannot promote, even after a successful native review.
    assert learning.projection(run["run_id"])["candidates"][0]["state"] != "verified"
    store.append_event(run["run_id"], "worker.integration.accepted", {"test_gate": {"ran": True, "ok": True}})
    learning.finish(terminal)
    state = learning.projection(run["run_id"])["candidates"][0]["state"]
    assert state == ("verified" if outcome == "approve" else "rejected" if outcome in {"reject", "changed"} else "candidate")
    if outcome == "approve":
        assert learning.retrieve(run, "held-out-attempt")
