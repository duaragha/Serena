"""Wiring between a finished Fleet leg and the completion-contract validator.

`core.fleet_completion` is deliberately pure. This module is the impure half:
it collects the persisted work-unit contracts, the durable path claims, the
real changed paths from the worker's isolated workspace, and the dependency
states, then asks the validator whether the leg may be recorded as completed.

It is kept separate from `core.fleet_supervisor` on purpose. The supervisor is
the hottest shared file in Fleet, so the amount of it this feature has to touch
is a few lines, not a few hundred.

An isolation store that never created a workspace means claims and a scoped
diff are not available for that run. Once a workspace exists, however, lookup
errors are evidence-infrastructure failures and must propagate to the
supervisor's fail-closed boundary. Missing dependency state likewise stays
missing instead of being invented as completed.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.fleet_completion import CompletionVerdict, evaluate_completion, extract_envelope

_DIRECT_TEST_TOOLS = frozenset(
    {
        "pytest",
        "ruff",
        "mypy",
        "pyright",
        "tsc",
        "eslint",
        "cargo",
        "go",
        "swift",
        "node",
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "gradle",
        "gradlew",
        "jest",
        "vitest",
        "biome",
    }
)
_SHELL_TOKENS = frozenset({"&&", "||", ";", "|", ">", ">>", "<", "2>", "2>&1"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PYTHON_TEST_MODULES = frozenset({"pytest", "ruff", "unittest", "py_compile"})
_SAFE_TEST_ENV = {
    "PYTHONDONTWRITEBYTECODE": frozenset({"1"}),
    "PYTHONPATH": frozenset({"."}),
}
_INHERITED_TEST_ENV = frozenset(
    {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ"}
)
_NODE_MODULE_BIN = re.compile(r"^(?:\./)?node_modules/\.bin/([A-Za-z0-9_.-]+)$")


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _work_units(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    policy = snapshot.get("policy")
    units = policy.get("work_units") if isinstance(policy, dict) else None
    return [unit for unit in (units or []) if isinstance(unit, dict)]


def _active_claims(run_id: str, worker_key: str) -> list[str] | None:
    """Claimed paths for this worker, or None when the registry is not engaged.

    Returning None means "unknown", which the validator treats as "do not test
    this rule". Returning [] means "this worker holds no claims", which will
    reject any declared file change.
    """

    from core.fleet_isolation import FleetIsolationStore

    store = FleetIsolationStore()
    workspace = store.get_workspace(run_id, worker_key)
    run_claims = store.active_claims(run_id)
    if workspace is None and not run_claims:
        return None
    return [
        str(claim.get("path") or "")
        for claim in run_claims
        if str(claim.get("worker_key") or "") == worker_key
    ]


def _observed_changed_paths(run_id: str, worker_key: str) -> list[str] | None:
    """Real changed paths from this worker's workspace, when one is provable."""

    from core.fleet_isolation import FleetIsolationStore, workspace_changed_paths

    store = FleetIsolationStore()
    workspace = store.get_workspace(run_id, worker_key)
    if workspace is None:
        return None
    return list(workspace_changed_paths(workspace))


def _dependency_states(
    snapshot: dict[str, Any], phase_index: int | None = None
) -> dict[str, str]:
    """Projected dependency states for the leg's current phase.

    A work unit advances to ``queued`` as soon as its next phase is available.
    Dependent integration units, however, use a phase barrier: they need the
    upstream units to have completed this phase, not every later phase in the
    run. Reading the top-level unit state here therefore rejects valid rollups
    after their dependencies advance. Legacy snapshots without phase metadata
    retain the previous top-level projection.
    """

    units = snapshot.get("work_units")
    if isinstance(units, list):
        projected: dict[str, str] = {}
        for unit in units:
            if not isinstance(unit, dict):
                continue
            unit_id = str(unit.get("id") or "")
            if not unit_id:
                continue
            if phase_index is None:
                projected[unit_id] = str(unit.get("state") or "")
                continue
            executions = unit.get("phase_executions")
            if not isinstance(executions, list):
                projected[unit_id] = str(unit.get("state") or "")
                continue
            matching = next(
                (
                    execution
                    for execution in executions
                    if isinstance(execution, dict)
                    and int(execution.get("phase_index", -1)) == phase_index
                ),
                None,
            )
            if matching is not None:
                projected[unit_id] = str(matching.get("state") or "")
        return projected
    return {}


