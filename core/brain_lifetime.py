"""Bounded resident-brain continuity and lifecycle telemetry.

Claude session JSONLs remain the searchable transcript. This module owns only
the small handoff needed to start a deliberately fresh SDK session without
losing the immediate conversational thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import subprocess
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "serena"
DEFAULT_JOURNAL_PATH = DEFAULT_STATE_DIR / "brain-thread.json"
DEFAULT_LIFETIME_PATH = DEFAULT_STATE_DIR / "brain-lifetime.json"
SCHEMA_VERSION = 1
BRAIN_SDK_ROLE_ENV = "SERENA_BRAIN_PROCESS_ROLE"
BRAIN_SDK_ROLE = "resident-sdk"
BRAIN_SDK_TOKEN_ENV = "SERENA_BRAIN_PROCESS_TOKEN"
_SECURED_DIRECTORIES: set[Path] = set()


def _state_path(environment_name: str, default: Path) -> Path:
    raw = os.environ.get(environment_name)
    return Path(raw).expanduser().resolve() if raw else default


def journal_path() -> Path:
    return _state_path("SERENA_BRAIN_THREAD_PATH", DEFAULT_JOURNAL_PATH)


def lifetime_path() -> Path:
    return _state_path("SERENA_BRAIN_LIFETIME_PATH", DEFAULT_LIFETIME_PATH)


def _windows_current_principal() -> str:
    try:
        result = subprocess.run(
            ["whoami"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    domain = os.environ.get("USERDOMAIN", "")
    username = os.environ.get("USERNAME", "")
    return f"{domain}\\{username}" if domain and username else username


def _secure_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return
    principal = _windows_current_principal()
    if not principal:
        raise RuntimeError("cannot identify Windows user for brain state ACL")
    result = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{principal}:(F)",
            "/grant:r",
            "*S-1-5-18:(F)",
            "/grant:r",
            "*S-1-5-32-544:(F)",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to restrict brain state ACL: {path}")


def secure_directory(path: Path) -> Path:
    """Create one user-only state directory and cache its verified ACL."""

    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path in _SECURED_DIRECTORIES:
        return path
    if os.name != "nt":
        path.chmod(0o700)
        _SECURED_DIRECTORIES.add(path)
        return path
    principal = _windows_current_principal()
    if not principal:
        raise RuntimeError("cannot identify Windows user for brain state directory ACL")
    result = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{principal}:(OI)(CI)(F)",
            "/grant:r",
            "*S-1-5-18:(OI)(CI)(F)",
            "/grant:r",
            "*S-1-5-32-544:(OI)(CI)(F)",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to restrict brain state directory ACL: {path}")
    _SECURED_DIRECTORIES.add(path)
    return path


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    secure_directory(path.parent)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        os.close(descriptor)
        if os.name != "nt":
            _secure_file(temporary)
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            _secure_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_text_atomic(path: Path, value: str) -> Path:
    """Atomically write user-only text and return its resolved path."""

    path = path.expanduser().resolve()
    secure_directory(path.parent)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    os.close(descriptor)
    try:
        _secure_file(temporary)
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(value)
            if value and not value.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _secure_file(path)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def append_json_line(path: Path, value: dict[str, Any]) -> None:
    """Append one durable JSON event to a user-only NDJSON file."""

    path = path.expanduser().resolve()
    secure_directory(path.parent)
    descriptor = os.open(
        path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        if os.name != "nt":
            _secure_file(path)
        payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while appending brain lifetime event")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default
    return value if isinstance(value, dict) else default


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


class RecentThreadJournal:
    """A fixed-size, delivery-aware handoff between clean SDK sessions."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_entries: int = 12,
        max_characters: int = 16_000,
    ) -> None:
        if max_entries < 1 or max_characters < 1:
            raise ValueError("journal bounds must be positive")
        self.path = (path or journal_path()).expanduser().resolve()
        self.max_entries = max_entries
        self.max_characters = max_characters

    def append_pending(
        self,
        *,
        user_text: str,
        assistant_text: str,
        protocol: str,
        model: str,
        session_id: str | None,
        turn_id: str | None = None,
        call_id: str | None = None,
        ledger_fingerprint: str | None = None,
        now: float | None = None,
    ) -> str:
        document = self._load()
        entry_id = str(uuid.uuid4())
        document["entries"].append(
            {
                "id": entry_id,
                "at": time.time() if now is None else now,
                "delivered": False,
                "transport_uncertain": False,
                "protocol": _bounded_text(protocol, 32),
                "model": _bounded_text(model, 128),
                "session_id": _bounded_text(session_id, 64) or None,
                "turn_id": _bounded_text(turn_id, 128) or None,
                "call_id": _bounded_text(call_id, 128) or None,
                "ledger_fingerprint": _bounded_text(ledger_fingerprint, 128) or None,
                "user": _bounded_text(user_text, 2_000),
                "assistant": _bounded_text(assistant_text, 4_000),
            }
        )
        self._prune(document)
        write_json_atomic(self.path, document)
        return entry_id

    def mark_delivered(self, entry_id: str) -> bool:
        document = self._load()
        found = False
        for entry in document["entries"]:
            if entry.get("id") == entry_id:
                entry["delivered"] = True
                found = True
                break
        if found:
            write_json_atomic(self.path, document)
        return found

    def discard(self, entry_id: str) -> bool:
        """Remove a response that never reached its surface."""

        document = self._load()
        before = len(document["entries"])
        document["entries"] = [
            entry for entry in document["entries"] if entry.get("id") != entry_id
        ]
        if len(document["entries"]) == before:
            return False
        write_json_atomic(self.path, document)
        return True

    def delivery_committed(self, entry_id: str) -> bool:
        return any(
            entry.get("id") == entry_id and bool(entry.get("delivered"))
            for entry in self._load()["entries"]
        )

    def mark_transport_uncertain(self, entry_id: str) -> bool:
        """Keep a committed reply when transport outcome cannot be known."""

        document = self._load()
        for entry in document["entries"]:
            if entry.get("id") != entry_id or not entry.get("delivered"):
                continue
            entry["transport_uncertain"] = True
            write_json_atomic(self.path, document)
            return True
        return False

    def discard_undelivered(self) -> int:
        """Drop stale pre-crash responses before building a fresh handoff."""

        document = self._load()
        before = len(document["entries"])
        document["entries"] = [entry for entry in document["entries"] if entry.get("delivered")]
        discarded = before - len(document["entries"])
        if discarded:
            write_json_atomic(self.path, document)
        return discarded

    def clear(self) -> int:
        """Atomically remove a fully inspected synthetic or obsolete handoff."""

        document = self._load()
        removed = len(document["entries"])
        if removed:
            write_json_atomic(
                self.path,
                {"schema_version": SCHEMA_VERSION, "entries": []},
            )
        return removed

    def render_handoff(self) -> str:
        entries = [entry for entry in self._load()["entries"] if entry.get("delivered")]
        if not entries:
            return ""
        opening = [
            "<recent-resident-thread>",
            "Current ledger state wins if this bounded handoff conflicts.",
        ]
        closing = "</recent-resident-thread>"
        base = "\n".join([*opening, closing])
        if len(base) > self.max_characters:
            return ""

        blocks: list[str] = []
        remaining = self.max_characters - len(base)
        for entry in reversed(entries):
            timestamp = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(float(entry.get("at") or 0))
            )
            protocol = entry.get("protocol") or "plain"
            delivery = " delivery-uncertain" if entry.get("transport_uncertain") else ""
            block = "\n".join(
                [
                    f"[{timestamp} via {protocol}{delivery}]",
                    f"raghav: {entry.get('user', '')}",
                    f"serena: {entry.get('assistant', '')}",
                ]
            )
            required = len(block) + 1
            if required <= remaining:
                blocks.insert(0, block)
                remaining -= required
                continue
            if not blocks and remaining > 2:
                blocks.append(block[: remaining - 2].rstrip() + "…")
            break
        return "\n".join([*opening, *blocks, closing])

    def snapshot(self) -> dict[str, Any]:
        document = self._load()
        entries = document["entries"]
        return {
            "path": str(self.path),
            "entries": len(entries),
            "delivered_entries": sum(bool(row.get("delivered")) for row in entries),
            "pending_entries": sum(not bool(row.get("delivered")) for row in entries),
            "transport_uncertain_entries": sum(
                bool(row.get("transport_uncertain")) for row in entries
            ),
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
            "max_entries": self.max_entries,
            "max_characters": self.max_characters,
        }

    def _load(self) -> dict[str, Any]:
        document = read_json(self.path, {"schema_version": SCHEMA_VERSION, "entries": []})
        if document.get("schema_version") != SCHEMA_VERSION or not isinstance(
            document.get("entries"), list
        ):
            return {"schema_version": SCHEMA_VERSION, "entries": []}
        document["entries"] = [row for row in document["entries"] if isinstance(row, dict)]
        return document

    def _prune(self, document: dict[str, Any]) -> None:
        kept: list[dict[str, Any]] = []
        characters = 0
        for entry in reversed(document["entries"]):
            size = len(str(entry.get("user") or "")) + len(str(entry.get("assistant") or ""))
            if kept and characters + size > self.max_characters:
                continue
            kept.append(entry)
            characters += size
            if len(kept) >= self.max_entries:
                break
        document["entries"] = list(reversed(kept))


