from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from core import fleet_store as store_module
from core.fleet_contracts import build_work_unit_contracts, extract_declared_paths
from core.fleet_policy import (
    PHASES,
    build_policy,
    build_provider_handoff_policy,
    builtin_config,
    expected_model_matches,
    extract_explicit_workstreams,
    policy_from_snapshot,
    resolve_activity,
    validate_config,
    validate_policy_snapshot,
)
from core.fleet_store import (
    MAX_STEERING_TOTAL_CHARS,
    FleetStore,
)


def _create(store: FleetStore, *, activity: str = "research", dry_run: bool = False):
    return store.create_run(
        task="line one\n\nline two",
        activity=activity,
        cwd=str(Path.cwd()),
        origin_session_id=None,
        origin_agent=None,
        dry_run=dry_run,
        policy=build_policy(activity, config=builtin_config()).to_dict(),
    )


def test_policy_routes_exact_models_and_safe_coding_writers():
    coding = build_policy("coding", config=builtin_config())
    assert coding.session_mode == "persistent_by_worker"
    assert tuple(phase.name for phase in coding.phases) == PHASES
    assert [phase.display_name for phase in coding.phases] == [
        "Research",
        "Code",
        "Review",
        "Fix",
    ]
    assert all(len(phase.workers) >= 2 for phase in coding.phases)
    assert [
        [(worker.model, worker.effort) for worker in phase.workers]
        for phase in coding.phases
    ] == [
        [("gpt-5.6-terra", "high"), ("claude-sonnet-5", "high")],
        [("gpt-5.6-sol", "xhigh"), ("claude-opus-5", "high")],
        [("gpt-5.6-terra", "xhigh"), ("claude-sonnet-5", "xhigh")],
        [("gpt-5.6-sol", "xhigh"), ("claude-opus-5", "high")],
    ]
    assert all(phase.execution == "parallel" for phase in coding.phases)
    assert all(worker.access_mode == "read_only" for worker in coding.phases[0].workers)
    assert all(worker.access_mode == "write" for worker in coding.phases[1].workers)
    assert all(worker.access_mode == "review" for worker in coding.phases[2].workers)
    assert all(worker.access_mode == "write" for worker in coding.phases[3].workers)
    assert [[worker.role for worker in phase.workers] for phase in coding.phases] == [
        ["codebase-researcher", "architecture-risk-researcher"],
        ["core-co-implementer", "integration-co-implementer"],
        ["correctness-reviewer", "regression-reviewer"],
        ["functional-fixer", "hardening-fixer"],
    ]

    research = build_policy("research", config=builtin_config())
    assert [
        [(worker.model, worker.effort) for worker in phase.workers]
        for phase in research.phases
    ] == [
        [("gpt-5.6-terra", "high"), ("claude-sonnet-5", "high")],
        [("gpt-5.6-terra", "high"), ("claude-sonnet-5", "high")],
        [("gpt-5.6-terra", "xhigh"), ("claude-sonnet-5", "xhigh")],
        [("gpt-5.6-terra", "high"), ("claude-sonnet-5", "high")],
    ]
    assert all(phase.execution == "parallel" for phase in research.phases)
    assert all(
        worker.access_mode in {"read_only", "review"}
        for phase in research.phases
        for worker in phase.workers
    )


def test_provider_handoff_preserves_the_logical_slot_and_uses_each_phase_model():
    original = build_policy(
        "coding",
        "tasks:\n- auth\n- checkout",
        config=builtin_config(),
        provider_mode="balanced",
    ).to_dict()

    replacement = build_provider_handoff_policy(
        original,
        phase_index=1,
        ordinal=1,
        target_provider="codex",
        reason="Claude usage exhausted",
        automatic=True,
        requested_at=123.0,
    )

    validate_policy_snapshot(replacement)
    assert replacement["provider_mode"] == "adaptive"
    assert replacement["phases"][0]["workers"][1]["provider"] == "claude"
    assert [phase["workers"][1]["model"] for phase in replacement["phases"][1:]] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ]
    assert all(
        phase["workers"][1]["worker_key"] == "claude:a"
        for phase in replacement["phases"]
    )
    assert replacement["phases"][1]["workers"][1]["worker_label"] == (
        "Codex pickup for Claude A"
    )
    assert replacement["handoffs"] == [
        {
            "phase_index": 1,
            "phase": "execute",
            "ordinal": 1,
            "worker_key": "claude:a",
            "from_provider": "claude",
            "to_provider": "codex",
            "reason": "Claude usage exhausted",
            "automatic": True,
            "requested_at": 123.0,
        }
    ]


