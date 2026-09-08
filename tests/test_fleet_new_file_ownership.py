import json

import pytest

from fleet.contracts import extract_declared_paths
from fleet.policy import build_policy, builtin_config
from fleet.store import FleetStore


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "Code edits queue.py and creates test_queue.py; preserve review_target.py until Review inspects it.",
            ["queue.py", "test_queue.py"],
        ),
        ("Create tests/new/test_queue.py.", ["tests/new/test_queue.py"]),
        ("Own queue.py and test_queue.py. Read review_target.py.", ["queue.py", "test_queue.py"]),
        ("Edit queue.py and do not edit review_target.py.", ["queue.py"]),
        ("Do not create test_queue.py. Never add danger.py.", []),
        ("Read missing/new.py and inspect review_target.py.", []),
        ("Own queue.py, then preserve review_target.py.", ["queue.py"]),
        ("Create ../outside.py and /tmp/absolute.py.", []),
        ("Own missing-directory/", []),
    ],
)
def test_positive_explicit_new_files_are_bounded(tmp_path, text, expected):
    (tmp_path / "queue.py").touch()
    (tmp_path / "review_target.py").touch()
    assert extract_declared_paths(text, cwd=tmp_path) == expected


def test_new_file_symlink_escape_is_not_owned(tmp_path):
    (tmp_path / "outside").symlink_to(tmp_path.parent, target_is_directory=True)
    assert extract_declared_paths("Create outside/new.py", cwd=tmp_path) == []


def test_normal_retry_refreshes_failed_unit_ownership_and_keeps_completed_attempts(tmp_path):
    task = "Code edits queue.py and creates test_queue.py; preserve review_target.py until Review inspects it."
    (tmp_path / "queue.py").touch()
    (tmp_path / "review_target.py").touch()
    policy = build_policy(
        "coding", config=builtin_config(), task=task, cwd=tmp_path, worker_count=1
    ).to_dict()
    # Reproduce an older persisted planner declaration, before new-file repair.
    unit = policy["work_units"][0]
    unit["file_ownership"]["declared_paths"] = ["queue.py", "review_target.py"]
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = store.create_run(
        task=task,
        activity="coding",
        cwd=str(tmp_path),
        origin_session_id=None,
        origin_agent=None,
        dry_run=False,
        policy=policy,
    )
    research = store.begin_attempt(run["phases"][0]["legs"][0]["leg_id"])
    store.finish_attempt(
        research["attempt_id"],
        state="completed",
        output_text="durable research receipt",
        exit_code=0,
    )
    code = store.begin_attempt(run["phases"][1]["legs"][0]["leg_id"])
    store.finish_attempt(
        code["attempt_id"],
        state="failed",
        output_text="durable failed implementation",
        error="unclaimed test_queue.py",
        exit_code=0,
    )
    store.fail_run(run["run_id"], "unclaimed test_queue.py")
    before = store.get_run(run["run_id"])
    retried = store.retry_run(run["run_id"])
    assert retried["policy"]["work_units"][0]["file_ownership"]["declared_paths"] == [
        "queue.py",
        "review_target.py",
        "test_queue.py",
    ]
    assert (
        retried["phases"][0]["legs"][0]["current_attempt"]
        == before["phases"][0]["legs"][0]["current_attempt"]
    )
    assert retried["phases"][0]["legs"][0]["state"] == "completed"
    assert (
        retried["phases"][1]["legs"][0]["current_attempt"]
        == before["phases"][1]["legs"][0]["current_attempt"]
    )
    with store._connect() as connection:
        record = connection.execute(
            "SELECT contract_json FROM fleet_work_units WHERE run_id = ?", (run["run_id"],)
        ).fetchone()
    assert json.loads(record["contract_json"])["file_ownership"]["declared_paths"] == [
        "queue.py",
        "review_target.py",
        "test_queue.py",
    ]
    assert store.has_event(run["run_id"], "run.retry_ownership_refreshed")