@dataclass(frozen=True, slots=True)
class RotationPolicy:
    max_age_seconds: float = 6 * 60 * 60
    max_completed_turns: int = 100
    max_context_percentage: float = 70.0
    rotate_on_compaction: bool = True
    max_child_rss_growth_bytes: int = 256 * 1024 * 1024
    max_child_rss_multiplier: float = 2.0


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """A PID bound to its creation time so reuse can never target a stranger."""

    pid: int
    create_time: float


def rotation_reason(
    policy: RotationPolicy,
    *,
    age_seconds: float,
    completed_turns: int,
    context_percentage: float | None,
    compact_boundary_seen: bool,
    child_rss_bytes: int | None,
    baseline_child_rss_bytes: int | None,
) -> str | None:
    if compact_boundary_seen and policy.rotate_on_compaction:
        return "compact_boundary"
    if context_percentage is not None and context_percentage >= policy.max_context_percentage:
        return "context_percentage"
    if completed_turns >= policy.max_completed_turns:
        return "completed_turns"
    if age_seconds >= policy.max_age_seconds:
        return "session_age"
    if child_rss_bytes is not None and baseline_child_rss_bytes:
        growth = child_rss_bytes - baseline_child_rss_bytes
        if growth >= policy.max_child_rss_growth_bytes:
            return "child_rss_growth"
        if child_rss_bytes >= baseline_child_rss_bytes * policy.max_child_rss_multiplier:
            return "child_rss_multiplier"
    return None


