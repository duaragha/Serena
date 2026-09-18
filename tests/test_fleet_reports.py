"""Reports use real private SQLite runs; only the narrative provider is mocked."""
import json
import re

import pytest

from fleet import reports
from fleet.policy import build_policy, builtin_config
from fleet.store import FleetStore
from fleet.workers import WorkerResult


def _make_run(tmp_path, provider_mode="codex"):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    policy = build_policy("coding", "inspect report behavior", config=builtin_config(),
                          provider_mode=provider_mode, worker_count=1).to_dict()
    run = store.create_run(task="inspect report behavior token=hidden-value", activity="coding",
                           cwd=str(tmp_path), origin_session_id=None, origin_agent="codex",
                           dry_run=False, policy=policy)
    return store, run


@pytest.fixture
def run_db(tmp_path):
    return _make_run(tmp_path)


@pytest.fixture
def claude_run_db(tmp_path):
    return _make_run(tmp_path, provider_mode="claude")


def finish(store, run):
    # Populate real attempts without launching workers or a nested Fleet run.
    for phase in run["phases"]:
        for leg in phase["legs"]:
            attempt = store.begin_attempt(leg["leg_id"])
            store.finish_attempt(attempt["attempt_id"], state="completed",
                                 output_text="Fix evidence: lesson helped by catching the missing test.")
    return store.complete_run(run["run_id"], "done")


def provider(request, **kwargs):
    assert len(request.prompt) <= 8000
    assert "hidden-value" not in request.prompt
    assert request.access_mode == "read"
    return WorkerResult(True, json.dumps({"narrative": "All steps passed token=private-value",
        "next_prompt": "Add a golden test first", "actions": [{"action": "Add coverage", "reason": "Prevent regressions"}],
        "lesson_votes": []}), None, request.model, request.effort, 0)


def test_roundtrip_redaction_migration_and_idempotence(run_db):
    store, run = run_db
    finish(store, run)
    report = reports.generate_report(run["run_id"], store=store, runner=provider)
    reopened = FleetStore(store.path)
    assert reopened.get_report(run["run_id"]) == report
    assert reopened.report_exists(run["run_id"])
    assert "private-value" not in json.dumps(report)
    assert report["score"]["score"] == 100
    assert report["score"]["size_class"] == "S"
    assert report["next_prompt"] == "Add a golden test first"
    assert reports.generate_report(run["run_id"], store=store,
        runner=lambda *a, **k: pytest.fail("cached report launched a provider")) == report
    assert len([e for e in store.events(run["run_id"]) if e["type"] == "run.report.ready"]) == 1


def test_live_run_and_missing_run_refused(run_db):
    store, run = run_db
    with pytest.raises(ValueError, match="terminal"):
        reports.generate_report(run["run_id"], store=store, runner=provider)
    with pytest.raises(KeyError):
        reports.generate_report("missing", store=store, runner=provider)
    assert not store.report_exists(run["run_id"])


@pytest.mark.parametrize("output", ["not JSON", '{"narrative":42}', '{"narrative":"ok","next_prompt":"ok","actions":[],"lesson_votes":[{"lesson_id":"invented","vote":"helped"}]}'])
def test_invalid_provider_output_ships_deterministic_report(run_db, output):
    store, run = run_db
    finish(store, run)
    def invalid(request, **kwargs):
        assert store.report_exists(run["run_id"]), "durable facts precede provider latency"
        return WorkerResult(True, output, None, request.model, request.effort, 0)
    report = reports.generate_report(run["run_id"], store=store, runner=invalid)
    assert report["generator"].startswith("none (")
    assert report["narrative"] is None
    assert report["score"]["score"] == 100
    assert store.has_event(run["run_id"], "run.report.failed")


def test_dead_provider_fallback(run_db):
    store, run = run_db
    finish(store, run)
    def dead(*a, **kw):
        raise RuntimeError("provider unavailable token=hidden-error")
    report = reports.generate_report(run["run_id"], store=store, runner=dead)
    assert "provider unavailable" in report["generator"]
    assert "hidden-error" not in json.dumps(report)
    assert report["timeline"] == []


