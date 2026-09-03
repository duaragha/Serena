"""Disposable Fleet acceptance canary and fail-closed activation receipt.

This module deliberately does not restart Serena.  It proves source behavior in
temporary repositories, writes an atomic receipt, and exposes a separate gate
that can only pass after every Fleet run in the selected database is terminal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from fleet.isolation import (
    FleetIsolationStore,
    cleanup_workspace,
    ensure_workspace,
    integrate_workspace,
    rollback_integration,
)
from fleet.policy import build_policy, builtin_config
from fleet.store import FleetStore

SCHEMA_VERSION = 1
DEFAULT_MAX_RECEIPT_AGE_SECONDS = 24 * 60 * 60
ACTIVE_RUN_STATES = frozenset(
    {"queued", "running", "stopping", "waiting_for_capacity"}
)
DEFAULT_RECEIPT_PATH = (
    Path.home() / ".local" / "state" / "serena" / "fleet-acceptance.json"
)
DEFAULT_FLEET_DB_PATH = Path.home() / ".local" / "state" / "serena" / "fleet.sqlite3"
DEFAULT_TESTS = (
    "tests/test_fleet_dag.py",
    "tests/test_fleet_isolation.py",
    "tests/test_fleet_completion.py",
    "tests/test_fleet_completion_gate.py",
    "tests/test_fleet_policy_store.py",
    "tests/test_fleet_supervisor.py",
    "tests/test_fleet_workers.py",
    "tests/test_fleet_chat_sidebar.py",
    "tests/test_fleet_web.py",
    "tests/test_operator_workspace.py",
    "tests/test_operator_web.py",
    "tests/test_web_terminal_performance.py",
    "tests/test_terminal_lifecycle_module.py",
)
DEFAULT_DESKTOP_TESTS = (
    "desktop/tests/test_runtime_hot_standby.py",
    "desktop/tests/test_runtime_lifecycle.py",
    "desktop/tests/test_split_tab_visibility.py",
)


class AcceptanceFailure(RuntimeError):
    """The disposable acceptance proof could not establish a required invariant."""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AcceptanceFailure(result.stderr.strip() or "git command failed")
    return result.stdout


def _record(checks: list[dict[str, Any]], name: str, evidence: str) -> None:
    checks.append({"name": name, "passed": True, "evidence": evidence})


def _require(condition: object, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def _repository_canary(root: Path, checks: list[dict[str, Any]]) -> None:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fleet-canary@serena.local")
    _git(repo, "config", "user.name", "Serena Fleet Canary")
    (repo / "core").mkdir()
    (repo / "core" / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("tracked base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "canary base")

    # A dirty tracked file and an untracked file stand in for Raghav's work.
    (repo / "README.md").write_text("precious dirty work\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("keep this too\n", encoding="utf-8")
    store = FleetIsolationStore(
        root / "isolation.sqlite3", workspace_root=root / "worktrees"
    )
    first = store.claim_paths(
        run_id="canary-run", worker_key="codex:a", paths=["core/alpha.py"]
    )
    refused = store.claim_paths(
        run_id="canary-run", worker_key="claude:a", paths=["core"]
    )
    _require(first.ok and not refused.ok and bool(refused.conflicts), "claim conflict escaped")
    _record(checks, "claims_and_conflict_refusal", "overlapping writer claim was refused")

    workspace = ensure_workspace(
        store, run_id="canary-run", worker_key="codex:a", cwd=repo
    )
    workspace_root = Path(workspace.path)
    _require(
        (workspace_root / "README.md").read_text(encoding="utf-8")
        == "precious dirty work\n",
        "dirty base was not frozen into the isolated worktree",
    )
    (workspace_root / "core" / "alpha.py").write_text(
        "alpha = 'integrated'\n", encoding="utf-8"
    )
    integrated = integrate_workspace(
        store,
        run_id="canary-run",
        worker_key="codex:a",
        cwd=repo,
        test_gate=[sys.executable, "-c", "raise SystemExit(0)"],
    )
    _require(integrated.ok and integrated.test_gate.get("ok"), integrated.reason)
    _require(
        (repo / "README.md").read_text(encoding="utf-8") == "precious dirty work\n"
        and (repo / "untracked.txt").read_text(encoding="utf-8") == "keep this too\n",
        "integration changed unrelated dirty work",
    )
    _record(
        checks,
        "worktree_isolation_and_integration_gate",
        "isolated change passed its gate while dirty and untracked base work survived",
    )

    rolled_back = rollback_integration(
        store, run_id="canary-run", worker_key="codex:a", cwd=repo
    )
    _require(
        rolled_back.get("ok")
        and (repo / "core" / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n",
        str(rolled_back.get("reason") or "rollback failed"),
    )
    _record(checks, "explicit_rollback", "recorded integration patch reversed cleanly")

    failed_gate = integrate_workspace(
        store,
        run_id="canary-run",
        worker_key="codex:a",
        cwd=repo,
        test_gate=[sys.executable, "-c", "raise SystemExit(7)"],
    )
    _require(
        not failed_gate.ok
        and failed_gate.test_gate.get("exit_code") == 7
        and (repo / "core" / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n",
        "failing integration gate did not restore the base checkout",
    )
    _record(checks, "automatic_gate_rollback", "exit 7 was refused and reversed")
    _require(
        cleanup_workspace(store, run_id="canary-run", worker_key="codex:a", cwd=repo),
        "disposable worktree cleanup failed",
    )


def _fleet_state_canary(root: Path, checks: list[dict[str, Any]]) -> None:
    store = FleetStore(root / "fleet.sqlite3")
    policy = build_policy(
        "coding",
        "Tasks:\n- one\n- two\n- three\n- four",
        config=builtin_config(),
        provider_mode="codex",
        worker_count=4,
    ).to_dict()
    run = store.create_run(
        task="disposable four-worker continuity canary",
        activity="coding",
        cwd=str(root),
        origin_session_id="canary-origin",
        origin_agent="codex",
        dry_run=False,
        policy=policy,
    )
    run_id = str(run["run_id"])
    try:
        store.complete_run(run_id, "too soon")
    except ValueError as error:
        _require("unfinished legs" in str(error), "unexpected completion refusal")
    else:
        raise AcceptanceFailure("unfinished Fleet run was accepted as complete")
    _record(checks, "completion_rejection", "unfinished legs rejected completion")

    # Keyed by (phase_index, ordinal). Research and Code share chat "a"; Review
    # opens chat "b" and Fix continues it.
    sessions = {
        (phase_index, ordinal): f"canary-session-{'a' if phase_index < 2 else 'b'}-{ordinal}"
        for phase_index in range(4)
        for ordinal in range(4)
    }
    first_leg = run["phases"][0]["legs"][0]
    failed = store.begin_attempt(str(first_leg["leg_id"]))
    store.finish_attempt(
        str(failed["attempt_id"]),
        state="failed",
        session_id=sessions[(0, 0)],
        error="synthetic retry",
        exit_code=1,
    )
    retry = store.begin_attempt(str(first_leg["leg_id"]))
    _require(
        retry["resume_session_id"] == sessions[(0, 0)]
        and retry["resume_kind"] == "retry",
        "same-provider retry did not resume its native session",
    )
    store.finish_attempt(
        str(retry["attempt_id"]),
        state="completed",
        session_id=sessions[(0, 0)],
        actual_model=str(first_leg["model"]),
        actual_effort=str(first_leg["effort"]),
        exit_code=0,
    )

    # This canary is codex-only, so every phase shares a provider and a worker
    # could in principle hold one chat throughout. It must not. Review is
    # required to open its own chat even here, because a reviewer that sat
    # through the work cannot independently disagree with it. Everything else
    # continues, giving exactly two chats per worker.
    for phase_index, phase in enumerate(run["phases"]):
        for leg in phase["legs"]:
            ordinal = int(leg["ordinal"])
            if phase_index == 0 and ordinal == 0:
                continue
            attempt = store.begin_attempt(str(leg["leg_id"]))
            if phase_index in {1, 3}:
                _require(
                    attempt["resume_session_id"] == sessions[(phase_index, ordinal)]
                    and attempt["resume_kind"] == "phase_continuation",
                    f"phase {phase_index} worker {ordinal} multiplied its chat",
                )
            elif phase_index == 2:
                _require(
                    attempt["resume_session_id"] is None,
                    f"Review worker {ordinal} continued the chat that researched it",
                )
            store.finish_attempt(
                str(attempt["attempt_id"]),
                state="completed",
                session_id=sessions[(phase_index, ordinal)],
                actual_model=str(leg["model"]),
                actual_effort=str(leg["effort"]),
                exit_code=0,
            )
    completed = store.complete_run(run_id, "canary complete")
    _require(
        completed["state"] == "completed"
        and completed["chat_count"] == 8
        and completed["agent_count"] == 4,
        "four logical workers did not produce exactly two persistent chats each",
    )
    _record(
        checks,
        "dag_retry_and_chat_continuity",
        "four workers crossed four phases plus one retry using two chats each: "
        "Research and Code shared one, Review opened its own and Fix continued it",
    )

    recovery = store.create_run(
        task="disposable crash recovery canary",
        activity="coding",
        cwd=str(root),
        origin_session_id=None,
        origin_agent=None,
        dry_run=False,
        policy=policy,
    )
    dead_pid = 2_000_000_000
    _require(store.claim_run(str(recovery["run_id"]), owner_pid=dead_pid), "claim failed")
    recovered = store.recover_stale_runs()
    recovered_run = store.get_run(str(recovery["run_id"]))
    _require(
        str(recovery["run_id"]) in recovered
        and recovered_run is not None
        and recovered_run["state"] == "queued",
        "dead supervisor ownership was not recovered to the queue",
    )
    _record(checks, "crash_recovery", "dead synthetic supervisor recovered to queued")


def run_disposable_canary() -> list[dict[str, Any]]:
    """Exercise dangerous Fleet transitions entirely below a temporary root."""

    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="serena-fleet-canary-") as temporary:
        root = Path(temporary)
        _repository_canary(root, checks)
        _fleet_state_canary(root, checks)
    return checks


def source_fingerprint(repo_root: str | Path) -> str:
    """Hash the exact tracked and untracked source view used by the receipt."""

    root = Path(repo_root).resolve()
    listed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        capture_output=True,
        check=False,
    )
    if listed.returncode:
        raise AcceptanceFailure(listed.stderr.decode(errors="replace").strip())
    digest = hashlib.sha256()
    for raw_path in sorted(part for part in listed.stdout.split(b"\0") if part):
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        candidate = root / relative
        if not candidate.is_file():
            continue
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(candidate.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def default_test_command(repo_root: str | Path) -> list[str]:
    root = Path(repo_root).resolve()
    local_python = root / ".venv" / "bin" / "python"
    interpreter = str(local_python) if local_python.is_file() else sys.executable
    return [
        interpreter,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *DEFAULT_TESTS,
    ]


def _pytest_command(repo_root: str | Path, *paths: str) -> list[str]:
    root = Path(repo_root).resolve()
    local_python = root / ".venv" / "bin" / "python"
    interpreter = str(local_python) if local_python.is_file() else sys.executable
    return [interpreter, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths]


def _run_tests(repo_root: Path, command: Sequence[str]) -> dict[str, Any]:
    started = time.monotonic()
    environment = dict(os.environ)
    # The harness is expected to run from a Fleet worker. The nested-run guard
    # protects production entrypoints, but these subprocesses exercise temp-DB
    # supervisor fixtures and must see the same neutral environment as CI.
    environment.pop("SERENA_FLEET_WORKER", None)
    with tempfile.TemporaryDirectory(prefix="serena-acceptance-state-") as temporary:
        state = Path(temporary)
        environment.update(
            {
                "CHATS_DATA_DIR": str(state / "chats"),
                "SERENA_ARTIFACT_ROOT": str(state / "artifacts"),
                "SERENA_ARTIFACT_DB": str(state / "artifacts.sqlite3"),
                "SERENA_ARTIFACT_KEY": str(state / "artifact.key"),
                "SERENA_OPERATOR_DB": str(state / "operator.sqlite3"),
                "SERENA_WORK_JOBS_PATH": str(state / "work-jobs.sqlite3"),
                "SERENA_VOICE_INBOX_PATH": str(state / "voice-inbox.sqlite3"),
                "SERENA_VOICE_WORK_MARKER_PATH": str(state / "voice-work.marker"),
                "SERENA_FLEET_DB_PATH": str(state / "fleet.sqlite3"),
            }
        )
        for inherited in (
            "SERENA_FLEET_ISOLATION_DB_PATH",
            "SERENA_FLEET_WORKSPACE_ROOT",
            "SERENA_FLEET_STATE_DIR",
        ):
            environment.pop(inherited, None)
        result = subprocess.run(
            list(command),
            cwd=repo_root,
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
    return {
        "command": list(command),
        "exit_code": int(result.returncode),
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_tail": ((result.stdout or "") + (result.stderr or ""))[-8_000:],
    }


def write_receipt(path: str | Path, receipt: dict[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        with suppress(FileNotFoundError):
            Path(temporary).unlink()


def run_acceptance(
    repo_root: str | Path,
    receipt_path: str | Path,
    *,
    test_command: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run the source suite and canary, then persist an immutable-input receipt."""

    root = Path(repo_root).resolve()
    fingerprint_before = source_fingerprint(root)
    started = time.time()
    checks = run_disposable_canary()
    tests = _run_tests(root, test_command or default_test_command(root))
    desktop_tests = (
        [
            _run_tests(root, _pytest_command(root, test_path))
            for test_path in DEFAULT_DESKTOP_TESTS
        ]
        if test_command is None
        else []
    )
    fingerprint_after = source_fingerprint(root)
    stable = fingerprint_before == fingerprint_after
    checks.append(
        {
            "name": "source_stability",
            "passed": stable,
            "evidence": "source fingerprint stayed fixed during acceptance",
        }
    )
    passed = bool(
        tests["exit_code"] == 0
        and all(item["exit_code"] == 0 for item in desktop_tests)
        and stable
        and all(item["passed"] for item in checks)
    )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": "serena.fleet.activation_receipt",
        "passed": passed,
        "repo_root": str(root),
        "source_fingerprint": fingerprint_after,
        "started_at": started,
        "completed_at": time.time(),
        "checks": checks,
        "tests": tests,
        "desktop_tests": desktop_tests,
        "proof_scope": "source and disposable state only; no live process was restarted",
        "live_checks_deferred": [
            {
                "check": "desktop/tests/test_vte_cold_resume.py",
                "reason": "requires a live display/VTE process and exits by signal in headless mode",
            },
            {
                "check": "loaded Fleet and Serena desktop build",
                "reason": "must wait until every Fleet run is terminal before restart",
            },
        ],
        "activation_sequence": [
            "wait until every Fleet run is terminal",
            "run this gate against the current source fingerprint and Fleet database",
            "restart the Fleet service",
            "restart Serena desktop only after the Fleet service is healthy",
        ],
    }
    write_receipt(receipt_path, receipt)
    return receipt


