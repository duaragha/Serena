"""Durable, user-visible transcript for Serena's spoken and typed turns.

The resident journal is deliberately small and delivery-oriented. This module
owns the separate lifelong transcript consumed by the normal chats indexer.
Only the text Raghav supplied and Serena's final visible text are written.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from core.brain_lifetime import secure_directory, write_text_atomic
from core.parser import SessionMeta, _extract_text

VOICE_SESSION_ID = "serena-voice-main"
VOICE_PROJECT_DIR = "serena-voice"
DEFAULT_VOICE_CHAT_ROOT = Path.home() / ".local" / "state" / "serena" / "voice-chats"
DEFAULT_VOICE_TRANSCRIPT_PATH = DEFAULT_VOICE_CHAT_ROOT / "serena-main.jsonl"
_SCHEMA_VERSION = 1
_SEED_MARKER_NAME = ".recent-journal-seeded-v1"
_PROCESS_LOCK = threading.RLock()


def voice_transcript_path() -> Path:
    raw = os.environ.get("SERENA_VOICE_TRANSCRIPT_PATH")
    return (
        Path(raw).expanduser().resolve()
        if raw
        else DEFAULT_VOICE_TRANSCRIPT_PATH.expanduser().resolve()
    )


def _clean(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _timestamp(value: datetime | str | float | int | None) -> str:
    if isinstance(value, datetime):
        current = value
    elif isinstance(value, (float, int)):
        current = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            current = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            current = datetime.now(timezone.utc)
    else:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def infer_surface(call_id: str | None, explicit: str | None = None) -> str:
    surface = _clean(explicit, 32).casefold()
    if surface:
        return surface
    call = _clean(call_id, 128).casefold()
    if call.startswith("desk-typed-"):
        return "desk-typed"
    if call.startswith("desk-"):
        return "desk-voice"
    if call:
        return "phone"
    return "voice"


def _turn_key(
    *,
    call_id: str | None,
    turn_id: str | None,
    fallback: str | None = None,
) -> str:
    call = _clean(call_id, 128)
    turn = _clean(turn_id, 128)
    if call or turn:
        return f"voice:{call}:{turn}"
    if fallback:
        return f"journal:{_clean(fallback, 128)}"
    return f"generated:{uuid.uuid4()}"


@contextmanager
def _exclusive_file_lock(path: Path):
    secure_directory(path.parent)
    handle = path.open("a+b", buffering=0)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    try:
        if os.name == "nt":  # pragma: no cover - production is Linux
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":  # pragma: no cover - production is Linux
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class VoiceTranscriptStore:
    """Append completed voice turns to one fixed Claude-compatible JSONL."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        seed_marker_path: Path | None = None,
    ) -> None:
        self.path = (path or voice_transcript_path()).expanduser().resolve()
        self.seed_marker_path = (
            seed_marker_path or self.path.parent / _SEED_MARKER_NAME
        ).expanduser().resolve()
        self.lock_path = self.path.parent / ".transcript-write.lock"
        # A completed turn has both roles.  Keep the roles too, rather than
        # treating the first valid JSONL record as a durable pair: a process
        # can die between the user and assistant writes.
        self._known_turn_roles: dict[str, set[str]] = {}
        self._known_signature: tuple[int, int] | None = None

    def append_turn(
        self,
        *,
        user_text: str,
        assistant_text: str,
        call_id: str | None,
        turn_id: str | None,
        surface: str | None,
        model: str | None,
        brain_session_id: str | None,
        timestamp: datetime | str | float | int | None = None,
        source_journal_id: str | None = None,
    ) -> bool:
        """Append one user/assistant pair durably. False means already present."""

        user = str(user_text or "").strip()
        assistant = str(assistant_text or "").strip()
        if not user:
            raise ValueError("voice transcript requires user text")
        key = _turn_key(
            call_id=call_id,
            turn_id=turn_id,
            fallback=source_journal_id,
        )
        with _PROCESS_LOCK, _exclusive_file_lock(self.lock_path):
            return self._append_turn_locked(
                user_text=user,
                assistant_text=assistant,
                call_id=call_id,
                turn_id=turn_id,
                surface=surface,
                model=model,
                brain_session_id=brain_session_id,
                timestamp=timestamp,
                source_journal_id=source_journal_id,
                turn_key=key,
            )

    def seed_from_recent_journal(self, journal_path: Path) -> int:
        """Import delivered voice entries once, without changing the journal."""

        with _PROCESS_LOCK, _exclusive_file_lock(self.lock_path):
            if self.seed_marker_path.exists():
                return 0
            try:
                document = json.loads(
                    journal_path.expanduser().read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                document = {}
            entries = document.get("entries") if isinstance(document, dict) else []
            imported = 0
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict):
                    continue
                if not entry.get("delivered") or entry.get("protocol") != "voice":
                    continue
                user = str(entry.get("user") or "").strip()
                if not user:
                    continue
                key = _turn_key(
                    call_id=entry.get("call_id"),
                    turn_id=entry.get("turn_id"),
                    fallback=entry.get("id"),
                )
                if self._append_turn_locked(
                    user_text=user,
                    assistant_text=str(entry.get("assistant") or "").strip(),
                    call_id=entry.get("call_id"),
                    turn_id=entry.get("turn_id"),
                    surface=None,
                    model=entry.get("model"),
                    brain_session_id=entry.get("session_id"),
                    timestamp=entry.get("at"),
                    source_journal_id=entry.get("id"),
                    turn_key=key,
                ):
                    imported += 1
            write_text_atomic(
                self.seed_marker_path,
                json.dumps(
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "seeded_at": _timestamp(None),
                        "imported_turns": imported,
                    },
                    separators=(",", ":"),
                ),
            )
            return imported

    def _append_turn_locked(
        self,
        *,
        user_text: str,
        assistant_text: str,
        call_id: str | None,
        turn_id: str | None,
        surface: str | None,
        model: str | None,
        brain_session_id: str | None,
        timestamp: datetime | str | float | int | None,
        source_journal_id: str | None,
        turn_key: str,
    ) -> bool:
        self._refresh_known_keys()
        known_roles = self._known_turn_roles.get(turn_key, set())
        if {"user", "assistant"}.issubset(known_roles):
            return False
        stamp = _timestamp(timestamp)
        metadata = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": VOICE_SESSION_ID,
            "turn_key": turn_key,
            "surface": infer_surface(call_id, surface),
            "call_id": _clean(call_id, 128) or None,
            "turn_id": _clean(turn_id, 128) or None,
            "model": _clean(model, 128) or None,
            "brain_session_id": _clean(brain_session_id, 128) or None,
            "source_journal_id": _clean(source_journal_id, 128) or None,
        }
        rows = []
        for role, text in (("user", user_text), ("assistant", assistant_text)):
            if role in known_roles:
                continue
            rows.append(
                {
                    "type": role,
                    "timestamp": stamp,
                    "message": {
                        "role": role,
                        "content": [{"type": "text", "text": text}],
                    },
                    "serena_voice": metadata,
                }
            )
        payload = "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ).encode("utf-8")
        if self._needs_line_separator():
            payload = b"\n" + payload
        secure_directory(self.path.parent)
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            if os.name != "nt":
                os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write while appending voice transcript")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._known_turn_roles.setdefault(turn_key, set()).update(
            row["type"] for row in rows
        )
        self._known_signature = self._signature()
        return True

    def _needs_line_separator(self) -> bool:
        try:
            with self.path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) != b"\n"
        except (OSError, ValueError):
            return False

    def _signature(self) -> tuple[int, int] | None:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return info.st_size, info.st_mtime_ns

    def _refresh_known_keys(self) -> None:
        signature = self._signature()
        if signature == self._known_signature:
            return
        keys: dict[str, set[str]] = {}
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    voice = row.get("serena_voice") if isinstance(row, dict) else None
                    key = voice.get("turn_key") if isinstance(voice, dict) else None
                    role = row.get("type") if isinstance(row, dict) else None
                    if isinstance(key, str) and key and role in {"user", "assistant"}:
                        keys.setdefault(key, set()).add(role)
        except OSError:
            pass
        self._known_turn_roles = keys
        self._known_signature = signature


