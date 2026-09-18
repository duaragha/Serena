"""`chats doctor`, written from the outages that needed it.

Each test reproduces the shape of a real failure from 2026-09-17/18 and
asserts the doctor names it and says what to do, because in every one of those
cases the system was working exactly as designed and simply never said so.
"""

import sqlite3
import time

import pytest

from core import doctor


def _scheduler_db(path, rows):
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE schedules (schedule_id TEXT, action TEXT, state TEXT, "
            "interval_seconds INTEGER, next_run_at REAL, consecutive_failures INTEGER)"
        )
        db.executemany("INSERT INTO schedules VALUES (?,?,?,?,?,?)", rows)
    return path


def _finding(findings, name):
    for finding in findings:
        if finding.name == name:
            return finding
    raise AssertionError(f"{name} not in {[f.name for f in findings]}")


# ---- the half-hour outage that looked like an empty queue ------------------


def test_a_disabled_schedule_is_named_with_the_command_that_revives_it(tmp_path, monkeypatch):
    now = 1_000_000.0
    path = _scheduler_db(tmp_path / "s.sqlite3", [
        ("id-start", "serena.fleet.start", "disabled", 60, now, 5),
        ("id-rec", "serena.fleet.reconcile", "disabled", 60, now, 5),
        ("id-poll", "serena.phone.poll", "active", 60, now + 30, 0),
    ])
    monkeypatch.setenv("SERENA_SCHEDULER_DB_PATH", str(path))

    finding = _finding(doctor.check_schedules(now=now), "schedules.disabled")

    assert finding.ok is False
    assert "serena.fleet.start" in finding.detail and "serena.fleet.reconcile" in finding.detail
    # The ids are in the fix so it can be pasted, not looked up.
    assert "chats schedule resume" in finding.fix
    assert "id-start" in finding.fix and "id-rec" in finding.fix


def test_a_schedule_climbing_toward_the_cap_warns_before_it_switches_off(tmp_path, monkeypatch):
    now = 1_000_000.0
    path = _scheduler_db(tmp_path / "s.sqlite3", [
        ("id-start", "serena.fleet.start", "active", 60, now + 30, 3),
    ])
    monkeypatch.setenv("SERENA_SCHEDULER_DB_PATH", str(path))

    finding = _finding(doctor.check_schedules(now=now), "schedules.failing")

    assert finding.severity == "warn"
    assert "3/5" in finding.detail


def test_a_stopped_automation_loop_shows_as_overdue(tmp_path, monkeypatch):
    now = 1_000_000.0
    path = _scheduler_db(tmp_path / "s.sqlite3", [
        ("id-poll", "serena.phone.poll", "active", 60, now - 600, 0),
    ])
    monkeypatch.setenv("SERENA_SCHEDULER_DB_PATH", str(path))

    finding = _finding(doctor.check_schedules(now=now), "schedules.overdue")
    assert "serena.phone.poll" in finding.detail


def test_healthy_schedules_say_so(tmp_path, monkeypatch):
    now = 1_000_000.0
    path = _scheduler_db(tmp_path / "s.sqlite3", [
        ("id-poll", "serena.phone.poll", "active", 60, now + 30, 0),
    ])
    monkeypatch.setenv("SERENA_SCHEDULER_DB_PATH", str(path))

    findings = doctor.check_schedules(now=now)
    assert [f.ok for f in findings] == [True]


# ---- the collision that refused every new brief ---------------------------


def test_duplicate_task_ids_are_reported_with_both_filenames(tmp_path, monkeypatch):
    from memory import store

    tasks = tmp_path / "task"
    tasks.mkdir(parents=True)
    (tasks / "1069-workout.md").write_text("---\nid: 1069\ntype: task\n---\n\nwork\n")
    (tasks / "1069-github.md").write_text("---\nid: 1069\ntype: task\n---\n\nother\n")
    (tasks / "1070-fine.md").write_text("---\nid: 1070\ntype: task\n---\n\nfine\n")
    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path)

    finding = _finding(doctor.check_task_store(), "tasks.duplicate_ids")

    assert finding.ok is False
    assert "1069-workout.md" in finding.detail and "1069-github.md" in finding.detail
    assert "queued" in finding.detail