def test_deterministic_golden(run_db):
    store, run = run_db
    leg = run["phases"][0]["legs"][0]["leg_id"]
    attempt = store.begin_attempt(leg)
    store.finish_attempt(attempt["attempt_id"], state="failed", error="fixture failure")
    # A second attempt makes exactly one retried leg.
    store.begin_attempt(leg)
    store.append_event(run["run_id"], "worker.stalled", {"reason": "no output"})
    store.append_event(run["run_id"], "leg.completion_evidence_rejected", {})
    store.append_event(run["run_id"], "context.budgeted", {"source_chars": 100, "delivered_chars": 49})
    store.append_event(run["run_id"], "run.waiting_for_capacity", {})
    store.fail_run(run["run_id"], "fixture")
    score = reports.compute_score(store, run["run_id"])
    assert score == {"score": 57, "size_class": "XS", "penalties": [
        {"reason": "retried legs", "points": 10},
        {"reason": "completion evidence rejected", "points": 5},
        {"reason": "stalled workers", "points": 5},
        {"reason": "context below half", "points": 5},
        {"reason": "capacity/resource waits", "points": 3},
        {"reason": "failed/cancelled", "points": 15}]}
    issues = reports.scan_timeline_issues(store, run["run_id"])
    assert [i["kind"] for i in issues] == ["attempt.failed", "worker.stalled",
        "leg.completion_evidence_rejected", "context.budgeted", "run.waiting_for_capacity"]
    assert set(issues[0]) == {"at", "kind", "leg_id", "attempt_id", "summary"}
    assert issues[0]["attempt_id"] == attempt["attempt_id"]
    assert reports.scan_timeline_issues(store, run["run_id"]) == issues
    assert reports.assemble_lessons(store, run["run_id"]) == {
        "lessons": [], "blind_spot_note": reports.BLIND_SPOT_NOTE}


@pytest.mark.parametrize("dead", [False, True])
def test_terminal_notification_precedes_provider(run_db, monkeypatch, dead):
    from types import SimpleNamespace

    from fleet import supervisor
    store, run = run_db
    finished = finish(store, run)
    order = []
    monkeypatch.setattr(supervisor, "_emit_terminal_plugin_hook", lambda *a: None)
    monkeypatch.setattr(supervisor, "_terminal_notice_pending", lambda *a: False)
    def notify(*a):
        assert store.report_exists(run["run_id"])
        order.append("notification")
        return SimpleNamespace(decision="sent", sent=True, channel="imessage", notification_id="fixture")
    def generate(request, **kw):
        order.append("provider")
        if dead:
            raise RuntimeError("offline")
        return provider(request, **kw)
    monkeypatch.setattr(supervisor, "_request_terminal_notification", notify)
    monkeypatch.setattr(supervisor, "run_worker", generate)
    supervisor._terminal_outcome(store, finished)
    assert order == ["notification", "provider"]
    assert f"/fleet_report/{run['run_id']}" in supervisor._terminal_notice_text(finished)
    assert store.get_report(run["run_id"])["generator"].startswith("none (") == dead


def test_all_surfaces_on_demand_and_cached(run_db, monkeypatch):
    from click.testing import CliRunner
    from flask import Flask

    from cli import main
    from fleet import mcp, supervisor
    from ui.fleet_web import fleet_bp
    store, run = run_db
    finish(store, run)
    monkeypatch.setattr(supervisor, "_store", lambda: store)
    calls = []
    def generate(request, **kwargs):
        calls.append(request)
        return provider(request, **kwargs)
    monkeypatch.setattr(reports, "run_worker", generate)
    first = mcp.fleet_report(run["run_id"])
    assert first["ok"]
    report = first["report"]
    cli = CliRunner().invoke(main, ["fleet", "report", run["run_id"], "--json"])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.output) == report
    human = CliRunner().invoke(main, ["fleet", "report", run["run_id"]])
    assert human.exit_code == 0
    assert "Add a golden test first" in human.output
    app = Flask(__name__)
    app.register_blueprint(fleet_bp)
    response = app.test_client().get(f"/fleet_report/{run['run_id']}")
    assert response.status_code == 200
    assert response.json["report"] == report
    assert len(calls) == 1
    assert app.test_client().get("/fleet_report/missing").status_code == 404
    assert app.test_client().get(f"/fleet_report/{run['run_id']}", environ_base={"REMOTE_ADDR": "10.0.0.1"}).status_code == 403


