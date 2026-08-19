"""Resident owner for coding work accepted through Serena voice."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from pathlib import Path
from typing import Any

from core.billing import present_metered_auth_env, strip_metered_auth_env
from core.brain_lifetime import write_text_atomic
from core.codex_turn_capture import capture_turn, wait_for_turn
from core.coding_job_contract import (
    CLAUDE_REVIEW_EFFORT,
    CLAUDE_REVIEW_MODEL,
    CODEX_EFFORT,
    CODEX_MODEL,
    capture_git_snapshot,
    frozen_implement_effort,
    prompt_brief,
    review_required,
    scoped_git_evidence,
    validate_repository_root,
)
from core.coding_model_preferences import (
    normalise_coding_model,
    preferred_provider_for,
)
from core.coding_provider import (
    CLAUDE_IMPLEMENT_EFFORT,
    CLAUDE_IMPLEMENT_MODEL,
    choose_providers,
)
from core.voice_inbox import (
    AUTOMATIC_RECOVERY_LIMIT,
    VoiceInboxItem,
    VoiceInboxStore,
    get_default_voice_inbox,
)

HOME = Path.home()
SUPERVISOR_MARKER = HOME / ".local" / "state" / "serena" / "work-supervisor.json"
OVERLAY_EVENT_SOCKET = HOME / ".local" / "state" / "serena" / "brain-events.sock"
POLL_SECONDS = 0.25
HEARTBEAT_SECONDS = 1.0
HEARTBEAT_STALE_SECONDS = 5.0
PROGRESS_NOTIFY_SECONDS = 90.0
MAX_OUTPUT_LINE_BYTES = 1024 * 1024
MAX_CLAUDE_OUTPUT_BYTES = 4 * 1024 * 1024
PRIVATE_REVIEW_TIMEOUT_SECONDS = 30 * 60
REUSED_TURN_TIMEOUT_SECONDS = 6 * 60 * 60
# A warm session is only worth resuming while its picture of the tree is still
# roughly true. Six hours covers a working day of jobs in one repository and
# stops well short of resuming a session from last week that would describe
# files that have since moved.
WARM_SESSION_MAX_AGE_SECONDS = 6 * 60 * 60
WARM_SESSION_REUSE = os.environ.get("SERENA_WARM_SESSION_REUSE", "1") != "0"
DEFAULT_JOB_CONCURRENCY = 2
MAX_JOB_CONCURRENCY = 4

_ROUTE_RECOVERY_ERRORS = (
    "runtime has an active turn",
    "existing-chat bridge is unavailable",
    "selected chats runtime did not answer",
    "selected existing-chat codex runtime is no longer present",
    "selected existing-chat codex worker is no longer alive",
    "committed existing-chat turn can no longer make progress",
)
_EVIDENCE_RECOVERY_ERRORS = (
    "mechanical completion evidence is incomplete",
    "review correction left incomplete mechanical evidence",
)
_REVIEW_RECOVERY_ERRORS = ("review still rejects the corrected scoped diff",)
_PROVIDER_RECOVERY_ERRORS = (
    "coding review failed with status",
    "coding review timed out",
    "coding review unavailable or failed",
    "implementation pass exceeded its time budget",
    "implementation exited with status",
    "codex exited with status",
    "no coding provider has capacity",
    "rate_limit",
    "usage limit",
    "out of capacity",
)


def automatic_recovery_kind(error: object) -> str:
    """Classify only failures that another bounded attempt can safely improve."""

    message = str(error or "").strip().casefold()
    for kind, patterns in (
        ("route", _ROUTE_RECOVERY_ERRORS),
        ("evidence", _EVIDENCE_RECOVERY_ERRORS),
        ("review", _REVIEW_RECOVERY_ERRORS),
        ("provider", _PROVIDER_RECOVERY_ERRORS),
    ):
        if any(pattern in message for pattern in patterns):
            return kind
    return ""


def configured_job_concurrency(value: object | None = None) -> int:
    from_environment = value is None
    raw = os.environ.get("SERENA_CODING_JOB_CONCURRENCY", "") if from_environment else value
    if str(raw).strip() == "":
        return DEFAULT_JOB_CONCURRENCY
    try:
        concurrency = int(str(raw).strip())
    except ValueError as error:
        if not from_environment:
            raise ValueError("coding job concurrency must be an integer") from error
        print(
            "[work-supervisor] invalid SERENA_CODING_JOB_CONCURRENCY; using 2",
            flush=True,
        )
        return DEFAULT_JOB_CONCURRENCY
    if not 1 <= concurrency <= MAX_JOB_CONCURRENCY:
        if not from_environment:
            raise ValueError(
                f"coding job concurrency must be between 1 and {MAX_JOB_CONCURRENCY}"
            )
        print(
            "[work-supervisor] out-of-range SERENA_CODING_JOB_CONCURRENCY; using 2",
            flush=True,
        )
        return DEFAULT_JOB_CONCURRENCY
    return concurrency


class _ReviewControlInterrupted(RuntimeError):
    def __init__(self, control: dict[str, object]) -> None:
        self.control = dict(control)
        action = str(control.get("action") or "control")
        super().__init__(f"coding review interrupted by {action}")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def resident_worker_available(
    marker_path: Path = SUPERVISOR_MARKER,
    *,
    now: float | None = None,
) -> bool:
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        heartbeat = float(payload["heartbeat"])
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False
    age = (time.time() if now is None else now) - heartbeat
    return -1.0 <= age <= HEARTBEAT_STALE_SECONDS and _pid_alive(pid)


def _codex_binary() -> str:
    found = shutil.which("codex")
    if found:
        return found
    candidates = sorted(
        (HOME / ".nvm" / "versions" / "node").glob("*/bin/codex"),
        reverse=True,
    )
    if candidates:
        return str(candidates[0])
    raise FileNotFoundError("Codex CLI is not installed")


def _claude_binary() -> str:
    found = shutil.which("claude")
    if found:
        return found
    # A systemd --user unit can start at boot before the login session exports
    # a full PATH, so `which` misses an installed CLI and the supervisor dies
    # with "not installed" (observed 2026-07-24 09:27). Codex already resolves
    # by filesystem for the same reason; claude must too, or spoken coding is
    # dead until something restarts the unit.
    candidates = [
        HOME / ".local" / "bin" / "claude",
        HOME / ".claude" / "local" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/usr/bin/claude"),
    ]
    candidates.extend(
        sorted((HOME / ".nvm" / "versions" / "node").glob("*/bin/claude"), reverse=True)
    )
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise FileNotFoundError("Claude CLI is not installed")


def _worker_environment() -> dict[str, str]:
    environment = strip_metered_auth_env(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def verify_codex_subscription(
    *,
    codex_bin: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    environment = _worker_environment()
    present = present_metered_auth_env(os.environ)
    if present:
        raise RuntimeError("metered provider authentication is configured: " + ", ".join(present))
    result = runner(
        [codex_bin or _codex_binary(), "login", "status"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=environment,
    )
    status = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0 or "chatgpt" not in status.casefold():
        raise RuntimeError("Codex ChatGPT subscription login is not verified")


def verify_claude_subscription(
    *,
    claude_bin: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    environment = _worker_environment()
    present = present_metered_auth_env(os.environ)
    if present:
        raise RuntimeError("metered provider authentication is configured: " + ", ".join(present))
    result = runner(
        [claude_bin or _claude_binary(), "auth", "status"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=environment,
    )
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Claude subscription login returned invalid status") from exc
    if (
        result.returncode != 0
        or status.get("loggedIn") is not True
        or status.get("authMethod") != "claude.ai"
        or status.get("apiProvider") != "firstParty"
    ):
        raise RuntimeError("Claude subscription login is not verified")


def _normalise_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _session_ids() -> set[str]:
    root = HOME / ".codex" / "sessions"
    if not root.is_dir():
        return set()
    ids: set[str] = set()
    for path in root.glob("**/rollout-*.jsonl"):
        match = re.search(r"-([0-9a-f-]{36})\.jsonl$", path.name)
        if match:
            ids.add(match.group(1))
    return ids


def _new_session_id(before: set[str]) -> str:
    candidates = _session_ids() - before
    if not candidates:
        return ""
    return max(
        candidates,
        key=lambda sid: max(
            (
                path.stat().st_mtime_ns
                for path in (HOME / ".codex" / "sessions").glob(
                    f"**/rollout-*-{sid}.jsonl"
                )
            ),
            default=0,
        ),
    )


def _codex_actual_identity(session_id: str) -> tuple[str, str]:
    """Read the model and effort Codex persisted for the actual session."""

    if not session_id:
        return "", ""
    matches = list((HOME / ".codex" / "sessions").glob(f"**/rollout-*-{session_id}.jsonl"))
    model = ""
    effort = ""
    for path in sorted(matches, key=lambda value: value.stat().st_mtime_ns):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") or {}
            if event.get("type") == "turn_context" and isinstance(payload, dict):
                model = str(payload.get("model") or model)
                effort = str(payload.get("reasoning_effort") or payload.get("effort") or effort)
            settings = payload.get("thread_settings") if isinstance(payload, dict) else None
            if isinstance(settings, dict):
                model = str(settings.get("model") or model)
                effort = str(settings.get("reasoning_effort") or settings.get("effort") or effort)
    return model, effort


def _claude_actual_identity(session_id: str, cwd: Path) -> tuple[str, str]:
    """Read Claude's persisted assistant identity when stream output omits it."""

    if not session_id:
        return "", ""
    try:
        from core.claude_bridge import CLAUDE_PROJECTS_ROOT, find_claude_jsonl
        from core.config import claude_project_dir_for

        candidate = CLAUDE_PROJECTS_ROOT / claude_project_dir_for(str(cwd)) / f"{session_id}.jsonl"
        path = candidate if candidate.is_file() else find_claude_jsonl(session_id)
    except Exception:
        path = None
    if path is None:
        return "", ""
    model = ""
    effort = ""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", ""
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") or {}
        if event.get("type") == "assistant" and isinstance(message, dict):
            model = str(message.get("model") or model)
            effort = str(event.get("effort") or effort)
    return model, effort


def _codex_identity_error(
    model: str,
    effort: str,
    *,
    requested_model: str = CODEX_MODEL,
    requested_effort: str = CODEX_EFFORT,
    allow_stronger_effort: bool = False,
) -> str:
    """Prove Codex ran the identity this job froze, not merely a familiar one.

    The requested effort is a per-job tier now, so this compares against what
    the accepted brief actually asked for. It is still an exact check: a job
    that froze high and reports xhigh is as wrong as the reverse, because
    either way the record would be describing a run that did not happen.
    """

    requested = str(requested_effort or CODEX_EFFORT)
    if not model:
        return "Codex did not report the model it actually ran"
    if model.casefold() != requested_model.casefold():
        return f"Codex model mismatch: requested {requested_model}, reported {model}"
    if not effort:
        return "Codex did not report the effort it actually ran"
    accepted_efforts = {requested.casefold()}
    if allow_stronger_effort:
        accepted_efforts.update({"max", "ultra"})
    if effort.casefold() not in accepted_efforts:
        return f"Codex effort mismatch: requested {requested}, reported {effort}"
    return ""


def _claude_identity_error(
    model: str,
    effort: str,
    *,
    requested_model: str = CLAUDE_REVIEW_MODEL,
    requested_effort: str = CLAUDE_REVIEW_EFFORT,
) -> str:
    normalized = model.casefold().strip()
    if not normalized:
        return "Claude did not report the review model it actually ran"
    required_model = requested_model.casefold().strip()
    if normalized != required_model and not normalized.startswith(
        required_model + "-"
    ):
        return (
            f"review model mismatch: required {requested_model}, reported {model}; "
            "no Claude family or generation fallback is allowed"
        )
    if not effort:
        return "Claude did not report the review effort it actually ran"
    if effort.casefold() != requested_effort.casefold():
        return (
            f"review effort mismatch: required {requested_effort}, reported {effort}"
        )
    return ""