def test_unique_task_ids_pass(tmp_path, monkeypatch):
    from memory import store

    tasks = tmp_path / "task"
    tasks.mkdir(parents=True)
    (tasks / "1.md").write_text("---\nid: 1\ntype: task\n---\n\na\n")
    (tasks / "2.md").write_text("---\nid: 2\ntype: task\n---\n\nb\n")
    monkeypatch.setattr(store, "MEMORY_DIR", tmp_path)

    assert [f.ok for f in doctor.check_task_store()] == [True]


# ---- the config drift that stopped every run ------------------------------


def test_a_config_the_policy_refuses_is_a_failure(monkeypatch):
    from fleet import policy

    def _raise():
        raise ValueError("research verify workers must match Fleet's fixed model policy")

    monkeypatch.setattr(policy, "load_config", _raise)
    finding = _finding(doctor.check_fleet_config(), "fleet.config")

    assert finding.ok is False
    assert "no run can start" in finding.detail


# ---- the import of a module nobody committed ------------------------------


def test_an_import_of_an_uncommitted_module_is_caught(tmp_path, monkeypatch):
    package = tmp_path / "fleet"
    package.mkdir()
    (package / "isolation.py").write_text("from fleet.artifacts import spill_testlog\n")
    monkeypatch.setattr(doctor, "_repo_root", lambda: tmp_path)

    finding = _finding(doctor.check_first_party_imports(roots=("fleet",)), "imports")

    assert finding.ok is False
    assert "fleet.artifacts" in finding.detail
    assert "fleet/isolation.py" in finding.detail


def test_imports_that_all_resolve_pass(tmp_path, monkeypatch):
    package = tmp_path / "fleet"
    package.mkdir()
    (package / "isolation.py").write_text("from fleet.helper import thing\n")
    (package / "helper.py").write_text("thing = 1\n")
    monkeypatch.setattr(doctor, "_repo_root", lambda: tmp_path)

    assert [f.ok for f in doctor.check_first_party_imports(roots=("fleet",))] == [True]


# ---- the PR that did nothing ----------------------------------------------


def test_an_untracked_task_queue_warns_that_a_worker_cannot_see_it(tmp_path, monkeypatch):
    import subprocess

    from memory import store

    repo = tmp_path / "repo"
    (repo / "memory" / "task").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(store, "MEMORY_DIR", repo / "memory")

    finding = _finding(doctor.check_dispatch_visibility(), "dispatch.visibility")

    assert finding.ok is False
    assert "invents unrelated work" in finding.detail


# ---- the report itself -----------------------------------------------------


def test_a_check_that_raises_becomes_a_finding_instead_of_killing_the_run():
    def boom():
        raise RuntimeError("kaboom")

    report = doctor.run(checks=(boom, lambda: [doctor.Finding("fine", True, "ok")]))

    assert any("kaboom" in f.detail for f in report.findings)
    assert any(f.name == "fine" for f in report.findings), "one broken check must not hide the rest"


def test_warnings_alone_do_not_make_the_report_fail():
    report = doctor.run(checks=(lambda: [doctor.Finding("w", False, "d", severity="warn")],))
    assert report.ok is True
    assert report.warnings and not report.failures


def test_render_puts_failures_first_and_pairs_each_with_its_fix():
    report = doctor.run(checks=(
        lambda: [doctor.Finding("healthy", True, "all good")],
        lambda: [doctor.Finding("broken", False, "it broke", fix="do the thing")],
    ))
    text = doctor.render(report)

    assert text.index("broken") < text.index("healthy")
    assert "fix: do the thing" in text
    assert "1 failure(s)" in text