def test_live_errors_across_surfaces(run_db, monkeypatch):
    from click.testing import CliRunner
    from flask import Flask

    from cli import main
    from fleet import mcp, supervisor
    from ui.fleet_web import fleet_bp
    store, run = run_db
    monkeypatch.setattr(supervisor, "_store", lambda: store)
    assert mcp.fleet_report(run["run_id"])["error_type"] == "ValueError"
    cli = CliRunner().invoke(main, ["fleet", "report", run["run_id"]])
    assert cli.exit_code == 1 and "terminal" in cli.output
    app = Flask(__name__)
    app.register_blueprint(fleet_bp)
    assert app.test_client().get(f"/fleet_report/{run['run_id']}").status_code == 409


def test_lesson_vote_requires_fix_evidence(run_db):
    from fleet.learning import FleetLearning
    store, run = run_db
    finish(store, run)
    FleetLearning(store)
    with store._connect() as db:
        db.execute("INSERT INTO fleet_lessons (id,project,source_run,source_attempt,author,summary,evidence,created,expires) "
                   "VALUES ('lesson-1','fixture',?,'attempt','worker','Always add tests','{}',1,9999999999)", (run["run_id"],))
        db.execute("INSERT INTO fleet_lesson_uses VALUES (?,'lesson-1','attempt','completed')", (run["run_id"],))
    expected = {"lesson_id": "lesson-1", "attempt_id": "attempt", "outcome": "completed", "vote": "unclear"}
    assert reports.assemble_lessons(store, run["run_id"])["lessons"] == [expected]
    evidence = "lesson helped by catching the missing test."
    def vote(request, **kwargs):
        assert evidence in request.prompt
        return WorkerResult(True, json.dumps({"narrative": "Useful lesson", "next_prompt": "Test first",
            "actions": [], "lesson_votes": [{"lesson_id": "lesson-1", "vote": "helped", "evidence": evidence}]}),
            None, request.model, request.effort, 0)
    report = reports.generate_report(run["run_id"], store=store, runner=vote)
    assert report["knowledge"]["lessons"] == [expected | {"vote": "helped", "evidence": evidence}]
    with pytest.raises(ValueError, match="Fix excerpt"):
        reports._parse(json.dumps({"narrative": "n", "next_prompt": "p", "actions": [],
            "lesson_votes": [{"lesson_id": "lesson-1", "vote": "hurt", "evidence": "invented"}]}),
            {"lesson-1"}, evidence)


def test_timeout_and_concurrent_read(run_db, monkeypatch):
    store, run = run_db
    finish(store, run)
    clock = [0]
    monkeypatch.setattr(reports.time, "monotonic", lambda: clock[0])
    def timeout(request, *, cancel_requested, **kwargs):
        cached = reports.generate_report(run["run_id"], store=FleetStore(store.path),
            runner=lambda *a, **k: pytest.fail("duplicate generation"))
        assert cached["generator"] == "none (generation pending)"
        clock[0] = 41
        assert cancel_requested()
        return provider(request)
    report = reports.generate_report(run["run_id"], store=store, runner=timeout)
    assert report["generator"] == "none (report generator timeout)"


def test_caps_and_bounded_indexed_timeline(run_db):
    store, run = run_db
    finish(store, run)
    with store._connect() as db:
        db.executemany("INSERT INTO fleet_events(run_id,type,payload_json,created_at) VALUES (?,?,?,?)",
            [(run["run_id"], kind, '{}', float(i)) for i in range(150)
             for kind in ("worker.stalled", "leg.completion_evidence_rejected", "leg.waiting_for_resources")])
        plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM fleet_events WHERE run_id=? ORDER BY event_seq",
                          (run["run_id"],)).fetchall()
        assert any("fleet_events_run_idx" in row[3] for row in plan)
    score = reports.compute_score(store, run["run_id"])
    assert score["score"] == 56
    assert [p["points"] for p in score["penalties"]] == [20, 15, 9]
    assert len(reports.scan_timeline_issues(store, run["run_id"])) == 100


