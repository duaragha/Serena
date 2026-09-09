"""Machine-local exclusive leases shared by structured and PTY session owners."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import psutil


class SessionOwnedError(RuntimeError):
    pass


def _alive(identity: dict | None) -> bool:
    if not identity:
        return False
    try:
        process = psutil.Process(identity["pid"])
        return (
            process.create_time() == identity["born"] and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, KeyError, TypeError):
        return True


def _identity(pid: int) -> dict:
    return {"pid": pid, "born": psutil.Process(pid).create_time()}


def _runtime_alive(record: dict) -> bool:
    if _alive(record.get("child")):
        return True
    group = record.get("process_group")
    if group is None or os.name == "nt":
        return False
    if type(group) is not int or group <= 0:
        return True
    # The leader may be gone while tool processes still hold the group. Never
    # infer their exit from the leader's PID, or kill a potentially reused group.
    for pid in psutil.pids():
        try:
            if os.getpgid(pid) == group and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                return True
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied):
            return True
    return False


class SessionLease:
    def __init__(self, session_id: str, *, directory: Path | None = None) -> None:
        if not session_id:
            raise ValueError("Session identity is required")
        root = directory or Path(
            os.environ.get("SERENA_RUNTIME_LEASE_DIR")
            or Path.home() / ".config/serena/runtime-leases"
        )
        root.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(session_id.encode()).hexdigest()
        self.file = (root / (key + ".lock")).open("a+b")
        self.metadata = root / (key + ".json")
        self.record = {}
        self.closed = False
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0, 2)
                if not self.file.tell():
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            self.closed = True
            raise SessionOwnedError("This session already has a runtime owner") from error
        try:
            previous = json.loads(self.metadata.read_text()) if self.metadata.exists() else {}
            if previous.get("phase") == "launching" or _runtime_alive(previous):
                raise SessionOwnedError(
                    "Previous runtime may still be running; recovery must confirm its exit"
                )
            self.record = {"owner": _identity(os.getpid()), "phase": "reserved"}
            self._save()
        except BaseException:
            self.file.close()
            self.closed = True
            raise

    def _save(self) -> None:
        # Never replace the locked inode. Metadata is a separate atomic file:
        # a crash during binding leaves the prior 'launching' marker intact.
        name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.metadata.parent, delete=False
            ) as out:
                name = out.name
                json.dump(self.record, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.metadata)
            if os.name != "nt":
                fd = os.open(self.metadata.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def launching(self) -> None:
        if self.closed:
            raise SessionOwnedError("Lease is closed")
        self.record["phase"] = "launching"
        self._save()

    def bind(self, pid: int) -> None:
        if self.closed:
            raise SessionOwnedError("Lease is closed")
        try:
            child = _identity(pid)
            group = None
            if os.name != "nt":
                candidate = os.getpgid(pid)
                if candidate == os.getsid(pid) and candidate != os.getpgrp():
                    group = candidate
        except (psutil.NoSuchProcess, ProcessLookupError) as error:
            raise SessionOwnedError("Runtime exited before its ownership could be verified") from error
        self.record.update(phase="bound", child=child)
        if group is not None:
            self.record["process_group"] = group
        self._save()

    def release(self) -> None:
        if self.closed:
            return
        # Keep crash/launch ambiguity and live orphan identity on disk. A new
        # owner must not interpret a released host lock as a dead agent.
        try:
            if self.record.get("phase") != "launching" and not _runtime_alive(self.record):
                self.record = {}
                self._save()
        finally:
            self.file.close()
            self.closed = True

    def cancel_before_launch(self) -> None:
        if self.record.get("phase") != "reserved":
            raise SessionOwnedError("Cannot clear a lease after launch began")
        self.release()