def _leg_phase(
    snapshot: dict[str, Any], leg: dict[str, Any]
) -> tuple[int | None, str]:
    """Resolve phase identity even though projected leg rows omit phase fields."""

    raw_leg_index = leg.get("phase_index")
    leg_index = int(raw_leg_index) if raw_leg_index is not None else None
    leg_phase = str(leg.get("phase") or "")
    if leg_index is not None and leg_phase:
        return leg_index, leg_phase
    leg_id = str(leg.get("leg_id") or "")
    phases = snapshot.get("phases")
    if not isinstance(phases, list):
        return leg_index, leg_phase
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        raw_index = phase.get("index")
        phase_index = int(raw_index) if raw_index is not None else None
        if leg_index is not None and phase_index == leg_index:
            return phase_index, str(phase.get("name") or "")
        phase_legs = phase.get("legs")
        if not isinstance(phase_legs, list):
            continue
        if leg_id and any(
            isinstance(candidate, dict)
            and str(candidate.get("leg_id") or "") == leg_id
            for candidate in phase_legs
        ):
            return (
                phase_index,
                str(phase.get("name") or ""),
            )
    return leg_index, leg_phase


def safe_test_argv(
    command: str, root: str, tool_root: str | None = None
) -> list[str] | None:
    """Return the runnable argv for a worker-declared check, or None to refuse.

    Integration re-runs a worker's own verification against the merged tree, so
    it needs the same answer this module already computes for the re-run path:
    is this string one trusted test process, or is it model-authored shell? The
    allowlist is the single source of truth for that, and it stays here.
    """

    spec = _test_spec(command, root, tool_root=tool_root)
    if spec is None:
        return None
    argv, _environment_overrides, _environment_unsets = spec
    return list(argv)


def _test_spec(
    command: str, workspace_path: str, tool_root: str | None = None
) -> tuple[list[str], dict[str, str], list[str]] | None:
    """Allow one trusted direct test process plus narrowly safe environment."""

    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or any(token in _SHELL_TOKENS for token in argv):
        return None
    environment: dict[str, str] = {}
    unset: list[str] = []
    # Accept an `env` prefix, but ONLY for unsetting variables: removing state
    # can't inject behavior, so `env -u NAME` is safe. Every other env option
    # (-i, -S, --chdir, …) stays rejected; assignments fall through to the
    # existing safelist loop below.
    if argv and os.path.basename(argv[0]) == "env":
        argv = argv[1:]
        while argv:
            token = argv[0]
            if token == "-u" and len(argv) >= 2 and _ENV_NAME.fullmatch(argv[1]):
                unset.append(argv[1])
                argv = argv[2:]
                continue
            if token.startswith("--unset="):
                name = token.split("=", 1)[1]
                if not _ENV_NAME.fullmatch(name):
                    return None
                unset.append(name)
                argv = argv[1:]
                continue
            if token.startswith("-"):
                return None
            break
    while argv and "=" in argv[0]:
        name, value = argv[0].split("=", 1)
        allowed_values = _SAFE_TEST_ENV.get(name)
        if allowed_values is None or value not in allowed_values:
            return None
        environment[name] = value
        argv = argv[1:]
    if not argv:
        return None
    executable = os.path.basename(argv[0])
    if executable.startswith("python"):
        if (
            len(argv) < 3
            or argv[1] != "-m"
            or argv[2] not in _PYTHON_TEST_MODULES
        ):
            return None
    elif executable not in _DIRECT_TEST_TOOLS:
        return None
    raw_executable = argv[0]
    node_bin = _NODE_MODULE_BIN.fullmatch(raw_executable)
    if node_bin is not None and tool_root:
        if node_bin.group(1) not in _DIRECT_TEST_TOOLS:
            return None
        candidate = Path(tool_root) / "node_modules" / ".bin" / node_bin.group(1)
    elif "/" in raw_executable:
        candidate = Path(raw_executable)
    else:
        resolved_executable = shutil.which(raw_executable)
        if not resolved_executable:
            return None
        candidate = Path(resolved_executable)
    if not candidate.is_absolute():
        candidate = Path(workspace_path) / candidate
    # Preserve the invoked path after validating it. Virtualenv interpreters
    # are normally symlinks to the system Python; replacing the path with its
    # resolved target silently drops the virtualenv and its installed tools.
    candidate = Path(os.path.abspath(candidate))
    try:
        resolved = candidate.resolve(strict=True)
        workspace = Path(workspace_path).resolve(strict=True)
    except OSError:
        return None
    # Check both sides of a symlink. The command path itself may not live in a
    # worker-controlled worktree, and it may not point back into one either.
    # One sanctioned exception: Fleet provisions `workspace/.venv` as a symlink
    # to the base checkout's virtualenv so workers can actually run pytest.
    # A command under that symlink is trusted iff the symlink still points
    # outside the worktree and the resolved binary is outside too — a worker
    # that replaces .venv with its own real directory resolves inside the
    # workspace and stays rejected.
    venv_link = workspace / ".venv"
    via_trusted_venv = (
        (venv_link == candidate or venv_link in candidate.parents)
        and venv_link.is_symlink()
        and resolved != workspace
        and workspace not in resolved.parents
    )
    if not via_trusted_venv and (
        candidate == workspace
        or workspace in candidate.parents
        or resolved == workspace
        or workspace in resolved.parents
    ):
        return None
    argv[0] = str(candidate)
    return argv, environment, unset