def scan_voice_sessions() -> Iterator[tuple[str, Path]]:
    path = voice_transcript_path()
    if path.is_file():
        yield VOICE_PROJECT_DIR, path


def parse_voice_metadata(file_path: Path) -> SessionMeta | None:
    first_text = ""
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    count = 0
    model: str | None = None
    last_surface = "voice"
    surfaces: set[str] = set()
    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict) or row.get("type") not in ("user", "assistant"):
                    continue
                message = row.get("message") or {}
                text = _extract_text(message.get("content", ""))
                count += 1
                stamp = _coerce_datetime(row.get("timestamp"))
                if stamp is not None:
                    first_ts = first_ts or stamp
                    last_ts = stamp
                voice = row.get("serena_voice") or {}
                if isinstance(voice, dict):
                    current_surface = _clean(voice.get("surface"), 32)
                    if current_surface:
                        last_surface = current_surface
                        surfaces.add(current_surface)
                    current_model = _clean(voice.get("model"), 128)
                    if current_model:
                        model = current_model
                if row.get("type") == "user" and not first_text and text.strip():
                    first_text = text.strip()[:500]
    except OSError:
        return None
    if count == 0:
        return None
    try:
        info = file_path.stat()
    except OSError:
        return None
    return SessionMeta(
        session_id=VOICE_SESSION_ID,
        project_dir=VOICE_PROJECT_DIR,
        cwd=None,
        device=last_surface,
        first_message=first_text or "Serena",
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        message_count=count,
        raw_message_count=count,
        model=model,
        file_path=str(file_path),
        file_size=info.st_size,
        file_mtime=info.st_mtime,
        devices_used=sorted(surfaces),
    )


def _coerce_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
