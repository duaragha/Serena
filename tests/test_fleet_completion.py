"""Adversarial tests for Fleet's machine-enforced completion contracts.

Every test here is a way a worker can claim success it did not earn. The gate
is only worth having if each one is refused.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from core.fleet_completion import (
    EVIDENCE_CLOSE,
    EVIDENCE_OPEN,
    evaluate_completion,
    extract_envelope,
    render_evidence_instructions,
)
from core.fleet_contracts import build_work_unit_contracts

PROSE = "Here is the real answer for the next phase, with enough substance to read."


def _units(activity: str = "coding") -> list[dict]:
    workstreams = [
        {"id": "ws-1", "title": "First", "description": "the first bounded unit"},
        {"id": "ws-2", "title": "Second", "description": "the second bounded unit"},
    ]
    workers = [
        {"worker_key": "claude:a", "assignment_ids": ["ws-1"], "review_target_ids": ["ws-2"]},
        {"worker_key": "codex:a", "assignment_ids": ["ws-2"], "review_target_ids": ["ws-1"]},
    ]
    return build_work_unit_contracts(activity, workstreams, workers)


def _envelope(payload: dict) -> str:
    return f"{PROSE}\n\n{EVIDENCE_OPEN}\n{json.dumps(payload)}\n{EVIDENCE_CLOSE}"


def _good_unit(unit: dict, unit_id: str = "ws-1", **overrides) -> dict:
    criteria = unit["completion_contract"]["acceptance_criteria"]
    entry = {
        "id": unit_id,
        "status": "completed",
        "acceptance": [
            {"criterion": item, "met": True, "evidence": f"observed: {item}"}
            for item in criteria
        ],
        "constraints_respected": True,
        "changed_paths": [],
        "tests": [],
        "stop_condition": "",
    }
    entry.update(overrides)
    return entry


def _online_research(*, accessed_at: str | None = None) -> dict:
    accessed = accessed_at or date.today().isoformat()
    types = ("official", "primary", "research", "reputable_secondary", "community")
    return {
        "search_queries": ["current guidance", "2026 best practices", "recent technology"],
        "sources": [
            {
                "url": f"https://source{index}.example/direct",
                "title": f"Source {index}",
                "publisher": f"Publisher {index}",
                "accessed_at": accessed,
                "source_type": types[index - 1],
                "currency": "current as of the access date",
                "finding": f"Finding {index}",
                "relevance": f"Relevance {index}",
            }
            for index in range(1, 6)
        ],
        "best_practices": ["practice one", "practice two"],
        "recent_developments": ["recent ecosystem change"],
        "recommendation_impact": "the current evidence changes the implementation plan",
    }


def _evaluate(entry: dict, **kwargs):
    units = kwargs.pop("units", None) or _units()
    return evaluate_completion(
        output_text=_envelope({"schema_version": 1, "units": [entry]}),
        units=units,
        assignment_ids=kwargs.pop("assignment_ids", ["ws-1"]),
        **kwargs,
    )


def test_a_clean_read_only_report_is_accepted():
    units = _units()
    verdict = _evaluate(_good_unit(units[0]), units=units, access_mode="read")
    assert verdict.enforced is True
    assert verdict.accepted is True, verdict.failures
    assert verdict.units[0].claimed_status == "completed"


def test_research_phase_requires_extensive_online_evidence_and_observed_searches():
    units = _units(activity="research")
    missing = _evaluate(
        _good_unit(units[0]),
        units=units,
        access_mode="read_only",
        activity="research",
        phase="discover",
        observed_research_activity={"searches": 0, "fetches": 0},
    )
    assert missing.accepted is False
    assert any("online_research" in item for item in missing.failures)

    complete = _evaluate(
        _good_unit(units[0], online_research=_online_research()),
        units=units,
        access_mode="read_only",
        activity="research",
        phase="discover",
        observed_research_activity={"searches": 3, "fetches": 5},
    )
    assert complete.accepted is True, complete.failures


def test_research_phase_rejects_shallow_stale_or_unobserved_web_work():
    units = _units(activity="research")
    shallow = _online_research(
        accessed_at=(date.today() - timedelta(days=8)).isoformat()
    )
    shallow["search_queries"] = ["one query"]
    shallow["sources"] = shallow["sources"][:2]
    shallow["best_practices"] = ["one practice"]
    shallow["recent_developments"] = []
    verdict = _evaluate(
        _good_unit(units[0], online_research=shallow),
        units=units,
        access_mode="read_only",
        activity="research",
        phase="discover",
        observed_research_activity={"searches": 1, "fetches": 0},
    )
    assert verdict.accepted is False
    assert any("distinct search queries" in item for item in verdict.failures)
    assert any("recorded provider web searches" in item for item in verdict.failures)
    assert any("unique direct sources" in item for item in verdict.failures)
    assert any("last 7 days" in item for item in verdict.failures)
    assert any("best-practice" in item for item in verdict.failures)
    assert any("recent technology" in item for item in verdict.failures)


def test_a_leg_with_no_contract_is_reported_unenforced_not_passed():
    verdict = evaluate_completion(
        output_text="done",
        units=[],
        assignment_ids=["ws-1"],
    )
    assert verdict.enforced is False
    assert verdict.accepted is True
    assert "no work-unit contract" in verdict.reason


def test_missing_envelope_blocks_completion():
    units = _units()
    verdict = evaluate_completion(
        output_text="I finished everything, trust me.",
        units=units,
        assignment_ids=["ws-1"],
    )
    assert verdict.accepted is False
    assert verdict.blocked is True
    assert any("no <serena-evidence>" in item for item in verdict.failures)


def test_malformed_json_envelope_blocks_completion():
    units = _units()
    verdict = evaluate_completion(
        output_text=f"{PROSE}\n{EVIDENCE_OPEN}\nnot json at all\n{EVIDENCE_CLOSE}",
        units=units,
        assignment_ids=["ws-1"],
    )
    assert verdict.accepted is False
    assert any("not valid JSON" in item for item in verdict.failures)


def test_two_envelopes_block_completion():
    units = _units()
    body = json.dumps({"schema_version": 1, "units": [_good_unit(units[0])]})
    verdict = evaluate_completion(
        output_text=(
            f"{PROSE}{EVIDENCE_OPEN}{body}{EVIDENCE_CLOSE}"
            f"{EVIDENCE_OPEN}{body}{EVIDENCE_CLOSE}"
        ),
        units=units,
        assignment_ids=["ws-1"],
    )
    assert verdict.accepted is False
    assert any("more than one" in item for item in verdict.failures)


def test_completed_while_a_stop_condition_triggered_is_a_contradiction():
    units = _units()
    verdict = _evaluate(
        _good_unit(units[0], stop_condition="a dependency conflicted with a peer"),
        units=units,
    )
    assert verdict.accepted is False
    assert any("contradictory evidence" in item for item in verdict.failures)


def test_blocked_without_a_stop_condition_is_rejected():
    units = _units()
    verdict = _evaluate(
        _good_unit(units[0], status="blocked", stop_condition=""),
        units=units,
    )
    assert verdict.accepted is False
    assert any("must name the stop condition" in item for item in verdict.failures)


def test_blocked_with_a_named_stop_condition_is_accepted_as_an_honest_stop():
    units = _units()
    verdict = _evaluate(
        _good_unit(
            units[0],
            status="blocked",
            stop_condition="the requested result cannot be verified from available evidence",
            acceptance=[],
        ),
        units=units,
    )
    assert verdict.accepted is True, verdict.failures
    assert verdict.units[0].claimed_status == "blocked"


def test_unanswered_acceptance_criteria_block_completion():
    units = _units()
    verdict = _evaluate(_good_unit(units[0], acceptance=[]), units=units)
    assert verdict.accepted is False
    assert any("acceptance criterion" in item for item in verdict.failures)


def test_partially_answered_acceptance_criteria_block_completion():
    units = _units()
    entry = _good_unit(units[0])
    entry["acceptance"] = entry["acceptance"][:1]
    verdict = _evaluate(entry, units=units)
    assert verdict.accepted is False
    assert any("of 3 acceptance criteria" in item for item in verdict.failures)


def test_acceptance_criteria_must_match_the_contract_not_just_its_count():
    units = _units()
    entry = _good_unit(units[0])
    entry["acceptance"] = [
        {
            "criterion": f"unrelated claim {index}",
            "met": True,
            "evidence": "self-attested",
        }
        for index in range(3)
    ]
    verdict = _evaluate(entry, units=units)
    assert verdict.accepted is False
    assert any("not answered exactly" in item for item in verdict.failures)
    assert any("unknown or duplicate" in item for item in verdict.failures)


def test_duplicate_acceptance_criterion_cannot_replace_a_required_one():
    units = _units()
    entry = _good_unit(units[0])
    entry["acceptance"][1]["criterion"] = entry["acceptance"][0]["criterion"]
    verdict = _evaluate(entry, units=units)
    assert verdict.accepted is False
    assert any("not answered exactly" in item for item in verdict.failures)
    assert any("unknown or duplicate" in item for item in verdict.failures)


def test_criterion_marked_met_with_no_evidence_is_rejected():
    units = _units()
    entry = _good_unit(units[0])
    entry["acceptance"][0]["evidence"] = ""
    verdict = _evaluate(entry, units=units)
    assert verdict.accepted is False
    assert any("no evidence" in item for item in verdict.failures)


def test_completed_with_an_unmet_criterion_is_a_contradiction():
    units = _units()
    entry = _good_unit(units[0])
    entry["acceptance"][0]["met"] = False
    verdict = _evaluate(entry, units=units)
    assert verdict.accepted is False
    assert any("not met" in item for item in verdict.failures)


def test_constraints_must_be_affirmed():
    units = _units()
    verdict = _evaluate(_good_unit(units[0], constraints_respected=False), units=units)
    assert verdict.accepted is False
    assert any("constraints" in item for item in verdict.failures)


def test_a_missing_owned_unit_blocks_completion():
    units = _units()
    verdict = evaluate_completion(
        output_text=_envelope({"schema_version": 1, "units": [_good_unit(units[0])]}),
        units=units,
        assignment_ids=["ws-1", "ws-2"],
    )
    assert verdict.accepted is False
    assert any("ws-2" in item for item in verdict.failures)


def test_reporting_a_unit_the_leg_does_not_own_blocks_completion():
    units = _units()
    verdict = _evaluate(_good_unit(units[1], unit_id="ws-2"), units=units)
    assert verdict.accepted is False
    assert any("does not own" in item for item in verdict.failures)


def test_a_duplicate_unit_report_blocks_completion():
    units = _units()
    entry = _good_unit(units[0])
    verdict = evaluate_completion(
        output_text=_envelope({"schema_version": 1, "units": [entry, dict(entry)]}),
        units=units,
        assignment_ids=["ws-1"],
    )
    assert verdict.accepted is False
    assert any("more than once" in item for item in verdict.failures)


def test_a_read_only_leg_claiming_file_changes_is_rejected():
    units = _units()
    verdict = _evaluate(
        _good_unit(units[0], changed_paths=["core/thing.py"]),
        units=units,
        access_mode="read",
    )
    assert verdict.accepted is False
    assert any("read-only leg reported changed files" in item for item in verdict.failures)


def test_changed_code_without_a_test_command_is_rejected():
    units = _units()
    verdict = _evaluate(
        _good_unit(units[0], changed_paths=["core/thing.py"], tests=[]),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
    )
    assert verdict.accepted is False
    assert any("no recorded test command" in item for item in verdict.failures)


def test_a_failing_test_command_blocks_completion():
    units = _units()
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[{"command": "python -m pytest tests/test_thing.py", "exit_code": 1}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
    )
    assert verdict.accepted is False
    assert any("never exited clean" in item for item in verdict.failures)


def test_a_test_rerun_green_after_a_failure_is_accepted():
    units = _units()
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[
                {"command": "python -m pytest tests/test_thing.py", "exit_code": 1},
                {"command": "python -m pytest tests/test_thing.py", "exit_code": 0},
            ],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
        observed_test_results={"python -m pytest tests/test_thing.py": 0},
    )
    assert verdict.accepted is True, verdict.failures


def test_a_fake_test_command_does_not_satisfy_the_gate():
    units = _units()
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[{"command": "echo all good", "exit_code": 0}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
    )
    assert verdict.accepted is False
    assert any("looks like a real test" in item for item in verdict.failures)


def test_an_absolute_trusted_test_runner_satisfies_the_gate():
    units = _units()
    command = "/opt/trusted-tools/bin/ruff check core tests"
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[{"command": command, "exit_code": 0}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
        observed_test_results={command: 0},
    )
    assert verdict.accepted is True, verdict.failures


def test_a_direct_node_test_file_with_runtime_flags_satisfies_the_gate():
    units = _units()
    command = (
        "node --experimental-strip-types "
        "apps/worker-node/src/verify-ml-workers.node-test.mjs"
    )
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[{"command": command, "exit_code": 0}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
        observed_test_results={command: 0},
    )
    assert verdict.accepted is True, verdict.failures


def test_unittest_and_vitest_are_real_verification_commands():
    units = _units()
    commands = (
        "python3 -m unittest discover -s tests -v",
        "pnpm --filter app exec vitest run tests/example.test.ts",
    )
    for command in commands:
        verdict = _evaluate(
            _good_unit(
                units[0],
                changed_paths=["core/thing.py"],
                tests=[{"command": command, "exit_code": 0}],
            ),
            units=units,
            access_mode="write",
            claimed_paths=["core/thing.py"],
            observed_changed_paths=["core/thing.py"],
            observed_test_results={command: 0},
        )
        assert verdict.accepted is True, verdict.failures


def test_a_verification_command_with_no_exit_code_is_rejected():
    units = _units()
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[{"command": "python -m pytest tests/test_thing.py"}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
    )
    assert verdict.accepted is False
    assert any("no exit code" in item for item in verdict.failures)


def test_writing_a_file_with_no_active_claim_is_rejected():
    units = _units()
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/other.py"],
            tests=[{"command": "python -m pytest tests/test_thing.py", "exit_code": 0}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/other.py"],
    )
    assert verdict.accepted is False
    assert any("without an active path claim" in item for item in verdict.failures)


def test_a_directory_claim_covers_files_beneath_it():
    units = _units()
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/sub/thing.py"],
            tests=[{"command": "python -m pytest tests/test_thing.py", "exit_code": 0}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/sub"],
        observed_changed_paths=["core/sub/thing.py"],
        observed_test_results={"python -m pytest tests/test_thing.py": 0},
    )
    assert verdict.accepted is True, verdict.failures


def test_a_repository_claim_covers_concrete_changed_files():
    units = _units()
    changed_paths = [
        "assets/auction-bid-tracker.js",
        "scripts/fw43-events.test.cjs",
    ]
    command = "python -m pytest tests/test_thing.py"
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=changed_paths,
            tests=[{"command": command, "exit_code": 0}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["*"],
        observed_changed_paths=changed_paths,
        observed_test_results={command: 0},
    )
    assert verdict.accepted is True, verdict.failures


def test_worker_reported_green_cannot_override_a_fleet_observed_failure():
    units = _units()
    command = "python -m pytest tests/test_thing.py"
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[{"command": command, "exit_code": 0}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
        observed_test_results={command: 1},
    )
    assert verdict.accepted is False
    assert any("disagreed with Fleet observations" in item for item in verdict.failures)
    assert any("Fleet-observed verification" in item for item in verdict.failures)


def test_fleet_receipt_supplies_an_omitted_worker_exit_code():
    units = _units()
    command = "python -m pytest tests/test_thing.py"
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[{"command": command}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
        observed_test_results={command: 0},
    )

    assert verdict.accepted is True, verdict.failures


def test_omitted_worker_exit_without_a_fleet_receipt_is_rejected():
    units = _units()
    command = "python -m pytest tests/test_thing.py"
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[{"command": command}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
        observed_test_results={},
    )

    assert verdict.accepted is False
    assert any("recorded no exit code" in item for item in verdict.failures)


def test_node_assertion_and_new_file_whitespace_checks_count_as_verification():
    units = _units()
    assertion = 'node --input-type=module -e "import assert from \'node:assert\'; assert.ok(true)"'
    whitespace = "git diff --no-index --check /dev/null core/thing.py"
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[
                {"command": assertion, "exit_code": 0},
                {"command": whitespace, "exit_code": 0},
            ],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
        observed_test_results={assertion: 0, whitespace: 0},
    )

    assert verdict.accepted is True, verdict.failures


def test_changed_code_without_machine_observed_tests_is_rejected():
    units = _units()
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[
                {"command": "python -m pytest tests/test_thing.py", "exit_code": 0}
            ],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py"],
        observed_changed_paths=["core/thing.py"],
    )
    assert verdict.accepted is False
    assert any("machine-observed" in item for item in verdict.failures)


def test_undeclared_working_tree_changes_block_completion():
    """The strongest gate: the diff disagrees with what the worker declared."""

    units = _units()
    verdict = _evaluate(
        _good_unit(
            units[0],
            changed_paths=["core/thing.py"],
            tests=[{"command": "python -m pytest tests/test_thing.py", "exit_code": 0}],
        ),
        units=units,
        access_mode="write",
        claimed_paths=["core/thing.py", "core/secret.py"],
        observed_changed_paths=["core/thing.py", "core/secret.py"],
    )
    assert verdict.accepted is False
    assert any("never declared in its evidence" in item for item in verdict.failures)
    assert any("core/secret.py" in item for item in verdict.failures)


def test_completed_with_an_incomplete_dependency_is_a_contradiction():
    units = build_work_unit_contracts(
        "coding",
        [
            {"id": "ws-1", "title": "First", "description": "the first bounded unit"},
            {
                "id": "ws-2",
                "title": "Rollup",
                "description": "the dependent rollup unit",
                "synthetic": True,
            },
        ],
        [
            {"worker_key": "claude:a", "assignment_ids": ["ws-1"], "review_target_ids": []},
            {"worker_key": "codex:a", "assignment_ids": ["ws-2"], "review_target_ids": []},
        ],
    )
    rollup = next(unit for unit in units if unit["id"] == "ws-2")
    assert rollup["dependency_ids"] == ["ws-1"]
    verdict = evaluate_completion(
        output_text=_envelope(
            {"schema_version": 1, "units": [_good_unit(rollup, unit_id="ws-2")]}
        ),
        units=units,
        assignment_ids=["ws-2"],
        dependency_states={"ws-1": "failed"},
    )
    assert verdict.accepted is False
    assert any("dependencies are not" in item for item in verdict.failures)


def test_completed_with_an_unknown_dependency_state_is_rejected():
    units = build_work_unit_contracts(
        "coding",
        [
            {"id": "ws-1", "title": "First", "description": "first"},
            {
                "id": "ws-2",
                "title": "Rollup",
                "description": "dependent",
                "synthetic": True,
            },
        ],
        [
            {"worker_key": "claude:a", "assignment_ids": ["ws-1"]},
            {"worker_key": "codex:a", "assignment_ids": ["ws-2"]},
        ],
    )
    rollup = next(unit for unit in units if unit["id"] == "ws-2")
    verdict = evaluate_completion(
        output_text=_envelope(
            {"schema_version": 1, "units": [_good_unit(rollup, unit_id="ws-2")]}
        ),
        units=units,
        assignment_ids=["ws-2"],
        dependency_states={},
    )
    assert verdict.accepted is False
    assert any("dependency state was unavailable" in item for item in verdict.failures)


def test_an_envelope_with_no_readable_answer_is_rejected():
    units = _units()
    verdict = evaluate_completion(
        output_text=(
            f"{EVIDENCE_OPEN}"
            + json.dumps({"schema_version": 1, "units": [_good_unit(units[0])]})
            + EVIDENCE_CLOSE
        ),
        units=units,
        assignment_ids=["ws-1"],
    )
    assert verdict.accepted is False
    assert any("final response obligation" in item for item in verdict.failures)


def test_a_fenced_envelope_still_parses():
    units = _units()
    body = json.dumps({"schema_version": 1, "units": [_good_unit(units[0])]})
    payload, prose, error = extract_envelope(
        f"{PROSE}\n{EVIDENCE_OPEN}\n```json\n{body}\n```\n{EVIDENCE_CLOSE}"
    )
    assert error == ""
    assert payload is not None and payload["units"][0]["id"] == "ws-1"
    assert PROSE in prose


def test_rendered_instructions_name_the_owned_units_and_the_write_rules():
    units = _units()
    text = render_evidence_instructions(units, ["ws-1"], access_mode="write")
    assert "ws-1" in text
    assert EVIDENCE_OPEN in text and EVIDENCE_CLOSE in text
    assert "active path claim" in text
    read_text = render_evidence_instructions(units, ["ws-1"], access_mode="read")
    assert "read-only" in read_text
    research_text = render_evidence_instructions(
        units, ["ws-1"], access_mode="read", phase="discover"
    )
    assert "online and extensive for every worker" in research_text
    assert "at least 3 distinct provider web searches" in research_text
    assert "at least 5 unique direct http(s) sources" in research_text
    assert '"online_research"' in research_text


def test_rendered_instructions_are_empty_without_a_contract():
    assert render_evidence_instructions([], ["ws-1"]) == ""


@pytest.mark.parametrize("status", ["done", "success", "", "COMPLETE"])
def test_an_invented_status_is_rejected(status):
    units = _units()
    verdict = _evaluate(_good_unit(units[0], status=status), units=units)
    assert verdict.accepted is False
    assert any("status must be one of" in item for item in verdict.failures)


def test_verdict_serialises_for_durable_storage():
    units = _units()
    verdict = _evaluate(_good_unit(units[0]), units=units)
    payload = verdict.to_dict()
    assert json.loads(json.dumps(payload))["accepted"] is True
    assert payload["units"][0]["unit_id"] == "ws-1"


def test_legacy_proportionate_depth_cannot_weaken_the_research_gate():
    units = _units()
    lean = _online_research()
    lean["search_queries"] = ["serena fleet integration gate"]
    lean["sources"] = lean["sources"][:2]
    lean["best_practices"] = ["one practice"]
    verdict = _evaluate(
        _good_unit(units[0], online_research=lean),
        units=units,
        access_mode="read_only",
        phase="discover",
        observed_research_activity={"searches": 1, "fetches": 2},
        research_depth="proportionate",
    )
    assert verdict.accepted is False
    assert any("distinct search queries" in item for item in verdict.failures)
    assert any("recorded provider web searches" in item for item in verdict.failures)
    assert any("unique direct sources" in item for item in verdict.failures)


def test_legacy_proportionate_depth_still_requires_attributable_dated_evidence():
    units = _units()
    empty = _online_research()
    empty["search_queries"] = []
    empty["sources"] = []
    verdict = _evaluate(
        _good_unit(units[0], online_research=empty),
        units=units,
        access_mode="read_only",
        phase="discover",
        observed_research_activity={"searches": 0, "fetches": 0},
        research_depth="proportionate",
    )
    assert verdict.accepted is False
    assert any("distinct search queries" in item for item in verdict.failures)
    assert any("recorded provider web searches" in item for item in verdict.failures)
    assert any("unique direct sources" in item for item in verdict.failures)


def test_unknown_research_depth_falls_back_to_the_full_mandate():
    units = _units()
    lean = _online_research()
    lean["sources"] = lean["sources"][:2]
    verdict = _evaluate(
        _good_unit(units[0], online_research=lean),
        units=units,
        access_mode="read_only",
        phase="discover",
        observed_research_activity={"searches": 3, "fetches": 2},
        research_depth="nonsense",
    )
    assert verdict.accepted is False
    assert any("unique direct sources" in item for item in verdict.failures)


def test_review_must_report_findings_as_structured_routable_entries():
    units = _units()
    silent = _evaluate(
        _good_unit(units[0]),
        units=units,
        access_mode="review",
        phase="verify",
    )
    assert silent.accepted is False
    assert any("findings list" in item for item in silent.failures)

    clean = _evaluate(
        _good_unit(units[0], findings=[]),
        units=units,
        access_mode="review",
        phase="verify",
    )
    assert clean.accepted is True, clean.failures

    raised = _evaluate(
        _good_unit(
            units[0],
            findings=[
                {
                    "unit_id": "ws-1",
                    "severity": "major",
                    "summary": "the retry path drops the original error",
                    "evidence": "core/x.py:41 reassigns err before raising",
                }
            ],
        ),
        units=units,
        access_mode="review",
        phase="verify",
    )
    assert raised.accepted is True, raised.failures


def test_a_finding_without_an_owner_or_severity_is_rejected():
    units = _units()
    verdict = _evaluate(
        _good_unit(
            units[0],
            findings=[{"summary": "something feels off", "severity": "vibes"}],
        ),
        units=units,
        access_mode="review",
        phase="verify",
    )
    assert verdict.accepted is False
    assert any("unit_id" in item for item in verdict.failures)
    assert any("severity" in item for item in verdict.failures)
    assert any("evidence" in item for item in verdict.failures)