def active_fleet_runs(database: str | Path) -> list[dict[str, str]]:
    """Read active runs without creating or migrating the production database."""

    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise AcceptanceFailure(f"Fleet database does not exist: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            "SELECT run_id, state FROM fleet_runs WHERE state IN (?, ?, ?, ?) "
            "ORDER BY created_at",
            tuple(sorted(ACTIVE_RUN_STATES)),
        ).fetchall()
    except sqlite3.Error as error:
        raise AcceptanceFailure(f"could not inspect Fleet database: {error}") from error
    finally:
        connection.close()
    return [{"run_id": str(row["run_id"]), "state": str(row["state"])} for row in rows]


def activation_gate(
    repo_root: str | Path,
    receipt_path: str | Path,
    fleet_database: str | Path,
    *,
    max_age_seconds: int = DEFAULT_MAX_RECEIPT_AGE_SECONDS,
) -> dict[str, Any]:
    """Fail closed unless source proof is current and all Fleet work is terminal."""

    reasons: list[str] = []
    receipt_file = Path(receipt_path).expanduser()
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        receipt = {}
        reasons.append(f"acceptance receipt is unreadable: {error}")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        reasons.append("acceptance receipt schema is unsupported")
    if receipt.get("kind") != "serena.fleet.activation_receipt":
        reasons.append("acceptance receipt kind is invalid")
    if receipt.get("passed") is not True:
        reasons.append("acceptance receipt did not pass")
    requested_root = str(Path(repo_root).resolve())
    if receipt.get("repo_root") != requested_root:
        reasons.append("acceptance receipt belongs to a different repository")
    checks = receipt.get("checks")
    if not isinstance(checks, list) or not checks or not all(
        isinstance(item, dict) and item.get("passed") is True for item in checks
    ):
        reasons.append("acceptance receipt has an incomplete canary")
    tests = receipt.get("tests")
    if not isinstance(tests, dict) or tests.get("exit_code") != 0:
        reasons.append("acceptance source tests did not pass")
    desktop_tests = receipt.get("desktop_tests")
    if not isinstance(desktop_tests, list) or not all(
        isinstance(item, dict) and item.get("exit_code") == 0 for item in desktop_tests
    ):
        reasons.append("acceptance desktop tests did not pass")
    completed_at = float(receipt.get("completed_at") or 0)
    age = max(0.0, time.time() - completed_at)
    if not completed_at or age > max(1, int(max_age_seconds)):
        reasons.append("acceptance receipt is stale")
    try:
        current_fingerprint = source_fingerprint(requested_root)
    except AcceptanceFailure as error:
        current_fingerprint = ""
        reasons.append(str(error))
    if receipt.get("source_fingerprint") != current_fingerprint:
        reasons.append("source changed after acceptance")
    try:
        active = active_fleet_runs(fleet_database)
    except AcceptanceFailure as error:
        active = []
        reasons.append(str(error))
    if active:
        reasons.append("one or more Fleet runs are still active")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "serena.fleet.activation_gate",
        "passed": not reasons,
        "reasons": reasons,
        "active_runs": active,
        "receipt": str(receipt_file.resolve()),
        "fleet_database": str(Path(fleet_database).expanduser().resolve()),
        "source_fingerprint": current_fingerprint,
        "receipt_age_seconds": round(age, 3),
        "next_action": (
            "restart Fleet, then Serena desktop" if not reasons else "do not restart Serena"
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run source tests and disposable acceptance canary")
    run.add_argument("--repo", type=Path, default=Path.cwd())
    run.add_argument("--output", type=Path, default=DEFAULT_RECEIPT_PATH)
    gate = commands.add_parser("gate", help="verify a receipt after all Fleet runs finish")
    gate.add_argument("--repo", type=Path, default=Path.cwd())
    gate.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT_PATH)
    gate.add_argument("--fleet-db", type=Path, default=DEFAULT_FLEET_DB_PATH)
    gate.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_RECEIPT_AGE_SECONDS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "run":
            result = run_acceptance(arguments.repo, arguments.output)
        else:
            result = activation_gate(
                arguments.repo,
                arguments.receipt,
                arguments.fleet_db,
                max_age_seconds=arguments.max_age_seconds,
            )
    except (AcceptanceFailure, OSError, ValueError) as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "passed": False,
            "reasons": [str(error)],
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
