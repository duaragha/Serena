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
    policy_models_match_contract,
    resolve_activity,
    validate_config,
    validate_policy_snapshot,
)
from core.fleet_store import (
    MAX_STEERING_TOTAL_CHARS,
    FleetStore,
)


def _create(
    store: FleetStore,
    *,
    activity: str = "research",
    dry_run: bool = False,
    worker_count: int = 2,
):
    # Two agents by default so peer, rotation and sibling behaviour still has a
    # peer to exercise. A phase now runs one model, so worker count is an
    # explicit choice rather than something the provider pairing forced.
    return store.create_run(
        task="line one\n\nline two",
        activity=activity,
        cwd=str(Path.cwd()),
        origin_session_id=None,
        origin_agent=None,
        dry_run=dry_run,
        policy=build_policy(
            activity, config=builtin_config(), worker_count=worker_count
        ).to_dict(),
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
    # One locked model per phase; every agent in that phase runs it.
    assert all(len(phase.workers) == 1 for phase in coding.phases)
    assert [
        [(worker.model, worker.effort) for worker in phase.workers]
        for phase in coding.phases
    ] == [
        [("gpt-5.6-luna", "max")],
        # Code runs medium and Fix runs high on purpose: Code's defects are
        # caught by Review and repaired by Fix, Fix's are not caught by anything.
        [("claude-opus-5", "medium")],
        [("gpt-5.6-sol", "high")],
        [("claude-opus-5", "high")],
    ]
    assert all(phase.execution == "parallel" for phase in coding.phases)
    assert all(worker.access_mode == "read_only" for worker in coding.phases[0].workers)
    assert all(worker.access_mode == "write" for worker in coding.phases[1].workers)
    assert all(worker.access_mode == "review" for worker in coding.phases[2].workers)
    assert all(worker.access_mode == "write" for worker in coding.phases[3].workers)
    assert [[worker.role for worker in phase.workers] for phase in coding.phases] == [
        ["codebase-researcher"],
        ["core-co-implementer"],
        ["correctness-reviewer"],
        ["functional-fixer"],
    ]

    research = build_policy("research", config=builtin_config())
    assert [
        [(worker.model, worker.effort) for worker in phase.workers]
        for phase in research.phases
    ] == [
        [("gpt-5.6-luna", "max")],
        [("claude-opus-5", "high")],
        [("gpt-5.6-sol", "high")],
        [("claude-opus-5", "high")],
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
    # Phase 0 ran before the handoff and keeps the pipeline's Codex research leg.
    assert replacement["phases"][0]["workers"][1]["provider"] == "codex"
    # Every unfinished phase moves to the Codex escape-hatch stack.
    assert [phase["workers"][1]["model"] for phase in replacement["phases"][1:]] == [
        "gpt-5.6-sol",
        "gpt-5.6-sol",
        "gpt-5.6-sol",
    ]
    assert [phase["workers"][1]["effort"] for phase in replacement["phases"][1:]] == [
        "xhigh",
        "high",
        "max",
    ]
    assert all(
        phase["workers"][1]["worker_key"] == "agent:b"
        for phase in replacement["phases"]
    )
    # The agent keeps its name through a handoff; only the runtime under it moves.
    assert replacement["phases"][1]["workers"][1]["worker_label"] == "Agent B"
    assert replacement["handoffs"] == [
        {
            "phase_index": 1,
            "phase": "execute",
            "ordinal": 1,
            "worker_key": "agent:b",
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
    # Phase 0 is Codex for every agent now, so the provider under pressure
    # there is Codex and the pickup provider is Claude.
    exhausted_leg = run["phases"][0]["legs"][1]
    first = store.begin_attempt(exhausted_leg["leg_id"])
    store.finish_attempt(
        first["attempt_id"],
        state="failed",
        output_text="edited parser.py and reached the focused test",
        error="rate_limit_reached",
        session_id="codex-native-session",
        actual_model="gpt-5.6-luna",
        actual_effort="max",
        exit_code=1,
    )
    replacement = build_provider_handoff_policy(
        run["policy"],
        phase_index=0,
        ordinal=1,
        target_provider="claude",
        reason="Codex usage exhausted",
        automatic=True,
        requested_at=456.0,
    )
    store.apply_leg_handoff(
        run["run_id"],
        exhausted_leg["leg_id"],
        policy=replacement,
        start_phase_index=0,
        target_provider="claude",
        reason="Codex usage exhausted",
        automatic=True,
    )

    pickup = store.begin_attempt(exhausted_leg["leg_id"])
    refreshed = store.get_run(run["run_id"])

    assert pickup["resume_session_id"] is None
    assert pickup["resume_kind"] == "provider_handoff"
    assert pickup["handoff_context"]["provider"] == "codex"
    assert "edited parser.py" in pickup["handoff_context"]["output_text"]
    assert pickup["handoff_context"]["session_id"] == "codex-native-session"
    assert refreshed["phases"][0]["legs"][1]["runtime"] == "claude"
    assert all(phase["legs"][1]["runtime"] == "claude" for phase in refreshed["phases"])
    assert refreshed["phases"][0]["legs"][1]["current_attempt"][
        "requested_provider"
    ] == "claude"


def test_live_handoff_interrupts_only_the_selected_owned_worker(tmp_path, monkeypatch):
    store = FleetStore(tmp_path / "live-handoff.sqlite3")
    run = _create(store, activity="research")
    # Phase 0 is Codex for every agent now, so the provider under pressure
    # there is Codex and the pickup provider is Claude.
    exhausted_leg = run["phases"][0]["legs"][1]
    attempt = store.begin_attempt(exhausted_leg["leg_id"])
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
        exhausted_leg["leg_id"],
        target_provider="claude",
        reason="switch before Codex exhausts",
    )

    live_leg = pending["phases"][0]["legs"][1]
    assert interrupted == [(4242, "owned-birth")]
    assert live_leg["state"] == "running"
    assert live_leg["handoff_requested"] is True
    assert live_leg["handoff_target_provider"] == "claude"
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
    config["profiles"]["coding"]["phases"]["verify"][0]["model"] = "gpt-5.6-terra"
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
    assert extract_explicit_workstreams(
        """Use four independent workstreams:
A. fix auth
B. build settings
C. migrate data
D. trim bundle

Acceptance criteria:
- full gate green
"""
    ) == (
        "fix auth",
        "build settings",
        "migrate data",
        "trim bundle",
    )
    assert (
        extract_explicit_workstreams("requirements:\n- fix auth\n- build settings\n- migrate data")
        == ()
    )
    assert extract_explicit_workstreams("1. research\n2. code\n3. review\n4. fix") == ()


def test_three_explicit_workstreams_select_three_durable_agents():
    policy = build_policy(
        "coding",
        "1. fix auth\n2. build settings\n3. migrate data",
        config=builtin_config(),
    )

    assert policy.max_parallel_workers == 4
    assert policy.max_parallel_writers == 2
    # One agent per workstream. The old rule rounded three up to four so the
    # codex/claude pairing stayed even; there is no pairing to keep even now.
    assert policy.scaling.selected_workers == 3
    assert policy.scaling.detected_workstreams == 3
    assert policy.scaling.capacity_limited is False
    # One workstream per agent, and one agent per detected workstream. The old
    # rule padded three up to four so the provider pairing stayed even.
    assert [workstream.workstream_id for workstream in policy.workstreams] == [
        "ws-1",
        "ws-2",
        "ws-3",
    ]
    # No synthetic padding: every workstream here is one the task named.
    assert all(not workstream.synthetic for workstream in policy.workstreams)
    assert policy.workstreams[-1].title == "migrate data"
    units = {unit["id"]: unit for unit in policy.work_units}
    # Three named workstreams, three agents, one unit each. Nothing synthetic is
    # appended now that the roster is not padded to an even provider pairing.
    assert set(units) == {"ws-1", "ws-2", "ws-3"}
    assert units["ws-1"]["owner_worker_key"] == "agent:a"
    assert units["ws-1"]["reviewer_worker_keys"] == ["agent:c"]
    assert units["ws-1"]["dependency_ids"] == []
    assert units["ws-3"]["owner_worker_key"] == "agent:c"
    assert units["ws-3"]["reviewer_worker_keys"] == ["agent:b"]
    assert units["ws-3"]["completion_contract"]["required_evidence"]
    assert units["ws-3"]["completion_contract"]["stop_conditions"]

    expected_keys = ["agent:a", "agent:b", "agent:c"]
    expected_models = [
        ["gpt-5.6-luna"] * 3,
        ["claude-opus-5"] * 3,
        ["gpt-5.6-sol"] * 3,
        ["claude-opus-5"] * 3,
    ]
    # Effort is named per model because the curves differ in shape; every agent
    # in a phase shares the phase's one rung.
    expected_efforts = [
        ["max"] * 3,
        ["medium"] * 3,
        ["high"] * 3,
        ["high"] * 3,
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
        ]
        assert [worker.review_target_ids for worker in phase.workers] == [
            ("ws-2",),
            ("ws-3",),
            ("ws-1",),
        ]
        for worker in phase.workers:
            assert not set(worker.assignment_ids) & set(worker.review_target_ids)


def test_independent_lettered_workstreams_do_not_create_synthetic_workers():
    policy = build_policy(
        "coding",
        """Use four independent workstreams:
A. fix auth
B. build settings
C. migrate data
D. trim bundle
""",
        config=builtin_config(),
        worker_count=4,
    )

    assert policy.scaling.detected_workstreams == 4
    assert [worker.assignment for worker in policy.phases[0].workers] == [
        "ws-1: fix auth",
        "ws-2: build settings",
        "ws-3: migrate data",
        "ws-4: trim bundle",
    ]
    assert all(not workstream.synthetic for workstream in policy.workstreams)


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
            "agent:a",
            "agent:b",
            "agent:c",
            "agent:d",
        ]
    assert [
        [worker.model for worker in phase.workers] for phase in policy.phases
    ] == [
        ["gpt-5.6-luna"] * 4,
        ["gpt-5.6-sol"] * 4,
        ["gpt-5.6-sol"] * 4,
        ["gpt-5.6-sol"] * 4,
    ]
    assert [
        [worker.effort for worker in phase.workers] for phase in policy.phases
    ] == [["max"] * 4, ["xhigh"] * 4, ["high"] * 4, ["max"] * 4]


@pytest.mark.parametrize(
    ("task", "provider", "models"),
    [
        (
            "no-claude: implement the fix",
            "codex",
            ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-sol", "gpt-5.6-sol"],
        ),
        (
            "no-codex: implement the fix",
            "claude",
            ["claude-opus-5", "claude-opus-5", "claude-opus-5", "claude-opus-5"],
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
        == [f"agent:{chr(ord('a') + index)}" for index in range(count)]
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
    assert policy.scaling.selected_workers == 1


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
    # Three agents is legal now. A phase runs one model, so there is no pair to
    # keep even and no reason to reject an odd count.
    assert (
        build_policy(
            "coding",
            config=builtin_config(),
            provider_mode="balanced",
            worker_count=3,
        ).scaling.selected_workers
        == 3
    )


def test_provider_directive_parser_does_not_treat_product_adjectives_as_routing():
    for task in (
        "research Codex-only APIs and Claude-only features",
        "No Claude evidence was found in the source material",
        "Only Codex supports this API according to the claim under review",
    ):
        policy = build_policy("research", task, config=builtin_config())
        assert policy.provider_mode == "balanced"
        assert policy.scaling.selected_workers == 1


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


def test_read_and_negated_paths_do_not_become_write_ownership(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("instructions\n")
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "shared.py").write_text("value = 1\n")

    paths = extract_declared_paths(
        "Read CLAUDE.md before acting. Do not edit core/shared.py. "
        "Implement issue #107 and run the repository checks.",
        cwd=tmp_path,
    )

    assert paths == []


def test_read_only_path_reference_uses_repository_serialized_write_claim(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("instructions\n")
    workstreams = [
        {
            "id": "ws-1",
            "title": "implementation",
            "description": "Read CLAUDE.md, then implement the requested fix.",
        }
    ]
    workers = [{"worker_key": "agent:a", "assignment_ids": ["ws-1"]}]

    units = build_work_unit_contracts("coding", workstreams, workers, cwd=tmp_path)

    assert units[0]["file_ownership"] == {
        "mode": "repository_serialized",
        "declared_paths": [],
        "claim_required_before_write": True,
        "claim_required_before_integration": True,
    }


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
        {"worker_key": "agent:a", "assignment_ids": ["ws-1"]},
        {"worker_key": "agent:a", "assignment_ids": ["ws-2"]},
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
        {"worker_key": "agent:a", "assignment_ids": ["ws-1"]},
        {"worker_key": "agent:b", "assignment_ids": ["ws-2"]},
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
    assert restored.scaling.selected_workers == 1
    assert restored.scaling.requested_workers is None
    assert restored.requested_provider_mode == "auto"
    assert restored.provider_mode == "balanced"
    assert restored.workstreams == ()
    assert restored.work_units == ()
    assert len(restored.phases[0].workers) == 1


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
    # Code is the Claude phase in the pipeline; Research runs on Codex.
    claude_leg = run["phases"][1]["legs"][0]

    first = store.begin_attempt(claude_leg["leg_id"])
    store.mark_attempt_session(first["attempt_id"], "assigned-but-never-created")
    store.finish_attempt(first["attempt_id"], state="failed", exit_code=1)
    second = store.begin_attempt(claude_leg["leg_id"])
    assert second["resume_session_id"] is None

    store.finish_attempt(
        second["attempt_id"],
        state="failed",
        session_id="confirmed-session",
        actual_model="claude-opus-5",
        exit_code=1,
    )
    third = store.begin_attempt(claude_leg["leg_id"])
    assert third["resume_session_id"] == "confirmed-session"


def test_code_and_fix_share_a_session_while_review_starts_clean(tmp_path):
    """The two halves of the session rule, in one run.

    Fix continues the session Code worked in: same provider, same agent, and the
    fixer repairing code it wrote is a benefit. Review deliberately does not
    continue Research even though both land on Codex, because a reviewer that
    sat through the research cannot independently disagree with it.
    """

    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store, activity="coding")

    def complete(leg, session_id):
        attempt = store.begin_attempt(leg["leg_id"])
        store.finish_attempt(
            attempt["attempt_id"],
            state="completed",
            session_id=session_id,
            actual_model=leg["model"],
            actual_effort=leg["effort"],
            exit_code=0,
        )
        return attempt

    for index, leg in enumerate(run["phases"][0]["legs"]):
        opened = complete(leg, f"research-session-{index}")
        assert opened["resume_session_id"] is None

    for index, leg in enumerate(run["phases"][1]["legs"]):
        opened = store.begin_attempt(leg["leg_id"])
        # Research ran on Codex, so Code opens a fresh Claude session.
        assert opened["resume_session_id"] is None
        store.finish_attempt(
            opened["attempt_id"],
            state="completed",
            session_id=f"code-session-{index}",
            actual_model=leg["model"],
            actual_effort=leg["effort"],
            exit_code=0,
        )

    for leg in run["phases"][2]["legs"]:
        review = store.begin_attempt(leg["leg_id"])
        assert leg["runtime"] == "codex"
        assert review["resume_session_id"] is None
        assert review["resume_kind"] is None

    for index, leg in enumerate(run["phases"][3]["legs"]):
        fix = store.begin_attempt(leg["leg_id"])
        assert fix["resume_session_id"] == f"code-session-{index}"
        assert fix["resume_kind"] == "phase_continuation"
        assert fix["resume_source_phase"] == "execute"


def test_claude_confirmed_lineage_survives_a_preinit_resumed_failure(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store)
    # Code and Fix are the Claude pair in the pipeline.
    execute = run["phases"][1]["legs"][0]
    first = store.begin_attempt(execute["leg_id"])
    store.finish_attempt(
        first["attempt_id"],
        state="completed",
        session_id="durable-claude",
        actual_model=execute["model"],
        actual_effort=execute["effort"],
        exit_code=0,
    )
    finalize = run["phases"][3]["legs"][0]
    resumed = store.begin_attempt(finalize["leg_id"])
    assert resumed["resume_session_id"] == "durable-claude"
    store.finish_attempt(
        resumed["attempt_id"],
        state="failed",
        session_id="durable-claude",
        exit_code=1,
    )

    retry = store.begin_attempt(finalize["leg_id"])
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


def test_every_coding_work_unit_gets_the_full_research_mandate():
    local = build_policy(
        "coding",
        "fix the handoff gate in ui/web.py so the electron shell stops bouncing it",
        config=builtin_config(),
    )
    external = build_policy(
        "coding",
        "upgrade the pinned numpy dependency and fix the breaking API changes",
        config=builtin_config(),
    )
    assert local.research_depth == "full"
    assert local.to_dict()["research_depth"] == "full"
    assert external.research_depth == "full"


def test_latest_events_returns_the_tail_in_chronological_order(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store, worker_count=1)
    for index in range(5):
        store.append_event(run["run_id"], f"test.event.{index}", {"index": index})

    earliest = store.events(run["run_id"], limit=2)
    latest = store.events(run["run_id"], limit=2, latest=True)

    assert [event["type"] for event in earliest] == ["run.created", "test.event.0"]
    assert [event["type"] for event in latest] == ["test.event.3", "test.event.4"]

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


def test_editing_the_matrix_does_not_strand_runs_already_in_flight():
    """A run keeps the contract it was created with, even after the matrix moves.

    Fleet freezes a policy at run creation so the run stays reproducible, but
    the contract check re-read the live matrix. Editing the matrix while a run
    was in flight therefore invalidated it, and its next handoff or retry died
    with "provider handoff violated Fleet's phase model contract" over a
    contract Fleet itself had issued. That is exactly what killed run 8678bc11.
    """

    frozen = build_policy(
        "coding", "tasks:\n- one\n- two", config=builtin_config(), worker_count=2
    ).to_dict()
    # Simulate the matrix moving under it: the run holds an effort no current
    # matrix or escape-hatch stack names any more.
    for phase in frozen["phases"]:
        if phase["name"] == "execute":
            for worker in phase["workers"]:
                worker["effort"] = "xhigh"

    assert policy_models_match_contract("coding", frozen) is False
    assert policy_models_match_contract("coding", frozen, baseline=frozen) is True

    # And the handoff that used to abort now rebuilds the slot.
    replacement = build_provider_handoff_policy(
        frozen,
        phase_index=1,
        ordinal=0,
        target_provider="codex",
        reason="claude exhausted",
        automatic=True,
    )
    assert replacement["phases"][1]["workers"][0]["provider"] == "codex"
    assert replacement["phases"][1]["workers"][0]["worker_key"] == "agent:a"

    # Inventing a model is still refused.
    invented = build_policy("coding", config=builtin_config()).to_dict()
    invented["phases"][1]["workers"][0]["model"] = "not-a-real-model"
    assert policy_models_match_contract("coding", invented, baseline=frozen) is False


def test_one_provider_may_carry_both_its_pipeline_and_escape_hatch_spec():
    """Keyed by provider alone, the second legal spec replaced the first."""

    from core.fleet_policy import PHASE_MODEL_POLICY, PROVIDER_ONLY_POLICY

    snapshot = build_policy("coding", config=builtin_config()).to_dict()
    for phase in snapshot["phases"]:
        name = phase["name"]
        pipeline = PHASE_MODEL_POLICY["coding"][name][0]
        hatch = PROVIDER_ONLY_POLICY[pipeline[0]]["coding"][name][0]
        for spec in (pipeline, hatch):
            for worker in phase["workers"]:
                worker["provider"] = spec[0]
                worker["runtime"] = spec[0]
                worker["model"] = spec[1]
                worker["effort"] = spec[2]
            assert policy_models_match_contract("coding", snapshot), (name, spec)


def test_a_wedged_run_can_be_forced_out_and_then_retried(tmp_path):
    """The one case where "just retry it" had no answer.

    Cancel is a request that only the supervisor converges, and retry demands a
    terminal state. A supervisor that is alive but cannot make progress -- the
    real case was ENOSPC, where it could not write the transition at all --
    leaves a run that can be neither cancelled nor retried. Forcing is narrow on
    purpose: never while a worker lives, never while the run is still moving.
    """

    store = FleetStore(tmp_path / "wedged.sqlite3")
    run = _create(store, activity="coding")
    run_id = str(run["run_id"])
    assert store.claim_run(run_id)

    leg = run["phases"][0]["legs"][0]
    attempt = store.begin_attempt(str(leg["leg_id"]))
    store.mark_attempt_process(str(attempt["attempt_id"]), os.getpid())

    # A live worker is never cut off, however long the run has been quiet.
    with pytest.raises(ValueError, match="live worker processes"):
        store.force_cancel_run(run_id, min_stall_seconds=0)

    store.finish_attempt(
        str(attempt["attempt_id"]),
        state="failed",
        error="worker died",
        exit_code=1,
    )

    # Nor is a supervisor that might simply be mid-step.
    with pytest.raises(ValueError, match="force needs"):
        store.force_cancel_run(run_id, min_stall_seconds=600)

    forced = store.force_cancel_run(run_id, min_stall_seconds=0, reason="ENOSPC")
    assert forced["state"] == "cancelled"
    assert "no progress and no live workers" in forced["error"]
    assert "ENOSPC" in forced["error"]

    # Which is the point: it is retryable again without a second Fleet.
    assert store.retry_run(run_id)["state"] == "queued"

    # And a run that already reached a terminal state is left alone.
    store.cancel_run(run_id)
    with pytest.raises(ValueError, match="already cancelled"):
        store.force_cancel_run(run_id, min_stall_seconds=0)


def test_terminal_run_retry_refuses_a_still_live_worker(tmp_path):
    store = FleetStore(tmp_path / "live-retry.sqlite3")
    run = _create(store, activity="coding", worker_count=1)
    run_id = str(run["run_id"])
    assert store.claim_run(run_id)

    leg = run["phases"][0]["legs"][0]
    attempt = store.begin_attempt(str(leg["leg_id"]))
    store.mark_attempt_process(str(attempt["attempt_id"]), os.getpid())
    store.fail_run(run_id, "controlled terminal race")

    with pytest.raises(ValueError, match="worker processes are still alive"):
        store.retry_run(run_id)

    store.finish_attempt(
        str(attempt["attempt_id"]),
        state="failed",
        error="worker drained",
        exit_code=1,
    )
    assert store.retry_run(run_id)["state"] == "queued"
