from __future__ import annotations

import json
import subprocess

import pytest

from core.coding_job_contract import (
    CODEX_EFFORT,
    CODEX_MODEL,
    DEFAULT_COMPLEXITY,
    CodingJobBrief,
    RepositoryResolutionError,
    capture_git_snapshot,
    discover_repository_roots,
    frozen_implement_effort,
    prompt_brief,
    resolve_repository_root,
    review_required,
    scoped_git_evidence,
    validate_repository_root,
)


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / "dirty.txt").write_text("clean baseline\n", encoding="utf-8")
    _git(path, "add", "app.py", "dirty.txt")
    _git(
        path,
        "-c",
        "user.name=Serena Test",
        "-c",
        "user.email=serena@example.test",
        "commit",
        "-qm",
        "baseline",
    )
    return path.resolve()


def test_repository_root_requires_git_and_never_falls_back_to_home(tmp_path) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()

    with pytest.raises(RepositoryResolutionError, match="not a Git repository"):
        validate_repository_root(ordinary)
    with pytest.raises(RepositoryResolutionError, match="project name"):
        resolve_repository_root(
            "fix it",
            roots=[],
            projects_root=tmp_path,
            serena_root=ordinary,
        )


def test_ambiguous_projects_are_rejected_instead_of_guessed(tmp_path) -> None:
    first = _repo(tmp_path / "alpha")
    second = _repo(tmp_path / "beta")

    with pytest.raises(RepositoryResolutionError, match="which Git project"):
        resolve_repository_root(
            "change alpha and beta",
            roots=[first, second],
            projects_root=tmp_path,
            serena_root=first,
        )


def test_api_route_token_does_not_override_a_valid_project_alias(tmp_path) -> None:
    repo = _repo(tmp_path / "serena")

    resolved = resolve_repository_root(
        "fix the /api/health route in serena",
        roots=[repo],
        projects_root=tmp_path,
        serena_root=repo,
    )

    assert resolved == repo


@pytest.mark.parametrize(
    "spoken_request",
    [
        "fix the coding pane so it can be reopened",
        "repair the coding panel in the coding app",
        "update the Chats app voice-work display",
        "fix the dot overlay",
        "change the Fleet tab",
    ],
)
def test_serena_owned_surfaces_resolve_without_a_repo_question(
    tmp_path,
    spoken_request: str,
) -> None:
    """My own UI names must not strand a spoken coding request at intake."""

    serena = _repo(tmp_path / "serena")
    openwhispr_app = _repo(tmp_path / "OpenWhispr" / "app")

    resolved = resolve_repository_root(
        spoken_request,
        roots=[serena, openwhispr_app],
        projects_root=tmp_path,
        serena_root=serena,
    )

    assert resolved == serena


def test_named_project_still_owns_its_coding_pane(tmp_path) -> None:
    """A Serena surface alias must not override another project named directly."""

    serena = _repo(tmp_path / "serena")
    locket = _repo(tmp_path / "locket")

    resolved = resolve_repository_root(
        "fix the coding pane in locket",
        roots=[serena, locket],
        projects_root=tmp_path,
        serena_root=serena,
    )

    assert resolved == locket


def test_generic_app_word_does_not_compete_with_serena(tmp_path) -> None:
    """The OpenWhispr app repo must not hijack a request for the Serena app."""

    serena = _repo(tmp_path / "serena")
    openwhispr_app = _repo(tmp_path / "OpenWhispr" / "app")
    request = (
        "Fix a bug in the Serena coding app / voice work display: when I dismiss "
        "the coding pane, keep the same job reachable."
    )

    resolved = resolve_repository_root(
        request,
        project_hint="Serena coding app",
        roots=[serena, openwhispr_app],
        projects_root=tmp_path,
        serena_root=serena,
    )

    assert resolved == serena