def test_cross_provider_attempt_never_resumes_a_foreign_session_and_carries_partial_work(
    tmp_path,
):
    store = FleetStore(tmp_path / "handoff.sqlite3")
    run = _create(store, activity="coding")
    claude_leg = run["phases"][0]["legs"][1]
    first = store.begin_attempt(claude_leg["leg_id"])
    store.finish_attempt(
        first["attempt_id"],
        state="failed",
        output_text="edited parser.py and reached the focused test",
        error="rate_limit_reached",
        session_id="claude-native-session",
        actual_model="claude-sonnet-5",
        actual_effort="high",
        exit_code=1,
    )
    replacement = build_provider_handoff_policy(
        run["policy"],
        phase_index=0,
        ordinal=1,
        target_provider="codex",
        reason="Claude usage exhausted",
        automatic=True,
        requested_at=456.0,
    )
    store.apply_leg_handoff(
        run["run_id"],
        claude_leg["leg_id"],
        policy=replacement,
        start_phase_index=0,
        target_provider="codex",
        reason="Claude usage exhausted",
        automatic=True,
    )

    pickup = store.begin_attempt(claude_leg["leg_id"])
    refreshed = store.get_run(run["run_id"])

    assert pickup["resume_session_id"] is None
    assert pickup["resume_kind"] == "provider_handoff"
    assert pickup["handoff_context"]["provider"] == "claude"
    assert "edited parser.py" in pickup["handoff_context"]["output_text"]
    assert pickup["handoff_context"]["session_id"] == "claude-native-session"
    assert refreshed["phases"][0]["legs"][1]["runtime"] == "codex"
    assert all(phase["legs"][1]["runtime"] == "codex" for phase in refreshed["phases"])
    assert refreshed["phases"][0]["legs"][1]["current_attempt"][
        "requested_provider"
    ] == "codex"


def test_live_handoff_interrupts_only_the_selected_owned_worker(tmp_path, monkeypatch):
    store = FleetStore(tmp_path / "live-handoff.sqlite3")
    run = _create(store, activity="research")
    claude_leg = run["phases"][0]["legs"][1]
    attempt = store.begin_attempt(claude_leg["leg_id"])
    monkeypatch.setattr(store_module, "process_start_token", lambda _pid: "owned-birth")
    store.mark_attempt_process(attempt["attempt_id"], 4242)
    interrupted = []
    monkeypatch.setattr(
        store_module,
        "_terminate_owned_process",
        lambda pid, token: interrupted.append((pid, token)) or True,
    )

    pending = store.request_leg_handoff(
        run["run_id"],
        claude_leg["leg_id"],
        target_provider="codex",
        reason="switch before Claude exhausts",
    )

    live_leg = pending["phases"][0]["legs"][1]
    assert interrupted == [(4242, "owned-birth")]
    assert live_leg["state"] == "running"
    assert live_leg["handoff_requested"] is True
    assert live_leg["handoff_target_provider"] == "codex"
    assert pending["phases"][0]["legs"][0]["handoff_requested"] is False


def test_coding_opus_identity_is_generation_strict() -> None:
    assert expected_model_matches("claude", "claude-opus-5", "claude-opus-5")
    assert expected_model_matches("claude", "opus", "claude-opus-5-20260801")
    assert not expected_model_matches("claude", "opus", "claude-opus-4-1-20250805")
    assert expected_model_matches(
        "claude", "claude-sonnet-5", "claude-sonnet-5-20260801"
    )
    assert expected_model_matches(
        "claude", "claude-haiku-4-5", "claude-haiku-4-5-20251001"
    )
    assert not expected_model_matches(
        "claude", "claude-haiku-4-5", "claude-haiku-5"
    )


def test_phase_model_policy_cannot_be_overridden_by_stale_config() -> None:
    config = builtin_config()
    config["profiles"]["coding"]["phases"]["verify"][0]["model"] = "gpt-5.6-sol"
    with pytest.raises(ValueError, match="fixed model policy"):
        validate_config(config)


def test_policy_accepts_a_single_worker_floor_and_auto_routes():
    config = builtin_config()
    assert config["defaults"]["minimum_workers_per_phase"] == 1
    config["defaults"]["max_parallel_workers"] = 1
    validate_config(config)
    solo = build_policy(
        "coding",
        config=config,
        provider_mode="codex",
        worker_count=1,
    )
    assert solo.scaling.selected_workers == 1
    assert resolve_activity("implement the backend", "auto") == "coding"
    assert resolve_activity("research primary sources", "auto") == "research"


def test_explicit_workstream_parser_is_conservative():
    assert extract_explicit_workstreams("objectives: fix auth; build settings; migrate data") == (
        "fix auth",
        "build settings",
        "migrate data",
    )
    assert extract_explicit_workstreams("tasks:\n- fix auth\n- build settings\n- migrate data") == (
        "fix auth",
        "build settings",
        "migrate data",
    )
    assert extract_explicit_workstreams("1. fix auth\n2. build settings\n3. migrate data") == (
        "fix auth",
        "build settings",
        "migrate data",
    )
    assert extract_explicit_workstreams(
        "task 1 — fix auth\ntask 2 — build settings\ntask 3 — migrate data"
    ) == (
        "fix auth",
        "build settings",
        "migrate data",
    )
    assert (
        extract_explicit_workstreams("requirements:\n- fix auth\n- build settings\n- migrate data")
        == ()
    )
    assert extract_explicit_workstreams("1. research\n2. code\n3. review\n4. fix") == ()