def test_retry_invalidates_report_and_fences_stale_enrichment(run_db):
    store, run = run_db
    run_id = run["run_id"]
    leg = run["phases"][0]["legs"][0]["leg_id"]
    attempt = store.begin_attempt(leg)
    store.finish_attempt(attempt["attempt_id"], state="failed", error="fixture failure")
    store.fail_run(run_id, "fixture")
    stale_work = reports.prepare_report(run_id, store)
    stale_score = store.get_report(run_id)["score"]["score"]
    assert stale_score < 100

    store.retry_run(run_id)
    assert store.get_report(run_id) is None
    assert not store.report_exists(run_id)
    assert store.has_event(run_id, "run.report.invalidated")

    finish(store, run)
    fresh = reports.generate_report(run_id, store=store, runner=provider)
    assert fresh["score"] == reports.compute_score(store, run_id)
    assert fresh["score"]["score"] != stale_score
    assert "run.retried" in {issue["kind"] for issue in fresh["timeline"]}

    # The pre-retry worker returns late; its generation no longer exists.
    assert reports.enrich_report(run_id, store, stale_work, runner=provider) is not None
    assert store.get_report(run_id) == fresh


def test_dated_provider_identity_is_accepted(claude_run_db):
    store, run = claude_run_db
    finish(store, run)
    seen = []

    def dated(request, **kwargs):
        seen.append(request.model)
        result = provider(request, **kwargs)
        return WorkerResult(True, result.output_text, None,
                            request.model + "-20260401", request.effort, 0)

    report = reports.generate_report(run["run_id"], store=store, runner=dated)
    assert seen == ["claude-opus-5"]
    assert report["generator"] == "claude-opus-5"
    assert report["narrative"]


def test_many_lessons_deliver_complete_records_and_omitted_stay_unclear(run_db):
    from fleet.learning import FleetLearning
    store, run = run_db
    finish(store, run)
    FleetLearning(store)
    with store._connect() as db:
        for index in range(100):
            db.execute("INSERT INTO fleet_lessons (id,project,source_run,source_attempt,author,summary,"
                       "evidence,created,expires) VALUES (?,'fixture',?,'attempt','worker',?,'{}',1,9999999999)",
                       (f"lesson-{index:03d}", run["run_id"], f"Lesson {index:03d}: always add a regression test"))
            db.execute("INSERT INTO fleet_lesson_uses VALUES (?,?,'attempt','completed')",
                       (run["run_id"], f"lesson-{index:03d}"))
    evidence = "lesson helped by catching the missing test."
    delivered = []

    def vote(request, **kwargs):
        # Every delivered lesson record must survive prompt budgeting intact.
        section = request.prompt.split("[lessons]\n", 1)[1].split("\n\n[", 1)[0]
        payload = json.loads(section)
        delivered.extend(item["lesson_id"] for item in payload["lessons"])
        assert delivered, "no complete lesson record reached the prompt"
        assert all(set(item) >= {"lesson_id", "attempt_id", "outcome"} for item in payload["lessons"])
        assert not re.search(r"context excerpt omitted", section)
        return WorkerResult(True, json.dumps({"narrative": "Lessons reviewed", "next_prompt": "Test first",
            "actions": [], "lesson_votes": [{"lesson_id": identifier, "vote": "helped", "evidence": evidence}
                                            for identifier in delivered]}),
            None, request.model, request.effort, 0)

    report = reports.generate_report(run["run_id"], store=store, runner=vote)
    assert report["generator"] == "gpt-6-astra", report["generator"]
    lessons = {lesson["lesson_id"]: lesson for lesson in report["knowledge"]["lessons"]}
    assert len(lessons) == 100
    assert 0 < len(delivered) < 100
    assert all(lessons[identifier]["vote"] == "helped" for identifier in delivered)
    assert all(lesson["vote"] == "unclear" for identifier, lesson in lessons.items()
               if identifier not in delivered)


def test_cancelled_report_and_old_schema_migration(run_db):
    store, run = run_db
    store.cancel_run(run["run_id"])
    with store._connect() as db:
        db.execute("DROP TABLE fleet_run_reports")
    reopened = FleetStore(store.path)
    report = reports.generate_report(run["run_id"], store=reopened, runner=provider)
    assert report["score"]["score"] == 85