def _test_argv(command: str, workspace_path: str) -> list[str] | None:
    """Compatibility helper returning the validated direct-process argv."""

    spec = _test_spec(command, workspace_path)
    return spec[0] if spec is not None else None


def _test_environment(
    overrides: dict[str, str], unsets: list[str]
) -> dict[str, str]:
    """Build a deterministic test environment without forwarding credentials."""

    environment = {
        name: value
        for name, value in os.environ.items()
        if name in _INHERITED_TEST_ENV or name.startswith("LC_")
    }
    environment.update({"CI": "1", "NO_COLOR": "1"})
    for name in unsets:
        environment.pop(name, None)
    environment.update(overrides)
    return environment


@contextmanager
def _base_node_modules_link(
    workspace_path: str,
    tool_root: str | None,
    argv: list[str],
):
    """Expose trusted base dependencies only while a Node check is rerun."""

    workspace_modules = Path(workspace_path) / "node_modules"
    if workspace_modules.exists() or workspace_modules.is_symlink() or not tool_root:
        yield
        return
    base_modules = Path(tool_root) / "node_modules"
    try:
        resolved_modules = base_modules.resolve(strict=True)
        executable = Path(argv[0]).resolve(strict=True)
    except OSError:
        yield
        return
    if executable != resolved_modules and resolved_modules not in executable.parents:
        yield
        return
    workspace_modules.symlink_to(resolved_modules, target_is_directory=True)
    try:
        yield
    finally:
        if workspace_modules.is_symlink():
            workspace_modules.unlink()


def _git_no_index_check_paths(command: str, workspace_path: str) -> list[str] | None:
    """Parse the exact whitespace check workers use for newly-created files."""

    segments = [segment.strip() for segment in command.split("&&")]
    if not segments or any(not segment for segment in segments):
        return None
    workspace = Path(workspace_path).resolve()
    paths: list[str] = []
    for segment in segments:
        try:
            argv = shlex.split(segment)
        except ValueError:
            return None
        if len(argv) != 6 or argv[1:5] != [
            "diff",
            "--no-index",
            "--check",
            "/dev/null",
        ]:
            return None
        if os.path.basename(argv[0]) != "git":
            return None
        target = Path(argv[5])
        if target.is_absolute():
            return None
        resolved = (workspace / target).resolve()
        if resolved == workspace or workspace not in resolved.parents:
            return None
        paths.append(argv[5])
    return paths


