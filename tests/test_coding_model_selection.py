from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.coding_job_contract import CodingJobBrief, capture_git_snapshot
from core.coding_model_preferences import (
    AUTO_MODEL,
    CLAUDE_MODEL,
    CODEX_MODEL,
    SONNET_MODEL,
    TERRA_MODEL,
    read_coding_model_preference,
    write_coding_model_preference,
)
from core.coding_provider import choose_providers
from core.voice_inbox import VoiceInboxStore
from core.voice_work_supervisor import VoiceWorkSupervisor
from core.work_authority import start_coding_work
from core.work_session_router import WorkRoute
from voice.desktop.coding_jobs_query import read_coding_jobs


def _repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("test repository\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Serena Tests",
            "-c",
            "user.email=serena-tests@local",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    return path


def _context(repo: Path) -> dict[str, object]:
    return {
        "project": str(repo),
        "requested_outcome": "make the requested coding change",
        "relevant_conversation": [],
        "project_context": [f"repository: {repo}"],
        "memory_guidance": [],
        "ledger_guidance": [],
        "handoff_guidance": [],
        "acceptance_criteria": ["the change works"],
        "authority_boundaries": ["edit only this repository"],
    }


class _Inbox:
    def __init__(self) -> None:
        self.brief: dict | None = None

    @staticmethod
    def resident_lease_active() -> bool:
        return True

    @staticmethod
    def item_for_turn(*, call_id: str, turn_id: str):
        return None

    def enqueue_accepted(self, brief, *, call_id: str, turn_id: str):
        self.brief = dict(brief)
        return SimpleNamespace(item_id=brief["item_id"])


def test_selected_model_is_frozen_when_a_future_job_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "project")
    preference = tmp_path / "coding-model.json"
    monkeypatch.setenv("SERENA_CODING_MODEL_PATH", str(preference))
    write_coding_model_preference(CLAUDE_MODEL)
    inbox = _Inbox()
    route = WorkRoute(
        mode="private",
        preference="auto",
        project_root=str(repo),
        reason="test starts privately",
    )

    result = start_coding_work(
        f"implement the model selection in {repo}",
        origin={"protocol": "voice", "text": "implement the model selection", "call_id": "c1", "turn_id": "t1"},
        brief_context=_context(repo),
        inbox=inbox,
        audit_path=tmp_path / "audit.jsonl",
        route_resolver=lambda _root, _spoken: route,
    )

    assert result.allowed
    assert inbox.brief is not None
    assert inbox.brief["coding_model"] == CLAUDE_MODEL
    write_coding_model_preference(CODEX_MODEL)
    assert inbox.brief["coding_model"] == CLAUDE_MODEL


def test_frozen_selection_drives_provider_choice_and_auto_keeps_default(tmp_path: Path) -> None:
    calls: list[dict] = []
    assignment = object()

    def choose(**kwargs):
        calls.append(kwargs)
        return assignment

    supervisor = VoiceWorkSupervisor(
        store=object(),
        marker_path=tmp_path / "worker.json",
        provider_chooser=choose,
    )

    assert supervisor._provider_assignment({"coding_model": CLAUDE_MODEL}) is assignment
    assert supervisor._provider_assignment({"coding_model": CODEX_MODEL}) is assignment
    assert supervisor._provider_assignment({}) is assignment
    assert calls == [
        {"preferred_implementer": "claude"},
        {"preferred_implementer": "codex"},
        {},
    ]


def test_missing_or_corrupt_preference_and_legacy_brief_fall_back_to_auto(
    tmp_path: Path,
) -> None:
    preference = tmp_path / "coding-model.json"
    assert read_coding_model_preference(preference) == AUTO_MODEL
    preference.write_text("not json", encoding="utf-8")
    assert read_coding_model_preference(preference) == AUTO_MODEL
    with pytest.raises(ValueError, match="unsupported coding model"):
        write_coding_model_preference("mystery-model", preference)

    repo = _repo(tmp_path / "legacy")
    brief = CodingJobBrief.create(
        item_id="legacy-model-job",
        exact_request="legacy request",
        triggering_request="implement the legacy request",
        project_root=repo,
        initial_git=capture_git_snapshot(repo, item_id="legacy-model-job", label="baseline"),
    ).to_dict()
    brief.pop("coding_model")
    store = VoiceInboxStore(tmp_path / "voice.sqlite3")
    store.enqueue_accepted(brief, call_id="legacy", turn_id="legacy:1")
    persisted = store.accepted_brief("legacy-model-job")
    assert persisted is not None
    assert persisted.get("coding_model", AUTO_MODEL) == AUTO_MODEL


def test_preference_file_round_trips_canonical_model(tmp_path: Path) -> None:
    preference = tmp_path / "coding-model.json"
    assert write_coding_model_preference("opus", preference) == CLAUDE_MODEL
    assert json.loads(preference.read_text(encoding="utf-8")) == {"model": CLAUDE_MODEL}
    assert read_coding_model_preference(preference) == CLAUDE_MODEL
    assert write_coding_model_preference("terra", preference) == TERRA_MODEL
    assert write_coding_model_preference("sonnet 5", preference) == SONNET_MODEL


def test_selected_model_is_visible_before_launch_and_capacity_fallback_is_truthful(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "visible")
    brief = CodingJobBrief.create(
        item_id="visible-model-job",
        exact_request="show the selected model",
        triggering_request="implement it with opus",
        project_root=repo,
        initial_git=capture_git_snapshot(
            repo, item_id="visible-model-job", label="baseline"
        ),
        context={"coding_model": CLAUDE_MODEL},
    ).to_dict()
    database = tmp_path / "voice.sqlite3"
    store = VoiceInboxStore(database)
    store.enqueue_accepted(brief, call_id="visible", turn_id="visible:1")

    projected = read_coding_jobs(database)
    assert projected[0]["model"] == {
        "selection": CLAUDE_MODEL,
        "requested": CLAUDE_MODEL,
        "effort": "high",
        "reported": "",
        "reported_effort": "",
    }

    capacity = {
        "codex": {"usable": True, "reason": "available"},
        "claude": {"usable": False, "reason": "rate limited"},
    }
    assignment = choose_providers(capacity, preferred_implementer="claude")
    assert assignment.implement_provider == "codex"
    assert "claude is out of capacity" in assignment.reason