def test_three_explicit_workstreams_select_four_balanced_durable_workers():
    policy = build_policy(
        "coding",
        "1. fix auth\n2. build settings\n3. migrate data",
        config=builtin_config(),
    )

    assert policy.max_parallel_workers == 4
    assert policy.max_parallel_writers == 2
    assert policy.scaling.selected_workers == 4
    assert policy.scaling.detected_workstreams == 3
    assert policy.scaling.capacity_limited is False
    assert [workstream.workstream_id for workstream in policy.workstreams] == [
        "ws-1",
        "ws-2",
        "ws-3",
        "ws-4",
    ]
    assert policy.workstreams[-1].synthetic is True
    assert policy.workstreams[-1].title == "integration and shared dependencies"
    units = {unit["id"]: unit for unit in policy.work_units}
    assert set(units) == {"ws-1", "ws-2", "ws-3", "ws-4"}
    assert units["ws-1"]["owner_worker_key"] == "codex:a"
    assert units["ws-1"]["reviewer_worker_keys"] == ["claude:b"]
    assert units["ws-1"]["dependency_ids"] == []
    assert units["ws-4"]["owner_worker_key"] == "claude:b"
    assert units["ws-4"]["reviewer_worker_keys"] == ["codex:b"]
    assert units["ws-4"]["dependency_ids"] == ["ws-1", "ws-2", "ws-3"]
    assert units["ws-4"]["dependency_mode"] == "phase_barrier"
    assert units["ws-4"]["file_ownership"] == {
        "mode": "repository_serialized",
        "declared_paths": [],
        "claim_required_before_write": True,
        "claim_required_before_integration": True,
    }
    assert units["ws-4"]["completion_contract"]["required_evidence"]
    assert units["ws-4"]["completion_contract"]["stop_conditions"]

    expected_keys = ["codex:a", "claude:a", "codex:b", "claude:b"]
    expected_models = [
        ["gpt-5.6-terra", "claude-sonnet-5"] * 2,
        ["gpt-5.6-sol", "claude-opus-5"] * 2,
        ["gpt-5.6-terra", "claude-sonnet-5"] * 2,
        ["gpt-5.6-sol", "claude-opus-5"] * 2,
    ]
    # Effort is per model, not per phase: Opus peaks at high on DeepSWE while
    # Sol keeps climbing to xhigh, so the coding rungs differ within a phase.
    expected_efforts = [
        ["high", "high"] * 2,
        ["xhigh", "high"] * 2,
        ["xhigh", "xhigh"] * 2,
        ["xhigh", "high"] * 2,
    ]
    for phase, phase_models, phase_efforts in zip(
        policy.phases, expected_models, expected_efforts, strict=True
    ):
        assert [worker.worker_key for worker in phase.workers] == expected_keys
        assert [worker.model for worker in phase.workers] == phase_models
        assert [worker.effort for worker in phase.workers] == phase_efforts
        assert [worker.assignment_ids for worker in phase.workers] == [
            ("ws-1",),
            ("ws-2",),
            ("ws-3",),
            ("ws-4",),
        ]
        assert [worker.review_target_ids for worker in phase.workers] == [
            ("ws-2",),
            ("ws-3",),
            ("ws-4",),
            ("ws-1",),
        ]
        for worker in phase.workers:
            assert not set(worker.assignment_ids) & set(worker.review_target_ids)


def test_more_than_four_workstreams_are_distributed_round_robin():
    policy = build_policy(
        "research",
        "tasks:\n- one\n- two\n- three\n- four\n- five\n- six",
        config=builtin_config(),
    )
    workers = policy.phases[0].workers
    assert [worker.assignment_ids for worker in workers] == [
        ("ws-1", "ws-5"),
        ("ws-2", "ws-6"),
        ("ws-3",),
        ("ws-4",),
    ]
    assert workers[0].review_target_ids == workers[1].assignment_ids
    assert workers[1].review_target_ids == workers[2].assignment_ids
    assert workers[2].review_target_ids == workers[3].assignment_ids
    assert workers[3].review_target_ids == workers[0].assignment_ids
    assert all(
        unit["file_ownership"]["mode"] == "read_only"
        and unit["file_ownership"]["claim_required_before_write"] is False
        and unit["file_ownership"]["claim_required_before_integration"] is False
        for unit in policy.work_units
    )


def test_work_unit_contract_validation_rejects_dependency_cycles_and_unknown_owners():
    snapshot = build_policy(
        "coding",
        "tasks:\n- parser\n- storage",
        config=builtin_config(),
    ).to_dict()
    snapshot["work_units"][0]["dependency_ids"] = ["ws-2"]
    snapshot["work_units"][0]["dependency_mode"] = "phase_barrier"
    snapshot["work_units"][1]["dependency_ids"] = ["ws-1"]
    snapshot["work_units"][1]["dependency_mode"] = "phase_barrier"
    with pytest.raises(ValueError, match="acyclic"):
        validate_policy_snapshot(snapshot)

    snapshot = build_policy("research", config=builtin_config()).to_dict()
    snapshot["work_units"][0]["owner_worker_key"] = "missing:owner"
    with pytest.raises(ValueError, match="unknown owner"):
        validate_policy_snapshot(snapshot)


def test_exact_four_codex_workers_only_directive_builds_four_native_codex_slots():
    policy = build_policy(
        "coding",
        "use FOUR Codex workers only",
        config=builtin_config(),
    )

    assert policy.requested_provider_mode == "codex"
    assert policy.provider_mode == "codex"
    assert policy.minimum_workers_per_phase == 1
    assert policy.scaling.requested_workers == 4
    assert policy.scaling.selected_workers == 4
    for phase in policy.phases:
        assert [worker.provider for worker in phase.workers] == ["codex"] * 4
        assert [worker.worker_key for worker in phase.workers] == [
            "codex:a",
            "codex:b",
            "codex:c",
            "codex:d",
        ]
    assert [
        [worker.model for worker in phase.workers] for phase in policy.phases
    ] == [
        ["gpt-5.6-terra"] * 4,
        ["gpt-5.6-sol"] * 4,
        ["gpt-5.6-terra"] * 4,
        ["gpt-5.6-sol"] * 4,
    ]
    assert [
        [worker.effort for worker in phase.workers] for phase in policy.phases
    ] == [["high"] * 4, ["xhigh"] * 4, ["xhigh"] * 4, ["xhigh"] * 4]