def test_dirty_worktree_preservation_and_scoped_diff_are_mechanical(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    (repo / "dirty.txt").write_text("Raghav's existing edit\n", encoding="utf-8")
    (repo / "personal.tmp").write_text("untracked user work\n", encoding="utf-8")
    baseline = capture_git_snapshot(repo, item_id="job-dirty", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-dirty",
        exact_request="change app.py",
        triggering_request="change app.py in project",
        project_root=repo,
        initial_git=baseline,
    )

    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    final = capture_git_snapshot(repo, item_id="job-dirty", label="final")
    evidence = scoped_git_evidence(
        brief.to_dict(),
        commands=[{"command": "pytest -q", "exit_code": 0, "output": "1 passed"}],
        final_snapshot=final,
    )

    assert evidence["complete"] is True
    assert evidence["changed_files"] == ["app.py"]
    assert {entry["path"] for entry in evidence["unrelated_dirty_changes"]} == {
        "dirty.txt",
        "personal.tmp",
    }
    assert all(entry["preserved"] for entry in evidence["unrelated_dirty_changes"])
    assert "VALUE = 2" in evidence["scoped_diff"]
    assert "Raghav's existing edit" not in evidence["scoped_diff"]


def test_evidence_completeness_rejects_code_without_test_exit_codes(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    baseline = capture_git_snapshot(repo, item_id="job-tests", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-tests",
        exact_request="change app.py",
        triggering_request="change app.py in project",
        project_root=repo,
        initial_git=baseline,
    )
    (repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    final = capture_git_snapshot(repo, item_id="job-tests", label="final")

    evidence = scoped_git_evidence(brief.to_dict(), commands=[], final_snapshot=final)

    assert evidence["complete"] is False
    assert any("test command" in error for error in evidence["errors"])


def test_failed_test_is_not_hidden_by_a_different_successful_command(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    baseline = capture_git_snapshot(repo, item_id="job-failed-test", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-failed-test",
        exact_request="change app.py",
        triggering_request="change app.py",
        project_root=repo,
        initial_git=baseline,
    )
    (repo / "app.py").write_text("VALUE = 4\n", encoding="utf-8")
    final = capture_git_snapshot(repo, item_id="job-failed-test", label="final")

    evidence = scoped_git_evidence(
        brief.to_dict(),
        commands=[
            {"command": "pytest tests/test_app.py", "exit_code": 1},
            {"command": "ruff check app.py", "exit_code": 0},
        ],
        final_snapshot=final,
    )

    assert evidence["complete"] is False
    assert any("never exited clean" in error for error in evidence["errors"])


def test_rerunning_the_same_failed_test_green_resolves_it(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    baseline = capture_git_snapshot(repo, item_id="job-rerun", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-rerun",
        exact_request="change app.py",
        triggering_request="change app.py",
        project_root=repo,
        initial_git=baseline,
    )
    (repo / "app.py").write_text("VALUE = 5\n", encoding="utf-8")
    final = capture_git_snapshot(repo, item_id="job-rerun", label="final")

    evidence = scoped_git_evidence(
        brief.to_dict(),
        commands=[
            {"command": "pytest tests/test_app.py", "exit_code": 1},
            {"command": "pytest tests/test_app.py", "exit_code": 0},
        ],
        final_snapshot=final,
    )

    assert evidence["complete"] is True
    assert evidence["tests"] == [
        {"command": "pytest tests/test_app.py", "exit_code": 0, "output": ""}
    ]


def test_marked_python_runtime_probe_counts_as_live_proof_but_python_and_tests_do_not(
    tmp_path,
) -> None:
    repo = _repo(tmp_path / "project")
    runtime_file = repo / "voice" / "desktop" / "app.py"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "voice/desktop/app.py")
    _git(
        repo,
        "-c",
        "user.name=Serena Test",
        "-c",
        "user.email=serena@example.test",
        "commit",
        "-qm",
        "runtime baseline",
    )
    baseline = capture_git_snapshot(repo, item_id="job-python-proof", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-python-proof",
        exact_request="change the live desktop runtime",
        triggering_request="change the live desktop runtime",
        project_root=repo,
        initial_git=baseline,
    )
    runtime_file.write_text("VALUE = 2\n", encoding="utf-8")
    final = capture_git_snapshot(repo, item_id="job-python-proof", label="final")
    test_command = "SERENA_EVIDENCE_KIND=live python -m pytest tests/test_app.py"
    plain_python = "python -m app.runtime_probe"
    marked_python = "SERENA_EVIDENCE_KIND=live python -m app.runtime_probe"

    missing = scoped_git_evidence(
        brief.to_dict(),
        commands=[
            {"command": test_command, "exit_code": 0},
            {"command": plain_python, "exit_code": 0},
        ],
        final_snapshot=final,
    )
    proven = scoped_git_evidence(
        brief.to_dict(),
        commands=[
            {"command": "pytest tests/test_app.py", "exit_code": 0},
            {"command": marked_python, "exit_code": 0},
        ],
        final_snapshot=final,
    )

    assert missing["complete"] is False
    assert missing["live_proof"] == []
    assert any("runtime-sensitive" in error for error in missing["errors"])
    assert proven["complete"] is True
    assert [entry["command"] for entry in proven["live_proof"]] == [marked_python]


def test_shell_wrapped_node_test_and_live_probe_are_classified(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    runtime_file = repo / "voice" / "desktop" / "app.js"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("const value = 1;\n", encoding="utf-8")
    _git(repo, "add", "voice/desktop/app.js")
    _git(
        repo,
        "-c",
        "user.name=Serena Test",
        "-c",
        "user.email=serena@example.test",
        "commit",
        "-qm",
        "runtime baseline",
    )
    baseline = capture_git_snapshot(repo, item_id="job-shell-proof", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-shell-proof",
        exact_request="change the desktop runtime",
        triggering_request="change the desktop runtime",
        project_root=repo,
        initial_git=baseline,
    )
    runtime_file.write_text("const value = 2;\n", encoding="utf-8")
    final = capture_git_snapshot(repo, item_id="job-shell-proof", label="final")
    test_command = "/bin/bash -lc 'node --test tests/runtime.test.cjs'"
    proof_command = "/bin/bash -lc 'systemctl --user is-active example.service'"

    evidence = scoped_git_evidence(
        brief.to_dict(),
        commands=[
            {"command": test_command, "exit_code": 0},
            {"command": proof_command, "exit_code": 0},
        ],
        final_snapshot=final,
    )

    assert evidence["complete"] is True
    assert [entry["command"] for entry in evidence["tests"]] == [test_command]
    assert [entry["command"] for entry in evidence["live_proof"]] == [proof_command]


def test_pwd_cannot_complete_an_unchanged_implementation_job(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    baseline = capture_git_snapshot(repo, item_id="job-pwd", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-pwd",
        exact_request="fix app.py",
        triggering_request="fix app.py",
        project_root=repo,
        initial_git=baseline,
    )
    final = capture_git_snapshot(repo, item_id="job-pwd", label="final")

    evidence = scoped_git_evidence(
        brief.to_dict(),
        commands=[{"command": "pwd", "exit_code": 0}],
        final_snapshot=final,
    )

    assert evidence["complete"] is False
    assert any("no successful test or proof" in error for error in evidence["errors"])


def test_model_identity_is_frozen_in_every_accepted_brief(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    baseline = capture_git_snapshot(repo, item_id="job-model", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-model",
        exact_request="change app.py",
        triggering_request="change app.py in project",
        project_root=repo,
        initial_git=baseline,
    )

    # The model is frozen; the effort is tiered per job and an unjudged job is
    # an ordinary one, so this default is deliberately not the ceiling.
    assert brief.codex_model == CODEX_MODEL
    assert (brief.complexity, brief.codex_effort) == (DEFAULT_COMPLEXITY, "high")
    assert (brief.implement_model, brief.implement_effort) == ("gpt-5.6-sol", "high")
    assert (brief.review_model, brief.review_effort) == ("gpt-5.6-luna", "high")
    assert brief.model_policy["policy_version"] == 1
    assert brief.commit_authorized is False


def test_hard_work_still_reaches_the_ceiling_and_ordinary_work_does_not(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    baseline = capture_git_snapshot(repo, item_id="job-effort", label="baseline")

    def make(complexity: object) -> CodingJobBrief:
        return CodingJobBrief.create(
            item_id="job-effort",
            exact_request="change app.py",
            triggering_request="change app.py in project",
            project_root=repo,
            initial_git=baseline,
            context={"complexity": complexity},
        )

    assert make("hard").codex_effort == CODEX_EFFORT == "xhigh"
    assert make("routine").implement_model == "gpt-5.6-terra"
    assert make("normal").implement_model == "gpt-5.6-sol"
    assert make("ordinary").codex_effort == "high"
    # Nonsense never escalates: an unreadable tier is an ordinary job.
    assert make("catastrophic").complexity == DEFAULT_COMPLEXITY
    assert make(None).codex_effort == "high"


def test_the_file_map_reaches_the_worker_labelled_as_a_guess(tmp_path) -> None:
    """A path handed over as fact sends the worker down a false path."""

    repo = _repo(tmp_path / "project")
    baseline = capture_git_snapshot(repo, item_id="job-map", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-map",
        exact_request="fix the coding drawer",
        triggering_request="fix the coding drawer",
        project_root=repo,
        initial_git=baseline,
        context={
            "likely_files": [
                "voice/desktop/renderer/code-panel.js drawer visibility",
                "core/voice_work_supervisor.py::_emit_job_snapshot emits code_start",
            ]
        },
    )

    assert brief.likely_files[0].startswith("voice/desktop/renderer/code-panel.js")
    projected = prompt_brief(brief.to_dict())
    assert projected["likely_files"]["paths"] == brief.likely_files
    caveat = projected["likely_files"]["status"].casefold()
    assert "unverified" in caveat and "not fact" in caveat
    # The caveat is inseparable from the paths: it is the same object.
    assert "voice/desktop" not in projected["likely_files"]["status"]


def test_a_brief_without_a_file_map_carries_no_empty_promise(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    baseline = capture_git_snapshot(repo, item_id="job-nomap", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-nomap",
        exact_request="fix it",
        triggering_request="fix it",
        project_root=repo,
        initial_git=baseline,
    )

    assert brief.likely_files == []
    assert "likely_files" not in prompt_brief(brief.to_dict())


def test_worker_prompt_omits_the_baseline_patch_and_untracked_hashes(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    (repo / "dirty.txt").write_text("private baseline edit\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    baseline = capture_git_snapshot(repo, item_id="job-prompt", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-prompt",
        exact_request="fix app.py",
        triggering_request="fix app.py",
        project_root=repo,
        initial_git=baseline,
    )

    projected = prompt_brief(brief.to_dict())

    assert "tracked_patch" not in projected["initial_git"]
    assert "status" not in projected["initial_git"]
    assert "untracked_hashes" not in projected["initial_git"]
    assert projected["initial_git"]["tree"] == baseline.tree


def test_the_prompt_projection_is_a_fraction_of_the_durable_brief(tmp_path) -> None:
    """The frozen patch is evidence, not context. It must not open the prompt."""

    repo = _repo(tmp_path / "project")
    (repo / "dirty.txt").write_text("x" * 20_000 + "\n", encoding="utf-8")
    baseline = capture_git_snapshot(repo, item_id="job-size", label="baseline")
    brief = CodingJobBrief.create(
        item_id="job-size",
        exact_request="change app.py",
        triggering_request="change app.py in project",
        project_root=repo,
        initial_git=baseline,
    ).to_dict()

    assert len(baseline.tracked_patch) > 10_000
    projected = prompt_brief(brief)

    assert projected["initial_git"]["patch_omitted"] is True
    assert projected["initial_git"]["head"] == baseline.head
    assert "dirty.txt" in projected["initial_git"]["dirty_paths"]
    assert projected["exact_request"] == "change app.py"
    assert len(json.dumps(projected)) < len(json.dumps(brief)) / 4


def test_an_unresolvable_path_still_explains_itself_when_nothing_else_matches(
    tmp_path,
) -> None:
    repo = _repo(tmp_path / "serena")

    with pytest.raises(RepositoryResolutionError, match="does not exist"):
        resolve_repository_root(
            "fix the /api/health route",
            roots=[repo],
            projects_root=tmp_path,
            serena_root=tmp_path / "missing",
        )


def test_project_discovery_does_not_walk_into_node_modules(tmp_path) -> None:
    """Acceptance runs inside a live call. It cannot descend the whole tree."""

    repo = _repo(tmp_path / "alpha")
    buried = tmp_path / "alpha" / "node_modules" / "pkg"
    buried.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(buried)], check=True)
    deep = tmp_path / "a" / "b" / "c" / "d" / "deep"
    deep.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(deep)], check=True)

    found = discover_repository_roots(tmp_path)

    assert repo in found
    assert buried.resolve() not in found
    assert deep.resolve() not in found


def test_saying_committed_is_not_authority_to_move_head(tmp_path) -> None:
    repo = _repo(tmp_path / "project")
    baseline = capture_git_snapshot(repo, item_id="job-commit", label="baseline")

    def _brief(trigger: str, *, commit_authorized: bool = False) -> dict:
        return CodingJobBrief.create(
            item_id="job-commit",
            exact_request="change app.py",
            triggering_request=trigger,
            project_root=repo,
            initial_git=baseline,
            context={"commit_authorized": commit_authorized},
        ).to_dict()

    (repo / "app.py").write_text("VALUE = 5\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(
        repo,
        "-c",
        "user.name=Serena Test",
        "-c",
        "user.email=serena@example.test",
        "commit",
        "-qm",
        "worker commit",
    )
    final = capture_git_snapshot(repo, item_id="job-commit", label="final")
    commands = [{"command": "pytest -q", "exit_code": 0}]

    loose = scoped_git_evidence(_brief("i'm committed to shipping this today"), commands=commands, final_snapshot=final)
    asked = scoped_git_evidence(
        _brief("fix app.py and commit it", commit_authorized=True),
        commands=commands,
        final_snapshot=final,
    )

    assert loose["complete"] is False
    assert any("commit authority" in error for error in loose["errors"])
    assert asked["complete"] is True


def test_superseded_job_refs_do_not_pile_up_in_his_repository(tmp_path) -> None:
    repo = _repo(tmp_path / "project")

    for label in ("baseline", "final-1", "review-fix-2", "final-3"):
        capture_git_snapshot(repo, item_id="job-refs", label=label)
    listed = _git(repo, "for-each-ref", "--format=%(refname)", "refs/serena/jobs/job-refs/*")

    assert sorted(listed.stdout.split()) == [
        "refs/serena/jobs/job-refs/baseline",
        "refs/serena/jobs/job-refs/final-3",
    ]


def test_an_overlay_change_is_not_documentation_because_it_is_markup() -> None:
    for path in ("voice/desktop/renderer/index.html", "ui/static/app.css"):
        needed, reason = review_required({"changed_files": [path], "errors": []})
        assert needed is True, path
        assert "documentation" not in reason

    needed, _reason = review_required({"changed_files": ["docs/guide.html"], "errors": []})
    assert needed is False


def test_secondary_review_is_conditional_on_actual_scoped_changes() -> None:
    needed, reason = review_required({"changed_files": ["README.md"], "errors": []})
    assert needed is False
    assert "documentation-only" in reason

    needed, reason = review_required({"changed_files": ["core/runtime.py"], "errors": []})
    assert needed is True
    assert "runtime" in reason

    for path in ("voice/desktop/renderer/index.html", "ui/static/app.css"):
        needed, reason = review_required({"changed_files": [path], "errors": []})
        assert needed is True, (path, reason)


def test_a_brief_accepted_before_tiering_still_runs_at_what_it_froze() -> None:
    """A speed change must not become an outage for queued work.

    Briefs accepted before effort was tiered name no complexity and froze the
    old ceiling. Deciding them by the new default would fail the frozen-policy
    gate on jobs already sitting in the queue.
    """

    legacy = {"codex_model": CODEX_MODEL, "codex_effort": "xhigh"}
    assert frozen_implement_effort(legacy) == "xhigh"
    # A brief that names its tier is decided by the tier, not by the string.
    assert frozen_implement_effort({"complexity": "ordinary", "codex_effort": "xhigh"}) == "high"
    assert frozen_implement_effort({"complexity": "hard", "codex_effort": "high"}) == "xhigh"
    # Nonsense falls back to ordinary rather than inventing an effort.
    assert frozen_implement_effort({"codex_effort": "banana"}) == "high"
    assert frozen_implement_effort(None) == "high"