def _run_git_no_index_checks(
    command: str, workspace_path: str, *, timeout: int
) -> int | None:
    paths = _git_no_index_check_paths(command, workspace_path)
    if paths is None:
        return None
    environment = _test_environment({}, [])
    for path in paths:
        try:
            completed = subprocess.run(
                ["git", "diff", "--no-index", "--check", "/dev/null", path],
                cwd=workspace_path,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 124
        except OSError:
            return 127
        # `git diff --no-index` returns 1 when clean files differ from /dev/null.
        # `--check` reports whitespace errors in output and returns a larger code.
        if int(completed.returncode) not in {0, 1} or completed.stdout:
            return int(completed.returncode) or 1
    return 0


def _run_git_diff_check(
    command: str, workspace_path: str, *, timeout: int
) -> int | None:
    """Rerun the one mutation-free repository whitespace check Fleet accepts."""

    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if argv != ["git", "diff", "--check"]:
        return None
    try:
        completed = subprocess.run(
            argv,
            cwd=workspace_path,
            env=_test_environment({}, []),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return int(completed.returncode)
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127


def _observed_test_results(
    run_id: str,
    worker_key: str,
    output_text: str,
    event_log_path: str | None = None,
    *,
    allow_rerun: bool = True,
    tool_root: str | None = None,
) -> dict[str, int] | None:
    """Read Fleet-owned command receipts, then re-run only missing safe checks."""

    from core.fleet_isolation import FleetIsolationStore

    workspace = FleetIsolationStore().get_workspace(run_id, worker_key)
    payload, _prose, error = extract_envelope(output_text)
    if error or not isinstance(payload, dict):
        return {}
    commands: list[str] = []
    for unit in payload.get("units") or []:
        if not isinstance(unit, dict):
            continue
        for item in unit.get("tests") or []:
            if isinstance(item, dict):
                command = " ".join(str(item.get("command") or "").split())
                if command and command not in commands:
                    commands.append(command)
    try:
        configured_timeout = int(os.environ.get("SERENA_FLEET_TEST_TIMEOUT_SECONDS", "300"))
    except ValueError:
        configured_timeout = 300
    timeout = min(900, max(10, configured_timeout))
    observed: dict[str, int] = {}
    receipts = _event_log_test_results(event_log_path)
    for command in commands:
        if allow_rerun and workspace is not None:
            no_index_result = _run_git_no_index_checks(
                command, workspace.path, timeout=timeout
            )
            if no_index_result is not None:
                observed[command] = no_index_result
                continue
        if command in receipts:
            observed[command] = receipts[command]
            continue
        if not allow_rerun or workspace is None:
            continue
        git_diff_result = _run_git_diff_check(
            command, workspace.path, timeout=timeout
        )
        if git_diff_result is not None:
            observed[command] = git_diff_result
            continue
        spec = _test_spec(command, workspace.path, tool_root=tool_root)
        if spec is None:
            observed[command] = 126
            continue
        argv, environment_overrides, environment_unsets = spec
        environment = _test_environment(environment_overrides, environment_unsets)
        try:
            with _base_node_modules_link(workspace.path, tool_root, argv):
                completed = subprocess.run(
                    argv,
                    cwd=workspace.path,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                    check=False,
                )
            observed[command] = int(completed.returncode)
        except subprocess.TimeoutExpired:
            observed[command] = 124
        except OSError:
            observed[command] = 127
    return observed


def _event_log_test_results(event_log_path: str | None) -> dict[str, int]:
    """Extract command exits from the raw provider stream Fleet wrote itself."""

    if not event_log_path:
        return {}
    path = Path(event_log_path)
    if not path.is_file():
        return {}

    observed: dict[str, int] = {}
    try:
        records = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with records:
        for raw in records:
            try:
                outer = json.loads(raw)
                event = json.loads(str(outer.get("line") or ""))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue

            item = event.get("item")
            if (
                event.get("type") == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "command_execution"
            ):
                command = _provider_command(str(item.get("command") or ""))
                try:
                    code = int(item.get("exit_code"))
                except (TypeError, ValueError):
                    continue
                if command:
                    observed[command] = code
                terminal = _terminal_shell_command(str(item.get("command") or ""))
                if terminal:
                    observed[terminal] = code
                continue

    return observed


def _event_log_research_activity(event_log_path: str | None) -> dict[str, int]:
    """Count completed native web searches/fetches from the provider stream."""

    if not event_log_path:
        return {"searches": 0, "fetches": 0}
    path = Path(event_log_path)
    if not path.is_file():
        return {"searches": 0, "fetches": 0}

    searches: set[str] = set()
    fetches: set[str] = set()
    reported_searches = 0
    reported_fetches = 0

    def record_action(action: object, fallback: str) -> None:
        if not isinstance(action, dict):
            return
        action_type = str(action.get("type") or "").casefold()
        if action_type in {"search", "web_search", "web_search_call"}:
            queries = action.get("queries")
            values = queries if isinstance(queries, list) else [action.get("query")]
            found = [str(value).strip() for value in values if str(value or "").strip()]
            searches.update(found or [fallback])
        elif action_type in {"open_page", "fetch", "find_in_page"}:
            target = str(action.get("url") or action.get("ref_id") or fallback).strip()
            fetches.add(target or fallback)

    try:
        records = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return {"searches": 0, "fetches": 0}
    with records:
        for raw in records:
            try:
                outer = json.loads(raw)
                event = json.loads(str(outer.get("line") or ""))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue

            if event.get("type") == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and str(item.get("type") or "").casefold() in {
                    "web_search",
                    "web_search_call",
                }:
                    fallback = str(item.get("id") or len(searches) + 1)
                    record_action(item.get("action") or item, fallback)

            if event.get("type") == "response_item":
                payload = event.get("payload")
                if (
                    isinstance(payload, dict)
                    and str(payload.get("type") or "").casefold() == "web_search_call"
                    and str(payload.get("status") or "completed").casefold() == "completed"
                ):
                    fallback = str(payload.get("id") or len(searches) + 1)
                    record_action(payload.get("action") or payload, fallback)

            if event.get("type") == "assistant":
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                for block in message.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = str(block.get("name") or "").casefold()
                    tool_input = block.get("input")
                    tool_input = tool_input if isinstance(tool_input, dict) else {}
                    fallback = str(block.get("id") or len(searches) + len(fetches) + 1)
                    if name == "websearch":
                        query = str(tool_input.get("query") or fallback).strip()
                        searches.add(query or fallback)
                    elif name == "webfetch":
                        target = str(tool_input.get("url") or fallback).strip()
                        fetches.add(target or fallback)
                usage = message.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                server_tools = usage.get("server_tool_use")
                server_tools = server_tools if isinstance(server_tools, dict) else {}
                reported_searches = max(
                    reported_searches,
                    int(server_tools.get("web_search_requests") or 0),
                )
                reported_fetches = max(
                    reported_fetches,
                    int(server_tools.get("web_fetch_requests") or 0),
                )

    return {
        "searches": max(len(searches), reported_searches),
        "fetches": max(len(fetches), reported_fetches),
    }


def _provider_command(command: str) -> str:
    """Remove the provider's bash wrapper while preserving its inner command."""

    try:
        argv = shlex.split(command)
    except ValueError:
        return _clean_command(command)
    if (
        len(argv) == 3
        and os.path.basename(argv[0]) in {"bash", "sh"}
        and argv[1] in {"-c", "-lc"}
    ):
        return _clean_command(argv[2])
    return _clean_command(command)


def _terminal_shell_command(command: str) -> str:
    """Return a wrapper script's final simple command, whose exit it reports."""

    try:
        argv = shlex.split(command)
    except ValueError:
        return ""
    if (
        len(argv) != 3
        or os.path.basename(argv[0]) not in {"bash", "sh"}
        or argv[1] not in {"-c", "-lc"}
    ):
        return ""
    lines = [line.strip() for line in argv[2].splitlines() if line.strip()]
    if len(lines) < 2:
        return ""
    terminal = lines[-1]
    try:
        terminal_argv = shlex.split(terminal)
    except ValueError:
        return ""
    if not terminal_argv or any(token in _SHELL_TOKENS for token in terminal_argv):
        return ""
    return _clean_command(terminal)


def _clean_command(value: object) -> str:
    return " ".join(str(value or "").split())


def evaluate_leg_completion(
    snapshot: dict[str, Any],
    leg: dict[str, Any],
    output_text: str,
    event_log_path: str | None = None,
) -> CompletionVerdict:
    """Return the completion verdict for one finished, successful leg."""

    worker_key = str(leg.get("worker_key") or "")
    run_id = str(snapshot.get("run_id") or "")
    access_mode = str(leg.get("access_mode") or "read")
    claims = _active_claims(run_id, worker_key) if access_mode == "write" else None
    observed = _observed_changed_paths(run_id, worker_key) if access_mode == "write" else None
    observed_tests = _observed_test_results(
        run_id,
        worker_key,
        output_text,
        event_log_path=event_log_path,
        allow_rerun=access_mode == "write",
        tool_root=str(snapshot.get("cwd") or "") or None,
    )
    phase_index, phase_name = _leg_phase(snapshot, leg)
    observed_research = _event_log_research_activity(event_log_path)
    return evaluate_completion(
        output_text=output_text,
        units=_work_units(snapshot),
        assignment_ids=_string_list(leg.get("assignment_ids")),
        access_mode=access_mode,
        activity=str(snapshot.get("activity") or "coding"),
        phase=phase_name,
        claimed_paths=claims,
        observed_changed_paths=observed,
        observed_test_results=observed_tests,
        observed_research_activity=observed_research,
        dependency_states=_dependency_states(snapshot, phase_index),
        research_depth=str(
            (snapshot.get("policy") or {}).get("research_depth") or "full"
        ).lower(),
    )