@pytest.mark.parametrize(
    ("task", "provider", "models"),
    [
        (
            "no-claude: implement the fix",
            "codex",
            ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-sol"],
        ),
        (
            "no-codex: implement the fix",
            "claude",
            ["claude-sonnet-5", "claude-opus-5", "claude-sonnet-5", "claude-opus-5"],
        ),
    ],
)
def test_excluded_provider_directive_selects_one_worker_with_an_honest_self_review(
    task,
    provider,
    models,
):
    policy = build_policy("coding", task, config=builtin_config())

    assert policy.provider_mode == provider
    assert policy.minimum_workers_per_phase == 1
    assert policy.scaling.selected_workers == 1
    assert policy.scaling.requested_workers is None
    for phase, model in zip(policy.phases, models, strict=True):
        assert len(phase.workers) == 1
        assert phase.workers[0].provider == provider
        assert phase.workers[0].model == model
        assert phase.workers[0].review_target_ids == ()


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_single_provider_accepts_every_worker_count_from_one_through_four(count):
    policy = build_policy(
        "research",
        "research the controlled question",
        config=builtin_config(),
        provider_mode="claude",
        worker_count=count,
    )

    assert policy.requested_provider_mode == "claude"
    assert policy.provider_mode == "claude"
    assert policy.scaling.requested_workers == count
    assert policy.scaling.selected_workers == count
    assert all(
        [worker.worker_key for worker in phase.workers]
        == [f"claude:{chr(ord('a') + index)}" for index in range(count)]
        for phase in policy.phases
    )


def test_single_provider_infers_one_worker_per_explicit_workstream_up_to_four():
    three = build_policy(
        "coding",
        "tasks:\n- fix auth\n- build settings\n- migrate data",
        config=builtin_config(),
        provider_mode="codex",
    )
    many = build_policy(
        "coding",
        "tasks:\n- one\n- two\n- three\n- four\n- five",
        config=builtin_config(),
        provider_mode="codex",
    )

    assert three.scaling.selected_workers == 3
    assert [worker.assignment_ids for worker in three.phases[0].workers] == [
        ("ws-1",),
        ("ws-2",),
        ("ws-3",),
    ]
    assert many.scaling.selected_workers == 4


def test_mixed_alias_is_preserved_as_requested_and_canonicalized_to_balanced():
    policy = build_policy(
        "research",
        config=builtin_config(),
        provider_mode="mixed",
    )
    assert policy.requested_provider_mode == "mixed"
    assert policy.provider_mode == "balanced"
    assert policy.scaling.selected_workers == 2


def test_provider_capacity_routes_auto_to_the_only_usable_provider():
    policy = build_policy(
        "research",
        "research the controlled question",
        config=builtin_config(),
        provider_capacity={
            "claude": {
                "usable": False,
                "status": "exhausted",
                "reason": "weekly cap reached",
            },
            "codex": {"usable": True, "status": "ready"},
        },
    )

    assert policy.requested_provider_mode == "auto"
    assert policy.provider_mode == "codex"
    assert policy.scaling.selected_workers == 1
    assert {worker.provider for worker in policy.phases[0].workers} == {"codex"}


def test_provider_and_worker_directive_conflicts_fail_closed():
    with pytest.raises(ValueError, match="provider_mode claude conflicts"):
        build_policy(
            "coding",
            "use FOUR Codex workers only",
            config=builtin_config(),
            provider_mode="claude",
        )
    with pytest.raises(ValueError, match="conflicting codex-only and claude-only"):
        build_policy(
            "coding",
            "no-claude. no-codex.",
            config=builtin_config(),
        )
    with pytest.raises(ValueError, match="worker_count conflicts"):
        build_policy(
            "coding",
            "use FOUR Codex workers only",
            config=builtin_config(),
            worker_count=2,
        )
    with pytest.raises(ValueError, match="exactly two or four"):
        build_policy(
            "coding",
            config=builtin_config(),
            provider_mode="balanced",
            worker_count=3,
        )


def test_provider_directive_parser_does_not_treat_product_adjectives_as_routing():
    for task in (
        "research Codex-only APIs and Claude-only features",
        "No Claude evidence was found in the source material",
        "Only Codex supports this API according to the claim under review",
    ):
        policy = build_policy("research", task, config=builtin_config())
        assert policy.provider_mode == "balanced"
        assert policy.scaling.selected_workers == 2


def test_explicit_unavailable_provider_fails_without_silent_fallback():
    with pytest.raises(ValueError, match="weekly cap reached"):
        build_policy(
            "research",
            config=builtin_config(),
            provider_mode="claude",
            provider_capacity={
                "claude": {"usable": False, "reason": "weekly cap reached"},
            },
        )


def test_four_worker_request_respects_a_two_worker_capacity():
    config = builtin_config()
    config["defaults"]["max_parallel_workers"] = 2
    policy = build_policy(
        "coding",
        "1. fix auth\n2. build settings\n3. migrate data",
        config=config,
    )
    assert len(policy.phases[0].workers) == 2
    assert policy.scaling.selected_workers == 2
    assert policy.scaling.capacity_limited is True


