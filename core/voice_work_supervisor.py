"""Resident owner for coding work accepted through Serena voice."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path

from core.billing import present_metered_auth_env, strip_metered_auth_env
from core.brain_lifetime import write_text_atomic
from core.voice_inbox import VoiceInboxItem, VoiceInboxStore, get_default_voice_inbox

HOME = Path.home()
PROJECTS_ROOT = HOME / "Documents" / "Projects"
SERENA_ROOT = PROJECTS_ROOT / "serena"
SUPERVISOR_MARKER = HOME / ".local" / "state" / "serena" / "work-supervisor.json"
OVERLAY_EVENT_SOCKET = HOME / ".local" / "state" / "serena" / "brain-events.sock"
POLL_SECONDS = 0.25
HEARTBEAT_SECONDS = 1.0
HEARTBEAT_STALE_SECONDS = 5.0
PROGRESS_NOTIFY_SECONDS = 90.0
MAX_OUTPUT_LINE_BYTES = 1024 * 1024
MAX_CLAUDE_OUTPUT_BYTES = 4 * 1024 * 1024
PRIVATE_REVIEW_TIMEOUT_SECONDS = 30 * 60


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


def _project_roots() -> Iterable[Path]:
    if not PROJECTS_ROOT.is_dir():
        return ()
    roots: list[Path] = []
    for git_dir in PROJECTS_ROOT.glob("**/.git"):
        try:
            relative = git_dir.relative_to(PROJECTS_ROOT)
        except ValueError:
            continue
        if len(relative.parts) > 5:
            continue
        roots.append(git_dir.parent)
    return roots


def resolve_work_cwd(request: str) -> Path:
    clean = str(request)
    for match in re.finditer(r"(?:/home/raghav|~)/[^\s,;]+", clean):
        candidate = Path(match.group(0).replace("~", str(HOME), 1)).expanduser()
        if candidate.is_file():
            candidate = candidate.parent
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(HOME)
        except (OSError, ValueError):
            continue
        if resolved.is_dir():
            return resolved

    normalised = f" {_normalise_name(clean)} "
    matches: list[tuple[int, Path]] = []
    for root in _project_roots():
        aliases = {
            _normalise_name(root.name),
            _normalise_name(str(root.relative_to(PROJECTS_ROOT))),
        }
        for alias in aliases:
            if alias and f" {alias} " in normalised:
                matches.append((len(alias), root))
    if matches:
        return max(matches, key=lambda item: item[0])[1]
    if " serena " in normalised or any(
        phrase in normalised
        for phrase in (" voice ", " wake word ", " dot display ", " brain daemon ")
    ):
        return SERENA_ROOT
    return HOME


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


def _work_title(request: str) -> str:
    current = request.rsplit("Current spoken request:", 1)[-1]
    current = " ".join(current.split()).strip(" .")
    if len(current) > 64:
        current = current[:61].rstrip() + "..."
    return "Serena: " + (current or "spoken coding work")


def _private_prompt(item: VoiceInboxItem) -> str:
    return (
        item.prompt
        + "\n\nThis is Serena's private coding session. Before acting, read "
        "/home/raghav/Documents/Projects/serena/Persona.md and Tooling.md and "
        "continue as the same Serena. Own the work end to end. "
        "Do not tell Raghav to open another app or terminal. Inspect the live state, "
        "make the requested changes, verify them proportionately, and give one concise "
        "completion report. If the request requires missing authority or a risky external "
        "state change, stop at that boundary and state the exact blocker. Do not send "
        "Telegram messages yourself; the resident supervisor handles completion delivery."
    )


def _private_review_prompt(item: VoiceInboxItem, first_pass: str) -> str:
    return (
        "You are the second private coding runtime inside one Serena. Read "
        "/home/raghav/Documents/Projects/serena/Persona.md and Tooling.md first. "
        "Inspect the live worktree and the original spoken request below. Review the "
        "first runtime's implementation, run the tests that matter, and directly fix "
        "any concrete problem you find. Preserve unrelated user changes. Return one "
        "concise final report written as Serena in first person. Never mention Claude, "
        "Codex, runtimes, panes, terminals, delegation, or a handoff.\n\n"
        "<spoken-request>\n"
        + item.prompt
        + "\n</spoken-request>\n\n<first-pass-report>\n"
        + first_pass[:16_000]
        + "\n</first-pass-report>"
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
    ) -> None:
        self.store = store or get_default_voice_inbox()
        self.marker_path = Path(marker_path)
        self.notifier = notifier or self._text_raghav
        self.overlay_sender = overlay_sender or _send_overlay_event
        self.indexer = indexer or self._refresh_index
        self.codex_bin = codex_bin or _codex_binary()
        self.claude_bin = claude_bin or _claude_binary()
        self.claude_model = claude_model or os.environ.get(
            "SERENA_VOICE_CLAUDE_MODEL", "fable"
        )
        self.stop_event = threading.Event()
        self.active_process: subprocess.Popen[str] | None = None
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
    def _text_raghav(message: str) -> None:
        chats = shutil.which("chats") or str(HOME / ".local" / "bin" / "chats")
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [chats, "text", message],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )

    def request_stop(self) -> None:
        self.stop_event.set()
        process = self.active_process
        if process is not None and process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)

    def run(self) -> None:
        verify_codex_subscription(codex_bin=self.codex_bin)
        verify_claude_subscription(claude_bin=self.claude_bin)
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
        try:
            while not self.stop_event.is_set():
                target_sid = f"headless-voice-{uuid.uuid4().hex}"
                item = self.store.claim_next(target_sid)
                if item is None:
                    self.stop_event.wait(POLL_SECONDS)
                    continue
                self._run_item(item, target_sid)
        finally:
            self.request_stop()
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            self.store.clear_resident_lease(self._owner_id)
            with suppress(FileNotFoundError):
                self.marker_path.unlink()

    def _run_private_review(
        self,
        item: VoiceInboxItem,
        cwd: Path,
        first_pass: str,
    ) -> tuple[str, str]:
        command = [
            self.claude_bin,
            "-p",
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--model",
            self.claude_model,
            "--effort",
            "high",
        ]
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
        self.active_process = process
        try:
            stdout, stderr = process.communicate(
                _private_review_prompt(item, first_pass),
                timeout=PRIVATE_REVIEW_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            raise RuntimeError("private coding second pass timed out")
        if len(stdout.encode("utf-8", errors="replace")) > MAX_CLAUDE_OUTPUT_BYTES:
            raise RuntimeError("private coding second pass returned oversized output")
        if process.returncode != 0:
            detail = _notification_summary(stderr or stdout, limit=800)
            raise RuntimeError(
                f"private coding second pass exited with status {process.returncode}: {detail}"
            )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("private coding second pass returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("is_error") is True:
            raise RuntimeError("private coding second pass reported an error")
        result = str(payload.get("result") or "").strip()
        if not result:
            raise RuntimeError("private coding second pass completed without a report")
        return result, str(payload.get("session_id") or "")

    def _run_item(self, item: VoiceInboxItem, target_sid: str) -> None:
        cwd = resolve_work_cwd(item.request)
        before = _session_ids()
        command = [
            self.codex_bin,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-C",
            str(cwd),
            "-",
        ]
        process: subprocess.Popen[str] | None = None
        session_id = ""
        final_message = ""
        work_started = False
        progress_done = threading.Event()

        def report_slow_work() -> None:
            if not progress_done.wait(PROGRESS_NOTIFY_SECONDS):
                self.notifier(
                    "still on it. the coding work is continuing in the background."
                )

        progress_thread: threading.Thread | None = None
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
            self.active_process = process
            if not self.store.acknowledge_started(
                item.item_id,
                target_sid=target_sid,
                cwd=str(cwd),
            ):
                raise RuntimeError("lost the durable work claim before Codex started")
            work_started = True
            progress_thread = threading.Thread(
                target=report_slow_work,
                name=f"serena-work-progress-{item.item_id[:8]}",
                daemon=True,
            )
            progress_thread.start()
            self._overlay(
                {
                    "type": "code_start",
                    "project": cwd.name,
                    "item_id": item.item_id,
                }
            )
            assert process.stdin is not None
            process.stdin.write(_private_prompt(item))
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
                overlay_event = _overlay_code_event(event)
                if overlay_event is not None:
                    self._overlay({"type": "code_event", "event": overlay_event})
                if event.get("type") == "thread.started":
                    session_id = str(event.get("thread_id") or "")
                    if session_id:
                        self.store.set_work_session(item.item_id, session_id)
                        with suppress(Exception):
                            from core.metadata import set_custom_title, set_resident_work

                            set_custom_title(session_id, _work_title(item.request))
                            set_resident_work(session_id)
                if event.get("type") == "item.completed":
                    completed = event.get("item") or {}
                    if completed.get("type") == "agent_message":
                        final_message = str(completed.get("text") or "").strip()
                if self.stop_event.is_set():
                    self.request_stop()
                    break
            return_code = process.wait(timeout=10)
            if not session_id:
                session_id = _new_session_id(before)
                if session_id:
                    self.store.set_work_session(item.item_id, session_id)
            if return_code != 0:
                raise RuntimeError(f"Codex exited with status {return_code}")
            if not final_message:
                raise RuntimeError("Codex completed without a final report")
            self._overlay(
                {
                    "type": "code_event",
                    "event": {
                        "kind": "text",
                        "summary": "checking the work and tightening the final result",
                    },
                }
            )
            final_message, claude_session_id = self._run_private_review(
                item,
                cwd,
                final_message,
            )
            if session_id and claude_session_id:
                with suppress(Exception):
                    from core.metadata import (
                        link_sessions,
                        set_custom_title,
                        set_resident_work,
                    )

                    title = _work_title(item.request)
                    set_custom_title(claude_session_id, title)
                    set_resident_work(claude_session_id)
                    link_sessions([session_id, claude_session_id])
            self.store.finish_work_item(
                item.item_id,
                summary=final_message,
            )
            with suppress(Exception):
                self.indexer()
            self._overlay(
                {
                    "type": "code_done",
                    "summary": _notification_summary(final_message, limit=2_000),
                }
            )
            self.notifier("done. " + _notification_summary(final_message))
            print(
                f"[work-supervisor] completed item={item.item_id[:8]} "
                f"session={session_id[:8] or 'unknown'} cwd={cwd}",
                flush=True,
            )
        except Exception as error:
            if process is not None and process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            if work_started and self.stop_event.is_set():
                self.store.requeue_work_item(
                    item.item_id,
                    error="resident worker stopped before completion",
                )
            elif work_started:
                self.store.finish_work_item(item.item_id, error=str(error))
            else:
                self.store.release(
                    item.item_id,
                    target_sid=target_sid,
                    error=str(error),
                )
            if not self.stop_event.is_set():
                self._overlay(
                    {
                        "type": "code_done",
                        "summary": "stopped: "
                        + _notification_summary(str(error), limit=1_000),
                    }
                )
                self.notifier(
                    "the coding job stopped: " + _notification_summary(str(error), limit=500)
                )
            print(
                f"[work-supervisor] failed item={item.item_id[:8]}: {error}",
                flush=True,
            )
        finally:
            progress_done.set()
            if progress_thread is not None:
                progress_thread.join(timeout=1)
            self.active_process = None


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
