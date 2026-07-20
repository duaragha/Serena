"""Live v2.5c proof: read-only brain tool to local speech with no panes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import psutil

from core.brain_tools import BRAIN_TOOL_NAMES
from core.config import METADATA_DIR

from .brain import DEFAULT_DISCOVERY, BrainDiscoveryClient
from .tts import create_tts_backend

DEFAULT_PROMPT = (
    "use git_latest to refresh the serena repo now, then tell me the current "
    "branch and newest commit subject in one short sentence."
)
DEFAULT_EXPECTED_TOOL = "mcp__serena-ro__git_latest"
_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
_CODEX_SESSIONS = Path.home() / ".codex" / "sessions"


class AcceptanceError(RuntimeError):
    """The live v2.5c proof could not establish an acceptance condition."""


def _jsonl_files(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {str(path.resolve()) for path in root.rglob("*.jsonl") if path.is_file()}


def _metadata_manifest(root: Path = METADATA_DIR) -> dict[str, tuple[int, int]]:
    if not root.is_dir():
        return {}
    manifest: dict[str, tuple[int, int]] = {}
    for path in root.glob("*.json"):
        try:
            stat_result = path.stat()
        except OSError as exc:
            raise AcceptanceError(f"session metadata could not be inspected: {exc}") from exc
        manifest[str(path.resolve())] = (stat_result.st_size, stat_result.st_mtime_ns)
    return manifest


def _is_assistant_cli(command: object) -> bool:
    if not isinstance(command, (list, tuple)) or not command:
        return False
    executable = Path(str(command[0])).name.lower()
    if executable in {"claude", "claude.exe", "codex", "codex.exe"}:
        return True
    for token in command[1:3]:
        name = Path(str(token)).name.lower()
        if name in {"claude", "claude.exe", "codex", "codex.exe"}:
            return True
    return False


def _assistant_cli_processes(
    process_iter: Callable[..., Iterable[Any]] = psutil.process_iter,
) -> dict[int, float]:
    processes: dict[int, float] = {}
    try:
        current_username = psutil.Process(os.getpid()).username()
        if not current_username:
            raise AcceptanceError("current process owner could not be inspected")
        candidates = process_iter(
            attrs=("pid", "cmdline", "create_time", "status", "username")
        )
        for candidate in candidates:
            info = getattr(candidate, "info", {})
            if not isinstance(info, dict):
                raise AcceptanceError("process census returned invalid metadata")
            if info.get("username") != current_username:
                continue
            if info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            pid = info.get("pid")
            command = info.get("cmdline")
            created = info.get("create_time")
            if not isinstance(pid, int) or isinstance(pid, bool):
                raise AcceptanceError("process census returned an invalid pid")
            if not isinstance(command, (list, tuple)) or not command:
                raise AcceptanceError(
                    f"same-user process {pid} has no inspectable command line"
                )
            try:
                created_value = float(created)
            except (TypeError, ValueError) as exc:
                raise AcceptanceError(
                    f"same-user process {pid} has no creation identity"
                ) from exc
            if _is_assistant_cli(command):
                processes[pid] = created_value
    except AcceptanceError:
        raise
    except (OSError, PermissionError, psutil.Error, TypeError) as exc:
        raise AcceptanceError(f"assistant process census failed: {exc}") from exc
    return processes


class _PaneGuard:
    """Continuously retain any pane evidence that appears during a live turn."""

    def __init__(self) -> None:
        self._baseline_claude = _jsonl_files(_CLAUDE_PROJECTS)
        self._baseline_codex = _jsonl_files(_CODEX_SESSIONS)
        self._baseline_metadata = _metadata_manifest()
        self._baseline_processes = set(_assistant_cli_processes().items())
        self._new_claude: set[str] = set()
        self._new_codex: set[str] = set()
        self._changed_metadata: set[str] = set()
        self._new_processes: set[tuple[int, float]] = set()
        self._error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._watch,
            name="serena-v25c-pane-guard",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _sample_processes(self) -> None:
        current = set(_assistant_cli_processes().items())
        self._new_processes.update(current.difference(self._baseline_processes))

    def _sample_files(self) -> None:
        self._new_claude.update(
            _jsonl_files(_CLAUDE_PROJECTS).difference(self._baseline_claude)
        )
        self._new_codex.update(
            _jsonl_files(_CODEX_SESSIONS).difference(self._baseline_codex)
        )
        current_metadata = _metadata_manifest()
        self._changed_metadata.update(
            path
            for path in set(self._baseline_metadata).union(current_metadata)
            if self._baseline_metadata.get(path) != current_metadata.get(path)
        )

    def _watch(self) -> None:
        next_file_sample = 0.0
        try:
            while not self._stop.wait(0.01):
                self._sample_processes()
                now = time.monotonic()
                if now >= next_file_sample:
                    self._sample_files()
                    next_file_sample = now + 0.1
        except BaseException as exc:
            self._error = exc
            self._stop.set()

    def stop(self) -> dict[str, int]:
        self._stop.set()
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            raise AcceptanceError("continuous pane guard did not stop")
        if self._error is not None:
            raise AcceptanceError(f"continuous pane guard failed: {self._error}")
        self._sample_processes()
        self._sample_files()
        if self._new_claude or self._new_codex or self._changed_metadata:
            raise AcceptanceError(
                "voice tool turn created pane evidence: "
                f"claude_sessions={len(self._new_claude)}, "
                f"codex_sessions={len(self._new_codex)}, "
                f"metadata={len(self._changed_metadata)}, "
                f"concurrent_cli_processes={len(self._new_processes)}"
            )
        # Other Serena panes may start concurrently while this proof runs.
        # A bare process start is not attributable to the voice turn. A real
        # pane must also create a session JSONL or change group metadata, both
        # of which are retained above even when the process is short-lived.
        return {
            "new_claude_sessions": 0,
            "new_codex_sessions": 0,
            "changed_session_metadata": 0,
            "new_pane_processes": 0,
            "concurrent_assistant_cli_processes": len(self._new_processes),
        }


def _load_discovery(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"brain discovery could not be read: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("session_id"):
        raise AcceptanceError("brain discovery has no resident session id")
    return payload


def _brain_transcript(session_id: str, root: Path = _CLAUDE_PROJECTS) -> Path:
    matches = list(root.rglob(f"{session_id}.jsonl")) if root.is_dir() else []
    if len(matches) != 1:
        raise AcceptanceError(
            f"expected one resident brain transcript for {session_id}, found {len(matches)}"
        )
    return matches[0].resolve()


def _read_appended_rows(path: Path, offset: int) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            appended = handle.read()
    except OSError as exc:
        raise AcceptanceError(f"brain transcript append could not be read: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line in appended.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AcceptanceError("brain transcript append contains malformed JSONL") from exc
        if not isinstance(row, dict):
            raise AcceptanceError("brain transcript append contains a non-object row")
        rows.append(row)
    return rows


def _tool_evidence(
    rows: Iterable[Mapping[str, Any]], expected_tool: str
) -> dict[str, Any]:
    tool_uses: list[tuple[str, str]] = []
    tool_results: set[str] = set()
    for row in rows:
        message = row.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "tool_use":
                tool_id = block.get("id")
                name = block.get("name")
                if isinstance(tool_id, str) and isinstance(name, str):
                    tool_uses.append((tool_id, name))
            elif block.get("type") == "tool_result":
                tool_id = block.get("tool_use_id")
                if isinstance(tool_id, str):
                    tool_results.add(tool_id)

    used_names = [name for _, name in tool_uses]
    unexpected = sorted(set(used_names).difference(BRAIN_TOOL_NAMES))
    expected_ids = [tool_id for tool_id, name in tool_uses if name == expected_tool]
    missing_results = sorted(set(expected_ids).difference(tool_results))
    if expected_tool not in BRAIN_TOOL_NAMES:
        raise AcceptanceError(f"expected tool is not in the brain allowlist: {expected_tool}")
    if unexpected:
        raise AcceptanceError(f"brain invoked tools outside the read-only allowlist: {unexpected}")
    if not expected_ids:
        raise AcceptanceError(f"brain did not invoke required tool {expected_tool}")
    if missing_results:
        raise AcceptanceError("required read-only tool did not return a result")
    return {
        "expected": expected_tool,
        "used": used_names,
        "completed": len(expected_ids),
    }


def _message_text(message: object) -> str:
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def _turn_evidence(
    rows: list[Mapping[str, Any]],
    *,
    expected_tool: str,
    call_id: str,
    turn_id: str,
    response: str,
) -> dict[str, Any]:
    context = json.dumps(
        {"call_id": call_id, "turn_id": turn_id},
        separators=(",", ":"),
    )
    context_marker = f"<voice-turn-context>{context}</voice-turn-context>"
    fresh_marker = f"fresh-check marker {call_id[-8:]}"
    starts = [
        index
        for index, row in enumerate(rows)
        if row.get("type") == "user"
        and context_marker in _message_text(row.get("message"))
        and fresh_marker in _message_text(row.get("message"))
    ]
    if len(starts) != 1:
        raise AcceptanceError(
            f"expected one transcript row for the exact voice turn, found {len(starts)}"
        )
    start = starts[0]
    end = len(rows)
    for index in range(start + 1, len(rows)):
        row = rows[index]
        text = _message_text(row.get("message"))
        if row.get("type") == "user" and "<voice-turn-context>" in text:
            end = index
            break
    segment = rows[start:end]
    tools = _tool_evidence(segment, expected_tool)
    assistant_texts = [
        _message_text(row.get("message")).strip()
        for row in segment
        if row.get("type") == "assistant" and _message_text(row.get("message")).strip()
    ]
    if not assistant_texts or assistant_texts[-1] != response.strip():
        raise AcceptanceError("tool transcript is not bound to the returned voice answer")
    return {
        **tools,
        "call_id_bound": True,
        "turn_id_bound": True,
        "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
    }


async def _run_brain_turn(
    *, prompt: str, call_id: str, turn_id: str, timeout: float
) -> tuple[str, dict[str, Any]]:
    started_ns = time.monotonic_ns()
    first_delta_ns: int | None = None
    response_parts: list[str] = []
    done_say = ""
    done_meta: dict[str, Any] = {}
    backends: set[str] = set()
    async for event in BrainDiscoveryClient(timeout=timeout).stream_turn(
        prompt,
        call_id=call_id,
        turn_id=turn_id,
        journal=False,
    ):
        backends.add(event.backend)
        if event.type == "delta":
            if first_delta_ns is None:
                first_delta_ns = time.monotonic_ns()
            response_parts.append(event.delta)
        elif event.type == "done":
            done_say = event.say
            done_meta = event.meta.as_dict() if event.meta is not None else {}
    response = done_say or "".join(response_parts).strip()
    if not response:
        raise AcceptanceError("resident brain returned no speakable answer")
    if backends != {"brain.sock"}:
        raise AcceptanceError(f"voice turn did not stay on brain.sock: {sorted(backends)}")
    if done_meta.get("turn_id") != turn_id:
        raise AcceptanceError("brain completion did not echo the exact voice turn id")
    if not isinstance(done_meta.get("session_turns"), int) or done_meta["session_turns"] < 1:
        raise AcceptanceError("brain completion has no positive session turn count")
    finished_ns = time.monotonic_ns()
    return response, {
        "backend": "brain.sock",
        "first_delta_ms": (
            round((first_delta_ns - started_ns) / 1_000_000, 3)
            if first_delta_ns is not None
            else None
        ),
        "done_ms": round((finished_ns - started_ns) / 1_000_000, 3),
        "response_chars": len(response),
        "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
        "session_id": done_meta.get("session_id"),
        "session_turns": done_meta.get("session_turns"),
    }


async def _synthesise_local(
    response: str,
    *,
    tts_python: Path | None = None,
) -> dict[str, Any]:
    previous_tts_python = os.environ.get("SERENA_CALL_TTS_PYTHON")
    if tts_python is not None:
        os.environ["SERENA_CALL_TTS_PYTHON"] = str(tts_python)
    try:
        backend = create_tts_backend()
    finally:
        if tts_python is not None:
            if previous_tts_python is None:
                os.environ.pop("SERENA_CALL_TTS_PYTHON", None)
            else:
                os.environ["SERENA_CALL_TTS_PYTHON"] = previous_tts_python
    if getattr(backend, "execution", None) != "local":
        raise AcceptanceError("selected TTS backend is not declared local")
    if getattr(backend, "model_source", None) not in {"local_path", "offline_cache"}:
        raise AcceptanceError("selected TTS backend has no offline model provenance")
    generation = uuid.uuid4().int & 0x7FFF_FFFF
    started_ns = time.monotonic_ns()
    first_pcm_ns: int | None = None
    chunks = 0
    samples = 0
    sample_rates: set[int] = set()
    provenance: dict[str, Any] = {}
    try:
        await backend.warm()
        raw_provenance = getattr(backend, "metadata", None)
        if not isinstance(raw_provenance, dict):
            raise AcceptanceError("local TTS returned no runtime provenance")
        provenance = dict(raw_provenance)
        required_provenance = {"execution": "local"}
        mismatches = [
            key
            for key, value in required_provenance.items()
            if provenance.get(key) != value
        ]
        if mismatches:
            raise AcceptanceError(
                "local TTS provenance mismatch: " + ", ".join(mismatches)
            )
        if provenance.get("network_isolation") in {None, "", "none"}:
            raise AcceptanceError("local TTS worker has no network isolation")
        if not isinstance(provenance.get("worker_pid"), int):
            raise AcceptanceError("local TTS worker identity was not attested")
        for key in ("python_binary_sha256",):
            value = provenance.get(key)
            if not isinstance(value, str) or len(value) != 64:
                raise AcceptanceError(f"local TTS provenance has no valid {key}")
        source = provenance.get("model_source")
        if source == "local_path":
            hash_keys = ("model_sha256", "voices_sha256")
        elif source == "offline_cache":
            hash_keys = ("asset_manifest_sha256",)
            if not isinstance(provenance.get("asset_count"), int) or int(
                provenance["asset_count"]
            ) < 1:
                raise AcceptanceError("offline TTS cache has no attested assets")
        else:
            raise AcceptanceError("local TTS returned an unknown model source")
        for key in hash_keys:
            value = provenance.get(key)
            if not isinstance(value, str) or len(value) != 64:
                raise AcceptanceError(f"local TTS provenance has no valid {key}")
        async for chunk in backend.stream(response, generation=generation):
            if not chunk.pcm or len(chunk.pcm) % 2:
                raise AcceptanceError("local TTS returned empty or partial PCM16")
            if first_pcm_ns is None:
                first_pcm_ns = time.monotonic_ns()
            chunks += 1
            samples += len(chunk.pcm) // 2
            sample_rates.add(int(chunk.sample_rate))
    finally:
        await backend.cancel(None)
    if first_pcm_ns is None or chunks < 1 or samples < 1 or len(sample_rates) != 1:
        raise AcceptanceError("local TTS produced no valid single-rate audio")
    finished_ns = time.monotonic_ns()
    return {
        "backend": getattr(backend, "name", type(backend).__name__),
        "execution": getattr(backend, "execution", None),
        "model_source": getattr(backend, "model_source", None),
        "provider": getattr(backend, "provider", None),
        "network_isolation": provenance.get("network_isolation"),
        "provenance_attested": True,
        "sample_rate": next(iter(sample_rates)),
        "chunks": chunks,
        "samples": samples,
        "first_pcm_ms": round((first_pcm_ns - started_ns) / 1_000_000, 3),
        "done_ms": round((finished_ns - started_ns) / 1_000_000, 3),
        "raw_audio_persisted": False,
    }


async def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    discovery_path = Path(args.discovery).expanduser().resolve()
    discovery = _load_discovery(discovery_path)
    session_id = str(discovery["session_id"])
    transcript = _brain_transcript(session_id)
    transcript_offset = transcript.stat().st_size
    call_id = f"v25c-{uuid.uuid4().hex}"
    turn_id = f"{call_id}:1"
    prompt = (
        f"{args.prompt} fresh-check marker {call_id[-8:]}. "
        "make a new tool call for this turn instead of relying on earlier context."
    )
    guard = _PaneGuard()
    guard.start()
    try:
        response, brain = await _run_brain_turn(
            prompt=prompt,
            call_id=call_id,
            turn_id=turn_id,
            timeout=args.timeout,
        )
        if brain.get("session_id") != session_id:
            raise AcceptanceError("voice turn completed in a different resident session")

        rows: list[dict[str, Any]] = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            rows = _read_appended_rows(transcript, transcript_offset)
            try:
                tools = _turn_evidence(
                    rows,
                    expected_tool=args.expected_tool,
                    call_id=call_id,
                    turn_id=turn_id,
                    response=response,
                )
                break
            except AcceptanceError:
                await asyncio.sleep(0.05)
        else:
            tools = _turn_evidence(
                rows,
                expected_tool=args.expected_tool,
                call_id=call_id,
                turn_id=turn_id,
                response=response,
            )

        tts = await _synthesise_local(response, tts_python=args.tts_python)
    finally:
        zero_panes = guard.stop()

    return {
        "ok": True,
        "acceptance": "v2.5c",
        "call_id": call_id,
        "turn_id": turn_id,
        "tool": tools,
        "brain": brain,
        "tts": tts,
        "zero_panes": zero_panes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--expected-tool", default=DEFAULT_EXPECTED_TOOL)
    parser.add_argument(
        "--tts-python",
        type=Path,
        help="dedicated local model interpreter, for example .venv-pocket/bin/python",
    )
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_acceptance(args))
    except Exception as exc:
        print(json.dumps({"ok": False, "acceptance": "v2.5c", "error": str(exc)}))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