def test_policy_rejects_worker_capacity_above_four_but_allows_writer_tuning():
    config = builtin_config()
    config["defaults"]["max_parallel_workers"] = 5
    with pytest.raises(ValueError, match="cannot exceed 4"):
        validate_config(config)

    config = builtin_config()
    config["defaults"]["max_parallel_writers"] = 3
    validate_config(config)
    assert build_policy("coding", config=config).max_parallel_writers == 3


def test_declared_paths_use_real_repository_tokens_and_ignore_prose(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "shared.py").write_text("value = 1\n")
    (tmp_path / "docs").mkdir()

    paths = extract_declared_paths(
        "Own only core/shared.py and docs/. Ignore missing/new.py during verification.",
        cwd=tmp_path,
    )

    assert paths == ["core/shared.py", "docs"]


def test_overlapping_declared_paths_gain_a_deterministic_dependency(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "shared.py").write_text("value = 1\n")
    workstreams = [
        {"id": "ws-1", "title": "core owner", "description": "edit core/"},
        {
            "id": "ws-2",
            "title": "shared owner",
            "description": "edit core/shared.py",
        },
    ]
    workers = [
        {"worker_key": "codex:a", "assignment_ids": ["ws-1"]},
        {"worker_key": "claude:a", "assignment_ids": ["ws-2"]},
    ]

    units = build_work_unit_contracts("coding", workstreams, workers, cwd=tmp_path)
    by_id = {unit["id"]: unit for unit in units}

    assert by_id["ws-1"]["dependency_ids"] == []
    assert by_id["ws-1"]["file_ownership"] == {
        "mode": "declare_before_edit",
        "declared_paths": ["core"],
        "claim_required_before_write": True,
        "claim_required_before_integration": True,
    }
    assert by_id["ws-2"]["dependency_ids"] == ["ws-1"]
    assert by_id["ws-2"]["dependency_mode"] == "phase_barrier"


def test_shared_verification_path_is_not_treated_as_owned(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "alpha.py").write_text("alpha = 1\n")
    (tmp_path / "core" / "beta.py").write_text("beta = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_probe.py").write_text("def test_probe(): pass\n")
    workstreams = [
        {
            "id": "ws-1",
            "title": "alpha",
            "description": (
                "Own only core/alpha.py. Run pytest tests/test_probe.py and record it."
            ),
        },
        {
            "id": "ws-2",
            "title": "beta",
            "description": (
                "Own only core/beta.py. Run pytest tests/test_probe.py and record it."
            ),
        },
    ]
    workers = [
        {"worker_key": "codex:a", "assignment_ids": ["ws-1"]},
        {"worker_key": "codex:b", "assignment_ids": ["ws-2"]},
    ]

    units = build_work_unit_contracts("coding", workstreams, workers, cwd=tmp_path)
    by_id = {unit["id"]: unit for unit in units}

    assert by_id["ws-1"]["file_ownership"]["declared_paths"] == ["core/alpha.py"]
    assert by_id["ws-2"]["file_ownership"]["declared_paths"] == ["core/beta.py"]
    assert by_id["ws-1"]["dependency_ids"] == []
    assert by_id["ws-2"]["dependency_ids"] == []


def test_old_policy_snapshots_default_new_scaling_fields_without_resizing():
    snapshot = build_policy("research", config=builtin_config()).to_dict()
    snapshot.pop("requested_provider_mode")
    snapshot.pop("provider_mode")
    snapshot.pop("max_parallel_writers")
    snapshot.pop("scaling")
    snapshot.pop("workstreams")
    snapshot.pop("work_units")
    for phase in snapshot["phases"]:
        for worker in phase["workers"]:
            worker.pop("worker_key")
            worker.pop("worker_label")
            worker.pop("assignment")
            worker.pop("assignment_ids")
            worker.pop("review_target_ids")

    restored = policy_from_snapshot(snapshot)
    assert restored.max_parallel_writers == 2
    assert restored.scaling.mode == "legacy"
    assert restored.scaling.selected_workers == 2
    assert restored.scaling.requested_workers is None
    assert restored.requested_provider_mode == "auto"
    assert restored.provider_mode == "balanced"
    assert restored.workstreams == ()
    assert restored.work_units == ()
    assert len(restored.phases[0].workers) == 2


def test_store_dry_run_is_a_durable_eight_leg_plan(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store, dry_run=True)
    assert run["state"] == "planned"
    assert run["task"] == "line one\n\nline two"
    assert run["progress"] == {"completed": 0, "total": 8}
    assert run["agent_count"] == 2
    assert run["chat_count"] == 0
    assert len(run["work_units"]) == 2
    assert {unit["state"] for unit in run["work_units"]} == {"planned"}
    assert all(unit["progress"] == {"completed": 0, "total": 4} for unit in run["work_units"])
    assert str(run["worker_group_id"]).startswith("g_")
    assert [phase["name"] for phase in run["phases"]] == list(PHASES)
    assert [phase["display_name"] for phase in run["phases"]] == [
        "Research",
        "Analyze",
        "Review",
        "Refine",
    ]
    assert all(phase["state"] == "planned" for phase in run["phases"])

    queued = _create(store)
    assert all(phase["state"] == "queued" for phase in queued["phases"])


