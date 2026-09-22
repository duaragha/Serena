"""Cross-run lifecycle proof using private stores and synthetic repositories."""
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_fleet_supervisor import fleet_env  # noqa: F401

from fleet import incidents
from fleet.collaboration import PeerStore, worker_key
from fleet.learning import FleetLearning
from fleet.peer_runtime import PeerCoordinator
from fleet.policy import build_policy, builtin_config
from fleet.project_identity import canonical_remote, repository_identity
from fleet.store import FleetStore
from fleet.workers import WorkerResult, _event_summary


def repo(path, remote="https://github.com/example/shared.git"):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "config", "remote.origin.url", remote], check=True)
    (path / "rules.txt").write_text("Build generated declarations before typecheck.\n", encoding="utf-8")
    return path


def run_at(store, path, task="Repair missing generated declarations before typecheck", worker_count=2):
    run = store.create_run(task=task, activity="coding", cwd=str(path), origin_session_id=None,
                           origin_agent=None, dry_run=False,
                           policy=build_policy("coding", config=builtin_config(), worker_count=worker_count).to_dict())
    leg = run["phases"][0]["legs"][0]
    attempt = store.begin_attempt(leg["leg_id"])
    peers = PeerStore(store)
    token = peers.issue(run["run_id"], leg, attempt["attempt_id"])
    return run, leg, attempt, token


@pytest.fixture
def shared(tmp_path, monkeypatch):
    from fleet import supervisor
    monkeypatch.setattr(supervisor, "_read_start_capacity", lambda: {"codex": {"usable": True}, "claude": {"usable": True}})
    store = FleetStore(tmp_path / "fleet.sqlite3")
    a = run_at(store, repo(tmp_path / "a"))
    b = run_at(store, repo(tmp_path / "b", "git@github.com:example/shared.git"))
    c = run_at(store, repo(tmp_path / "c", "https://github.com/example/unrelated.git"))
    return store, PeerStore(store), a, b, c


def test_identity(tmp_path):
    assert canonical_remote("git@github.com:Example/Shared.git") == canonical_remote("https://github.com/example/shared")
    assert canonical_remote("ssh://git@github.com/example/shared.git") == "git:github.com/example/shared"
    for value in ("https://secret@github.com/a/b", "https://a:b@github.com/a/b", "ssh://bad@github.com/a/b", "https://github.com/a/b?token=x"):
        assert canonical_remote(value) is None
    a, b = repo(tmp_path / "a", ""), repo(tmp_path / "b", "")
    assert repository_identity(str(a)) != repository_identity(str(b))
    # Git's .git indirection represents a shared common repository, not the path spelling.
    sibling = tmp_path / "s"
    sibling.mkdir()
    (sibling / ".git").write_text("gitdir: " + str(a / ".git") + "\n", encoding="utf-8")
    assert repository_identity(str(a)) == repository_identity(str(sibling))


def failure_event(store, run, leg, attempt, item="command-1"):
    summary = _event_summary({"type": "item.completed", "item": {"id": item,
        "type": "command_execution", "command": "typecheck generated declarations", "exit_code": 2,
        "status": "completed"}})
    store.append_event(run["run_id"], "worker.event", summary, leg_id=leg["leg_id"], attempt_id=attempt["attempt_id"])


def test_event_capture_restart_backfill_recovery_and_recall(shared, monkeypatch):
    store, peers, a, b, c = shared
    run, leg, attempt, _ = a
    failure_event(store, run, leg, attempt)
    failure_event(store, run, leg, attempt)
    assert len(incidents.report(store, run["cwd"])["incidents"]) == 1
    original = incidents.capture
    monkeypatch.setattr(incidents, "capture", lambda *args: (_ for _ in ()).throw(RuntimeError("broken learning write")))
    failure_event(store, run, leg, attempt, "command-2")
    assert store.get_run(run["run_id"])
    monkeypatch.setattr(incidents, "capture", original)
    restarted = FleetStore(store.path)
    store.append_event(run["run_id"], "worker.integration.accepted", {"test_gate": {"ran": True, "ok": True}},
                       leg_id=leg["leg_id"], attempt_id=attempt["attempt_id"])
    rows = incidents.recall(restarted, b[0], b[2]["attempt_id"])
    assert len(rows) == 2 and all(r["recovery_event"] for r in rows)
    assert incidents.recall(restarted, c[0], c[2]["attempt_id"]) == []
    assert incidents.report(restarted, b[0]["cwd"])["incidents"][0]["recurrence"] == 2
    assert incidents.report(restarted, b[0]["cwd"])["supplied_advice"] == 2