def _claude_implement_identity_error(
    model: str,
    effort: str,
    *,
    requested_model: str = CLAUDE_IMPLEMENT_MODEL,
    requested_effort: str,
) -> str:
    """Prove Claude implemented with the model and effort frozen in the brief."""

    normalized = model.casefold().strip()
    required_model = requested_model.casefold()
    if not normalized:
        return "Claude did not report the implementation model it actually ran"
    if normalized != required_model and not normalized.startswith(required_model + "-"):
        return (
            f"implementation model mismatch: required {requested_model}, "
            f"reported {model}; no Claude family or generation fallback is allowed"
        )
    if not effort:
        return "Claude did not report the implementation effort it actually ran"
    required_effort = str(requested_effort).casefold().strip()
    if effort.casefold().strip() != required_effort:
        return (
            f"implementation effort mismatch: required {requested_effort}, "
            f"reported {effort}"
        )
    return ""


def _work_title(request: str) -> str:
    current = " ".join(str(request).split()).strip(" .")
    if len(current) > 64:
        current = current[:61].rstrip() + "..."
    return "Serena: " + (current or "spoken coding work")


def _private_prompt(item: VoiceInboxItem) -> str:
    return (
        item.prompt
        + "\n\nThis is Serena's private coding session. Before acting, read "
        "/home/raghav/Documents/Projects/serena/Persona.md and Tooling.md and "
        "continue as the same Serena. The accepted brief, resolved root, frozen "
        "baseline tree, model policy, acceptance criteria, and authority boundaries "
        "are immutable. Own the work end to end. "
        "Do not tell Raghav to open another app or terminal. Inspect the live state, "
        "make only scoped changes, preserve every unrelated dirty or untracked path, "
        "run tests scoped to what you changed, and perform safe live proof when runtime "
        "behavior changed. "
        "Every test and proof must run through a command so its exit code is observable. "
        "If a live proof uses Python or another command that is not an obvious runtime "
        "tool, prefix that command with SERENA_EVIDENCE_KIND=live. Never put the marker "
        "on tests or static inspection. "
        "Scope that test command to the area you touched, its own test files or node "
        "ids, and reach for the whole suite only when the change is broad enough to "
        "earn it. One real scoped run that exits zero is the evidence; a full-suite "
        "run repeated on every pass is minutes he waits through for nothing. "
        "Do not commit unless the triggering request explicitly authorizes it. Give one concise "
        "summary, but understand that Git trees and command events decide completion. "
        "If the request requires missing authority or a risky external "
        "state change, stop at that boundary and state the exact blocker. Do not send "
        "Telegram messages yourself; the resident supervisor handles completion delivery."
    )


def _reused_prompt(item: VoiceInboxItem) -> str:
    return (
        item.prompt
        + "\n\nContinue this accepted job in this exact existing chat. Treat it as "
        "Raghav speaking directly into the conversation, but work only in the "
        "resolved project_root from the durable brief. Do not switch to the chat's "
        "startup directory when it differs. Preserve unrelated dirty work and keep "
        "all normal authority boundaries. For every command used as a test or live "
        "proof, render the complete exec result with text(JSON.stringify(result)) so "
        "the resident supervisor can record the real exit_code. Prefix a Python or other "
        "non-obvious runtime proof command with SERENA_EVIDENCE_KIND=live, and never put "
        "that marker on tests or static inspection. Run the test files or "
        "node ids covering what you changed first. Use the full suite only when the "
        "change is broad or no scoped test exists. One scoped run that exits zero "
        "satisfies the evidence gate. Do not start Fleet or "
        "spawn another coding agent. Give one concise completion summary."
    )


def _bridge_receipt_suffix(project_root: Path) -> str:
    return (
        "\n\n<serena-existing-chat-job>\n"
        f"exact_project_root: {project_root}\n"
        "stay in this exact conversation and exact repository. for every test or "
        "live proof exec call, print the complete result with "
        "text(JSON.stringify(result)) so exit_code is durable. prefix python or other "
        "non-obvious runtime proof commands with SERENA_EVIDENCE_KIND=live. do not use Fleet or "
        "delegate this job.\n"
        "</serena-existing-chat-job>"
    )


def _private_review_prompt(item: VoiceInboxItem, evidence: dict) -> str:
    return (
        "Review an accepted Serena coding job. You are strictly read-only. Judge the "
        "actual frozen-baseline-to-final diff and mechanical command evidence below, "
        "not the first worker's prose and not unrelated live worktree changes. Check "
        "correctness, regressions, scope, dirty-worktree preservation, tests, and authority. "
        "Return only JSON with keys approved (boolean), findings (array of objects with "
        "severity, file, and detail), and summary (string). Do not use markdown fences.\n\n"
        "<accepted-brief>\n"
        + json.dumps(prompt_brief(item.brief), ensure_ascii=False, sort_keys=True)
        + "\n</accepted-brief>\n\n<mechanical-evidence>\n"
        + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        + "\n</mechanical-evidence>"
    )


