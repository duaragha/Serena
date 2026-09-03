"""Read one exact Codex turn from its durable rollout.

The interactive bridge owns terminal input. This module only reads the JSONL
after a voice job has been committed, so evidence and restart reconciliation
do not depend on terminal output or an in-memory HTTP request.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.codex_bridge import find_codex_jsonl

_JS_CMD = re.compile(r"\bcmd\s*:\s*(\"(?:\\.|[^\"\\])*\")", re.DOTALL)
_EXIT_CODE = (
    re.compile(r'["\']?exit_code["\']?\s*[:=]\s*(-?\d+)'),
    re.compile(r"\bexit\s*=\s*(-?\d+)"),
    re.compile(r"process exited with (?:status|code)\s+(-?\d+)", re.IGNORECASE),
)
_CELL_ID = re.compile(r"(?:cell ID|session_id[\"']?\s*[:=]\s*)(\d+)", re.IGNORECASE)
_WAIT_CELL = re.compile(r"\b(?:cell_id|session_id)\s*:\s*(\d+)")


@dataclass(slots=True)
class CapturedCodexTurn:
    session_id: str
    start_offset: int
    end_offset: int
    source_available: bool = False
    source_error: str = ""
    progress_error: str = ""
    saw_prompt: bool = False
    completed: bool = False
    message: str = ""
    model: str = ""
    effort: str = ""
    commands: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


def line_count(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(1 for _line in handle)
    except OSError:
        return 0


def _prompt_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _flatten_output(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value or "")


def _command_from_js(value: object) -> str:
    source = str(value or "")
    match = _JS_CMD.search(source)
    if not match:
        return ""
    try:
        decoded = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ""
    return decoded if isinstance(decoded, str) else ""


def _exit_code(value: str) -> int | None:
    for pattern in _EXIT_CODE:
        match = pattern.search(value)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def _wait_cell(value: object) -> str:
    source = str(value or "")
    match = _WAIT_CELL.search(source)
    return match.group(1) if match else ""


def capture_turn(
    session_id: str,
    *,
    start_offset: int,
    end_offset: int | None = None,
    prompt_sha256: str = "",
) -> CapturedCodexTurn:
    """Project one committed prompt and its terminal event into job evidence."""

    result = CapturedCodexTurn(
        session_id=str(session_id),
        start_offset=max(0, int(start_offset)),
        end_offset=max(0, int(start_offset)),
    )
    path = find_codex_jsonl(session_id)
    if path is None:
        result.source_error = f"no Codex rollout for {session_id}"
        return result
    calls: dict[str, dict[str, str]] = {}
    wait_calls: dict[str, str] = {}
    cell_commands: dict[str, str] = {}
    selected = not prompt_sha256

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as error:
        result.source_error = f"Codex rollout is unavailable: {error}"
        return result
    result.source_available = True
    with handle:
        for index, raw in enumerate(handle):
            if index < result.start_offset:
                continue
            if end_offset is not None and index >= int(end_offset):
                break
            result.end_offset = index + 1
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            outer = str(event.get("type") or "")
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            kind = str(payload.get("type") or "")

            if outer == "turn_context":
                result.model = str(payload.get("model") or result.model)
                result.effort = str(
                    payload.get("reasoning_effort")
                    or payload.get("effort")
                    or result.effort
                )
                continue
            if outer == "event_msg" and kind in {
                "thread_settings",
                "thread_settings_applied",
            }:
                settings = payload.get("thread_settings") or payload.get("settings") or {}
                if isinstance(settings, dict):
                    result.model = str(settings.get("model") or result.model)
                    result.effort = str(
                        settings.get("reasoning_effort")
                        or settings.get("effort")
                        or result.effort
                    )
                continue
            if outer == "event_msg" and kind == "user_message":
                message = str(payload.get("message") or payload.get("text") or "")
                matches = not prompt_sha256 or _prompt_hash(message) == prompt_sha256
                if matches:
                    selected = True
                    result.saw_prompt = True
                elif selected and result.saw_prompt:
                    break
                continue
            if not selected or not result.saw_prompt:
                continue
            if outer == "event_msg" and kind in {"agent_message", "assistant_message"}:
                text = str(payload.get("message") or payload.get("text") or "").strip()
                if text:
                    result.message = text
                continue
            if outer == "event_msg" and kind in {"task_complete", "task_completed"}:
                final = str(payload.get("last_agent_message") or "").strip()
                if final:
                    result.message = final
                result.completed = True
                break
            if outer != "response_item":
                continue

            call_id = str(payload.get("call_id") or "")
            if kind == "custom_tool_call" and payload.get("name") == "exec":
                command = _command_from_js(payload.get("input"))
                if command and call_id:
                    calls[call_id] = {"command": command}
                continue
            if kind == "function_call" and payload.get("name") in {"wait", "write_stdin"}:
                cell = _wait_cell(payload.get("arguments"))
                if cell and call_id:
                    wait_calls[call_id] = cell
                continue
            if kind not in {"custom_tool_call_output", "function_call_output"}:
                continue

            output = _flatten_output(payload.get("output"))
            command = ""
            if call_id in calls:
                command = calls[call_id]["command"]
                match = _CELL_ID.search(output)
                if match:
                    cell_commands[match.group(1)] = command
            elif call_id in wait_calls:
                command = cell_commands.get(wait_calls[call_id], "")
            if not command:
                continue
            code = _exit_code(output)
            if code is None:
                continue
            receipt = {
                "command": command,
                "exit_code": code,
                "output": output[-8_000:],
            }
            result.commands.append(receipt)
            result.events.append(
                {
                    "type": "command_execution",
                    "command": command[:2_000],
                    "exit_code": code,
                    "output": output[-8_000:],
                }
            )
    return result


def wait_for_turn(
    session_id: str,
    *,
    start_offset: int,
    prompt_sha256: str,
    timeout: float,
    poll_interval: float = 0.5,
    progress_probe: Callable[[], str] | None = None,
    progress_probe_interval: float = 2.0,
) -> CapturedCodexTurn:
    """Wait without resending while the exact owner can still make progress."""

    deadline = time.time() + max(0.0, float(timeout))
    next_probe = time.monotonic()
    latest = capture_turn(
        session_id,
        start_offset=start_offset,
        prompt_sha256=prompt_sha256,
    )
    while latest.source_available and not latest.completed and time.time() < deadline:
        if progress_probe is not None and time.monotonic() >= next_probe:
            try:
                latest.progress_error = str(progress_probe() or "")
            except Exception as error:
                latest.progress_error = f"turn progress probe failed: {error}"
            if latest.progress_error:
                break
            next_probe = time.monotonic() + max(0.1, float(progress_probe_interval))
        time.sleep(max(0.05, poll_interval))
        latest = capture_turn(
            session_id,
            start_offset=start_offset,
            prompt_sha256=prompt_sha256,
        )
    return latest
