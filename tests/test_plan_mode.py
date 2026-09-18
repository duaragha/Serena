"""Tests for plan mode: citation normalization + prompt builder."""

from __future__ import annotations

import pytest

from core.plan_mode import (
    Citation,
    Evidence,
    build_fleet_prompt,
    resolve_repo_cwd,
    suggest_clarifiers,
)


def test_citation_line_shapes():
    assert (
        Citation(source="chat", key="a1b2c3d4", title="Fix login", excerpt="...").line()
        == "chat:a1b2c3d4 — Fix login (...)"
    )
    assert Citation(source="kb", key="google-ads", excerpt="x").line() == "kb:google-ads (x)"
    assert (
        Citation(source="mem", key="project:41", excerpt="y").line()
        == "mem:project:41 (y)"
    )
    assert Citation(source="ledger", key="auth").line() == "ledger:auth"


def test_suggest_clarifiers_pins_repo_first():
    evidence = Evidence(query="q")
    questions = suggest_clarifiers(evidence)
    assert questions
    assert "repo" in questions[0].lower()

    evidence.memory = [Citation(source="mem", key="user:1", excerpt="x")]
    evidence.knowledge = [Citation(source="kb", key="k", excerpt="x")]
    questions = suggest_clarifiers(evidence, repo_hint="/home/raghav/x")
    assert questions == []


def test_resolve_repo_cwd_refuses_guesses(tmp_path):
    with pytest.raises(ValueError):
        resolve_repo_cwd("")
    with pytest.raises(ValueError):
        resolve_repo_cwd("relative/path")
    with pytest.raises(ValueError):
        resolve_repo_cwd(str(tmp_path / "nope"))


def test_build_fleet_prompt_golden(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    evidence = Evidence(
        query="add retries",
        chats=[Citation(source="chat", key="a1b2c3d4", title="T", excerpt="E")],
        memory=[Citation(source="mem", key="user:1", excerpt="M")],
        receipt_ids=["r1"],
    )
    artifact = build_fleet_prompt(
        evidence, {"repo": str(repo), "context": "flaky", "done": "green"}
    )
    assert artifact.cwd == str(repo.resolve())
    assert "Task: add retries" in artifact.task
    assert "Operator context: flaky" in artifact.task
    assert "Done means: green" in artifact.task
    assert "chat:a1b2c3d4 — T (E)" in artifact.task
    assert "mem:user:1 (M)" in artifact.task
    assert "r1" in artifact.task
    assert artifact.citations == ["chat:a1b2c3d4 — T (E)", "mem:user:1 (M)"]


def test_build_fleet_prompt_requires_repo():
    with pytest.raises(ValueError):
        build_fleet_prompt(Evidence(query="q"), {})