def test_cross_run_mail_usefulness_restart_and_fencing(shared):
    store, peers, a, b, c = shared
    assert any(r["run_id"] == a[0]["run_id"] for r in peers.discover(b[3], "declarations"))
    assert all(r["run_id"] != c[0]["run_id"] for r in peers.discover(b[3]))
    request = peers.request_help(b[3], worker_key(a[1]), "How did generated declarations recover?", dedupe="q", target_run=a[0]["run_id"], evidence_paths=["rules.txt"])
    incoming = peers.inbox(a[3])["messages"][0]
    assert incoming["run_id"] == b[0]["run_id"] and incoming["evidence"] != "{}"
    def reply(_):
        return peers.send(a[3], worker_key(b[1]), "Build the declarations, then rerun typecheck.", dedupe="reply", kind="reply", reply_to=incoming["id"], target_run=b[0]["run_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(reply, range(2)))
    assert replies[0]["id"] == replies[1]["id"]
    restarted = PeerStore(FleetStore(store.path))
    assert restarted.inbox(b[3])["messages"][0]["id"] == replies[0]["id"]
    outcome = restarted.resolve_request(b[3], incoming["id"], resolved=True, reason="Build and typecheck passed against the evidence.")
    assert outcome["outcome"] == "resolved"
    with pytest.raises(PermissionError):
        peers.send(c[3], worker_key(a[1]), "unrelated", dedupe="bad", target_run=a[0]["run_id"])
    with pytest.raises(PermissionError):
        peers.resolve_request(a[3], incoming["id"], resolved=True, reason="I cannot confirm another worker's outcome")
    store.finish_attempt(a[2]["attempt_id"], state="completed", output_text="done")
    with pytest.raises(PermissionError):
        peers.discover(a[3])
    assert request["helper_run"] == a[0]["run_id"]


def test_ended_turn_consultation_is_read_only_and_bounded(shared):
    store, peers, a, b, _ = shared
    job = peers.request_help(b[3], worker_key(a[1]), "Explain declaration build evidence", dedupe="consult", target_run=a[0]["run_id"])
    store.finish_attempt(a[2]["attempt_id"], state="completed", output_text="previous evidence")
    requests = []
    def runner(request, *, cancel_requested, on_event):
        requests.append(request)
        assert request.access_mode == "read" and request.cwd == a[0]["cwd"]
        assert not cancel_requested()
        return WorkerResult(True, "Build generated declarations, then test typecheck.", "synthetic", request.model, request.effort, 0)
    with PeerCoordinator(store, b[0]["run_id"], runner=runner) as coordinator:
        coordinator.pump()
        coordinator.future.result(timeout=30)
        coordinator.pump()
    assert len(requests) == 1
    assert peers.projection(b[0]["run_id"])["help"][0]["state"] == "answered"
    assert peers.inbox(b[3])["messages"][0]["reply_to"] == job["message_id"]
    peers.reconcile_outcomes(b[0]["run_id"], now=time.time() + 301)
    assert peers.inbox(b[3])["requests"][0]["outcome"] == "escalated"


def test_incident_to_reviewed_lesson_to_natural_language_reuse(shared):
    store, peers, a, b, c = shared
    run, leg, attempt, token = a
    failure_event(store, run, leg, attempt)
    incident = incidents.recall(store, run, attempt["attempt_id"])[0]
    learning = FleetLearning(store)
    lesson = learning.propose(peers.identity(token), "Build generated declarations before typecheck; verify the build and typecheck outputs.", ["rules.txt"], [incident["id"]])
    assert learning.retrieve(b[0], b[2]["attempt_id"]) == []
    reviewer = run["phases"][2]["legs"][1]
    ra = store.begin_attempt(reviewer["leg_id"])
    rt = peers.issue(run["run_id"], reviewer, ra["attempt_id"])
    learning.review(peers.identity(rt), lesson["lesson_id"], True, "Independently inspected rules.txt and the observed command evidence.")
    store.finish_attempt(attempt["attempt_id"], state="completed", output_text="built and checked")
    store.finish_attempt(ra["attempt_id"], state="completed", output_text="reviewed")
    terminal = {**store.get_run(run["run_id"]), "state": "completed"}
    learning.finish(terminal)
    assert learning.retrieve(b[0], b[2]["attempt_id"]) == []
    store.append_event(run["run_id"], "worker.integration.accepted", {"test_gate": {"ran": True, "ok": True}}, leg_id=leg["leg_id"], attempt_id=attempt["attempt_id"])
    learning.finish(terminal)
    assert learning.retrieve(b[0], b[2]["attempt_id"])[0]["id"] == lesson["lesson_id"]
    assert learning.retrieve(c[0], c[2]["attempt_id"]) == []
    store.request_cancel(run["run_id"])
    assert learning.retrieve(b[0], "after-cancellation") == []
    learning.rollback(lesson["lesson_id"], "Evidence no longer supports this advice")
    assert learning.retrieve(b[0], "after-revocation") == []


def test_provider_errors_redacted_and_completion_rejections_captured(shared):
    store, _, a, _, _ = shared
    for kind, payload in (("worker.event", _event_summary({"type": "error", "error": "api_key=supersecret HTTP 529"})),
                          ("leg.completion_evidence_rejected", {"accepted": False, "reason": "missing tests"}),
                          ("run.recovery_exhausted", {"error": "recovery budget exhausted"})):
        store.append_event(a[0]["run_id"], kind, payload, leg_id=a[1]["leg_id"], attempt_id=a[2]["attempt_id"])
    rows = incidents.report(store, a[0]["cwd"])["incidents"]
    assert len(rows) == 3
    assert "supersecret" not in json.dumps(rows)
    assert {r["category"] for r in rows} == {"gate", "recovery", "provider_or_runtime"}


def test_proactive_finding_recall_feedback_and_changed_evidence(shared):
    from fleet.findings import feedback, publish, recall
    store, peers, a, b, c = shared
    finding = publish(peers, a[3], "Build generated declarations before typecheck to satisfy workspace prerequisites.", ["rules.txt"], "build")
    assert publish(peers, a[3], "Build generated declarations before typecheck to satisfy workspace prerequisites.", ["rules.txt"], "build")["id"] == finding["id"]
    assert recall(peers, c[3]) == []
    assert recall(peers, b[3])[0]["state"] == "unverified_advice"
    feedback(peers, b[3], finding["id"], True, "The build then typecheck succeeded locally.")
    assert recall(PeerStore(FleetStore(store.path)), b[3])[0]["useful_count"] == 1
    with pytest.raises(PermissionError):
        feedback(peers, c[3], finding["id"], True, "Foreign project cannot confirm this")
    from pathlib import Path
    (Path(b[0]["cwd"]) / "rules.txt").write_text("changed evidence", encoding="utf-8")
    assert recall(peers, b[3]) == []


def test_supervisor_hook_captures_then_prompts_reflection(fleet_env, monkeypatch):  # noqa: F811
    from fleet import supervisor
    observed = []
    def runner(request, *, cancel_requested, on_event):
        assert "Reflect on observed failures" in request.prompt
        observed.append(request)
        on_event("worker.event", _event_summary({"type": "item.completed", "item": {
            "id": "synthetic-probe", "type": "command_execution", "command": "typecheck generated declarations", "exit_code": 2}}))
        # Exercise the authenticated reflection/proposal path within a real
        # supervisor turn after its tool callback has persisted the incident.
        store = FleetStore()
        peers = PeerStore(store)
        who = peers.identity(request.peer_token)
        rows = incidents.recall(store, store.get_run(request.run_id), request.attempt_id)
        assert rows
        FleetLearning(store).propose(who, "Build generated declarations then run typecheck against rules.txt.", ["rules.txt"], [rows[0]["id"]])
        return WorkerResult(True, "synthetic completed", None, request.model, request.effort, 0)
    (fleet_env / "rules.txt").write_text("Build generated declarations before typecheck.\n", encoding="utf-8")
    monkeypatch.setattr(supervisor, "run_worker", runner)
    store = FleetStore()
    run = store.create_run(task="Repair declarations", activity="coding", cwd=str(fleet_env), origin_session_id=None,
                           origin_agent=None, dry_run=False,
                           policy=build_policy("coding", config=builtin_config(), worker_count=1).to_dict())
    result = supervisor._execute_leg(store, run["run_id"], run["phases"][0]["legs"][0])
    assert result.ok and len(observed) == 1
    projection = FleetLearning(FleetStore(store.path)).projection(run["run_id"])
    assert len(projection["incidents"]) == 1
    assert projection["candidates"][0]["state"] == "candidate"
    assert projection["incident_links"]


def test_expired_project_changed_and_cancelled_capabilities(shared):
    store, peers, a, b, c = shared
    peers.request_help(b[3], worker_key(a[1]), "Explain the declaration recovery", dedupe="q", target_run=a[0]["run_id"])
    store.request_cancel(a[0]["run_id"])
    with pytest.raises(PermissionError):
        peers.send(a[3], worker_key(b[1]), "late", dedupe="late", target_run=b[0]["run_id"])
    with store._connect() as db:
        db.execute("UPDATE fleet_peer_tokens SET expires=0 WHERE run_id=?", (c[0]["run_id"],))
    with pytest.raises(PermissionError):
        peers.discover(c[3])
    subprocess.run(["git", "-C", b[0]["cwd"], "config", "remote.origin.url", "https://github.com/example/new.git"], check=True)
    with pytest.raises(PermissionError):
        peers.inbox(b[3])


def test_unavailable_incident_schema_preserves_journal_then_recovers(tmp_path, monkeypatch):
    import sqlite3
    original = incidents.initialize
    monkeypatch.setattr(incidents, "initialize", lambda db: (_ for _ in ()).throw(sqlite3.OperationalError("projection unavailable")))
    store = FleetStore(tmp_path / "broken.sqlite3")
    run, leg, attempt, _ = run_at(store, repo(tmp_path / "r"))
    failure_event(store, run, leg, attempt)
    assert store.has_event(run["run_id"], "worker.event")
    monkeypatch.setattr(incidents, "initialize", original)
    recovered = FleetStore(store.path)
    assert len(incidents.report(recovered, run["cwd"])["incidents"]) == 1


def test_missing_incident_blocks_promotion(shared):
    store, peers, a, _, _ = shared
    failure_event(store, a[0], a[1], a[2])
    row = incidents.recall(store, a[0], a[2]["attempt_id"])[0]
    lesson = FleetLearning(store).propose(peers.identity(a[3]), "Build generated declarations before typecheck.", ["rules.txt"], [row["id"]])
    with store._connect() as db:
        db.execute("DELETE FROM fleet_incidents WHERE id=?", (row["id"],))
        assert not incidents.links_valid(db, lesson["lesson_id"], repository_identity(a[0]["cwd"]))


def test_prompt_rejects_changed_source_project(shared):
    _, peers, a, b, _ = shared
    peers.send(a[3], worker_key(b[1]), "ROOT-CROSS-PROJECT-MARKER",
               dedupe="boundary", target_run=b[0]["run_id"])
    subprocess.run(["git", "-C", a[0]["cwd"], "config", "remote.origin.url",
                    "https://github.com/example/changed.git"], check=True)
    assert not peers.inbox(b[3])["messages"]
    assert "ROOT-CROSS-PROJECT-MARKER" not in peers.prompt(b[0], b[1])


def test_backfill_after_source_checkout_deleted(shared, monkeypatch, tmp_path):
    store, _, a, b, _ = shared
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise RuntimeError("synthetic projection outage")
        patch.setattr(incidents, "capture", unavailable)
        failure_event(store, a[0], a[1], a[2])
    # Only this test's synthetic checkout may be removed.
    from pathlib import Path
    source = Path(a[0]["cwd"]).resolve()
    assert source.is_relative_to(tmp_path.resolve())
    shutil.rmtree(source)
    restarted = FleetStore(store.path)
    assert len(incidents.recall(restarted, b[0], b[2]["attempt_id"])) == 1
    incidents.reconcile(restarted)
    assert len(incidents.recall(restarted, b[0], b[2]["attempt_id"])) == 1
    assert incidents.report(restarted, a[0]["cwd"])["total_incidents"] == 1


def test_failure_diagnostics_are_redacted_bounded_and_failure_only():
    item = {"id": "native-command", "type": "command_execution", "exit_code": 1,
            "aggregated_output": "Missing declarations token=super-secret-value " + "x" * 10000}
    failed = _event_summary({"type": "item.completed", "item": item})
    assert failed["item_id"] == "native-command"
    assert "Missing declarations" in failed["failure_excerpt"]
    assert "super-secret-value" not in failed["failure_excerpt"]
    assert len(failed["failure_excerpt"]) <= 1500
    item["exit_code"] = 0
    assert "failure_excerpt" not in _event_summary({"type": "item.completed", "item": item})


def test_savepoint_outage_is_optional():
    class BrokenConnection:
        def execute(self, *args):
            raise RuntimeError("savepoint unavailable")
    assert incidents.capture_safely(BrokenConnection(), {}) is False


def test_repository_validation_reports_redacted_exit_and_stderr(tmp_path, monkeypatch):
    from core import coding_job_contract as contract
    monkeypatch.setattr(contract, "_git", lambda *args, **kwargs: subprocess.CompletedProcess(
        [], 128, "", "fatal: detected dubious ownership token=super-secret-value " + "x" * 2000))
    with pytest.raises(contract.RepositoryResolutionError) as error:
        contract.validate_repository_root(tmp_path)
    assert "git exit 128" in str(error.value) and "dubious ownership" in str(error.value)
    assert "super-secret-value" not in str(error.value)
    assert len(str(error.value)) < 1500


def test_cross_run_consultant_different_roster_sizes(shared, tmp_path):
    store, peers, a, _, _ = shared
    requester = run_at(store, repo(tmp_path / "single"), worker_count=1)
    helper = a[0]["phases"][0]["legs"][1]
    started = store.begin_attempt(helper["leg_id"])
    store.finish_attempt(started["attempt_id"], state="completed", output_text="prior evidence")
    assert worker_key(helper) == "agent:b"
    job = peers.request_help(requester[3], "agent:b", "Explain declaration build evidence",
                             dedupe="different-rosters", target_run=a[0]["run_id"])
    requests = []
    def runner(request, *, cancel_requested, on_event):
        requests.append(request)
        assert request.access_mode == "read" and request.cwd == a[0]["cwd"]
        assert not cancel_requested()
        return WorkerResult(True, "Inspect generated declarations and rerun checks.",
                            "synthetic", request.model, request.effort, 0)
    with PeerCoordinator(store, requester[0]["run_id"], runner=runner) as coordinator:
        coordinator.pump()
        coordinator.future.result(timeout=30)
        coordinator.pump()
    assert len(requests) == 1
    assert peers.projection(requester[0]["run_id"])["help"][0]["state"] == "answered"
    assert peers.inbox(requester[3])["messages"][0]["reply_to"] == job["message_id"]


def test_recall_and_reconcile_outage_are_optional(shared, monkeypatch):
    store, _, a, _, _ = shared
    def unavailable(*args):
        raise RuntimeError("projection connection unavailable")
    monkeypatch.setattr(store, "_connect", unavailable)
    incidents.reconcile(store)
    assert incidents.recall(store, a[0], a[2]["attempt_id"]) == []


@pytest.mark.parametrize("legacy", [False, True])
def test_event_project_survives_remote_change_outage_and_cleanup(shared, monkeypatch, tmp_path, legacy):
    from pathlib import Path

    store, _, a, b, c = shared
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise RuntimeError("synthetic projection outage")
        patch.setattr(incidents, "capture", unavailable)
        failure_event(store, a[0], a[1], a[2], "old-project")
        if legacy:
            with store._connect() as db:
                db.execute("UPDATE fleet_events SET repository_identity=NULL WHERE run_id=?", (a[0]["run_id"],))
        subprocess.run(["git", "-C", a[0]["cwd"], "config", "remote.origin.url",
                        "https://github.com/example/unrelated.git"], check=True)
        failure_event(store, a[0], a[1], a[2], "new-project")
        store.append_event(a[0]["run_id"], "worker.integration.accepted",
                           {"test_gate": {"ran": True, "ok": True}},
                           leg_id=a[1]["leg_id"], attempt_id=a[2]["attempt_id"])
    source = Path(a[0]["cwd"]).resolve()
    assert source.is_relative_to(tmp_path.resolve())
    shutil.rmtree(source)
    restarted = FleetStore(store.path)
    old = incidents.recall(restarted, b[0], b[2]["attempt_id"])
    new = incidents.recall(restarted, c[0], c[2]["attempt_id"])
    assert len(old) == len(new) == 1
    assert "old-project" in old[0]["summary"] and old[0]["recovery_event"] is None
    assert "new-project" in new[0]["summary"] and new[0]["recovery_event"] is not None
    incidents.reconcile(restarted)
    with restarted._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM fleet_incidents").fetchone()[0] == 2


def test_identity_distinguishes_unavailable_metadata_from_changed_remote(tmp_path, monkeypatch):
    from fleet.project_identity import project_identity

    root = repo(tmp_path / "identity")
    original = repository_identity(str(root))
    run = {"cwd": str(root), "repository_identity": original}
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", lambda *args, **kwargs:
                      subprocess.CompletedProcess([], 128, "", "metadata unavailable"))
        assert project_identity(run) == original
    subprocess.run(["git", "-C", str(root), "config", "remote.origin.url",
                    "https://github.com/example/changed.git"], check=True)
    assert project_identity(run) == "git:github.com/example/changed"
    subprocess.run(["git", "-C", str(root), "config", "--unset", "remote.origin.url"], check=True)
    assert project_identity(run).startswith("local:")
    assert project_identity(run) != original


@pytest.mark.parametrize("source_change", ["remote", "deleted", "remote_deleted"])
def test_findings_and_verified_lessons_revalidate_source(shared, tmp_path, source_change):
    from pathlib import Path

    from fleet.findings import publish, recall

    store, peers, a, b, _ = shared
    finding = publish(peers, a[3], "Build generated declarations before typecheck.", ["rules.txt"], "source-check")
    learning = FleetLearning(store)
    lesson = learning.propose(peers.identity(a[3]), "Build generated declarations before typecheck.", ["rules.txt"])
    reviewer = a[0]["phases"][2]["legs"][1]
    attempt = store.begin_attempt(reviewer["leg_id"])
    token = peers.issue(a[0]["run_id"], reviewer, attempt["attempt_id"])
    learning.review(peers.identity(token), lesson["lesson_id"], True, "Inspected the declaration rules.txt evidence.")
    store.finish_attempt(a[2]["attempt_id"], state="completed", output_text="done")
    store.finish_attempt(attempt["attempt_id"], state="completed", output_text="reviewed")
    store.append_event(a[0]["run_id"], "worker.integration.accepted",
                       {"test_gate": {"ran": True, "ok": True}},
                       leg_id=a[1]["leg_id"], attempt_id=a[2]["attempt_id"])
    learning.finish({**store.get_run(a[0]["run_id"]), "state": "completed"})
    assert recall(peers, b[3])[0]["id"] == finding["id"]
    assert learning.retrieve(b[0], "before-change")[0]["id"] == lesson["lesson_id"]
    if source_change.startswith("remote"):
        subprocess.run(["git", "-C", a[0]["cwd"], "config", "remote.origin.url",
                        "https://github.com/example/changed.git"], check=True)
        failure_event(store, a[0], a[1], a[2], "changed-source")
    if source_change.endswith("deleted"):
        source = Path(a[0]["cwd"]).resolve()
        assert source.is_relative_to(tmp_path.resolve())
        shutil.rmtree(source)
    restarted = FleetStore(store.path)
    found = recall(PeerStore(restarted), b[3])
    lessons = FleetLearning(restarted).retrieve(b[0], "after-change")
    if source_change.startswith("remote"):
        assert found == lessons == []
    else:
        assert found[0]["id"] == finding["id"]
        assert lessons[0]["id"] == lesson["lesson_id"]


def test_consultation_incidents_separate_jobs_and_restart_dispatches(shared):
    store, peers, a, b, _ = shared
    calls = []

    class ServiceExit(BaseException):
        pass

    def runner(request, *, cancel_requested, on_event):
        calls.append(request)
        event = _event_summary({"type": "item.completed", "item": {
            "id": "item_0", "type": "command_execution", "command": "typecheck declarations", "exit_code": 1}})
        # Duplicate transport delivery within one dispatch must remain idempotent.
        on_event("worker.event", event)
        on_event("worker.event", event)
        if len(calls) == 1:
            raise ServiceExit("synthetic abrupt service exit")
        return WorkerResult(True, "Inspect declarations before typecheck.", "synthetic", request.model, request.effort, 0)

    first = peers.request_help(b[3], worker_key(a[1]), "Explain declarations", dedupe="first", target_run=a[0]["run_id"])
    interrupted = PeerCoordinator(store, b[0]["run_id"], runner=runner)
    try:
        interrupted.pump()
        with pytest.raises(ServiceExit):
            interrupted.future.result(timeout=30)
    finally:
        # Simulate loss of the coordinator without its normal cancellation path.
        interrupted.pool.shutdown(wait=True)
    with PeerCoordinator(FleetStore(store.path), b[0]["run_id"], runner=runner) as restarted:
        restarted.pump()
        restarted.future.result(timeout=30)
        restarted.pump()
        second = peers.request_help(b[3], worker_key(a[1]), "Explain declarations again", dedupe="second", target_run=a[0]["run_id"])
        restarted.pump()
        restarted.future.result(timeout=30)
        restarted.pump()
    assert len(calls) == 3
    with store._connect() as db:
        rows = db.execute("SELECT * FROM fleet_incidents WHERE run_id=?", (b[0]["run_id"],)).fetchall()
        assert len(rows) == 3
        assert {r["attempt_id"] for r in rows} == {first["id"], second["id"]}
        assert len({r["fingerprint"] for r in rows}) == 1
        events = db.execute("SELECT attempt_id,payload_json FROM fleet_events WHERE type='peer.worker.event'").fetchall()
        assert len(events) == 6
        assert {json.loads(r["payload_json"])["consultation_dispatch"] for r in events} == {1, 2}
        assert all(r["attempt_id"] in {first["id"], second["id"]} for r in events)
    incidents.reconcile(FleetStore(store.path))
    assert incidents.report(store, b[0]["cwd"])["total_incidents"] == 3