class LifetimeLedger:
    """Small audit record linking each fresh SDK session to a bounded epoch."""

    def __init__(self, path: Path | None = None, *, max_epochs: int = 64) -> None:
        self.path = (path or lifetime_path()).expanduser().resolve()
        self.max_epochs = max_epochs

    def start_epoch(
        self,
        session_id: str,
        *,
        reason: str,
        process_token: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        document = self._load()
        timestamp = time.time() if now is None else now
        if document["epochs"] and document["epochs"][-1].get("ended_at") is None:
            document["epochs"][-1]["ended_at"] = timestamp
            document["epochs"][-1]["end_reason"] = "superseded"
        epoch = {
            "number": int(document.get("next_epoch") or 1),
            "session_id": session_id,
            "process_token": process_token,
            "started_at": timestamp,
            "ended_at": None,
            "start_reason": reason,
            "end_reason": None,
            "completed_turns": 0,
            "compactions": 0,
            "last_context": None,
            "last_process": None,
        }
        document["next_epoch"] = epoch["number"] + 1
        document["epochs"].append(epoch)
        document["epochs"] = document["epochs"][-self.max_epochs :]
        write_json_atomic(self.path, document)
        return dict(epoch)

    def record_turn(
        self,
        *,
        context: dict[str, Any] | None,
        process: dict[str, Any] | None,
        compact_boundary_seen: bool,
    ) -> dict[str, Any]:
        document = self._load()
        if not document["epochs"]:
            raise RuntimeError("brain lifetime epoch has not started")
        epoch = document["epochs"][-1]
        epoch["completed_turns"] = int(epoch.get("completed_turns") or 0) + 1
        if compact_boundary_seen:
            epoch["compactions"] = int(epoch.get("compactions") or 0) + 1
        epoch["last_context"] = context
        epoch["last_process"] = process
        write_json_atomic(self.path, document)
        return dict(epoch)

    def end_epoch(self, reason: str, *, now: float | None = None) -> None:
        document = self._load()
        if document["epochs"]:
            epoch = document["epochs"][-1]
            if epoch.get("ended_at") is None:
                epoch["ended_at"] = time.time() if now is None else now
                epoch["end_reason"] = reason
                write_json_atomic(self.path, document)

    def snapshot(self) -> dict[str, Any]:
        document = self._load()
        active = document["epochs"][-1] if document["epochs"] else None
        return {
            "path": str(self.path),
            "epoch_count": len(document["epochs"]),
            "active": active,
            "epochs": [
                {
                    key: row.get(key)
                    for key in (
                        "number",
                        "session_id",
                        "process_token",
                        "started_at",
                        "ended_at",
                        "start_reason",
                        "end_reason",
                        "completed_turns",
                        "compactions",
                    )
                }
                for row in document["epochs"]
            ],
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def _load(self) -> dict[str, Any]:
        document = read_json(
            self.path,
            {"schema_version": SCHEMA_VERSION, "next_epoch": 1, "epochs": []},
        )
        if document.get("schema_version") != SCHEMA_VERSION or not isinstance(
            document.get("epochs"), list
        ):
            return {"schema_version": SCHEMA_VERSION, "next_epoch": 1, "epochs": []}
        document["epochs"] = [row for row in document["epochs"] if isinstance(row, dict)]
        return document


def process_tree_snapshot(pid: int | None = None) -> dict[str, Any]:
    """Return bounded process metrics when psutil is installed."""

    try:
        import psutil
    except ImportError:
        return {"available": False, "error": "psutil not installed"}
    root_pid = os.getpid() if pid is None else pid
    try:
        root = psutil.Process(root_pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.Error as exc:
        return {"available": False, "error": str(exc)}
    rows: list[dict[str, Any]] = []
    for process in processes:
        try:
            memory = process.memory_info()
            try:
                full_memory = process.memory_full_info()
                uss_bytes = int(getattr(full_memory, "uss", 0)) or None
                pss_bytes = int(getattr(full_memory, "pss", 0)) or None
            except psutil.Error:
                uss_bytes = None
                pss_bytes = None
            rows.append(
                {
                    "pid": process.pid,
                    "ppid": process.ppid(),
                    "name": process.name(),
                    "create_time": process.create_time(),
                    "rss_bytes": int(memory.rss),
                    "uss_bytes": uss_bytes,
                    "pss_bytes": pss_bytes,
                    "threads": process.num_threads(),
                    "fds": process.num_fds() if hasattr(process, "num_fds") else None,
                    "handles": (process.num_handles() if hasattr(process, "num_handles") else None),
                }
            )
        except psutil.Error:
            continue
    return {
        "available": True,
        "root_pid": root_pid,
        "processes": rows,
        "rss_bytes": sum(row["rss_bytes"] for row in rows),
        "uss_bytes": sum(int(row.get("uss_bytes") or 0) for row in rows) or None,
        "pss_bytes": sum(int(row.get("pss_bytes") or 0) for row in rows) or None,
        "threads": sum(int(row.get("threads") or 0) for row in rows),
        "fds": sum(int(row.get("fds") or 0) for row in rows),
        "handles": sum(int(row.get("handles") or 0) for row in rows),
        "descendants": max(0, len(rows) - 1),
    }


def child_rss_bytes(snapshot: dict[str, Any]) -> int | None:
    if not snapshot.get("available"):
        return None
    root_pid = snapshot.get("root_pid")
    return sum(
        int(row.get("rss_bytes") or 0)
        for row in snapshot.get("processes", [])
        if row.get("pid") != root_pid
    )


def process_identities(snapshot: dict[str, Any]) -> list[ProcessIdentity]:
    root_pid = snapshot.get("root_pid")
    identities = []
    for row in snapshot.get("processes", []):
        try:
            pid = int(row["pid"])
            create_time = float(row["create_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid > 0 and pid != os.getpid() and pid != root_pid:
            identities.append(ProcessIdentity(pid=pid, create_time=create_time))
    return identities


def brain_sdk_process_snapshot(
    active_token: str | None = None,
    *,
    known_tokens: Iterable[str] = (),
) -> dict[str, Any]:
    """Find every marked resident SDK process, including reparented orphans."""

    try:
        import psutil
    except ImportError:
        return {"available": False, "error": "psutil not installed"}
    rows: list[dict[str, Any]] = []
    scan_errors = 0
    expected_tokens = {str(token) for token in known_tokens if str(token)}
    if active_token:
        expected_tokens.add(active_token)
    for process in psutil.process_iter(attrs=("pid", "name")):
        try:
            name = str(process.info.get("name") or "")
            if not name.lower().startswith("claude"):
                continue
            command = process.cmdline()
            token = next((item for item in command if item in expected_tokens), None)
            if token is None:
                continue
            memory = process.memory_info()
            rows.append(
                {
                    "pid": int(process.info["pid"]),
                    "ppid": int(process.ppid()),
                    "name": name,
                    "create_time": float(process.create_time()),
                    "rss_bytes": int(getattr(memory, "rss", 0)),
                    "process_token": token,
                    "active": bool(active_token and token == active_token),
                }
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            psutil.AccessDenied,
            psutil.NoSuchProcess,
            psutil.ZombieProcess,
        ):
            continue
        except psutil.Error:
            scan_errors += 1
    tokens = sorted({str(row["process_token"]) for row in rows if row.get("process_token")})
    active_rows = [row for row in rows if row.get("active")]
    stale_rows = [row for row in rows if not row.get("active")]
    return {
        "available": True,
        "role": BRAIN_SDK_ROLE,
        "active_token": active_token,
        "known_tokens": sorted(expected_tokens),
        "processes": rows,
        "process_count": len(rows),
        "tokens": tokens,
        "token_count": len(tokens),
        "active_processes": len(active_rows),
        "stale_processes": len(stale_rows),
        "stale_tokens": sorted(
            {str(row["process_token"]) for row in stale_rows if row.get("process_token")}
        ),
        "scan_errors": scan_errors,
    }


def brain_sdk_process_identities(
    process_token: str,
) -> list[ProcessIdentity]:
    snapshot = brain_sdk_process_snapshot(process_token, known_tokens=(process_token,))
    identities = []
    for row in snapshot.get("processes", []):
        if row.get("process_token") != process_token:
            continue
        try:
            identities.append(
                ProcessIdentity(pid=int(row["pid"]), create_time=float(row["create_time"]))
            )
        except (KeyError, TypeError, ValueError):
            continue
    return identities


async def reap_processes(
    identities: Iterable[ProcessIdentity],
    *,
    timeout_seconds: float = 5.0,
    terminate_seconds: float = 2.0,
    kill_seconds: float = 2.0,
) -> list[int]:
    """Reap confirmed identities within a fixed async wall-clock budget."""

    try:
        import psutil
    except ImportError:
        return []

    unique = {
        (identity.pid, identity.create_time): identity
        for identity in identities
        if identity.pid > 0 and identity.pid != os.getpid()
    }

    def matching_process(identity: ProcessIdentity):
        try:
            process = psutil.Process(identity.pid)
            if abs(process.create_time() - identity.create_time) > 0.01:
                return None
            if process.status() == psutil.STATUS_ZOMBIE:
                return None
            return process
        except psutil.NoSuchProcess:
            return None
        except psutil.Error:
            return False

    def live_pids() -> list[int]:
        live = []
        for identity in unique.values():
            process = matching_process(identity)
            if process is False or process is not None:
                live.append(identity.pid)
        return live

    async def wait_until(deadline: float) -> bool:
        while time.monotonic() < deadline:
            if not live_pids():
                return True
            await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return not live_pids()

    if await wait_until(time.monotonic() + max(0.0, timeout_seconds)):
        return []

    for identity in unique.values():
        process = matching_process(identity)
        if process is False or process is None:
            continue
        with contextlib.suppress(psutil.Error):
            process.terminate()
    if await wait_until(time.monotonic() + max(0.0, terminate_seconds)):
        return []

    for identity in unique.values():
        process = matching_process(identity)
        if process is False or process is None:
            continue
        with contextlib.suppress(psutil.Error):
            process.kill()
    await wait_until(time.monotonic() + max(0.0, kill_seconds))
    return live_pids()


def policy_from_environment() -> RotationPolicy:
    return RotationPolicy(
        max_age_seconds=float(os.environ.get("SERENA_BRAIN_EPOCH_SECONDS", 6 * 60 * 60)),
        max_completed_turns=int(os.environ.get("SERENA_BRAIN_EPOCH_TURNS", 100)),
        max_context_percentage=float(os.environ.get("SERENA_BRAIN_CONTEXT_ROTATE_PERCENT", 70)),
        rotate_on_compaction=os.environ.get("SERENA_BRAIN_ROTATE_ON_COMPACT", "1").lower()
        not in {"0", "false", "no"},
        max_child_rss_growth_bytes=int(
            os.environ.get("SERENA_BRAIN_CHILD_RSS_GROWTH_BYTES", 256 * 1024 * 1024)
        ),
        max_child_rss_multiplier=float(os.environ.get("SERENA_BRAIN_CHILD_RSS_MULTIPLIER", 2.0)),
    )


def policy_snapshot(policy: RotationPolicy) -> dict[str, Any]:
    return asdict(policy)
