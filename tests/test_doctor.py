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


def _untracked_queue(tmp_path, monkeypatch):
    import subprocess

    from memory import store

    repo = tmp_path / "repo"
    (repo / "memory" / "task").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setattr(store, "MEMORY_DIR", repo / "memory")
    return repo


def test_an_untracked_queue_passes_while_the_handoff_compensates(tmp_path, monkeypatch):
    """Untracked is the intended state; what matters is the dispatcher's note.

    Warning forever about a handled condition trains him to ignore warnings,
    so this checks the mitigation rather than the fact.
    """

    _untracked_queue(tmp_path, monkeypatch)

    finding = _finding(doctor.check_dispatch_visibility(), "dispatch.visibility")

    assert finding.ok is True
    assert "as intended" in finding.detail


def test_an_untracked_queue_fails_when_the_handoff_stops_compensating(tmp_path, monkeypatch):
    """The regression this guards: someone drops the unseen-state note."""

    from core import scheduler_actions

    _untracked_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(scheduler_actions, "_unseen_state_rules", lambda brief: "")

    finding = _finding(doctor.check_dispatch_visibility(), "dispatch.visibility")

    assert finding.ok is False
    assert "invents unrelated" in finding.detail


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


# ---- code on disk that is not what was shipped -----------------------------


def _repo_with_upstream(tmp_path, commits_behind):
    """A checkout whose origin/master has moved ahead of it."""

    import subprocess

    def git(where, *args):
        subprocess.run(["git", "-C", str(where), *args], check=True,
                       capture_output=True, text=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "master")
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    (origin / "a.txt").write_text("one\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "first")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)

    for index in range(commits_behind):
        (origin / "a.txt").write_text(f"change {index}\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", f"shipped {index}")
    git(clone, "fetch", "-q", "origin")
    return clone


def test_a_checkout_behind_its_remote_is_reported_with_the_count(tmp_path, monkeypatch):
    """The laptop ran 43 commits behind while fixes were being deployed."""

    clone = _repo_with_upstream(tmp_path, commits_behind=3)
    monkeypatch.setattr(doctor, "_repo_root", lambda: clone)

    finding = _finding(doctor.check_repo_freshness(), "repo.behind")

    assert finding.ok is False
    assert "3 commit(s) behind" in finding.detail
    assert "not running what was shipped" in finding.detail
    assert "git fetch" in finding.fix
    # Behind is a judgement call, not a broken system, so it must not fail the run.
    assert finding.severity == "warn"


def test_a_current_checkout_passes(tmp_path, monkeypatch):
    clone = _repo_with_upstream(tmp_path, commits_behind=0)
    monkeypatch.setattr(doctor, "_repo_root", lambda: clone)

    findings = doctor.check_repo_freshness()
    assert [f.ok for f in findings] == [True]
    assert "level with" in findings[0].detail


def test_a_side_branch_that_tracks_itself_is_still_measured_against_master(
    tmp_path, monkeypatch
):
    """The exact blind spot that let a checkout run a day behind everything.

    The laptop sat on laptop-master, which tracked origin/laptop-master, which
    matched it exactly. Comparing HEAD to its own upstream therefore said "up
    to date" while the tree was far behind origin/master in one direction and
    far ahead in the other, and both machines ran it.
    """

    import subprocess

    clone = _repo_with_upstream(tmp_path, commits_behind=3)

    def git(*args):
        subprocess.run(["git", "-C", str(clone), *args], check=True,
                       capture_output=True, text=True)

    git("checkout", "-q", "-b", "side")
    (clone / "b.txt").write_text("only here\n")
    git("add", "-A")
    git("commit", "-qm", "unlanded work")
    # Its own upstream is itself, so the old check had nothing to report.
    git("push", "-q", "origin", "side")
    git("branch", "--set-upstream-to=origin/side", "side")
    monkeypatch.setattr(doctor, "_repo_root", lambda: clone)

    findings = doctor.check_repo_freshness()

    behind = _finding(findings, "repo.behind")
    assert behind.ok is False
    assert "3 commit(s) behind origin/master" in behind.detail
    assert "side" in behind.detail

    unlanded = _finding(findings, "repo.unlanded")
    assert unlanded.ok is False
    assert "1 commit(s) on side are not on origin/master" in unlanded.detail
    assert "unlanded work" in unlanded.detail
    assert "pull request" in unlanded.fix
    # Neither is a broken system, so neither may fail the run.
    assert {behind.severity, unlanded.severity} == {"warn"}


def test_a_level_checkout_whose_fetch_is_old_says_so(tmp_path, monkeypatch):
    """"Up to date" is only as true as the last fetch.

    The PC runtime answered "up to date" while it was thirty-five commits
    behind, because nothing there had fetched since the morning. A doctor that
    does not reach the network has to say how old its answer is.
    """

    import os

    clone = _repo_with_upstream(tmp_path, commits_behind=0)
    stale = time.time() - (30 * 3600)
    for name in ("FETCH_HEAD", "refs/remotes/origin/master"):
        target = clone / ".git" / name
        if target.exists():
            os.utime(target, (stale, stale))
    monkeypatch.setattr(doctor, "_repo_root", lambda: clone)

    finding = _finding(doctor.check_repo_freshness(), "repo.stale_fetch")

    assert finding.ok is False
    assert "30 hours" in finding.detail
    assert "only as current as the last fetch" in finding.detail
    assert "git fetch" in finding.fix
    assert finding.severity == "warn"


def test_a_fresh_fetch_is_reported_plainly(tmp_path, monkeypatch):
    clone = _repo_with_upstream(tmp_path, commits_behind=0)
    monkeypatch.setattr(doctor, "_repo_root", lambda: clone)

    findings = doctor.check_repo_freshness()

    assert [f.ok for f in findings] == [True]
    assert "level with" in findings[0].detail
    assert "fetched" in findings[0].detail


def test_the_fetch_age_is_found_inside_a_worktree_too(tmp_path, monkeypatch):
    """A worktree's .git is a file, and guessing it is a directory gave None."""

    import subprocess

    clone = _repo_with_upstream(tmp_path, commits_behind=0)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q",
                    str(linked), "-b", "side"], check=True, capture_output=True)
    assert (linked / ".git").is_file(), "a worktree points at its git dir"

    assert doctor._fetch_age_seconds(linked) is not None


def test_the_freshness_check_never_reaches_the_network(tmp_path, monkeypatch):
    """A doctor must be safe and instant; it reads the last fetch, not the remote."""

    import subprocess

    clone = _repo_with_upstream(tmp_path, commits_behind=2)
    monkeypatch.setattr(doctor, "_repo_root", lambda: clone)
    real_run = subprocess.run

    def _guard(args, *rest, **kwargs):
        assert "fetch" not in args and "pull" not in args, f"doctor reached the network: {args}"
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", _guard)
    doctor.check_repo_freshness()