def test_phase_stays_running_while_a_failed_workers_peer_is_live(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    assert store.claim_run(run["run_id"])
    failed_leg, live_leg = run["phases"][0]["legs"]
    failed = store.begin_attempt(failed_leg["leg_id"])
    store.begin_attempt(live_leg["leg_id"])
    store.finish_attempt(failed["attempt_id"], state="failed", error="network", exit_code=1)

    snapshot = store.get_run(run["run_id"])
    assert snapshot is not None
    assert snapshot["phases"][0]["state"] == "running"


def test_store_status_previews_but_explicit_result_is_full(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    assert store.claim_run(run["run_id"])
    for phase in run["phases"]:
        for leg in phase["legs"]:
            attempt = store.begin_attempt(leg["leg_id"])
            store.finish_attempt(
                attempt["attempt_id"],
                state="completed",
                output_text="worker-" + ("x" * 8_000),
                actual_model=leg["model"],
                actual_effort=leg["effort"],
                exit_code=0,
            )
    full = "final-" + ("z" * 20_000)
    snapshot = store.complete_run(run["run_id"], full)
    assert snapshot["result_truncated"] is True
    assert len(snapshot["result_text"]) == 4_000
    assert all(
        len(leg["current_attempt"]["output_text"]) == 4_000
        for phase in snapshot["phases"]
        for leg in phase["legs"]
    )
    assert store.get_result(run["run_id"])["result_text"] == full


def test_cancel_wins_the_completion_race(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    assert store.claim_run(run["run_id"])
    assert store.request_cancel(run["run_id"])["state"] == "stopping"
    completed = store.complete_run(run["run_id"], "must not escape")
    assert completed["state"] == "cancelled"
    assert completed["result_text"] is None


def test_claude_retry_resumes_only_after_provider_init_confirmed_the_session(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    claude_leg = next(leg for leg in run["phases"][0]["legs"] if leg["runtime"] == "claude")

    first = store.begin_attempt(claude_leg["leg_id"])
    store.mark_attempt_session(first["attempt_id"], "assigned-but-never-created")
    store.finish_attempt(first["attempt_id"], state="failed", exit_code=1)
    second = store.begin_attempt(claude_leg["leg_id"])
    assert second["resume_session_id"] is None

    store.finish_attempt(
        second["attempt_id"],
        state="failed",
        session_id="confirmed-session",
        actual_model="claude-sonnet-5",
        exit_code=1,
    )
    third = store.begin_attempt(claude_leg["leg_id"])
    assert third["resume_session_id"] == "confirmed-session"


def test_phase_legs_resume_the_same_two_native_worker_sessions(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    first_phase = run["phases"][0]
    session_by_runtime = {
        "codex": "codex-durable-session",
        "claude": "claude-durable-session",
    }
    for leg in first_phase["legs"]:
        attempt = store.begin_attempt(leg["leg_id"])
        assert attempt["resume_session_id"] is None
        store.finish_attempt(
            attempt["attempt_id"],
            state="completed",
            session_id=session_by_runtime[leg["runtime"]],
            actual_model=leg["model"],
            actual_effort=leg["effort"],
            exit_code=0,
        )

    for leg in run["phases"][1]["legs"]:
        attempt = store.begin_attempt(leg["leg_id"])
        assert attempt["resume_session_id"] == session_by_runtime[leg["runtime"]]
        assert attempt["resume_kind"] == "phase_continuation"
        assert attempt["resume_source_phase"] == "discover"


def test_claude_confirmed_lineage_survives_a_preinit_resumed_failure(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    discover = next(leg for leg in run["phases"][0]["legs"] if leg["runtime"] == "claude")
    first = store.begin_attempt(discover["leg_id"])
    store.finish_attempt(
        first["attempt_id"],
        state="completed",
        session_id="durable-claude",
        actual_model=discover["model"],
        actual_effort=discover["effort"],
        exit_code=0,
    )
    execute = next(leg for leg in run["phases"][1]["legs"] if leg["runtime"] == "claude")
    resumed = store.begin_attempt(execute["leg_id"])
    assert resumed["resume_session_id"] == "durable-claude"
    store.finish_attempt(
        resumed["attempt_id"],
        state="failed",
        session_id="durable-claude",
        exit_code=1,
    )

    retry = store.begin_attempt(execute["leg_id"])
    assert retry["resume_session_id"] == "durable-claude"
    assert retry["resume_kind"] == "retry"


def test_legacy_policy_snapshot_keeps_phase_local_sessions(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    policy = build_policy("research", config=builtin_config()).to_dict()
    policy.pop("session_mode")
    run = store.create_run(
        task="legacy run",
        activity="research",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent=None,
        dry_run=False,
        policy=policy,
    )
    first_leg = run["phases"][0]["legs"][0]
    first = store.begin_attempt(first_leg["leg_id"])
    store.finish_attempt(
        first["attempt_id"],
        state="completed",
        session_id="legacy-session",
        actual_model=first_leg["model"],
        actual_effort=first_leg["effort"],
        exit_code=0,
    )
    next_leg = run["phases"][1]["legs"][0]
    assert store.begin_attempt(next_leg["leg_id"])["resume_session_id"] is None


def test_unstarted_run_from_a_stale_mcp_is_safely_upgraded(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    policy = build_policy("research", config=builtin_config()).to_dict()
    policy.pop("session_mode")
    run = store.create_run(
        task="new run from stale host",
        activity="research",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent="codex",
        dry_run=False,
        policy=policy,
    )
    assert "session_mode" not in run["policy"]
    assert store.upgrade_unstarted_session_mode(run["run_id"]) is True
    upgraded = store.get_run(run["run_id"])
    assert upgraded is not None
    assert upgraded["policy"]["session_mode"] == "persistent_by_worker"
    assert store.upgrade_unstarted_session_mode(run["run_id"]) is False


def test_started_legacy_run_is_never_upgraded_midflight(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    policy = build_policy("research", config=builtin_config()).to_dict()
    policy.pop("session_mode")
    run = store.create_run(
        task="started legacy run",
        activity="research",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent="codex",
        dry_run=False,
        policy=policy,
    )
    store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    assert store.upgrade_unstarted_session_mode(run["run_id"]) is False
    frozen = store.get_run(run["run_id"])
    assert frozen is not None
    assert "session_mode" not in frozen["policy"]


def test_active_worker_retry_requeues_immediately_while_sibling_is_live(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    assert store.claim_run(run["run_id"])
    failed_leg, live_leg = run["phases"][0]["legs"]
    failed = store.begin_attempt(failed_leg["leg_id"])
    live = store.begin_attempt(live_leg["leg_id"])
    store.finish_attempt(failed["attempt_id"], state="failed", error="network", exit_code=1)

    queued = store.request_leg_retry(run["run_id"], failed_leg["leg_id"])
    queued_leg = queued["phases"][0]["legs"][0]
    assert queued["state"] == "running"
    assert queued_leg["state"] == "queued"
    assert queued_leg["retry_requested"] is False

    retry = store.begin_attempt(failed_leg["leg_id"])
    store.finish_attempt(retry["attempt_id"], state="completed", output_text="retried")
    store.finish_attempt(live["attempt_id"], state="completed", output_text="done", exit_code=0)
    completed_phase = store.get_run(run["run_id"])["phases"][0]
    assert completed_phase["state"] == "completed"


def test_cancel_wins_retry_activation_at_the_phase_barrier(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    assert store.claim_run(run["run_id"])
    failed_leg, live_leg = run["phases"][0]["legs"]
    failed = store.begin_attempt(failed_leg["leg_id"])
    live = store.begin_attempt(live_leg["leg_id"])
    store.finish_attempt(failed["attempt_id"], state="failed", error="network", exit_code=1)
    store.request_leg_retry(run["run_id"], failed_leg["leg_id"])
    store.finish_attempt(live["attempt_id"], state="completed", output_text="done", exit_code=0)

    assert store.request_cancel(run["run_id"])["state"] == "stopping"
    resolved = store.resolve_phase_failure(run["run_id"], "discover", "controlled")
    assert resolved["state"] == "cancelled"
    assert resolved["retry_activated"] is False
    assert resolved["phases"][0]["legs"][0]["state"] == "cancelled"
    assert resolved["phases"][0]["legs"][0]["retry_requested"] is False


def test_terminal_run_retry_can_target_one_failed_worker(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    assert store.claim_run(run["run_id"])
    first_leg, second_leg = run["phases"][0]["legs"]
    for leg in (first_leg, second_leg):
        attempt = store.begin_attempt(leg["leg_id"])
        store.finish_attempt(attempt["attempt_id"], state="failed", error="controlled")
    store.fail_run(run["run_id"], "discover failed")

    retried = store.request_leg_retry(run["run_id"], first_leg["leg_id"])
    states = {leg["leg_id"]: leg["state"] for leg in retried["phases"][0]["legs"]}
    assert retried["state"] == "queued"
    assert states[first_leg["leg_id"]] == "queued"
    assert states[second_leg["leg_id"]] == "failed"


def test_terminal_run_can_queue_multiple_failed_workers_before_claim(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    assert store.claim_run(run["run_id"])
    first_leg, second_leg = run["phases"][0]["legs"]
    for leg in (first_leg, second_leg):
        attempt = store.begin_attempt(leg["leg_id"])
        store.finish_attempt(attempt["attempt_id"], state="failed", error="controlled")
    store.fail_run(run["run_id"], "discover failed")

    store.request_leg_retry(run["run_id"], first_leg["leg_id"])
    retried = store.request_leg_retry(run["run_id"], second_leg["leg_id"])
    states = {leg["leg_id"]: leg["state"] for leg in retried["phases"][0]["legs"]}
    assert retried["state"] == "queued"
    assert states == {first_leg["leg_id"]: "queued", second_leg["leg_id"]: "queued"}


def test_capacity_wait_is_durable_unclaimable_and_resumes_the_same_worker(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    assert store.claim_run(run["run_id"])
    failed_leg, sibling = run["phases"][0]["legs"]
    failed_attempt = store.begin_attempt(failed_leg["leg_id"])
    sibling_attempt = store.begin_attempt(sibling["leg_id"])
    store.finish_attempt(
        failed_attempt["attempt_id"],
        state="failed",
        output_text="saved work",
        error="rate_limit_reached",
        exit_code=1,
    )
    store.finish_attempt(
        sibling_attempt["attempt_id"],
        state="completed",
        output_text="sibling complete",
        exit_code=0,
    )
    due = 1_900_000_000.0
    parked = store.request_capacity_wait(
        run["run_id"],
        failed_leg["leg_id"],
        failed_provider=failed_leg["runtime"],
        eligible_providers=[failed_leg["runtime"]],
        reason="native usage exhausted",
        not_before=due,
        resets_at=due + 60,
    )
    wait = parked["phases"][0]["legs"][0]["capacity_wait"]
    assert wait["eligible_providers"] == [failed_leg["runtime"]]
    assert wait["not_before"] == due

    resolved = store.resolve_phase_failure(run["run_id"], "discover", "capacity")
    assert resolved["state"] == "waiting_for_capacity"
    assert resolved["capacity_waiting"] is True
    assert store.claim_run(run["run_id"]) is False
    assert store.next_queued_run() is None
    assert store.capacity_waits(run["run_id"])[0]["leg_id"] == failed_leg["leg_id"]

    resumed = store.resume_capacity_wait(
        run["run_id"],
        failed_leg["leg_id"],
        provider=failed_leg["runtime"],
        reason="native capacity recovered",
    )
    assert resumed["state"] == "queued"
    assert resumed["phases"][0]["legs"][0]["state"] == "queued"
    assert resumed["capacity_waits"] == []
    assert store.next_queued_run() == run["run_id"]


def test_capacity_wait_can_be_cancelled_without_leaving_a_resume_record(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    assert store.claim_run(run["run_id"])
    first, second = run["phases"][0]["legs"]
    first_attempt = store.begin_attempt(first["leg_id"])
    second_attempt = store.begin_attempt(second["leg_id"])
    store.finish_attempt(first_attempt["attempt_id"], state="failed", error="quota")
    store.finish_attempt(second_attempt["attempt_id"], state="completed", output_text="done")
    store.request_capacity_wait(
        run["run_id"],
        first["leg_id"],
        failed_provider=first["runtime"],
        eligible_providers=[first["runtime"]],
        reason="quota",
        not_before=1_900_000_000.0,
    )
    store.resolve_phase_failure(run["run_id"], "discover", "quota")

    cancelled = store.request_cancel(run["run_id"])
    assert cancelled["state"] == "cancelled"
    assert cancelled["capacity_waits"] == []
    assert store.capacity_waits(run["run_id"]) == []


def test_steering_preserves_newlines_and_caps_cumulative_context(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    message = "first line\n\nsecond line"
    store.add_steering(run["run_id"], message)
    assert store.steering_messages(run["run_id"]) == [message]
    remaining = MAX_STEERING_TOTAL_CHARS - len(message)
    store.add_steering(run["run_id"], "x" * min(8_000, remaining))
    with pytest.raises(ValueError, match="cumulative"):
        store.add_steering(run["run_id"], "y" * 8_000)


def test_delete_run_requires_terminal_state_and_cascades_all_records(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)

    with pytest.raises(RuntimeError, match="stop this Fleet"):
        store.delete_run(run["run_id"])

    cancelled = store.request_cancel(run["run_id"])
    assert cancelled["state"] == "cancelled"
    deleted = store.delete_run(run["run_id"])

    assert deleted["run_id"] == run["run_id"]
    assert deleted["state"] == "cancelled"
    assert store.get_run(run["run_id"]) is None
    assert all(item["run_id"] != run["run_id"] for item in store.list_runs())


def test_stale_recovery_terminates_only_token_matched_worker(
    tmp_path,
    monkeypatch,
):
    token = "linux-start-token"
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(store_module, "process_start_token", lambda _pid: token)
    monkeypatch.setattr(store_module, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    owner_pid = 987_654_320
    worker_pid = 987_654_321
    assert store.claim_run(run["run_id"], owner_pid=owner_pid)
    attempt = store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    store.mark_attempt_process(attempt["attempt_id"], worker_pid)

    assert store.recover_stale_runs() == [run["run_id"]]
    recovered = store.get_run(run["run_id"])
    assert recovered is not None and recovered["state"] == "queued"
    assert recovered["phases"][0]["legs"][0]["current_attempt"]["state"] == "interrupted"
    assert killed == [(worker_pid, signal.SIGTERM)]


def test_stale_recovery_fails_closed_when_live_worker_token_is_unverifiable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(store_module, "process_start_token", lambda _pid: None)
    monkeypatch.setattr(store_module, "_pid_exists", lambda _pid: True)
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    assert store.claim_run(run["run_id"], owner_pid=987_654_310)
    attempt = store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    store.mark_attempt_process(attempt["attempt_id"], 987_654_311)

    assert store.recover_stale_runs() == []
    blocked = store.get_run(run["run_id"])
    assert blocked is not None and blocked["state"] == "running"
    assert store.events(run["run_id"])[-1]["type"] == "run.recovery_blocked"


def test_local_coding_work_gets_a_proportionate_research_mandate():
    """A local defect must not pay the full external-citation quota."""

    local = build_policy(
        "coding",
        "fix the handoff gate in ui/web.py so the electron shell stops bouncing it",
        config=builtin_config(),
    )
    assert local.research_depth == "proportionate"
    assert local.to_dict()["research_depth"] == "proportionate"


def test_outward_facing_coding_work_keeps_the_full_research_mandate():
    external = build_policy(
        "coding",
        "upgrade the pinned numpy dependency and fix the breaking API changes",
        config=builtin_config(),
    )
    assert external.research_depth == "full"

    research = build_policy(
        "research",
        "compare local vector stores for the knowledge index",
        config=builtin_config(),
    )
    assert research.research_depth == "full"


def test_a_snapshot_written_before_depth_existed_keeps_the_full_mandate():
    snapshot = build_policy("coding", "fix a local typo", config=builtin_config()).to_dict()
    snapshot.pop("research_depth")
    assert policy_from_snapshot(snapshot).research_depth == "full"
