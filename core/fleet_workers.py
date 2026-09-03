"""Direct, genuine Claude and Codex CLI workers for Serena Fleet."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.billing import strip_metered_auth_env
from core.fleet_context import redact_text
from core.fleet_policy import expected_model_matches
from core.fleet_read_mcp import claude_flags as claude_read_mcp_flags
from core.fleet_read_mcp import codex_flags as codex_read_mcp_flags

MAX_EVENT_LINE_BYTES = 1024 * 1024
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_TRUNCATED_EVENT_PREVIEW_CHARS = 8_000
MAX_TRUNCATED_EVENT_RECEIPT_CHARS = 64_000
DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60
DEFAULT_EXIT_DRAIN_SECONDS = 2.0
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "serena" / "fleet"


EventCallback = Callable[[str, dict[str, Any]], None]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    run_id: str
    leg_id: str
    attempt_id: str
    task: str
    activity: str
    phase: str
    role: str
    provider: str
    model: str
    effort: str
    access_mode: str
    cwd: str
    prompt: str
    worker_key: str = ""
    worker_label: str = ""
    assignment: str = ""
    assignment_ids: tuple[str, ...] = ()
    review_target_ids: tuple[str, ...] = ()
    resume_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerResult:
    ok: bool
    output_text: str
    session_id: str | None
    actual_model: str | None
    actual_effort: str | None
    exit_code: int
    error: str | None = None
    cancelled: bool = False
    event_log_path: str | None = None


def run_worker(
    request: WorkerRequest,
    *,
    cancel_requested: CancelCallback,
    on_event: EventCallback,
) -> WorkerResult:
    provider = request.provider.lower()
    if provider == "codex":
        return _run_codex(request, cancel_requested=cancel_requested, on_event=on_event)
    if provider == "claude":
        return _run_claude(request, cancel_requested=cancel_requested, on_event=on_event)
    raise ValueError(f"unsupported Fleet provider {request.provider}")


def worker_command(request: WorkerRequest, *, session_id: str | None = None) -> list[str]:
    """Build the exact provider argv, exposed for policy and regression tests."""

    if request.provider == "codex":
        base = [
            _binary("codex"),
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--disable",
            "multi_agent",
            "-m",
            request.model,
            "-c",
            f'model_reasoning_effort="{request.effort}"',
            "-c",
            'approval_policy="never"',
            "--sandbox",
            _codex_sandbox(request.access_mode),
        ]
        if request.phase == "discover" or request.activity == "research":
            base += ["--enable", "standalone_web_search"]
        # A non-writing leg gets Serena's read-only MCP gateway and nothing
        # else. Writers keep zero MCP: they already have a shell.
        base += codex_read_mcp_flags(request.access_mode)
        if request.resume_session_id:
            base += ["resume", request.resume_session_id, "-"]
        else:
            base += ["-C", request.cwd, "-"]
        return base

    if request.provider == "claude":
        sid = session_id or request.resume_session_id or str(uuid.uuid4())
        disallowed = (
            "Agent,Task,TaskCreate,TaskGet,TaskList,TaskOutput,TaskStop,TaskUpdate,"
            "TeamCreate,TeamDelete,SendMessage,Skill,Workflow"
        )
        if request.access_mode != "write":
            disallowed += ",Edit,Write,NotebookEdit"
        # Same rule as Codex: read legs get the gateway, writers get nothing.
        # --strict-mcp-config keeps the user's own MCP configuration out either
        # way, so the only reachable tools are the ones Fleet chose per tool.
        read_mcp = claude_read_mcp_flags(request.access_mode)
        # --safe-mode disables MCP servers outright, so a leg that was actually
        # granted read access swaps it for the narrower isolation that does the
        # same job here: no user, project, or local settings, so no hooks,
        # plugins, or custom agents leak in. Slash commands, MCP selection, and
        # the tool allowlists below are already pinned by their own flags. Every
        # other leg, including a read leg with no catalog, keeps --safe-mode.
        isolation = ["--setting-sources", ""] if read_mcp else ["--safe-mode"]
        base = [
            _binary("claude"),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            request.model,
            "--effort",
            request.effort,
            *isolation,
            "--strict-mcp-config",
            *(read_mcp or ["--mcp-config", '{"mcpServers":{}}']),
            "--disable-slash-commands",
            "--disallowedTools",
            disallowed,
        ]
        if request.access_mode == "write":
            base += [
                "--dangerously-skip-permissions",
                "--tools",
                "Bash,Read,Glob,Grep,Edit,Write,NotebookEdit",
            ]
        else:
            tools = "Read,Glob,Grep,Bash"
            if request.phase == "discover" or request.activity == "research":
                tools += ",WebSearch,WebFetch"
            base += [
                "--permission-mode",
                "dontAsk",
                "--tools",
                tools,
            ]
        if request.resume_session_id:
            base += ["--resume", request.resume_session_id]
        else:
            base += ["--session-id", sid]
        return base
    raise ValueError(f"unsupported Fleet provider {request.provider}")


def runtime_doctor() -> dict[str, Any]:
    providers: dict[str, Any] = {}
    for provider in ("codex", "claude"):
        try:
            binary = _binary(provider)
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=_worker_environment(),
            )
            help_command = [binary, "exec", "--help"] if provider == "codex" else [binary, "--help"]
            help_result = subprocess.run(
                help_command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=_worker_environment(),
            )
            help_text = help_result.stdout or help_result.stderr
            required = (
                ("--ignore-user-config", "--disable", "--sandbox", "--model")
                if provider == "codex"
                else (
                    "--strict-mcp-config",
                    "--disable-slash-commands",
                    "--setting-sources",
                    "--model",
                    "--effort",
                )
            )
            command_contract_ok = help_result.returncode == 0 and all(
                flag in help_text for flag in required
            )
            providers[provider] = {
                "ok": result.returncode == 0 and command_contract_ok,
                "binary": binary,
                "version": (result.stdout or result.stderr).strip()[:300],
                "command_contract_ok": command_contract_ok,
                "error": (
                    None
                    if result.returncode == 0 and command_contract_ok
                    else "version or required CLI flag probe failed"
                ),
            }
        except Exception as exc:
            providers[provider] = {
                "ok": False,
                "binary": None,
                "version": "",
                "error": str(exc),
            }
    ready = [provider for provider, item in providers.items() if item["ok"]]
    full_mixed_ready = len(ready) == len(providers)
    return {
        # Fleet remains operable when either native runtime is healthy. The
        # selected run policy is responsible for requiring both runtimes for a
        # balanced run.
        "ok": bool(ready),
        "degraded": bool(ready) and not full_mixed_ready,
        "full_mixed_ready": full_mixed_ready,
        "ready_providers": ready,
        "providers": providers,
    }


def _run_codex(
    request: WorkerRequest,
    *,
    cancel_requested: CancelCallback,
    on_event: EventCallback,
) -> WorkerResult:
    command = worker_command(request)
    session_id = request.resume_session_id
    output_text = ""
    actual_model: str | None = None
    actual_effort: str | None = None
    error_detail = ""

    def parse(line: str) -> None:
        nonlocal session_id, output_text, actual_model, actual_effort, error_detail
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        event_type = str(event.get("type") or "")
        if event_type == "thread.started":
            session_id = str(event.get("thread_id") or session_id or "") or None
            if session_id:
                on_event("session.started", {"session_id": session_id})
        elif event_type in {"thread.settings", "thread.settings_applied"}:
            settings = event.get("thread_settings") or event.get("settings") or event
            if isinstance(settings, dict):
                actual_model = str(settings.get("model") or actual_model or "") or None
                actual_effort = (
                    str(
                        settings.get("reasoning_effort")
                        or settings.get("effort")
                        or actual_effort
                        or ""
                    )
                    or None
                )
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if not isinstance(item, dict):
                return
            if item.get("type") == "agent_message":
                output_text = str(item.get("text") or "").strip() or output_text
            elif item.get("type") == "error":
                error_detail = str(item.get("message") or "").strip() or error_detail
        elif event_type == "turn.failed":
            failure = event.get("error") or {}
            error_detail = (
                str(failure.get("message") if isinstance(failure, dict) else failure).strip()
                or error_detail
            )
        elif event_type == "error":
            error_detail = str(event.get("message") or "").strip() or error_detail
        on_event("worker.event", _event_summary(event))

    process = _stream_process(
        command,
        request=request,
        parse_stdout=parse,
        cancel_requested=cancel_requested,
        on_event=on_event,
    )
    if session_id and (not actual_model or not actual_effort):
        discovered_model, discovered_effort = _codex_actual_identity(session_id)
        actual_model = actual_model or discovered_model
        actual_effort = actual_effort or discovered_effort
    if process.cancelled:
        return WorkerResult(
            False,
            output_text,
            session_id,
            actual_model,
            actual_effort,
            process.exit_code,
            "cancelled by user",
            True,
            process.event_log_path,
        )
    if process.exit_code != 0:
        detail = (
            error_detail
            or process.stderr[-2_000:]
            or f"Codex exited with status {process.exit_code}"
        )
        return WorkerResult(
            False,
            output_text,
            session_id,
            actual_model,
            actual_effort,
            process.exit_code,
            detail,
            False,
            process.event_log_path,
        )
    identity_error = _identity_error(request, actual_model, actual_effort)
    if identity_error:
        return WorkerResult(
            False,
            output_text,
            session_id,
            actual_model,
            actual_effort,
            process.exit_code,
            identity_error,
            False,
            process.event_log_path,
        )
    if not output_text:
        return WorkerResult(
            False,
            "",
            session_id,
            actual_model,
            actual_effort,
            process.exit_code,
            error_detail or "Codex completed without a final response",
            False,
            process.event_log_path,
        )
    return WorkerResult(
        True,
        output_text,
        session_id,
        actual_model,
        actual_effort,
        process.exit_code,
        None,
        False,
        process.event_log_path,
    )


def _run_claude(
    request: WorkerRequest,
    *,
    cancel_requested: CancelCallback,
    on_event: EventCallback,
) -> WorkerResult:
    assigned_sid = request.resume_session_id or str(uuid.uuid4())
    command = worker_command(request, session_id=assigned_sid)
    session_id: str | None = assigned_sid
    output_text = ""
    text_blocks: list[str] = []
    actual_model: str | None = None
    actual_effort: str | None = None
    error_detail = ""
    announced_session_id: str | None = assigned_sid

    def parse(line: str) -> None:
        nonlocal \
            session_id, \
            output_text, \
            actual_model, \
            actual_effort, \
            error_detail, \
            announced_session_id
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        event_type = str(event.get("type") or "")
        actual_effort = str(event.get("effort") or actual_effort or "") or None
        if event_type == "system" and event.get("subtype") == "init":
            session_id = str(event.get("session_id") or session_id or "") or None
            actual_model = _reported_model(event.get("model")) or actual_model
            if session_id and session_id != announced_session_id:
                on_event("session.started", {"session_id": session_id})
                announced_session_id = session_id
        elif event_type == "assistant":
            message = event.get("message") or {}
            if isinstance(message, dict):
                actual_model = _reported_model(message.get("model")) or actual_model
                content = message.get("content") or []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            value = str(block.get("text") or "").strip()
                            if value:
                                text_blocks.append(value)
        elif event_type == "result":
            session_id = str(event.get("session_id") or session_id or "") or None
            output_text = str(event.get("result") or "").strip() or output_text
            if event.get("is_error"):
                error_detail = output_text or str(
                    event.get("subtype") or "Claude reported an error"
                )
        elif event_type == "error":
            error_detail = str(event.get("error") or event.get("message") or "").strip()
        on_event("worker.event", _event_summary(event))

    # Claude's session id is assigned before launch, so Fleet can expose the
    # transcript immediately without filesystem polling or a relay process.
    on_event("session.started", {"session_id": assigned_sid})
    process = _stream_process(
        command,
        request=request,
        parse_stdout=parse,
        cancel_requested=cancel_requested,
        on_event=on_event,
    )
    if session_id:
        discovered_model, discovered_effort = _claude_actual_identity(
            session_id,
            request.cwd,
        )
        # The streamed events belong to this invocation; the durable session
        # transcript also contains older phase turns.  Never let an older
        # phase's identity replace current-turn telemetry.
        actual_model = actual_model or _reported_model(discovered_model)
        actual_effort = actual_effort or discovered_effort
    if actual_model and not actual_effort:
        # Claude Code accepts --effort as an invocation override but some
        # models (currently Haiku) omit it from both stream-json and the
        # persisted assistant events.  In that case the accepted command-line
        # setting is the only current-turn effort signal.  A reported value
        # still wins above and is checked strictly below.
        actual_effort = request.effort
    output_text = output_text or (text_blocks[-1] if text_blocks else "")
    if process.cancelled:
        return WorkerResult(
            False,
            output_text,
            session_id,
            actual_model,
            actual_effort,
            process.exit_code,
            "cancelled by user",
            True,
            process.event_log_path,
        )
    if process.exit_code != 0 or error_detail:
        detail = (
            error_detail
            or process.stderr[-2_000:]
            or f"Claude exited with status {process.exit_code}"
        )
        return WorkerResult(
            False,
            output_text,
            session_id,
            actual_model,
            actual_effort,
            process.exit_code,
            detail,
            False,
            process.event_log_path,
        )
    identity_error = _identity_error(request, actual_model, actual_effort)
    if identity_error:
        return WorkerResult(
            False,
            output_text,
            session_id,
            actual_model,
            actual_effort,
            process.exit_code,
            identity_error,
            False,
            process.event_log_path,
        )
    if not output_text:
        return WorkerResult(
            False,
            "",
            session_id,
            actual_model,
            actual_effort,
            process.exit_code,
            "Claude completed without a final response",
            False,
            process.event_log_path,
        )
    return WorkerResult(
        True,
        output_text,
        session_id,
        actual_model,
        actual_effort,
        process.exit_code,
        None,
        False,
        process.event_log_path,
    )


@dataclass(frozen=True, slots=True)
class _ProcessResult:
    exit_code: int
    stderr: str
    cancelled: bool
    event_log_path: str


def _stream_process(
    command: list[str],
    *,
    request: WorkerRequest,
    parse_stdout: Callable[[str], None],
    cancel_requested: CancelCallback,
    on_event: EventCallback,
) -> _ProcessResult:
    log_path = _event_log_path(request)
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    process = subprocess.Popen(
        command,
        cwd=request.cwd,
        env=_worker_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    readers: list[threading.Thread] = []
    try:
        # Claude waits only briefly for piped input. Start draining output and
        # deliver the prompt before any callback that may refresh Serena's
        # index or touch slower metadata stores.
        output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def drain(name: str, pipe) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    output_queue.put((name, line))
            finally:
                output_queue.put((name, None))

        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        assert process.stdin is not None
        with suppress(BrokenPipeError, OSError):
            process.stdin.write(request.prompt)
            process.stdin.close()
        on_event(
            "process.started",
            {"pid": process.pid, "command": command[:4], "event_log_path": str(log_path)},
        )
        timeout = float(
            os.environ.get("SERENA_FLEET_WORKER_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS
        )
        deadline = time.monotonic() + max(1.0, timeout)
        exit_drain_seconds = max(
            0.1,
            float(
                os.environ.get("SERENA_FLEET_EXIT_DRAIN_SECONDS")
                or DEFAULT_EXIT_DRAIN_SECONDS
            ),
        )
        exit_seen_at: float | None = None
        open_pipes = len(readers)
        stderr_parts: list[str] = []
        captured_bytes = 0
        capture_limit_reported = False
        event_line_limit_reported = False
        cancelled = False
        timed_out = False
        with log_path.open("a", encoding="utf-8") as log:
            while open_pipes:
                if cancel_requested():
                    cancelled = True
                    _terminate_process_group(process)
                if time.monotonic() >= deadline and process.poll() is None:
                    timed_out = True
                    _terminate_process_group(process)
                if process.poll() is not None:
                    exit_seen_at = exit_seen_at or time.monotonic()
                    if time.monotonic() - exit_seen_at >= exit_drain_seconds:
                        # A crashed worker can leave a browser or other grandchild
                        # holding stdout/stderr open. Do not let inherited pipe FDs
                        # pin the whole Fleet run after the owned process is gone.
                        _terminate_process_group(process)
                        break
                try:
                    source, line = output_queue.get(timeout=0.25)
                except queue.Empty:
                    if process.poll() is not None and all(
                        not reader.is_alive() for reader in readers
                    ):
                        break
                    continue
                if line is None:
                    open_pipes -= 1
                    continue
                encoded_size = len(line.encode("utf-8", errors="replace"))
                clean_line = line.rstrip("\n")
                if encoded_size > MAX_EVENT_LINE_BYTES:
                    # Provider stream-json includes complete tool results in one
                    # JSON line.  A verbose read or test can therefore cross the
                    # log safety bound even though the worker is healthy.  Keep
                    # parsing the original event, but persist only a bounded,
                    # valid JSON receipt instead of killing the process.
                    log_line = _truncated_event_receipt(clean_line, encoded_size)
                    if not event_line_limit_reported:
                        event_line_limit_reported = True
                        on_event(
                            "process.output_truncated",
                            {
                                "source": source,
                                "reason": "event_line_limit",
                                "original_bytes": encoded_size,
                                "logged_as_receipt": True,
                            },
                        )
                else:
                    log_line = redact_text(clean_line)[0]
                record = json.dumps(
                    {"at": time.time(), "stream": source, "line": log_line},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                record_bytes = len(record.encode("utf-8", errors="replace")) + 1
                if captured_bytes + record_bytes <= MAX_CAPTURE_BYTES:
                    log.write(record + "\n")
                    log.flush()
                    captured_bytes += record_bytes
                elif not capture_limit_reported:
                    # The capture limit bounds Fleet-owned diagnostics, not the
                    # provider turn itself.  Parsing must continue so a noisy
                    # tool cannot turn a successful worker into a failed leg.
                    capture_limit_reported = True
                    on_event(
                        "process.output_truncated",
                        {
                            "source": source,
                            "reason": "capture_limit",
                            "capture_limit_bytes": MAX_CAPTURE_BYTES,
                            "logged_as_receipt": False,
                        },
                    )
                if source == "stdout":
                    parse_stdout(line)
                else:
                    stderr_parts.append(line[-8_000:])
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            _terminate_process_group(process)
        exit_code = process.returncode if process.returncode is not None else -1
        if timed_out:
            stderr_parts.append(f"worker timed out after {timeout:.0f}s")
        return _ProcessResult(
            exit_code=exit_code,
            stderr="".join(stderr_parts)[-8_000:],
            cancelled=cancelled,
            event_log_path=str(log_path),
        )
    finally:
        if process.poll() is None:
            _terminate_process_group(process)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                with suppress(OSError):
                    pipe.close()
        for reader in readers:
            reader.join(timeout=1)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if os.name != "nt":
        # The direct process may already have exited while a descendant still
        # owns its process group and inherited output pipes.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        if process.poll() is not None:
            return
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    else:
        if process.poll() is not None:
            return
        with suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                process.kill()


def _identity_error(
    request: WorkerRequest,
    actual_model: str | None,
    actual_effort: str | None,
) -> str | None:
    if not actual_model:
        return f"{request.provider} did not report the model it actually ran"
    if not expected_model_matches(request.provider, request.model, actual_model):
        return f"model mismatch: requested {request.model}, provider reported {actual_model}"
    if not actual_effort:
        return f"{request.provider} did not report the effort it actually ran"
    if actual_effort.casefold() != request.effort.casefold():
        return f"effort mismatch: requested {request.effort}, provider reported {actual_effort}"
    return None


def _codex_actual_identity(session_id: str) -> tuple[str | None, str | None]:
    try:
        from core.codex_bridge import find_codex_jsonl

        path = find_codex_jsonl(session_id)
    except Exception:
        path = None
    if path is None:
        return None, None
    model: str | None = None
    effort: str | None = None
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") == "turn_context" and isinstance(payload, dict):
                    model = str(payload.get("model") or model or "") or None
                    effort = (
                        str(
                            payload.get("reasoning_effort") or payload.get("effort") or effort or ""
                        )
                        or None
                    )
                if event.get("type") == "event_msg" and isinstance(payload, dict):
                    settings = payload.get("thread_settings") or {}
                    if payload.get("type") == "thread_settings_applied" and isinstance(
                        settings, dict
                    ):
                        model = str(settings.get("model") or model or "") or None
                        effort = str(settings.get("reasoning_effort") or effort or "") or None
    except OSError:
        return model, effort
    return model, effort


def _claude_actual_identity(
    session_id: str,
    cwd: str | None = None,
) -> tuple[str | None, str | None]:
    try:
        from core.claude_bridge import CLAUDE_PROJECTS_ROOT, find_claude_jsonl
        from core.config import claude_project_dir_for

        path = None
        if cwd:
            candidate = CLAUDE_PROJECTS_ROOT / claude_project_dir_for(cwd) / f"{session_id}.jsonl"
            if candidate.is_file():
                path = candidate
        path = path or find_claude_jsonl(session_id)
    except Exception:
        path = None
    if path is None:
        return None, None
    model: str | None = None
    effort: str | None = None
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                message = event.get("message") or {}
                if event.get("type") == "assistant" and isinstance(message, dict):
                    reported_model = _reported_model(message.get("model"))
                    reported_effort = str(event.get("effort") or "") or None
                    if reported_model and reported_model != model:
                        # Effort is turn/model scoped.  Carrying the prior
                        # model's value across a phase switch can manufacture
                        # a false mismatch when the new model omits telemetry.
                        model = reported_model
                        effort = reported_effort
                    else:
                        model = reported_model or model
                        effort = reported_effort or effort
    except OSError:
        return model, effort
    return model, effort


def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "event")
    summary: dict[str, Any] = {"type": event_type}
    settings = event.get("thread_settings") or event.get("settings") or {}
    if isinstance(settings, dict):
        model = _reported_model(settings.get("model"))
        if model:
            summary["model"] = model
        summary["effort"] = settings.get("reasoning_effort") or settings.get("effort")
    model = _reported_model(event.get("model"))
    if model:
        summary["model"] = model
    if event.get("effort"):
        summary["effort"] = event.get("effort")
    item = event.get("item")
    if isinstance(item, dict):
        summary["item_type"] = item.get("type")
        if item.get("type") == "command_execution":
            summary["command"] = str(item.get("command") or "")[:1_000]
            if item.get("exit_code") is not None:
                summary["exit_code"] = item.get("exit_code")
            if item.get("status"):
                summary["status"] = item.get("status")
        elif item.get("type") == "agent_message":
            summary["text"] = str(item.get("text") or "")[:2_000]
    message = event.get("message")
    if isinstance(message, dict):
        model = _reported_model(message.get("model"))
        if model:
            summary["model"] = model
    return summary


def _truncated_event_receipt(line: str, original_bytes: int) -> str:
    """Return bounded, valid JSON evidence for one oversized provider event."""

    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError, ValueError):
        event = None
    if not isinstance(event, dict):
        preview = redact_text(line[:MAX_TRUNCATED_EVENT_PREVIEW_CHARS])[0]
        return json.dumps(
            {
                "type": "serena.truncated_event",
                "_serena_truncated": True,
                "original_bytes": original_bytes,
                "preview": preview,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    receipt: dict[str, Any] = {
        "type": str(event.get("type") or "event"),
        "_serena_truncated": True,
        "original_bytes": original_bytes,
        "summary": _event_summary(event),
    }
    receipt_budget = [MAX_TRUNCATED_EVENT_RECEIPT_CHARS]
    for key in ("subtype", "session_id", "model", "effort", "is_error"):
        value = event.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            receipt[key] = value

    # Preserve the exact provider shapes consumed by Fleet's completion gate.
    item = event.get("item")
    if isinstance(item, dict):
        receipt["item"] = _receipt_fields(
            item,
            "type",
            "id",
            "command",
            "exit_code",
            "status",
            "action",
            budget=receipt_budget,
        )
    payload = event.get("payload")
    if isinstance(payload, dict):
        receipt["payload"] = _receipt_fields(
            payload,
            "type",
            "id",
            "status",
            "action",
            budget=receipt_budget,
        )
    message = event.get("message")
    if isinstance(message, dict):
        compact_message = _receipt_fields(
            message,
            "model",
            "usage",
            budget=receipt_budget,
        )
        tool_uses = []
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_uses.append(
                _receipt_fields(
                    block,
                    "type",
                    "id",
                    "name",
                    "input",
                    budget=receipt_budget,
                )
            )
        if tool_uses:
            compact_message["content"] = tool_uses[:64]
        receipt["message"] = compact_message
    for key in ("error", "message", "result"):
        value = event.get(key)
        if isinstance(value, str) and value:
            receipt[key] = _receipt_value(value, receipt_budget)
    encoded = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    return redact_text(encoded)[0]


def _receipt_fields(
    source: dict[str, Any],
    *keys: str,
    budget: list[int],
) -> dict[str, Any]:
    receipt: dict[str, Any] = {}
    for key in keys:
        if key not in source or budget[0] <= 0:
            continue
        receipt[key] = _receipt_value(source[key], budget)
    return receipt


def _receipt_value(value: Any, budget: list[int], *, depth: int = 0) -> Any:
    if budget[0] <= 0 or depth >= 4:
        return None
    if isinstance(value, str):
        limit = min(MAX_TRUNCATED_EVENT_PREVIEW_CHARS, budget[0])
        clipped = value[:limit]
        budget[0] -= len(clipped)
        return clipped
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {
            str(key): _receipt_value(nested, budget, depth=depth + 1)
            for key, nested in list(value.items())[:32]
            if budget[0] > 0
        }
    if isinstance(value, list):
        return [
            _receipt_value(nested, budget, depth=depth + 1)
            for nested in value[:32]
            if budget[0] > 0
        ]
    text = str(value)
    limit = min(MAX_TRUNCATED_EVENT_PREVIEW_CHARS, budget[0])
    clipped = text[:limit]
    budget[0] -= len(clipped)
    return clipped


def _reported_model(value: object) -> str | None:
    """Drop Claude's synthetic terminal error marker, not the last real model."""

    clean = str(value or "").strip()
    if not clean or (clean.startswith("<") and clean.endswith(">")):
        return None
    return clean


def _codex_sandbox(access_mode: str) -> str:
    return "workspace-write" if access_mode == "write" else "read-only"


def _binary(provider: str) -> str:
    override = os.environ.get(f"SERENA_FLEET_{provider.upper()}_BIN", "").strip()
    if override:
        return str(Path(override).expanduser())
    found = shutil.which(provider)
    if found:
        return found
    home = Path.home()
    if provider == "codex":
        candidates = sorted((home / ".nvm" / "versions" / "node").glob("*/bin/codex"), reverse=True)
    else:
        candidates = [
            home / ".local" / "bin" / "claude",
            home / ".claude" / "local" / "claude",
            Path("/usr/local/bin/claude"),
            Path("/usr/bin/claude"),
        ]
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise FileNotFoundError(f"{provider} CLI is not installed")


def _worker_environment() -> dict[str, str]:
    environment = strip_metered_auth_env(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    environment["SERENA_FLEET_WORKER"] = "1"
    return environment


def _event_log_path(request: WorkerRequest) -> Path:
    configured = os.environ.get("SERENA_FLEET_STATE_DIR", "").strip()
    root = Path(configured).expanduser() if configured else DEFAULT_STATE_DIR
    return root / "runs" / request.run_id / f"{request.attempt_id}.jsonl"