def _notification_summary(value: str, *, limit: int = 1_200) -> str:
    clean = " ".join(str(value).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _send_overlay_event(message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > 60_000:
        return
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(payload, str(OVERLAY_EVENT_SOCKET))
    except OSError:
        pass
    finally:
        client.close()


def _ignore_overlay_event(_message: dict) -> None:
    """Keep isolated stores and test processes off the live desktop socket."""


def _overlay_code_event(event: dict) -> dict | None:
    event_type = str(event.get("type") or "")
    if event_type == "error":
        return {
            "kind": "text",
            "summary": _notification_summary(
                str(event.get("message") or "coding error"),
                limit=2_000,
            ),
        }
    if event_type not in {"item.started", "item.completed", "item.updated"}:
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "")
    if item_type == "agent_message":
        text = str(item.get("text") or "").strip()
        return {"kind": "text", "summary": text[:8_000]} if text else None
    if item_type == "command_execution":
        command = str(item.get("command") or "").strip()
        output = str(item.get("aggregated_output") or item.get("output") or "").strip()
        return {
            "kind": "bash",
            "summary": command[:2_000] or "running command",
            "detail": output[-8_000:],
        }
    if item_type == "file_change":
        changes = item.get("changes") or []
        paths = [
            str(change.get("path") or "")
            for change in changes
            if isinstance(change, dict) and change.get("path")
        ]
        return {
            "kind": "file_edit",
            "filename": paths[0] if len(paths) == 1 else f"{len(paths)} files",
            "summary": ", ".join(paths)[:2_000] or "files changed",
        }
    if item_type == "mcp_tool_call":
        server = str(item.get("server") or "")
        tool = str(item.get("tool") or item.get("name") or "tool")
        return {
            "kind": "tool_call",
            "summary": f"{server}.{tool}".strip("."),
        }
    return None


class VoiceWorkSupervisor:
    def __init__(
        self,
        *,
        store: VoiceInboxStore | None = None,
        marker_path: Path = SUPERVISOR_MARKER,
        notifier: Callable[[str], None] | None = None,
        overlay_sender: Callable[[dict], None] | None = None,
        indexer: Callable[[], None] | None = None,
        codex_bin: str | None = None,
        claude_bin: str | None = None,
        claude_model: str | None = None,
        reused_turn_timeout: float = REUSED_TURN_TIMEOUT_SECONDS,
        provider_chooser: Callable[..., Any] | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        live_default_store = store is None
        self.store = store or get_default_voice_inbox()
        self.marker_path = Path(marker_path)
        self.notifier = notifier or self._text_raghav
        if overlay_sender is not None:
            self.overlay_sender = overlay_sender
        elif live_default_store and not os.environ.get("PYTEST_CURRENT_TEST"):
            self.overlay_sender = _send_overlay_event
        else:
            self.overlay_sender = _ignore_overlay_event
        self.indexer = indexer or self._refresh_index
        self.codex_bin = codex_bin or _codex_binary()
        self.claude_bin = claude_bin
        self.claude_model = claude_model or CLAUDE_REVIEW_MODEL
        # Injectable so a test can state the capacity it is testing instead of
        # inheriting whatever this laptop's accounts happen to be doing today.
        self.provider_chooser = provider_chooser or choose_providers
        self.reused_turn_timeout = max(1.0, float(reused_turn_timeout))
        self.max_concurrency = configured_job_concurrency(max_concurrency)
        self.stop_event = threading.Event()
        self._active_processes: dict[str, subprocess.Popen[str]] = {}
        self._active_processes_lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._last_heartbeat = 0.0
        self._heartbeat_lock = threading.Lock()
        self._owner_id = f"{os.getpid()}-{uuid.uuid4().hex}"

    @staticmethod
    def _refresh_index() -> None:
        from core.indexer import update_index

        update_index()

    def _overlay(self, message: dict) -> None:
        with suppress(Exception):
            self.overlay_sender(message)

    @staticmethod
    def _voice_journal():
        """The shared control plane's view of this surface, or None.

        The voice inbox stays the authoritative journal for what a job is
        doing. This is the obligation ledger alongside it, so a job whose
        outcome never reached Raghav is still visible after a restart. It is
        deliberately best-effort: a broken control plane must never take down
        a real coding job.
        """

        try:
            from core.surface_journal import journal

            return journal("voice")
        except Exception:
            return None

    def _owe_outcome(self, item_id: str, request: str) -> None:
        """Serena took the job, so she now owes him how it ended."""

        entry = self._voice_journal()
        if entry is None:
            return
        with suppress(Exception):
            entry.opened(
                item_id,
                summary=_notification_summary(request or "a spoken coding job", limit=300),
            )

    def _deliver_outcome(self, item_id: str, message: str) -> None:
        """Tell him how the job ended, and record whether that landed.

        This is the only place the obligation is discharged. A job that failed
        still discharges it: the promise was to tell him how it ended, not that
        it ended well. What leaves it open is him never being told.
        """

        entry = self._voice_journal()
        try:
            delivered = self.notifier(message)
        except Exception as error:
            if entry is not None:
                with suppress(Exception):
                    entry.failed(item_id, error=f"{type(error).__name__}: {error}")
            raise
        if delivered is False:
            # The transport ran and told us it did not land. The job itself is
            # finished, so this does not raise; it stays on the books as still
            # owed, and the recurring sweep is what tries again.
            if entry is not None:
                with suppress(Exception):
                    entry.failed(item_id, error="the notifier reported the message did not send")
            return
        if entry is not None:
            with suppress(Exception):
                entry.fulfilled(item_id)

    def _heartbeat(self, *, force: bool = False) -> None:
        with self._heartbeat_lock:
            now = time.time()
            if not force and now - self._last_heartbeat < HEARTBEAT_SECONDS:
                return
            self.marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_text_atomic(
                self.marker_path,
                json.dumps({"pid": os.getpid(), "heartbeat": now}) + "\n",
            )
            self.store.renew_resident_lease(
                self._owner_id,
                pid=os.getpid(),
                heartbeat=now,
            )
            self._last_heartbeat = now

    @staticmethod
    def _text_raghav(message: str) -> bool:
        """Text him, and say honestly whether it actually went out.

        This returns a real answer because the obligation ledger believes it.
        Swallowing a missing binary or a nonzero exit here would let Serena
        record that she told him something she never told him.
        """

        chats = shutil.which("chats") or str(HOME / ".local" / "bin" / "chats")
        try:
            result = subprocess.run(
                [chats, "text", message],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def request_stop(self) -> None:
        self.stop_event.set()
        with self._active_processes_lock:
            processes = list(self._active_processes.values())
        for process in processes:
            if process.poll() is not None:
                continue
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)

    def _set_active_process(
        self,
        item_id: str,
        process: subprocess.Popen[str] | None,
    ) -> None:
        with self._active_processes_lock:
            if process is None:
                self._active_processes.pop(str(item_id), None)
            else:
                self._active_processes[str(item_id)] = process

    @staticmethod
    def _resource_key(item: VoiceInboxItem) -> str:
        brief = item.brief or {}
        root = str(brief.get("project_root") or "").strip()
        if not root:
            return f"unresolved:{item.item_id}"
        return str(Path(root).expanduser().resolve())

    def run(self) -> None:
        verify_codex_subscription(codex_bin=self.codex_bin)
        recovered = self.store.recover_headless_work()
        if recovered:
            print(f"[work-supervisor] requeued {recovered} interrupted job(s)", flush=True)
        heartbeat_stop = threading.Event()

        def keep_heartbeat() -> None:
            while not heartbeat_stop.is_set():
                try:
                    self._heartbeat(force=True)
                except Exception as error:
                    print(f"[work-supervisor] heartbeat failed: {error}", flush=True)
                heartbeat_stop.wait(HEARTBEAT_SECONDS)

        heartbeat_thread = threading.Thread(
            target=keep_heartbeat,
            name="serena-work-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        executor = ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix="serena-coding-job",
        )
        active: dict[Future[None], str] = {}

        def reap(futures: set[Future[None]]) -> None:
            for future in futures:
                active.pop(future, None)
                try:
                    future.result()
                except BaseException as error:
                    print(f"[work-supervisor] worker thread failed: {error}", flush=True)

        try:
            while not self.stop_event.is_set():
                reap({future for future in active if future.done()})
                launched = False
                while len(active) < self.max_concurrency and not self.stop_event.is_set():
                    target_sid = f"headless-voice-{uuid.uuid4().hex}"
                    item = self.store.claim_next(
                        target_sid,
                        excluded_project_roots=set(active.values()),
                    )
                    if item is None:
                        break
                    resource = self._resource_key(item)
                    future = executor.submit(self._run_item, item, target_sid)
                    active[future] = resource
                    launched = True
                if active:
                    done, _pending = wait(
                        tuple(active),
                        timeout=0 if launched else POLL_SECONDS,
                        return_when=FIRST_COMPLETED,
                    )
                    reap(done)
                elif not launched:
                    self.stop_event.wait(POLL_SECONDS)
        finally:
            self.request_stop()
            executor.shutdown(wait=True, cancel_futures=False)
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            self.store.clear_resident_lease(self._owner_id)
            with suppress(FileNotFoundError):
                self.marker_path.unlink()

    @staticmethod
    def _claude_review_command(
        claude_bin: str,
        _review_session_id: str,
        *,
        model: str = CLAUDE_REVIEW_MODEL,
        effort: str = CLAUDE_REVIEW_EFFORT,
    ) -> list[str]:
        return [
            claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--effort",
            effort,
            "--safe-mode",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--disallowedTools",
            "Agent,Task,TaskCreate,TaskGet,TaskList,TaskOutput,TaskStop,TaskUpdate,"
            "TeamCreate,TeamDelete,SendMessage,Skill,Workflow,Edit,Write,NotebookEdit",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "Read,Glob,Grep,Bash",
        ]

    def _run_private_review(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        evidence: dict,
        *,
        model: str = CLAUDE_REVIEW_MODEL,
        effort: str = CLAUDE_REVIEW_EFFORT,
    ) -> dict:
        claude_bin = self.claude_bin or _claude_binary()
        verify_claude_subscription(claude_bin=claude_bin)
        review_session_id = str(uuid.uuid4())
        command = self._claude_review_command(
            claude_bin,
            review_session_id,
            model=model,
            effort=effort,
        )
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_worker_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self._set_active_process(item.item_id, process)
        control: dict[str, object] = {}
        control_stop = threading.Event()
        control_lock = threading.Lock()

        def watch_controls() -> None:
            while not control_stop.wait(0.2):
                if process.poll() is not None:
                    return
                pending = self.store.pending_controls(item.item_id)
                if not pending:
                    continue
                with control_lock:
                    if control:
                        return
                    control.update(pending[0])
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                return

        watcher = threading.Thread(
            target=watch_controls,
            name=f"serena-review-control-{item.item_id[:8]}",
            daemon=True,
        )
        watcher.start()
        try:
            stdout, stderr = process.communicate(
                _private_review_prompt(item, evidence),
                timeout=PRIVATE_REVIEW_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            raise RuntimeError(f"{model} coding review timed out") from error
        finally:
            control_stop.set()
            watcher.join(timeout=1)
        with control_lock:
            selected_control = dict(control)
        if selected_control:
            raise _ReviewControlInterrupted(selected_control)
        if len(stdout.encode("utf-8", errors="replace")) > MAX_CLAUDE_OUTPUT_BYTES:
            raise RuntimeError(f"{model} coding review returned oversized output")
        if process.returncode != 0:
            detail = _notification_summary(stderr or stdout, limit=800)
            raise RuntimeError(
                f"{model} coding review unavailable or failed with status "
                f"{process.returncode}: {detail}"
            )
        result = ""
        actual_model = ""
        actual_effort = ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            review_session_id = str(event.get("session_id") or review_session_id)
            if event.get("type") == "assistant":
                message = event.get("message") or {}
                if isinstance(message, dict):
                    actual_model = str(message.get("model") or actual_model)
                actual_effort = str(event.get("effort") or actual_effort)
            if event.get("type") == "result":
                result = str(event.get("result") or result).strip()
                actual_model = str(event.get("model") or actual_model)
                actual_effort = str(event.get("effort") or actual_effort)
        persisted_model, persisted_effort = _claude_actual_identity(review_session_id, cwd)
        actual_model = actual_model or persisted_model
        actual_effort = actual_effort or persisted_effort or effort
        identity_error = _claude_identity_error(
            actual_model,
            actual_effort,
            requested_model=model,
            requested_effort=effort,
        )
        if identity_error:
            raise RuntimeError(identity_error)
        if not result:
            raise RuntimeError(f"{model} coding review completed without a report")
        try:
            payload = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{model} coding review returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("approved"), bool):
            raise RuntimeError(f"{model} coding review returned an invalid decision")
        findings = payload.get("findings")
        if not isinstance(findings, list):
            raise RuntimeError(f"{model} coding review returned invalid findings")
        return {
            "approved": bool(payload["approved"]),
            "findings": findings,
            "summary": str(payload.get("summary") or ""),
            "session_id": review_session_id,
            "reported_model": actual_model,
            "reported_effort": actual_effort,
        }

    def _run_codex_review(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        evidence: dict,
        *,
        model: str,
        effort: str,
    ) -> dict:
        """Run the frozen Codex reviewer with a read-only sandbox."""

        command = [
            self.codex_bin,
            "exec",
            "--json",
            "--ignore-user-config",
            "--disable",
            "multi_agent",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-c",
            'approval_policy="never"',
            "--sandbox",
            "read-only",
            "-C",
            str(cwd),
            "-",
        ]
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_worker_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._set_active_process(item.item_id, process)
        control: dict[str, object] = {}
        control_stop = threading.Event()

        def watch_controls() -> None:
            while not control_stop.wait(0.2):
                if process.poll() is not None:
                    return
                pending = self.store.pending_controls(item.item_id)
                if not pending:
                    continue
                control.update(pending[0])
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                return

        watcher = threading.Thread(
            target=watch_controls,
            name=f"serena-codex-review-{item.item_id[:8]}",
            daemon=True,
        )
        watcher.start()
        review_session_id = ""
        actual_model = ""
        actual_effort = ""
        result = ""
        output_bytes = 0
        try:
            assert process.stdin is not None
            process.stdin.write(_private_review_prompt(item, evidence))
            process.stdin.close()
            assert process.stdout is not None
            for line in process.stdout:
                output_bytes += len(line.encode("utf-8", errors="replace"))
                if output_bytes > MAX_CLAUDE_OUTPUT_BYTES:
                    raise RuntimeError(f"{model} coding review returned oversized output")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type") or "")
                if event_type == "thread.started":
                    review_session_id = str(event.get("thread_id") or review_session_id)
                elif event_type in {"thread.settings", "thread.settings_applied"}:
                    settings = event.get("thread_settings") or event.get("settings") or event
                    if isinstance(settings, dict):
                        actual_model = str(settings.get("model") or actual_model)
                        actual_effort = str(
                            settings.get("reasoning_effort")
                            or settings.get("effort")
                            or actual_effort
                        )
                elif event_type == "item.completed":
                    completed = event.get("item") or {}
                    if isinstance(completed, dict) and completed.get("type") == "agent_message":
                        result = str(completed.get("text") or result).strip()
            return_code = process.wait(timeout=10)
        except BaseException:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            raise
        finally:
            control_stop.set()
            watcher.join(timeout=1)
            self._set_active_process(item.item_id, None)
        if control:
            raise _ReviewControlInterrupted(dict(control))
        if return_code != 0:
            raise RuntimeError(f"{model} coding review failed with status {return_code}")
        identity_error = _codex_identity_error(
            actual_model,
            actual_effort,
            requested_model=model,
            requested_effort=effort,
        )
        if identity_error:
            raise RuntimeError(identity_error)
        if not result:
            raise RuntimeError(f"{model} coding review completed without a report")
        try:
            payload = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{model} coding review returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("approved"), bool):
            raise RuntimeError(f"{model} coding review returned an invalid decision")
        findings = payload.get("findings")
        if not isinstance(findings, list):
            raise RuntimeError(f"{model} coding review returned invalid findings")
        return {
            "approved": bool(payload["approved"]),
            "findings": findings,
            "summary": str(payload.get("summary") or ""),
            "session_id": review_session_id,
            "reported_model": actual_model,
            "reported_effort": actual_effort,
        }

    def _run_policy_review(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        evidence: dict,
        assignment,
    ) -> dict:
        if assignment.review_provider == "codex":
            return self._run_codex_review(
                item,
                cwd,
                evidence,
                model=assignment.review_model,
                effort=assignment.review_effort,
            )
        return self._run_private_review(
            item,
            cwd,
            evidence,
            model=assignment.review_model,
            effort=assignment.review_effort,
        )

    def _finish_or_requeue_review_control(
        self,
        item: VoiceInboxItem,
        control: dict[str, object],
    ) -> None:
        action = str(control.get("action") or "")
        control_id = str(control.get("control_id") or "")
        if action == "cancel":
            self.store.finish_control(control_id)
            self.store.finish_work_item(
                item.item_id,
                state="cancelled",
                summary="cancelled at Raghav's request",
            )
            self._emit_job_snapshot(item.item_id, event_type="code_done")
            self.notifier("stopped. the coding job was cancelled.")
            return
        if action == "steer":
            if not self.store.requeue_work_item(
                item.item_id,
                error="steering interrupted conditional review",
            ):
                raise RuntimeError("could not durably requeue review steering")
            self._emit_job_snapshot(item.item_id)
            return
        self.store.finish_control(control_id, error="unsupported review control")
        raise RuntimeError("unsupported coding job control during review")

    @staticmethod
    def _codex_command(
        codex_bin: str,
        cwd: Path,
        *,
        model: str = CODEX_MODEL,
        effort: str = CODEX_EFFORT,
        resume_session_id: str = "",
    ) -> list[str]:
        command = [
            codex_bin,
            "exec",
            "--json",
            "--ignore-user-config",
            "--disable",
            "multi_agent",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort or CODEX_EFFORT}"',
            "-c",
            'approval_policy="never"',
            "--sandbox",
            "workspace-write",
        ]
        if resume_session_id:
            command.extend(["resume", resume_session_id, "-"])
        else:
            command.extend(["-C", str(cwd), "-"])
        return command

    def _emit_job_snapshot(self, item_id: str, *, event_type: str = "code_snapshot") -> None:
        snapshot = self.store.overlay_snapshot(item_id)
        if snapshot is None:
            return
        message: dict[str, object] = {"type": event_type, "snapshot": snapshot}
        if event_type == "code_start":
            message.update(
                {
                    "item_id": item_id,
                    "project": snapshot.get("project") or "project",
                    "status": snapshot.get("state") or "working",
                }
            )
        elif event_type == "code_done":
            message["summary"] = str(snapshot.get("summary") or "")[:2_000]
        self._overlay(message)

    @staticmethod
    def _event_record(event: dict) -> dict:
        record: dict[str, object] = {"type": str(event.get("type") or "event")}
        settings = event.get("thread_settings") or event.get("settings") or {}
        if isinstance(settings, dict):
            record["model"] = str(settings.get("model") or "")
            record["effort"] = str(
                settings.get("reasoning_effort") or settings.get("effort") or ""
            )
        item = event.get("item")
        if isinstance(item, dict):
            record["item_type"] = str(item.get("type") or "")
            if item.get("type") == "command_execution":
                record.update(
                    {
                        "command": str(item.get("command") or "")[:2_000],
                        "exit_code": item.get("exit_code"),
                        "status": str(item.get("status") or ""),
                        "output": str(
                            item.get("aggregated_output") or item.get("output") or ""
                        )[-8_000:],
                    }
                )
            elif item.get("type") == "file_change":
                changes = item.get("changes") or []
                record["paths"] = [
                    str(change.get("path") or "")[:2_000]
                    for change in changes
                    if isinstance(change, dict) and change.get("path")
                ]
            elif item.get("type") == "agent_message":
                record["text"] = str(item.get("text") or "")[:8_000]
        if event.get("message"):
            record["message"] = str(event.get("message"))[:2_000]
        return record

    @staticmethod
    def _post_local_json(
        port: int,
        path: str,
        payload: dict,
        *,
        timeout: float,
    ) -> dict:
        if not 1 <= int(port) <= 65_535:
            raise RuntimeError("the selected Chats bridge port is invalid")
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(1.0, timeout)) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"the selected Chats runtime did not answer: {error}") from error
        if not isinstance(decoded, dict):
            raise RuntimeError("the selected Chats runtime returned an invalid response")
        return decoded

    @staticmethod
    def _get_local_json(port: int, path: str, *, timeout: float) -> dict:
        """Read one bounded same-host runtime snapshot without changing it."""

        if not 1 <= int(port) <= 65_535:
            raise RuntimeError("the selected Chats bridge port is invalid")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{int(port)}{path}",
                timeout=max(0.1, float(timeout)),
            ) as response:
                decoded = json.loads(response.read(256_001).decode("utf-8"))
        except (
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise RuntimeError(f"the selected Chats runtime did not answer: {error}") from error
        if not isinstance(decoded, dict) or decoded.get("ok") is False:
            raise RuntimeError("the selected Chats runtime returned an invalid response")
        return decoded

    def _reused_runtime_error(self, port: int, session_id: str) -> str:
        """Explain why an exact reused chat cannot append to its rollout."""

        try:
            context = self._get_local_json(port, "/api/runtime-context", timeout=1)
        except Exception as error:
            return f"the existing-chat bridge is unavailable: {error}"
        runtimes = context.get("runtimes") or context.get("sessions") or []
        if not isinstance(runtimes, list):
            return "the existing-chat bridge returned no usable runtime list"
        for runtime in runtimes:
            if not isinstance(runtime, dict) or str(runtime.get("sid") or "") != session_id:
                continue
            if str(runtime.get("agent") or "").casefold() != "codex":
                return "the selected existing-chat runtime is not a Codex worker"
            if runtime.get("alive") is not True:
                return "the selected existing-chat Codex worker is no longer alive"
            return ""
        return "the selected existing-chat Codex runtime is no longer present"

    def _interrupt_reused_turn(self, route: dict, item_id: str) -> None:
        port = int(route.get("bridge_port") or 0)
        if not port:
            return
        with suppress(Exception):
            self._post_local_json(
                port,
                "/api/codex-work-interrupt",
                {
                    "target_sid": str(route.get("session_id") or ""),
                    "item_id": str(item_id),
                },
                timeout=5,
            )

    def _run_reused_codex_attempt(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        prompt: str,
        *,
        route: dict,
        commands: list[dict],
        reconcile_existing: bool = False,
    ) -> dict:
        """Run one turn through the terminal process that already owns a chat."""

        session_id = str(route.get("session_id") or "").strip()
        port = int(route.get("bridge_port") or 0)
        if not session_id or not port:
            raise RuntimeError("the frozen existing-chat route is incomplete")
        effort = self._implement_effort(item)
        attempt_id, attempt_no = self.store.start_attempt(
            item.item_id,
            provider="codex",
            model=CODEX_MODEL,
            effort=effort,
            resume_session_id=session_id,
        )
        try:
            self.store.set_attempt_session(attempt_id, session_id)
            self.store.set_work_session(item.item_id, session_id)
            self._emit_job_snapshot(item.item_id)
            return self._run_started_reused_codex_attempt(
                item,
                cwd,
                prompt,
                route=route,
                commands=commands,
                reconcile_existing=reconcile_existing,
                attempt_id=attempt_id,
                attempt_no=attempt_no,
                session_id=session_id,
                port=port,
                effort=effort,
            )
        except BaseException as error:
            self.store.finish_attempt(
                attempt_id,
                state="failed",
                exit_code=None,
                error=str(error),
            )
            raise

    def _run_started_reused_codex_attempt(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        prompt: str,
        *,
        route: dict,
        commands: list[dict],
        reconcile_existing: bool,
        attempt_id: str,
        attempt_no: int,
        session_id: str,
        port: int,
        effort: str,
    ) -> dict:
        """Finish an already-recorded reused-chat attempt."""

        full_prompt = prompt + _bridge_receipt_suffix(cwd)
        prompt_sha256 = hashlib.sha256(full_prompt.encode("utf-8")).hexdigest()
        route_state = self.store.route_record(item.item_id) or route
        state = str(route_state.get("state") or "selected")
        stored_digest = str(route_state.get("prompt_sha256") or "")
        reconciling = reconcile_existing and state in {
            "committed",
            "uncertain",
            "completed",
        }
        if reconciling:
            if not stored_digest:
                raise RuntimeError("the committed existing-chat turn has no prompt digest")
            prompt_sha256 = stored_digest
        elif state in {"committed", "uncertain"} and stored_digest not in {
            "",
            prompt_sha256,
        }:
            raise RuntimeError(
                "a different existing-chat prompt is still awaiting reconciliation"
            )
        if not reconciling and (
            not stored_digest
            or state == "completed"
            or (stored_digest != prompt_sha256 and state == "selected")
        ):
            if not self.store.prepare_route_dispatch(item.item_id, prompt_sha256):
                raise RuntimeError("could not durably prepare the existing-chat turn")
            route_state = self.store.route_record(item.item_id) or route_state
            state = str(route_state.get("state") or "selected")
        selected_control: dict[str, object] = {}
        bridge_result: dict = {}

        if state == "selected":
            finished = threading.Event()
            outcome: dict[str, object] = {}

            def dispatch() -> None:
                try:
                    outcome["result"] = self._post_local_json(
                        port,
                        "/api/codex-work-bridge",
                        {
                            "target_sid": session_id,
                            "prompt": full_prompt,
                            "item_id": item.item_id,
                            "timeout": self.reused_turn_timeout,
                        },
                        timeout=self.reused_turn_timeout + 30,
                    )
                except Exception as error:
                    outcome["error"] = error
                finally:
                    finished.set()

            bridge_thread = threading.Thread(
                target=dispatch,
                name=f"serena-existing-chat-{item.item_id[:8]}",
                daemon=True,
            )
            bridge_thread.start()
            interrupted_at = 0.0
            while not finished.wait(0.2):
                self._heartbeat()
                pending = self.store.pending_controls(item.item_id)
                if pending and not selected_control:
                    selected_control.update(pending[0])
                    self._interrupt_reused_turn(route, item.item_id)
                    interrupted_at = time.monotonic()
                if self.stop_event.is_set() and not interrupted_at:
                    self._interrupt_reused_turn(route, item.item_id)
                    interrupted_at = time.monotonic()
                if interrupted_at and time.monotonic() - interrupted_at > 30:
                    break
            if finished.is_set():
                error = outcome.get("error")
                if isinstance(error, Exception):
                    bridge_error = error
                else:
                    raw = outcome.get("result")
                    bridge_result = dict(raw) if isinstance(raw, dict) else {}
                    bridge_error = None
            else:
                bridge_error = RuntimeError(
                    "the existing chat did not acknowledge the requested interrupt"
                )

            route_state = self.store.route_record(item.item_id) or route_state
            state = str(route_state.get("state") or state)
            if bridge_result.get("committed") and state == "selected":
                raw_committed_offset = bridge_result.get("start_offset")
                if raw_committed_offset is None:
                    raise RuntimeError(
                        "the existing chat committed without a durable start offset"
                    )
                self.store.mark_route_dispatch(
                    item.item_id,
                    "committed",
                    start_offset=int(raw_committed_offset),
                    prompt_sha256=prompt_sha256,
                )
                route_state = self.store.route_record(item.item_id) or route_state
                state = str(route_state.get("state") or state)
            if bridge_error is not None and state == "selected":
                self.store.finish_attempt(
                    attempt_id,
                    state="failed",
                    exit_code=None,
                    error=str(bridge_error),
                )
                raise bridge_error
            if bridge_result and not bridge_result.get("ok") and state == "selected":
                message = str(bridge_result.get("message") or "existing chat refused the job")
                self.store.finish_attempt(
                    attempt_id,
                    state="failed",
                    exit_code=None,
                    error=message,
                )
                raise RuntimeError(message)

        route_state = self.store.route_record(item.item_id) or route_state
        raw_start_offset = bridge_result.get("start_offset")
        if raw_start_offset is None:
            raw_start_offset = route_state.get("start_offset")
        start_offset = int(raw_start_offset or 0)
        stored_hash = str(route_state.get("prompt_sha256") or prompt_sha256)
        if raw_start_offset is None and str(route_state.get("state") or "") != "selected":
            raise RuntimeError("the committed existing-chat turn has no durable start offset")

        captured = capture_turn(
            session_id,
            start_offset=start_offset,
            end_offset=(
                int(route_state["end_offset"])
                if route_state.get("end_offset") is not None
                else None
            ),
            prompt_sha256=stored_hash,
        )
        # A bridge timeout can freeze an end offset before Codex appends its
        # terminal event. Reconciliation must inspect the full committed slice
        # before deciding that a dead owner made progress impossible.
        if not captured.completed and route_state.get("end_offset") is not None:
            captured = capture_turn(
                session_id,
                start_offset=start_offset,
                prompt_sha256=stored_hash,
            )
        if not captured.source_available:
            error = captured.source_error or "the committed Codex transcript is unavailable"
            self.store.mark_route_dispatch(
                item.item_id,
                "uncertain",
                start_offset=start_offset,
                prompt_sha256=stored_hash,
            )
            raise RuntimeError(error)
        if not captured.completed and not selected_control and not self.stop_event.is_set():
            runtime_error = self._reused_runtime_error(port, session_id)
            if runtime_error:
                self.store.mark_route_dispatch(
                    item.item_id,
                    "uncertain",
                    start_offset=start_offset,
                    end_offset=captured.end_offset,
                    prompt_sha256=stored_hash,
                )
                raise RuntimeError(runtime_error)
            captured = wait_for_turn(
                session_id,
                start_offset=start_offset,
                prompt_sha256=stored_hash,
                timeout=self.reused_turn_timeout,
                progress_probe=lambda: self._reused_runtime_error(port, session_id),
            )
        if captured.progress_error or not captured.source_available:
            error = (
                captured.progress_error
                or captured.source_error
                or "the committed existing-chat turn can no longer make progress"
            )
            self.store.mark_route_dispatch(
                item.item_id,
                "uncertain",
                start_offset=start_offset,
                end_offset=captured.end_offset,
                prompt_sha256=stored_hash,
            )
            raise RuntimeError(error)
        if captured.completed:
            self.store.mark_route_dispatch(
                item.item_id,
                "completed",
                start_offset=start_offset,
                end_offset=captured.end_offset,
                prompt_sha256=stored_hash,
            )

        for record in captured.events:
            self.store.record_job_event(
                item.item_id,
                "codex.event",
                record,
                attempt_id=attempt_id,
            )
            if record.get("type") == "command_execution":
                self._overlay(
                    {
                        "type": "code_event",
                        "item_id": item.item_id,
                        "event": {
                            "kind": "bash",
                            "summary": str(record.get("command") or "")[:2_000],
                            "detail": str(record.get("output") or "")[-8_000:],
                        },
                    }
                )
        commands.extend(captured.commands)

        persisted_model, persisted_effort = _codex_actual_identity(session_id)
        actual_model = captured.model or persisted_model
        actual_effort = captured.effort or persisted_effort
        identity_error = _codex_identity_error(
            actual_model,
            actual_effort,
            requested_effort=effort,
        )
        if identity_error:
            self.store.finish_attempt(
                attempt_id,
                state="failed",
                exit_code=None,
                reported_model=actual_model,
                reported_effort=actual_effort,
                error=identity_error,
            )
            raise RuntimeError(identity_error)
        if selected_control:
            action = str(selected_control.get("action") or "")
            attempt_state = "cancelled" if action == "cancel" else "steered"
            self.store.finish_attempt(
                attempt_id,
                state=attempt_state,
                exit_code=None,
                reported_model=actual_model,
                reported_effort=actual_effort,
            )
            self.store.finish_control(str(selected_control.get("control_id") or ""))
            return {
                "attempt_no": attempt_no,
                "session_id": session_id,
                "message": captured.message,
                "control": selected_control,
            }
        if not captured.saw_prompt or not captured.completed:
            error = "the committed existing-chat turn did not reach a durable completion"
            self.store.mark_route_dispatch(
                item.item_id,
                "uncertain",
                start_offset=start_offset,
                prompt_sha256=stored_hash,
            )
            self.store.finish_attempt(
                attempt_id,
                state="failed",
                exit_code=None,
                reported_model=actual_model,
                reported_effort=actual_effort,
                error=error,
            )
            raise RuntimeError(error)
        self.store.finish_attempt(
            attempt_id,
            state="completed",
            exit_code=0,
            reported_model=actual_model,
            reported_effort=actual_effort,
        )
        return {
            "attempt_no": attempt_no,
            "session_id": session_id,
            "message": captured.message or str(bridge_result.get("response") or ""),
            "control": {},
        }

    def _implement_effort(self, item: VoiceInboxItem) -> str:
        """The effort this accepted job froze, from its own complexity tier.

        Read from the durable brief rather than passed down every call site, so
        a resumed or repaired attempt cannot quietly run at a different depth
        than the one the job was accepted at.
        """

        brief = item.brief or self.store.accepted_brief(item.item_id) or {}
        return frozen_implement_effort(brief)

    def _provider_assignment(self, brief: dict[str, Any]) -> Any:
        """Apply the model frozen at intake without rereading live preference."""

        model_policy = brief.get("model_policy")
        if isinstance(model_policy, dict) and model_policy:
            implement = model_policy.get("implement") or {}
            try:
                return self.provider_chooser(
                    preferred_model=str(implement.get("model") or "auto"),
                    complexity=str(
                        model_policy.get("lane") or brief.get("complexity") or "ordinary"
                    ),
                    risk=str(model_policy.get("risk") or brief.get("risk") or "normal"),
                    request=str(
                        brief.get("exact_request") or brief.get("triggering_request") or ""
                    ),
                )
            except TypeError as error:
                if "unexpected keyword argument" not in str(error):
                    raise
                # Test and legacy injectors supplied a zero-argument capacity
                # chooser before the shared policy existed. They already
                # return a complete frozen assignment, so keep accepting them.
                return self.provider_chooser()
        selected = normalise_coding_model(brief.get("coding_model"), strict=True)
        preferred = preferred_provider_for(selected)
        if not preferred:
            return self.provider_chooser()
        return self.provider_chooser(preferred_implementer=preferred)

    def _warm_claude_session(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        *,
        preferred_session_id: str = "",
    ) -> str:
        """Resume a Claude session that already knows this repository, or don't.

        Every private job used to boot a Claude that had never seen the tree,
        so the same orientation was bought again on every request. A session
        that finished a job in this exact repository recently has already read
        the layout, and resuming it is the difference between starting at the
        problem and starting at the file listing.

        Safety is the whole design here, and a refusal is always free because
        the caller just starts cold. The repository root is re-validated from
        Git at the moment of reuse rather than trusted from the row that stored
        it. The session must be bound to that same canonical root, through the
        same immutable binding a reused Codex chat goes through, so a session
        already owned by another project raises instead of being retargeted.
        The transcript must still exist on disk, because --resume against a
        deleted session id fails the whole attempt. Any doubt returns "".
        """

        if not WARM_SESSION_REUSE:
            return ""
        try:
            root = validate_repository_root(cwd)
        except Exception:
            return ""
        candidate = str(preferred_session_id or "").strip()
        reason = "resuming the session this job already had"
        if not candidate:
            warm = self.store.warm_session_for_project(
                str(root),
                provider="claude",
                max_age_seconds=WARM_SESSION_MAX_AGE_SECONDS,
                exclude_item_id=item.item_id,
            )
            if not warm:
                return ""
            candidate = str(warm.get("session_id") or "").strip()
            reason = "reusing a recent session already oriented in this repository"
        if not candidate:
            return ""
        try:
            from core.claude_bridge import find_claude_jsonl

            if find_claude_jsonl(candidate) is None:
                return ""
        except Exception:
            return ""
        try:
            from core.metadata import set_work_project_root

            set_work_project_root(candidate, root)
        except Exception as error:
            print(
                f"[work-supervisor] not reusing claude session {candidate[:8]}: "
                f"{_notification_summary(str(error), limit=200)}",
                flush=True,
            )
            return ""
        self.store.record_job_event(
            item.item_id,
            "warm_session_reuse",
            {
                "provider": "claude",
                "session_id": candidate,
                "project_root": str(root),
                "reason": reason,
            },
        )
        print(
            f"[work-supervisor] warm claude session {candidate[:8]} for "
            f"item={item.item_id[:8]} root={root}",
            flush=True,
        )
        return candidate

    def _run_codex_attempt(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        prompt: str,
        *,
        resume_session_id: str,
        commands: list[dict],
        model: str = CODEX_MODEL,
        effort: str = "",
    ) -> dict:
        if resume_session_id:
            from core.metadata import set_work_project_root

            # Re-check the immutable project binding before a historical
            # transcript gets a new owner. This closes the gap between route
            # acceptance and process spawn without relying on its old cwd.
            set_work_project_root(resume_session_id, cwd)
        effort = effort or self._implement_effort(item)
        attempt_id, attempt_no = self.store.start_attempt(
            item.item_id,
            provider="codex",
            model=model,
            effort=effort,
            resume_session_id=resume_session_id,
        )
        try:
            return self._run_started_codex_attempt(
                item,
                cwd,
                prompt,
                resume_session_id=resume_session_id,
                commands=commands,
                attempt_id=attempt_id,
                attempt_no=attempt_no,
                model=model,
                effort=effort,
            )
        except BaseException as error:
            self.store.finish_attempt(
                attempt_id,
                state="failed",
                exit_code=None,
                error=str(error),
            )
            raise

    @staticmethod
    def _claude_implement_command(
        claude_bin: str,
        *,
        model: str = CLAUDE_IMPLEMENT_MODEL,
        effort: str = CLAUDE_IMPLEMENT_EFFORT,
        resume_session_id: str = "",
    ) -> list[str]:
        """Claude, allowed to actually change files.

        The review command next to this one bans Edit and Write on purpose,
        because Claude was only ever the reviewer here. That made the whole
        coding path die whenever Codex ran out of quota: on 2026-08-04 Codex
        reported 100% used and Raghav could not start any job at all. Writing
        is the entire difference between the two commands.
        """
        return [
            claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--effort",
            effort or CLAUDE_IMPLEMENT_EFFORT,
            # An implementer has to be able to run the repository's tests,
            # and neither gentler mode allows that headlessly: acceptEdits
            # prompts for Bash and a -p run cannot answer a prompt, while
            # dontAsk denies it outright with "Permission to use Bash has been
            # denied". Observed live across two runs, the worker wrote both
            # files correctly and then failed nine straight pytest calls.
            # This is the same authority the Codex worker beside it already
            # has, which runs approval_policy="never" in a workspace-write
            # sandbox, and it is bounded the same way: one validated Git root,
            # a frozen brief, and evidence captured on the way out.
            "--permission-mode",
            "bypassPermissions",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--disallowedTools",
            "Agent,Task,TaskCreate,TaskGet,TaskList,TaskOutput,TaskStop,TaskUpdate,"
            "TeamCreate,TeamDelete,SendMessage,Skill,Workflow",
            "--tools",
            "Read,Glob,Grep,Bash,Edit,Write,NotebookEdit",
        ] + (["--resume", resume_session_id] if resume_session_id else [])

    def _run_claude_attempt(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        prompt: str,
        *,
        commands: list[dict],
        resume_session_id: str = "",
        model: str = CLAUDE_IMPLEMENT_MODEL,
        effort: str = "",
    ) -> dict:
        """Run the implementation pass on Claude when Codex has no capacity.

        The session is resumed on later passes, never restarted. The evidence
        gate can ask for a repair pass, and on 2026-08-04 a fresh Claude got
        that request with no memory of its own work, saw a comment it did not
        recognise, and removed the very line it had just written. Codex resumes
        for exactly this reason; Claude has to as well.
        """

        claude_bin = self.claude_bin or _claude_binary()
        verify_claude_subscription(claude_bin=claude_bin)
        effort = effort or self._implement_effort(item)
        attempt_id, attempt_no = self.store.start_attempt(
            item.item_id,
            provider="claude",
            model=model,
            effort=effort,
            resume_session_id=resume_session_id,
        )
        command = self._claude_implement_command(
            claude_bin,
            model=model,
            effort=effort,
            resume_session_id=resume_session_id,
        )
        # The evidence gate only accepts a test command that exited clean, and
        # a worker that reaches for bare `python` in a venv repo fails that
        # gate having done the work correctly. Codex learned this repo's
        # conventions from its own config; a fresh headless Claude has none,
        # so the interpreter it should use is stated rather than assumed.
        #
        # Scope is stated with it. The gate has never wanted the whole suite,
        # only one real command that exited clean, but "proportionate" left
        # that to taste and on 2026-08-05 a one-file job paid for all 1141
        # tests on every pass.
        interpreter = cwd / ".venv" / "bin" / "python"
        if interpreter.exists():
            prompt = (
                f"Run this repository's tests with {interpreter} -m pytest, never bare "
                "`python` or `pytest`, which resolve outside the project virtualenv "
                "and fail. Your completion is judged on a test command that exits "
                "zero. Scope that command to what you changed, the specific test "
                "files or node ids covering it, and run the full suite only when the "
                "change is broad enough to need it. A scoped run that exits zero "
                "satisfies the gate; a full run costs minutes on every pass.\n\n"
                + prompt
            )
        session_id = ""
        final_message = ""
        actual_model = ""
        actual_effort = ""
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=_worker_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            self._set_active_process(item.item_id, process)
            control_stop = threading.Event()

            def watch_controls() -> None:
                while not control_stop.wait(0.5):
                    control = self.store.pending_control(item.item_id)
                    if control and str(control.get("kind")) == "cancel":
                        with suppress(Exception):
                            process.terminate()
                        return

            watcher = threading.Thread(target=watch_controls, daemon=True)
            watcher.start()
            try:
                stdout, _ = process.communicate(
                    prompt, timeout=PRIVATE_REVIEW_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired:
                with suppress(Exception):
                    process.kill()
                stdout, _ = process.communicate()
                raise RuntimeError(
                    "the Claude implementation pass exceeded its time budget"
                )
            finally:
                control_stop.set()
                watcher.join(timeout=1)
                self._set_active_process(item.item_id, None)

            # The commands Claude runs INSIDE its session are the evidence,
            # not the CLI invocation wrapping them. Recording only the outer
            # process meant a job that wrote a test and ran pytest was failed
            # for "code changed without a recorded test command", because the
            # pytest run was invisible from out here.
            pending: dict[str, str] = {}
            for line in (stdout or "").splitlines():
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not session_id and event.get("session_id"):
                    session_id = str(event["session_id"])
                if event.get("type") == "result":
                    final_message = str(event.get("result") or final_message)
                message = event.get("message")
                if event.get("type") == "assistant" and isinstance(message, dict):
                    actual_model = str(message.get("model") or actual_model)
                    actual_effort = str(event.get("effort") or actual_effort)
                blocks = (message or {}).get("content") if isinstance(message, dict) else None
                for block in blocks or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and block.get("name") == "Bash":
                        shell = str((block.get("input") or {}).get("command") or "").strip()
                        if shell:
                            pending[str(block.get("id") or "")] = shell
                    elif block.get("type") == "tool_result":
                        shell = pending.pop(str(block.get("tool_use_id") or ""), "")
                        if shell:
                            commands.append(
                                {
                                    "command": shell,
                                    "exit_code": 1 if block.get("is_error") else 0,
                                    "provider": "claude",
                                }
                            )
            commands.append(
                {
                    "command": " ".join(command[:6]),
                    "exit_code": process.returncode,
                    "provider": "claude",
                }
            )
            if process.returncode != 0:
                raise RuntimeError(
                    f"Claude implementation exited with status {process.returncode}"
                )
            persisted_model, persisted_effort = _claude_actual_identity(session_id, cwd)
            actual_model = actual_model or persisted_model
            actual_effort = actual_effort or persisted_effort
            identity_error = _claude_implement_identity_error(
                actual_model,
                actual_effort,
                requested_model=model,
                requested_effort=effort,
            )
            if identity_error:
                raise RuntimeError(identity_error)
        except BaseException as error:
            self.store.finish_attempt(
                attempt_id, state="failed", exit_code=None, error=str(error)
            )
            raise
        # Bind the session BEFORE finishing the attempt. set_attempt_session
        # only updates a row while it is still running, so doing this after
        # finish_attempt silently wrote nothing: the id was lost, the repair
        # pass resumed nothing, and a fresh Claude with no memory of the work
        # was handed the whole prompt again. That is the "it sent the prompt
        # twice" he watched happen.
        if session_id:
            self.store.set_attempt_session(attempt_id, session_id)
            self.store.set_work_session(item.item_id, session_id)
        self.store.finish_attempt(
            attempt_id,
            state="completed",
            exit_code=0,
            reported_model=actual_model,
            reported_effort=actual_effort,
        )
        return {
            "attempt_no": attempt_no,
            "session_id": session_id,
            "message": final_message,
            "control": {},
        }

    def _run_started_codex_attempt(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        prompt: str,
        *,
        resume_session_id: str,
        commands: list[dict],
        attempt_id: str,
        attempt_no: int,
        model: str = CODEX_MODEL,
        effort: str = "",
    ) -> dict:
        """Finish an already-recorded private Codex attempt."""

        before = _session_ids()
        effort = effort or self._implement_effort(item)
        command = self._codex_command(
            self.codex_bin,
            cwd,
            model=model,
            effort=effort,
            resume_session_id=resume_session_id,
        )
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_worker_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._set_active_process(item.item_id, process)
        session_id = resume_session_id
        final_message = ""
        actual_model = ""
        actual_effort = ""
        control: dict[str, object] = {}
        control_stop = threading.Event()
        control_lock = threading.Lock()
        external_runtime_sid = ""

        def claim_external_runtime(sid: str) -> None:
            nonlocal external_runtime_sid
            clean_sid = str(sid or "").strip()
            if not clean_sid or clean_sid == external_runtime_sid:
                return
            from core.metadata import set_external_runtime

            set_external_runtime(
                clean_sid,
                kind="voice-work",
                pid=process.pid,
                lease_seconds=self.reused_turn_timeout + 60,
            )
            external_runtime_sid = clean_sid

        def watch_controls() -> None:
            while not control_stop.wait(0.2):
                if process.poll() is not None:
                    return
                pending = self.store.pending_controls(item.item_id)
                if not pending:
                    continue
                selected = pending[0]
                if str(selected.get("action") or "") == "steer" and (
                    not session_id or not actual_model or not actual_effort
                ):
                    # Keep early steering durable until this attempt has a
                    # resumable session and verified identity. Killing it at
                    # thread.started can race thread.settings and turn a valid
                    # correction into a false model-identity failure.
                    continue
                with control_lock:
                    if control:
                        return
                    control.update(selected)
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                return

        watcher = threading.Thread(
            target=watch_controls,
            name=f"serena-job-control-{item.item_id[:8]}",
            daemon=True,
        )
        watcher.start()
        try:
            claim_external_runtime(session_id)
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()
            assert process.stdout is not None
            for line in process.stdout:
                self._heartbeat()
                if len(line.encode("utf-8", errors="replace")) > MAX_OUTPUT_LINE_BYTES:
                    raise RuntimeError("Codex emitted an oversized event")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                record = self._event_record(event)
                self.store.record_job_event(
                    item.item_id,
                    "codex.event",
                    record,
                    attempt_id=attempt_id,
                )
                overlay_event = _overlay_code_event(event)
                if overlay_event is not None:
                    self._overlay(
                        {
                            "type": "code_event",
                            "item_id": item.item_id,
                            "event": overlay_event,
                        }
                    )
                event_type = str(event.get("type") or "")
                if event_type == "thread.started":
                    session_id = str(event.get("thread_id") or session_id)
                    if session_id:
                        from core.metadata import set_work_project_root

                        set_work_project_root(session_id, cwd)
                        claim_external_runtime(session_id)
                        self.store.set_attempt_session(attempt_id, session_id)
                        self.store.set_work_session(item.item_id, session_id)
                        self._emit_job_snapshot(item.item_id)
                elif event_type in {"thread.settings", "thread.settings_applied"}:
                    settings = event.get("thread_settings") or event.get("settings") or event
                    if isinstance(settings, dict):
                        actual_model = str(settings.get("model") or actual_model)
                        actual_effort = str(
                            settings.get("reasoning_effort")
                            or settings.get("effort")
                            or actual_effort
                        )
                if event_type == "item.completed":
                    completed = event.get("item") or {}
                    if not isinstance(completed, dict):
                        continue
                    if completed.get("type") == "agent_message":
                        final_message = str(completed.get("text") or "").strip()
                    elif completed.get("type") == "command_execution":
                        commands.append(
                            {
                                "command": str(completed.get("command") or ""),
                                "exit_code": completed.get("exit_code"),
                                "output": str(
                                    completed.get("aggregated_output")
                                    or completed.get("output")
                                    or ""
                                ),
                            }
                        )
                if self.stop_event.is_set():
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    break
            return_code = process.wait(timeout=10)
        except Exception as error:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            self.store.finish_attempt(
                attempt_id,
                state="failed",
                exit_code=process.returncode,
                reported_model=actual_model,
                reported_effort=actual_effort,
                error=str(error),
            )
            raise
        finally:
            control_stop.set()
            watcher.join(timeout=1)
            if external_runtime_sid:
                with suppress(Exception):
                    from core.metadata import clear_external_runtime

                    clear_external_runtime(external_runtime_sid, pid=process.pid)

        if not session_id:
            session_id = _new_session_id(before)
            if session_id:
                self.store.set_attempt_session(attempt_id, session_id)
                self.store.set_work_session(item.item_id, session_id)
        persisted_model, persisted_effort = _codex_actual_identity(session_id)
        actual_model = actual_model or persisted_model
        actual_effort = actual_effort or persisted_effort
        with control_lock:
            selected_control = dict(control)
        if selected_control:
            action = str(selected_control.get("action") or "")
            if action == "steer":
                identity_error = _codex_identity_error(
                    actual_model,
                    actual_effort,
                    requested_model=model,
                    requested_effort=effort,
                )
                if identity_error:
                    self.store.finish_attempt(
                        attempt_id,
                        state="failed",
                        exit_code=return_code,
                        reported_model=actual_model,
                        reported_effort=actual_effort,
                        error=identity_error,
                    )
                    self.store.finish_control(
                        str(selected_control.get("control_id") or ""),
                        error=identity_error,
                    )
                    raise RuntimeError(identity_error)
            state = "cancelled" if action == "cancel" else "steered"
            self.store.finish_attempt(
                attempt_id,
                state=state,
                exit_code=return_code,
                reported_model=actual_model,
                reported_effort=actual_effort,
            )
            self.store.finish_control(str(selected_control.get("control_id") or ""))
            # A steer can land before Codex reports a thread id. There is
            # nothing to resume then, so the caller restarts the accepted job
            # with the correction folded in rather than failing him for timing.
            return {
                "attempt_no": attempt_no,
                "session_id": session_id,
                "message": final_message,
                "control": selected_control,
            }
        if return_code != 0:
            error = f"Codex exited with status {return_code}"
            self.store.finish_attempt(
                attempt_id,
                state="failed",
                exit_code=return_code,
                reported_model=actual_model,
                reported_effort=actual_effort,
                error=error,
            )
            raise RuntimeError(error)
        identity_error = _codex_identity_error(
            actual_model,
            actual_effort,
            requested_model=model,
            requested_effort=effort,
        )
        if identity_error:
            self.store.finish_attempt(
                attempt_id,
                state="failed",
                exit_code=return_code,
                reported_model=actual_model,
                reported_effort=actual_effort,
                error=identity_error,
            )
            raise RuntimeError(identity_error)
        self.store.finish_attempt(
            attempt_id,
            state="completed",
            exit_code=return_code,
            reported_model=actual_model,
            reported_effort=actual_effort,
        )
        if session_id:
            with suppress(Exception):
                from core.metadata import set_custom_title, set_resident_work

                set_custom_title(session_id, _work_title(item.request))
                set_resident_work(session_id)
        return {
            "attempt_no": attempt_no,
            "session_id": session_id,
            "message": final_message,
            "control": {},
        }

    def _capture_evidence(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        commands: list[dict],
        *,
        label: str,
    ) -> dict:
        brief = item.brief or self.store.accepted_brief(item.item_id)
        if not brief:
            raise RuntimeError("accepted coding brief is missing")
        final_snapshot = capture_git_snapshot(cwd, item_id=item.item_id, label=label)
        evidence = scoped_git_evidence(
            brief,
            commands=commands,
            final_snapshot=final_snapshot,
        )
        self.store.record_evidence(item.item_id, evidence)
        self.store.record_job_event(
            item.item_id,
            "evidence.captured",
            {
                "complete": evidence.get("complete"),
                "changed_files": evidence.get("changed_files") or [],
                "test_count": len(evidence.get("tests") or []),
                "live_proof_count": len(evidence.get("live_proof") or []),
                "errors": evidence.get("errors") or [],
            },
        )
        self._emit_job_snapshot(item.item_id)
        return evidence

    def _pre_start_controls(self, item: VoiceInboxItem) -> tuple[bool, list[str]]:
        """Apply the controls that landed before this attempt spawned Codex.

        Two things arrive here. A cancel on a queued or resume-queued job has no
        process to interrupt, so it has to stop the job outright instead of
        waiting for one. And a steer that requeued the job during review is
        still pending: leaving it pending means the control watcher SIGTERMs the
        fresh resume attempt about a fifth of a second after it starts, which
        burns the attempt and can fail the whole job on a missing identity.
        """

        steering: list[str] = []
        record = self.store.work_record(item.item_id) or {}
        if str(record.get("state") or "") == "cancelled":
            return True, steering
        for control in self.store.pending_controls(item.item_id):
            action = str(control.get("action") or "")
            control_id = str(control.get("control_id") or "")
            self.store.finish_control(control_id)
            if action == "cancel":
                self.store.cancel_before_start(item.item_id)
                return True, steering
            if action == "steer":
                text = str(control.get("text") or "").strip()
                if text:
                    steering.append(text)
        return False, steering

    def _run_item(self, item: VoiceInboxItem, target_sid: str) -> None:
        self._owe_outcome(item.item_id, item.request)
        brief = item.brief or self.store.accepted_brief(item.item_id)
        route: dict = {}
        route_mode = "private"
        cwd: Path | None = None
        session_id = ""
        final_message = ""
        work_started = False
        active_recovery_id = ""
        commands: list[dict] = []
        progress_done = threading.Event()

        def report_slow_work() -> None:
            if not progress_done.wait(PROGRESS_NOTIFY_SECONDS):
                self.notifier(
                    "still on it. the coding work is continuing in the background."
                )

        progress_thread: threading.Thread | None = None
        try:
            if not brief:
                raise RuntimeError(
                    "queue item has no validated structured brief; refusing unsafe legacy execution"
                )
            # Effort is tiered per job, so this checks the frozen value against
            # the tier the brain judged instead of against one constant. It is
            # still an exact match that fails the job on any disagreement: the
            # gate is retargeted, not relaxed, and nothing here can pick its
            # own effort.
            implement_effort = frozen_implement_effort(brief)
            model_policy = brief.get("model_policy")
            if isinstance(model_policy, dict) and model_policy:
                from core.serena_policy import SerenaPolicyError, validate_frozen_decision

                try:
                    frozen_implement = validate_frozen_decision(
                        model_policy.get("implement_decision"),
                        profile="coding",
                        role="implement",
                    )
                    frozen_review = validate_frozen_decision(
                        model_policy.get("review_decision"),
                        profile="coding",
                        role="review",
                    )
                except SerenaPolicyError as error:
                    raise RuntimeError(
                        "accepted coding job has an invalid shared model policy"
                    ) from error
                if (
                    brief.get("implement_model") != frozen_implement["model"]
                    or brief.get("implement_effort") != frozen_implement["effort"]
                    or brief.get("review_model") != frozen_review["model"]
                    or brief.get("review_effort") != frozen_review["effort"]
                ):
                    raise RuntimeError(
                        "accepted coding job shared model policy is inconsistent"
                    )
            else:
                if (
                    brief.get("codex_model") != CODEX_MODEL
                    or brief.get("codex_effort") != implement_effort
                ):
                    raise RuntimeError("accepted coding job has an invalid Codex model policy")
                if brief.get("review_model") != CLAUDE_REVIEW_MODEL or brief.get(
                    "review_effort"
                ) != CLAUDE_REVIEW_EFFORT:
                    raise RuntimeError("accepted coding job has an invalid review model policy")
            try:
                selected_model = normalise_coding_model(
                    brief.get("coding_model"), strict=True
                )
            except ValueError as error:
                raise RuntimeError("accepted coding job has an invalid model selection") from error
            # Ask who actually has capacity before spending a turn on a
            # provider that cannot answer. On 2026-08-04 a job asking for
            # exactly this change was dispatched to Codex while Codex read
            # 100% rate-limited, and it sat at "working" with no log line
            # until Raghav cancelled it by hand. The reading existed; nothing
            # consulted it.
            if isinstance(model_policy, dict) and model_policy:
                assignment = self._provider_assignment(brief)
            elif selected_model == "auto":
                assignment = self.provider_chooser()
            else:
                assignment = self._provider_assignment(brief)
            # Capacity chooses who. The accepted brief chooses how hard they
            # think, and the recorded assignment has to say what will really
            # run or the job record is fiction.
            assignment = assignment.with_implement_effort(implement_effort)
            assignment_payload = assignment.to_dict()
            assignment_payload["selected_model"] = selected_model
            self.store.record_job_event(
                item.item_id,
                "provider_assignment",
                assignment_payload,
            )
            if not assignment.usable:
                raise RuntimeError(assignment.reason)
            cwd = validate_repository_root(str(brief.get("project_root") or ""))
            if str(cwd) != str(Path(str(brief.get("project_root"))).resolve()):
                raise RuntimeError("accepted project root no longer resolves to the same repository")
            previous_work = self.store.work_record(item.item_id) or {}
            latest_recovery = self.store.latest_automatic_recovery(item.item_id) or {}
            if str(latest_recovery.get("state") or "") in {"scheduled", "running"}:
                active_recovery_id = str(latest_recovery.get("recovery_id") or "")
            automatic_continuation = bool(
                active_recovery_id
                and (
                    not previous_work
                    or (
                        previous_work.get("state") in {"failed", "resume_queued"}
                        and str(previous_work.get("last_error") or "")
                        == str(latest_recovery.get("error") or "")
                    )
                )
            )
            resume_queued = previous_work.get("state") == "resume_queued"
            persisted_resume_session = (
                str(previous_work.get("session_id") or "") if resume_queued else ""
            )
            persisted_resume_provider = self.store.attempt_provider_for_session(
                item.item_id, persisted_resume_session
            )
            provider_resume_mismatch = bool(
                persisted_resume_session
                and persisted_resume_provider != assignment.implement_provider
            )
            if provider_resume_mismatch:
                self.store.record_job_event(
                    item.item_id,
                    "provider_resume_fallback",
                    {
                        "session_provider": persisted_resume_provider or "unknown",
                        "implement_provider": assignment.implement_provider,
                        "reason": "persisted work sessions cannot cross providers",
                    },
                )
            stored_route = self.store.route_record(item.item_id)
            if stored_route is not None:
                route = dict(stored_route)
            elif isinstance(brief.get("work_route"), dict):
                route = dict(brief["work_route"])
            route_mode = str(route.get("mode") or "private")
            if route_mode not in {"private", "reuse"}:
                raise RuntimeError("accepted coding job has an invalid frozen route")
            brief_route = brief.get("work_route")
            route_effort = (
                str(brief_route.get("effort") or "").strip().casefold()
                if isinstance(brief_route, dict)
                else ""
            )
            if route_mode == "reuse" and not route_effort:
                _route_model, route_effort = _codex_actual_identity(
                    str(route.get("session_id") or "")
                )
                route_effort = route_effort.strip().casefold()
            reconcile_frozen_route = bool(
                previous_work.get("state") == "resume_queued"
                and str(route.get("state") or "selected")
                in {"committed", "uncertain", "completed"}
            )
            route_effort_mismatch = bool(
                route_mode == "reuse"
                and route_effort != implement_effort
                and not reconcile_frozen_route
            )
            if route_effort_mismatch:
                reason = (
                    f"the selected reuse chat runs {route_effort or 'an unknown effort'}, "
                    f"but this job froze {implement_effort}"
                )
                if str(route.get("preference") or "auto") == "existing":
                    raise RuntimeError(reason)
                self.store.record_job_event(
                    item.item_id,
                    "route_effort_fallback",
                    {
                        "route_effort": route_effort,
                        "implement_effort": implement_effort,
                        "reason": reason,
                    },
                )
                route_mode = "private"
                route = {}
            # A reuse route is bound to an existing Codex chat, so it can only
            # run on Codex. When Codex has no capacity, take a fresh private
            # Claude session rather than refusing the job outright.
            if assignment.implement_provider != "codex" and route_mode == "reuse":
                route_mode = "private"
                route = {}
            if provider_resume_mismatch:
                route_mode = "private"
                route = {}
            if (
                resume_queued
                and assignment.implement_provider == "codex"
                and not route_effort_mismatch
                and not provider_resume_mismatch
            ):
                session_id = persisted_resume_session
                if not session_id:
                    raise RuntimeError("resume was queued without a persisted Codex session")
            elif route_mode == "reuse":
                session_id = str(route.get("session_id") or "")
                if not session_id:
                    raise RuntimeError("accepted reuse route has no Codex session")
            if (
                not session_id
                and assignment.implement_provider == "claude"
                and not provider_resume_mismatch
            ):
                session_id = self._warm_claude_session(
                    item,
                    cwd,
                    preferred_session_id=(
                        persisted_resume_session if resume_queued else ""
                    ),
                )
            cancelled, steering = self._pre_start_controls(item)
            if cancelled:
                self._emit_job_snapshot(item.item_id, event_type="code_done")
                self.notifier("stopped. that coding job was cancelled before it started.")
                print(
                    f"[work-supervisor] cancelled before start item={item.item_id[:8]}",
                    flush=True,
                )
                return
            if not self.store.acknowledge_started(
                item.item_id,
                target_sid=target_sid,
                cwd=str(cwd),
            ):
                raise RuntimeError("lost the durable work claim before Codex started")
            work_started = True
            claimed_recovery = self.store.claim_automatic_recovery(item.item_id)
            if claimed_recovery is not None:
                active_recovery_id = str(claimed_recovery.get("recovery_id") or "")
            progress_thread = threading.Thread(
                target=report_slow_work,
                name=f"serena-work-progress-{item.item_id[:8]}",
                daemon=True,
            )
            progress_thread.start()
            self._emit_job_snapshot(item.item_id, event_type="code_start")

            restarted = previous_work.get("state") == "resume_queued"
            base_prompt = (
                _reused_prompt(item) if route_mode == "reuse" else _private_prompt(item)
            )
            if automatic_continuation:
                prompt = (
                    "This is a bounded automatic continuation of the same logical coding "
                    f"job, recovery {latest_recovery.get('recovery_no')} of "
                    f"{latest_recovery.get('budget')}. The prior attempt stopped with a "
                    f"recoverable {latest_recovery.get('kind')} failure: "
                    f"{latest_recovery.get('error')}. Inspect that failure and the live tree, "
                    "continue the persisted work without broadening the accepted brief, and "
                    "rerun proportionate verification. Do not repeat the same failed approach "
                    "without diagnosing it first.\n\n"
                    + base_prompt
                )
            elif restarted:
                prompt = (
                    "Resume the persisted accepted coding job after a supervisor restart. "
                    "Re-read the durable brief below, inspect the live state, and continue "
                    "without discarding prior work.\n\n"
                    + base_prompt
                )
            else:
                prompt = base_prompt
            if steering:
                prompt = (
                    "Raghav steered this job before this attempt started. Apply these "
                    "corrections inside the accepted scope, then rerun proportionate "
                    "verification.\n\n<steering>\n"
                    + "\n".join(steering)
                    + "\n</steering>\n\n"
                    + prompt
                )
            reconcile_reused_turn = bool(restarted and route_mode == "reuse")

            def run_attempt(turn_prompt: str) -> dict:
                nonlocal reconcile_reused_turn
                if route_mode == "reuse" and route.get("bridge_port"):
                    attempt_result = self._run_reused_codex_attempt(
                        item,
                        cwd,
                        turn_prompt,
                        route=route,
                        commands=commands,
                        reconcile_existing=reconcile_reused_turn,
                    )
                    reconcile_reused_turn = False
                    return attempt_result
                if assignment.implement_provider == "claude":
                    attempt_result = self._run_claude_attempt(
                        item,
                        cwd,
                        turn_prompt,
                        commands=commands,
                        resume_session_id=session_id,
                        model=assignment.implement_model,
                        effort=assignment.implement_effort,
                    )
                else:
                    attempt_result = self._run_codex_attempt(
                        item,
                        cwd,
                        turn_prompt,
                        resume_session_id=session_id,
                        commands=commands,
                        model=assignment.implement_model,
                        effort=assignment.implement_effort,
                    )
                reconcile_reused_turn = False
                return attempt_result

            while True:
                result = run_attempt(prompt)
                session_id = str(result.get("session_id") or session_id)
                final_message = str(result.get("message") or final_message)
                control = dict(result.get("control") or {})
                if not control:
                    pending = self.store.pending_controls(item.item_id)
                    if pending:
                        control = pending[0]
                        self.store.finish_control(str(control.get("control_id") or ""))
                action = str(control.get("action") or "")
                if action == "cancel":
                    with suppress(Exception):
                        self._capture_evidence(
                            item,
                            cwd,
                            commands,
                            label=f"cancelled-{result.get('attempt_no') or 0}",
                        )
                    self.store.finish_work_item(
                        item.item_id,
                        state="cancelled",
                        summary="cancelled at Raghav's request",
                    )
                    self._emit_job_snapshot(item.item_id, event_type="code_done")
                    self.notifier("stopped. the coding job was cancelled.")
                    return
                if action == "steer":
                    steer_text = str(control.get("text") or "")
                    if session_id:
                        prompt = (
                            "Raghav steered the accepted job. Continue in this exact persisted "
                            "session. Apply the correction without broadening the accepted authority, "
                            "then rerun proportionate verification.\n\n<steering>\n"
                            + steer_text
                            + "\n</steering>\n\n"
                            + item.prompt
                        )
                    else:
                        # The steer landed before Codex reported a thread id, so
                        # there is no session to resume. Start the accepted job
                        # over with the correction already applied instead of
                        # failing him for saying it half a second too early.
                        prompt = (
                            "Raghav steered this job before it got going, so start the accepted "
                            "work fresh with the correction already applied.\n\n<steering>\n"
                            + steer_text
                            + "\n</steering>\n\n"
                            + _private_prompt(item)
                        )
                    self._emit_job_snapshot(item.item_id)
                    continue
                break

            evidence = self._capture_evidence(
                item,
                cwd,
                commands,
                label=f"final-{result.get('attempt_no') or 0}",
            )
            repairable = [
                error
                for error in evidence.get("errors") or []
                if "without a recorded test" in str(error)
                or "no scoped change" in str(error)
                or "never exited clean" in str(error)
                or "live proof command" in str(error)
            ]
            if repairable and session_id:
                repair_prompt = (
                    "Mechanical completion evidence is incomplete. Stay in the accepted scope. "
                    "Resolve these evidence gaps by fixing remaining defects if needed and running "
                    "the exact tests or safe proof commands required. Prefix Python or other "
                    "non-obvious runtime proof commands with SERENA_EVIDENCE_KIND=live, never tests "
                    "or static inspection. Do not merely describe them.\n\n"
                    + json.dumps(repairable, ensure_ascii=False)
                )
                while True:
                    repair = run_attempt(repair_prompt)
                    session_id = str(repair.get("session_id") or session_id)
                    final_message = str(repair.get("message") or final_message)
                    repair_control = dict(repair.get("control") or {})
                    if not repair_control:
                        break
                    if repair_control.get("action") == "cancel":
                        self.store.finish_work_item(
                            item.item_id,
                            state="cancelled",
                            summary="cancelled at Raghav's request",
                        )
                        self._emit_job_snapshot(item.item_id, event_type="code_done")
                        self.notifier("stopped. the coding job was cancelled.")
                        return
                    # Steering here used to fail the job outright. He corrected
                    # it while it was closing an evidence gap, which is exactly
                    # when he is watching; keep the same session and carry both.
                    repair_prompt = (
                        "Raghav steered the job while its completion evidence was being repaired. "
                        "Continue in this exact persisted session: apply the correction, then "
                        "finish closing these evidence gaps.\n\n<steering>\n"
                        + str(repair_control.get("text") or "")
                        + "\n</steering>\n\n"
                        + json.dumps(repairable, ensure_ascii=False)
                    )
                evidence = self._capture_evidence(
                    item,
                    cwd,
                    commands,
                    label=f"final-{repair.get('attempt_no') or 0}",
                )
            if evidence.get("complete") is not True:
                raise RuntimeError(
                    "mechanical completion evidence is incomplete: "
                    + "; ".join(map(str, evidence.get("errors") or []))
                )

            needs_review, review_reason = review_required(evidence)
            review_id = self.store.begin_review(
                item.item_id,
                required=needs_review,
                reason=review_reason,
                model=assignment.review_model if needs_review else "",
                effort=assignment.review_effort if needs_review else "",
            )
            review_decision: dict | None = None
            if needs_review:
                self._overlay(
                    {
                        "type": "code_event",
                        "item_id": item.item_id,
                        "event": {
                            "kind": "text",
                            "summary": "reviewing the scoped diff and mechanical evidence",
                        },
                    }
                )
                try:
                    review_decision = self._run_policy_review(
                        item,
                        cwd,
                        evidence,
                        assignment,
                    )
                except _ReviewControlInterrupted as interrupted:
                    self.store.finish_review(
                        review_id,
                        approved=False,
                        findings=[],
                        reported_model="",
                        reported_effort="",
                        error=str(interrupted),
                    )
                    self._finish_or_requeue_review_control(item, interrupted.control)
                    return
                except Exception as error:
                    self.store.finish_review(
                        review_id,
                        approved=False,
                        findings=[],
                        reported_model="",
                        reported_effort="",
                        error=str(error),
                    )
                    raise
                self.store.finish_review(
                    review_id,
                    approved=bool(review_decision["approved"]),
                    findings=review_decision["findings"],
                    reported_model=str(review_decision["reported_model"]),
                    reported_effort=str(review_decision["reported_effort"]),
                    session_id=str(review_decision["session_id"]),
                )
                if not review_decision["approved"]:
                    if not session_id:
                        raise RuntimeError("review found defects but the Codex session is not resumable")
                    correction_prompt = (
                        f"The conditional {assignment.review_model} review found concrete issues "
                        "in the actual scoped "
                        "diff. Fix only these findings in this persisted session, preserve unrelated "
                        "dirty work, and rerun the relevant tests.\n\n"
                        + json.dumps(review_decision["findings"], ensure_ascii=False)
                    )
                    while True:
                        correction = run_attempt(correction_prompt)
                        session_id = str(correction.get("session_id") or session_id)
                        correction_control = dict(correction.get("control") or {})
                        if not correction_control:
                            break
                        if correction_control.get("action") == "cancel":
                            self.store.finish_work_item(
                                item.item_id,
                                state="cancelled",
                                summary="cancelled at Raghav's request",
                            )
                            self._emit_job_snapshot(item.item_id, event_type="code_done")
                            self.notifier("stopped. the coding job was cancelled.")
                            return
                        correction_prompt = (
                            "Raghav steered the review correction. Continue in this exact persisted "
                            "session without broadening the accepted authority, then rerun the "
                            "relevant tests.\n\n<steering>\n"
                            + str(correction_control.get("text") or "")
                            + "\n</steering>"
                        )
                    session_id = str(correction.get("session_id") or session_id)
                    final_message = str(correction.get("message") or final_message)
                    evidence = self._capture_evidence(
                        item,
                        cwd,
                        commands,
                        label=f"review-fix-{correction.get('attempt_no') or 0}",
                    )
                    if evidence.get("complete") is not True:
                        raise RuntimeError("review correction left incomplete mechanical evidence")
                    second_id = self.store.begin_review(
                        item.item_id,
                        required=True,
                        reason="verify corrections against the updated scoped diff",
                        model=assignment.review_model,
                        effort=assignment.review_effort,
                    )
                    try:
                        second = self._run_policy_review(
                            item,
                            cwd,
                            evidence,
                            assignment,
                        )
                    except _ReviewControlInterrupted as interrupted:
                        self.store.finish_review(
                            second_id,
                            approved=False,
                            findings=[],
                            reported_model="",
                            reported_effort="",
                            error=str(interrupted),
                        )
                        self._finish_or_requeue_review_control(item, interrupted.control)
                        return
                    except Exception as error:
                        self.store.finish_review(
                            second_id,
                            approved=False,
                            findings=[],
                            reported_model="",
                            reported_effort="",
                            error=str(error),
                        )
                        raise
                    self.store.finish_review(
                        second_id,
                        approved=bool(second["approved"]),
                        findings=second["findings"],
                        reported_model=str(second["reported_model"]),
                        reported_effort=str(second["reported_effort"]),
                        session_id=str(second["session_id"]),
                    )
                    review_decision = second
                    if not second["approved"]:
                        raise RuntimeError(
                            f"{assignment.review_model} review still rejects the corrected "
                            "scoped diff: "
                            + json.dumps(second.get("findings") or [], ensure_ascii=False)
                        )

            pending_after_review = self.store.pending_controls(item.item_id)
            if pending_after_review:
                self._finish_or_requeue_review_control(item, pending_after_review[0])
                return

            review_session_id = str((review_decision or {}).get("session_id") or "")
            if session_id and review_session_id and route_mode != "reuse":
                with suppress(Exception):
                    from core.metadata import (
                        link_sessions,
                        set_custom_title,
                        set_resident_work,
                    )

                    title = _work_title(item.request)
                    set_custom_title(review_session_id, title)
                    set_resident_work(review_session_id)
                    link_sessions([session_id, review_session_id])
            summary = final_message or "completed with mechanical diff and test evidence"
            self.store.finish_work_item(
                item.item_id,
                summary=summary,
                require_evidence=True,
            )
            with self._index_lock, suppress(Exception):
                self.indexer()
            self._emit_job_snapshot(item.item_id, event_type="code_done")
            self._deliver_outcome(item.item_id, "done. " + _notification_summary(summary))
            print(
                f"[work-supervisor] completed item={item.item_id[:8]} "
                f"session={session_id[:8] or 'unknown'} cwd={cwd}",
                flush=True,
            )
        except Exception as error:
            error_text = str(error)
            recovery: dict[str, Any] = {}
            terminal_recorded = work_started
            if self.stop_event.is_set():
                if work_started:
                    self.store.requeue_work_item(
                        item.item_id,
                        error="resident worker stopped before completion",
                    )
                else:
                    self.store.release(
                        item.item_id,
                        target_sid=target_sid,
                        error="resident worker stopped before completion",
                    )
            else:
                recovery_kind = automatic_recovery_kind(error_text)
                if recovery_kind:
                    recovery = self.store.queue_automatic_recovery(
                        item.item_id,
                        error=error_text,
                        kind=recovery_kind,
                        max_recoveries=AUTOMATIC_RECOVERY_LIMIT,
                        force_private_route=recovery_kind == "route",
                        drop_uncommitted_session=recovery_kind == "route",
                        active_recovery_id=active_recovery_id,
                    )
                recovery_reason = str(recovery.get("reason") or "")
                terminal_error = error_text + (
                    f"; {recovery_reason}" if recovery_reason else ""
                )
                if recovery.get("queued") is not True:
                    if recovery.get("terminal") is True:
                        terminal_recorded = True
                    elif work_started:
                        if cwd is not None:
                            with suppress(Exception):
                                self._capture_evidence(
                                    item,
                                    cwd,
                                    commands,
                                    label="failed",
                                )
                        self.store.finish_work_item(item.item_id, error=terminal_error)
                    else:
                        terminal_recorded = self.store.fail_claimed_item(
                            item.item_id,
                            target_sid=target_sid,
                            error=terminal_error,
                            cwd=str(cwd or ""),
                        )
            if not self.stop_event.is_set():
                if recovery.get("queued") is True:
                    self._emit_job_snapshot(item.item_id)
                    self.notifier(
                        "that coding attempt hit a recoverable failure. "
                        "i queued a bounded continuation instead of abandoning it."
                    )
                    print(
                        f"[work-supervisor] recovering item={item.item_id[:8]} "
                        f"kind={automatic_recovery_kind(error_text)} "
                        f"attempt={recovery.get('recoveries')}/{recovery.get('budget')}: "
                        f"{error_text}",
                        flush=True,
                    )
                else:
                    if terminal_recorded:
                        self._emit_job_snapshot(item.item_id, event_type="code_done")
                    else:
                        self._overlay(
                            {
                                "type": "code_done",
                                "summary": "stopped: "
                                + _notification_summary(error_text, limit=1_000),
                            }
                        )
                    self._deliver_outcome(
                        item.item_id,
                        "the coding job stopped: "
                        + _notification_summary(terminal_error, limit=500),
                    )
                    print(
                        f"[work-supervisor] failed item={item.item_id[:8]}: "
                        f"{terminal_error}",
                        flush=True,
                    )
        finally:
            progress_done.set()
            if progress_thread is not None:
                progress_thread.join(timeout=1)
            self._set_active_process(item.item_id, None)


def main() -> int:
    supervisor = VoiceWorkSupervisor()

    def stop(_signum: int, _frame: object) -> None:
        supervisor.request_stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    supervisor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
