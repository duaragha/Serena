"""Explicit, durable mid-call draft jobs with scoped artifact delivery."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from core.artifacts import ArtifactRegistry, get_default_artifact_registry
from core.billing import command_fingerprint, environment_fingerprint, strip_metered_auth_env
from core.voice_inbox import VoiceInboxItem, VoiceInboxStore, get_default_voice_inbox
from core.work_jobs import WorkJob, WorkJobEvent, WorkJobStore, process_start_token

_DRAFT_LINK = re.compile(
    r"^(?:(?:okay|ok)\s*,?\s+)?(?:please\s+)?draft\s+(?P<request>.+?)"
    r"\s+and\s+send\s+me\s+a\s+link[.!]*$",
    re.IGNORECASE | re.DOTALL,
)
_DRAFT_CANCELLATION = re.compile(
    r"\b(?:do\s+not|don't|dont|never|cancel|stop|abort|nix|"
    r"skip(?:\s+(?:actually\s+)?)?(?:creating|drafting|writing|making|doing|it)|"
    r"avoid(?:\s+(?:actually\s+)?)?(?:creating|drafting|writing|making|doing|it)|"
    r"refrain\s+from\s+(?:creating|drafting|writing|making|doing)|"
    r"hold\s+off\s+on\s+(?:creating|drafting|writing|making|doing|it)|"
    r"(?:do\s+not|don't|dont)\s+bother\s+(?:creating|drafting|writing|making|doing)|"
    r"no\s+need\s+to\s+(?:create|draft|write|make|do)|"
    r"leave\s+(?:it|that|this)\s+(?:unwritten|undrafted|uncreated|unmade|alone)|"
    r"not\s+(?:create|draft|write|make|do)|"
    r"without\s+(?:creating|drafting|writing|making|doing))\b",
    re.IGNORECASE,
)
_WAKE_PREFIX = re.compile(r"^(?:hey\s+)?serena[,.!?]*\s+", re.IGNORECASE)
_LIVE_RELAY = re.compile(
    r"^(?:(?:please\s+)?(?:send|route|pass)(?:\s+this)?\s+to|"
    r"(?:please\s+)?tell|(?:please\s+)?ask)\s+"
    r"(?:(?:my|the)\s+)?(?:coding\s+)?(?:session|pane|serena|codex|claude)"
    r"(?:\s+(?:session|pane))?\s*(?:to\s+|that\s+|[:,]\s*)"
    r"(?P<request>.+)$",
    re.IGNORECASE,
)
# Spoken work language is broader than a bare verb-first imperative. The
# original pattern only fired when the sentence literally began with one of
# ~24 verbs, so ordinary asks ("go fix the deep link", "start working on it",
# "keep going on the voice work") silently never became jobs, they were not
# refused, they simply never reached the inbox. Lead-ins are stripped and the
# remainder must still open with a work verb, so questions ("how can we fix"),
# explanations ("tell me why"), hypotheticals ("what if we built"), and
# cancellations ("don't update") stay non-executing exactly as before.
_LIVE_WORK = re.compile(
    r"^(?:(?:okay|ok|alright|right|yeah|yep)\s*,?\s+)?"
    r"(?:(?:please\s+)?(?:can\s+you|could\s+you|would\s+you)\s+(?:please\s+)?"
    r"|please\s+"
    r"|i\s+(?:want|need)\s+you\s+to\s+"
    r"|(?:i\s+)?(?:would|'d)\s+like\s+you\s+to\s+"
    r"|go\s+ahead\s+and\s+"          # must precede bare "go"
    r"|let'?s\s+"
    r"|go\s+"
    r")?"
    r"(?P<request>(?:fix|build|implement|change|update|add|remove|replace|refactor|debug|"
    r"investigate|test|verify|review|finish|deploy|ship|wire|hook\s+up|"
    r"work\s+on|create|write|code|do"
    r"|start|continue|keep\s+going|get\s+started|carry\s+on"
    r"|look\s+into|dig\s+into|jump\s+into|tackle"
    r"|sort\s+out|sort|handle|take\s+care\s+of|deal\s+with"
    r"|clean\s+up|set\s+up|make|patch|repair|migrate|rename|polish"
    r"|optimi[sz]e|document|connect|move|split|merge|extract"
    r")\b.*)$",
    re.IGNORECASE,
)
# Passive phrasing that names the target first: "i need the deep link fixed".
_LIVE_WORK_PASSIVE = re.compile(
    r"^(?:(?:okay|ok|alright)\s*,?\s+)?"
    r"i\s+(?:want|need)\s+"
    r"(?P<request>(?!you\s+to\b).+?\s+"
    r"(?:fixed|done|built|updated|changed|removed|added|replaced|working|"
    r"sorted|handled|cleaned\s+up|set\s+up|finished|patched))"
    r"[.!]*$",
    re.IGNORECASE,
)
_CODE_PANEL_TARGET = re.compile(
    r"\b(?:coding|code)\s+(?:panel|terminal|window)\b",
    re.IGNORECASE,
)
_CODE_PANEL_OPEN = re.compile(r"\b(?:open|show|bring\s+up|display)\b", re.IGNORECASE)
_CODE_PANEL_HIDE = re.compile(r"\b(?:hide|close|dismiss)\b", re.IGNORECASE)
MAX_DRAFT_REQUEST_CHARS = 4_000
MAX_DRAFT_OUTPUT_CHARS = 256_000
MAX_WORKER_STDOUT_BYTES = 512 * 1024
MAX_WORKER_STDERR_BYTES = 64 * 1024
HEADLESS_JOB_CWD = Path.home() / ".cache" / "serena" / "headless-call-jobs"
_WORKER_GATE = Path(__file__).with_name("worker_gate.py")


@dataclass(frozen=True, slots=True)
class DraftLinkIntent:
    request: str


@dataclass(frozen=True, slots=True)
class LiveWorkIntent:
    request: str


@dataclass(frozen=True, slots=True)
class CodePanelIntent:
    action: str


@dataclass(frozen=True, slots=True)
class DraftResult:
    content: str
    worker_session_id: str


class DraftRunner(Protocol):
    def run(
        self,
        job: WorkJob,
        *,
        worker_session_id: str,
        adopt_worker: Callable[[int], bool],
        record_billing: Callable[[dict[str, Any]], bool] | None = None,
    ) -> DraftResult: ...


class WorkerClaimRejected(RuntimeError):
    pass


def parse_draft_link_intent(text: str) -> DraftLinkIntent | None:
    clean = " ".join(str(text).strip().split())
    if not clean or len(clean) > MAX_DRAFT_REQUEST_CHARS:
        return None
    match = _DRAFT_LINK.fullmatch(clean)
    if match is None:
        return None
    request = match.group("request").strip(" ,.;:")
    if len(request) < 3 or _DRAFT_CANCELLATION.search(request):
        return None
    return DraftLinkIntent(request=request)


def parse_live_work_intent(text: str) -> LiveWorkIntent | None:
    """Accept direct build instructions without treating questions as jobs."""

    clean = " ".join(str(text).strip().split())
    clean = _WAKE_PREFIX.sub("", clean, count=1).strip()
    if not clean or len(clean) > MAX_DRAFT_REQUEST_CHARS:
        return None
    relay = _LIVE_RELAY.fullmatch(clean)
    if relay is not None:
        request = relay.group("request").strip(" ,.;:")
        if request and not _DRAFT_CANCELLATION.search(request):
            return LiveWorkIntent(request=request)
    if _DRAFT_CANCELLATION.search(clean):
        return None
    match = _LIVE_WORK.fullmatch(clean)
    if match is not None:
        return LiveWorkIntent(request=match.group("request").strip())
    passive = _LIVE_WORK_PASSIVE.fullmatch(clean)
    if passive is not None:
        return LiveWorkIntent(request=passive.group("request").strip())
    return None


def parse_code_panel_intent(text: str) -> CodePanelIntent | None:
    """Recognize direct show and hide commands for the desk coding panel."""

    clean = " ".join(str(text).strip().split())
    clean = _WAKE_PREFIX.sub("", clean, count=1).strip()
    if not clean or len(clean) > 300 or _CODE_PANEL_TARGET.search(clean) is None:
        return None
    if _CODE_PANEL_HIDE.search(clean):
        return CodePanelIntent("hide")
    if _CODE_PANEL_OPEN.search(clean):
        return CodePanelIntent("open")
    return None


class HeadlessClaudeDraftRunner:
    """Generate text only. The worker receives no tools and cannot edit files."""

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        model: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        self.claude_bin = claude_bin
        self.model = model or os.environ.get("SERENA_CALL_JOB_MODEL", "sonnet")
        self.timeout = timeout

    def run(
        self,
        job: WorkJob,
        *,
        worker_session_id: str,
        adopt_worker: Callable[[int], bool],
        record_billing: Callable[[dict[str, Any]], bool] | None = None,
    ) -> DraftResult:
        worker_session_id = str(uuid.UUID(worker_session_id))
        result_path, error_path = self._spool_paths(job, worker_session_id)
        completed = self._read_result(result_path, worker_session_id)
        if completed is not None and self._replay_evidence_matches(job, worker_session_id):
            return completed
        env = strip_metered_auth_env(os.environ)
        prompt = (
            "Create the requested draft as clean Markdown. Return only the finished "
            "artifact, with no preamble, no analysis, no code fence, and no offer to "
            "do more. Do not claim you sent, published, or changed anything.\n\n"
            "<call-job-context>"
            + json.dumps(
                {
                    "job_id": job.job_id,
                    "call_id": job.call_id,
                    "turn_id": job.turn_id,
                },
                separators=(",", ":"),
            )
            + "</call-job-context>\n\nRequest: "
            + job.request
        )
        command = [
            self.claude_bin,
            "-p",
            prompt,
            "--model",
            self.model,
            "--effort",
            "low",
            "--output-format",
            "json",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--session-id",
            worker_session_id,
        ]
        env["SERENA_WORKER_STDOUT_LIMIT"] = str(MAX_WORKER_STDOUT_BYTES)
        env["SERENA_WORKER_STDERR_LIMIT"] = str(MAX_WORKER_STDERR_BYTES)
        HEADLESS_JOB_CWD.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            HEADLESS_JOB_CWD.chmod(0o700)
        spool_directory = result_path.parent
        spool_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            spool_directory.chmod(0o700)
        result_path.unlink(missing_ok=True)
        error_path.unlink(missing_ok=True)
        attestation_path = spool_directory / (
            f"{uuid.UUID(job.job_id)}.{uuid.UUID(worker_session_id)}.attestation.json"
        )
        attestation_path.unlink(missing_ok=True)
        env["SERENA_WORKER_ATTESTATION_PATH"] = str(attestation_path)
        expected_command_hash = command_fingerprint(command)
        expected_gate_environment_hash = environment_fingerprint(env)
        output_fd = os.open(result_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        error_fd = os.open(error_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(output_fd, "wb") as output, os.fdopen(error_fd, "wb") as errors:
            process = subprocess.Popen(
                [sys.executable, str(_WORKER_GATE), *command],
                cwd=str(HEADLESS_JOB_CWD),
                env=env,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=errors,
                start_new_session=True,
            )
            try:
                if not adopt_worker(process.pid):
                    raise WorkerClaimRejected("draft worker reservation was superseded")
                assert process.stdin is not None
                process.stdin.write(b"A")
                process.stdin.flush()
                evidence = self._read_attestation(
                    attestation_path, process, expected_stage="authenticated"
                )
                if (
                    evidence.get("schema_version") != 3
                    or evidence.get("stage") != "authenticated"
                    or evidence.get("gate_pid") != process.pid
                    or evidence.get("worker_token") != process_start_token(process.pid)
                    or evidence.get("worker_session_id") != worker_session_id
                    or evidence.get("command_sha256") != expected_command_hash
                    or evidence.get("environment_sha256") != expected_gate_environment_hash
                    or evidence.get("ok") is not True
                ):
                    raise RuntimeError("draft worker attestation did not match the adopted gate")
                if record_billing is not None and not record_billing(evidence):
                    raise WorkerClaimRejected("draft worker billing reservation was superseded")
                process.stdin.write(b"G")
                process.stdin.flush()
                final_evidence = self._read_attestation(
                    attestation_path, process, expected_stage="exec_ready"
                )
                expected_exec_environment = {
                    **env,
                    "SERENA_WORKER_GATE_PID": str(process.pid),
                }
                exec_pid = final_evidence.get("exec_pid")
                expected_containment = (
                    "windows_kill_on_close_job" if os.name == "nt" else "linux_pdeathsig"
                )
                if (
                    final_evidence.get("schema_version") != 3
                    or final_evidence.get("stage") != "exec_ready"
                    or final_evidence.get("gate_pid") != process.pid
                    or final_evidence.get("worker_token") != process_start_token(process.pid)
                    or final_evidence.get("worker_session_id") != worker_session_id
                    or final_evidence.get("command_sha256") != expected_command_hash
                    or final_evidence.get("environment_sha256")
                    != environment_fingerprint(expected_exec_environment)
                    or not isinstance(exec_pid, int)
                    or isinstance(exec_pid, bool)
                    or final_evidence.get("exec_token") != process_start_token(exec_pid)
                    or final_evidence.get("containment") != expected_containment
                    or final_evidence.get("ok") is not True
                ):
                    raise RuntimeError("draft worker exec identity was not contained and bound")
                if record_billing is not None and not record_billing(final_evidence):
                    raise WorkerClaimRejected("draft worker exec reservation was superseded")
                process.stdin.write(b"E")
                process.stdin.flush()
                process.stdin.close()
                process.stdin = None
                try:
                    return_code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self._stop_gate(process, force=True)
                    raise RuntimeError("draft worker timed out") from None
            except BaseException:
                self._stop_gate(process)
                raise
            finally:
                attestation_path.unlink(missing_ok=True)
        if return_code != 0:
            error = _read_tail(error_path, MAX_WORKER_STDERR_BYTES)
            raise RuntimeError((error.strip() or "worker failed")[-500:])
        completed = self._read_result(result_path, worker_session_id)
        if completed is None:
            raise RuntimeError("draft worker returned malformed JSON")
        return completed

    @staticmethod
    def _read_attestation(
        path: Path,
        process: subprocess.Popen[bytes],
        *,
        expected_stage: str,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                if process.poll() is not None:
                    break
                time.sleep(0.01)
                continue
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"draft worker attestation could not be read: {exc}") from exc
            if isinstance(payload, dict):
                if payload.get("stage") == expected_stage:
                    return payload
                if process.poll() is None:
                    time.sleep(0.01)
                    continue
            break
        raise RuntimeError("draft worker did not produce a valid subscription attestation")

    @staticmethod
    def _stop_gate(process: subprocess.Popen[bytes], *, force: bool = False) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt" and force:
            with suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
        elif os.name != "nt" and force:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            with suppress(ProcessLookupError):
                process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                process.kill()
            process.wait(timeout=5)

    @staticmethod
    def _replay_evidence_matches(job: WorkJob, worker_session_id: str) -> bool:
        evidence = job.billing_evidence
        return bool(
            isinstance(evidence, dict)
            and evidence.get("schema_version") == 3
            and evidence.get("stage") == "exec_ready"
            and evidence.get("ok") is True
            and evidence.get("worker_session_id") == worker_session_id
            and evidence.get("auth_mode") == "subscription_oauth_guarded"
            and evidence.get("logged_in") is True
            and evidence.get("auth_method") == "claude.ai"
            and evidence.get("api_provider") == "firstParty"
            and evidence.get("subscription_type") == "max"
            and evidence.get("api_key_present") is False
            and evidence.get("metered_auth_env_present") == []
            and evidence.get("setting_sources") == []
            and evidence.get("containment") in {"linux_pdeathsig", "windows_kill_on_close_job"}
            and isinstance(evidence.get("exec_pid"), int)
            and isinstance(evidence.get("exec_token"), str)
            and bool(evidence.get("exec_token"))
        )

    @staticmethod
    def _spool_paths(job: WorkJob, worker_session_id: str) -> tuple[Path, Path]:
        stem = f"{uuid.UUID(job.job_id)}.{uuid.UUID(worker_session_id)}"
        directory = HEADLESS_JOB_CWD / ".results"
        return directory / f"{stem}.json", directory / f"{stem}.stderr"

    @staticmethod
    def _read_result(path: Path, worker_session_id: str) -> DraftResult | None:
        try:
            if path.stat().st_size > MAX_WORKER_STDOUT_BYTES:
                return None
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None
        content = payload.get("result") if isinstance(payload, dict) else None
        reported_session = payload.get("session_id") if isinstance(payload, dict) else None
        if reported_session != worker_session_id:
            return None
        if not isinstance(content, str) or not content.strip():
            return None
        clean = content.strip()
        if len(clean) > MAX_DRAFT_OUTPUT_CHARS:
            return None
        return DraftResult(content=clean + "\n", worker_session_id=worker_session_id)


class CallTaskDispatcher:
    def __init__(
        self,
        *,
        store: WorkJobStore | None = None,
        artifacts: ArtifactRegistry | None = None,
        runner: DraftRunner | None = None,
        voice_inbox: VoiceInboxStore | None = None,
        executor: Executor | None = None,
        recover: bool = True,
    ) -> None:
        self.store = store or WorkJobStore()
        self.artifacts = artifacts or get_default_artifact_registry()
        self.runner = runner or HeadlessClaudeDraftRunner()
        self.voice_inbox = voice_inbox
        self.executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="serena-call-job"
        )
        self._scheduled: set[str] = set()
        self._lock = threading.Lock()
        if recover:
            for job in self.store.recoverable():
                self._schedule(job)

    def submit_if_explicit(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
    ) -> WorkJob | None:
        intent = parse_draft_link_intent(text)
        if intent is None:
            return None
        job, _created = self.store.create_draft_link(
            call_id=call_id,
            turn_id=turn_id,
            request=intent.request,
        )
        if job.state not in {"artifact_ready", "failed", "cancelled"}:
            self._schedule(job)
        return self.store.get(job.job_id) or job

    def submit_live_work_if_explicit(
        self,
        text: str,
        *,
        call_id: str,
        turn_id: str,
        context: list[str] | None = None,
    ) -> VoiceInboxItem | None:
        intent = parse_live_work_intent(text)
        if intent is None:
            return None
        request = intent.request
        prior = [" ".join(str(item).split()) for item in (context or [])]
        prior = [item for item in prior if item][-6:]
        if prior:
            labelled = [
                item
                if item.lower().startswith(("raghav:", "serena:"))
                else f"Raghav: {item}"
                for item in prior
            ]
            request = (
                "Recent voice context:\n"
                + "\n".join(labelled)
                + "\n\nCurrent spoken request: "
                + request
            )
        inbox = self.voice_inbox or get_default_voice_inbox()
        if not inbox.resident_lease_active():
            raise RuntimeError("my private coding runtime is not available")
        return inbox.enqueue(
            request,
            call_id=call_id,
            turn_id=turn_id,
        )

    def link_origin_session(self, job_id: str, session_id: str) -> None:
        self.store.link_origin_session(job_id, session_id)

    def events_for_call(self, call_id: str, *, after: int = 0) -> list[WorkJobEvent]:
        self._recover_pending()
        return self.store.events_for_call(call_id, after=after)

    def event_for_call(self, call_id: str, event_seq: int) -> WorkJobEvent | None:
        return self.store.event_for_call(call_id, event_seq)

    def consume_artifact_receipt(
        self,
        receipt: str,
        *,
        job_id: str,
        artifact_id: str,
        sha256: str,
    ) -> bool:
        return self.artifacts.consume_receipt(
            receipt,
            job_id=job_id,
            artifact_id=artifact_id,
            sha256=sha256,
        )

    def _recover_pending(self) -> None:
        for job in self.store.recoverable():
            self._schedule(job)

    def _schedule(self, job: WorkJob) -> None:
        with self._lock:
            if job.job_id in self._scheduled:
                return
            self._scheduled.add(job.job_id)
        try:
            self.executor.submit(self._run_job, job.job_id)
        except Exception:
            with self._lock:
                self._scheduled.discard(job.job_id)
            raise

    def _run_job(self, job_id: str) -> None:
        stored = self.store.get(job_id)
        worker_session_id = (
            stored.worker_session_id
            if stored is not None and stored.worker_session_id
            else str(uuid.uuid4())
        )
        try:
            job = self.store.mark_running(job_id, worker_session_id)
            if job is None:
                return
            result = self.runner.run(
                job,
                worker_session_id=worker_session_id,
                adopt_worker=lambda pid: self.store.adopt_worker(
                    job.job_id,
                    worker_session_id=worker_session_id,
                    owner_pid=os.getpid(),
                    worker_pid=pid,
                ),
                record_billing=lambda evidence: self.store.record_worker_billing(
                    job.job_id,
                    worker_session_id=worker_session_id,
                    worker_pid=int(evidence.get("gate_pid") or 0),
                    evidence=evidence,
                ),
            )
            worker_session_id = result.worker_session_id
            output = self.artifacts.write_job_artifact(
                job_id=job.job_id,
                name="draft.md",
                content=result.content,
            )
            artifact = self.artifacts.register(
                job_id=job.job_id,
                path=output,
                name="draft.md",
            )
            self.store.mark_artifact_ready(
                job.job_id,
                artifact_id=artifact.artifact_id,
                name=artifact.name,
                url=artifact.url,
                sha256=artifact.sha256,
                size=artifact.size,
                worker_session_id=worker_session_id,
            )
        except WorkerClaimRejected:
            return
        except Exception as exc:
            with suppress(KeyError, ValueError):
                self.store.mark_failed(job_id, str(exc))
        finally:
            with self._lock:
                self._scheduled.discard(job_id)


_DEFAULT_DISPATCHER: CallTaskDispatcher | None = None
_DEFAULT_DISPATCHER_LOCK = threading.Lock()


def _read_tail(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            if size > limit:
                handle.seek(size - limit)
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def get_default_task_dispatcher() -> CallTaskDispatcher:
    global _DEFAULT_DISPATCHER
    if _DEFAULT_DISPATCHER is not None:
        return _DEFAULT_DISPATCHER
    with _DEFAULT_DISPATCHER_LOCK:
        if _DEFAULT_DISPATCHER is None:
            _DEFAULT_DISPATCHER = CallTaskDispatcher()
    return _DEFAULT_DISPATCHER
