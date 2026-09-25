"""Machine-local exclusive leases shared by structured and PTY session owners."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
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


def _lease_root(directory: Path | None) -> Path:
    return directory or Path(
        os.environ.get("SERENA_RUNTIME_LEASE_DIR")
        or Path.home() / ".config/serena/runtime-leases"
    )


def terminate_recorded_runtime(
    session_id: str, *, directory: Path | None = None, grace: float = 2.0
) -> bool:
    """Stop the runtime a session's lease records, from outside its owner.

    An explicit delete does not wait for whichever window owns the chat: it
    ends the bound agent and everything in its process group (its children on
    Windows). Identity is checked against the recorded start time, so a reused
    PID is never signalled, and neither is this process or its own group.
    Returns True when a live recorded runtime was signalled.
    """
    if not session_id:
        return False
    key = hashlib.sha256(session_id.encode()).hexdigest()
    try:
        record = json.loads((_lease_root(directory) / (key + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    victims: dict[int, psutil.Process] = {}
    child = record.get("child")
    if isinstance(child, dict) and child.get("pid") != os.getpid():
        try:
            process = psutil.Process(child["pid"])
            if process.create_time() == child["born"]:
                victims[process.pid] = process
                for descendant in process.children(recursive=True):
                    victims[descendant.pid] = descendant
        except (psutil.Error, KeyError, TypeError):
            pass
    group = record.get("process_group")
    if os.name != "nt" and type(group) is int and group > 0 and group != os.getpgrp():
        for pid in psutil.pids():
            try:
                if os.getpgid(pid) == group:
                    victims.setdefault(pid, psutil.Process(pid))
            except (ProcessLookupError, PermissionError, psutil.Error):
                continue
    victims.pop(os.getpid(), None)
    if not victims:
        return False
    for process in victims.values():
        try:
            process.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(list(victims.values()), timeout=grace)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=1)
    return True


class SessionLease:
    def __init__(self, session_id: str, *, directory: Path | None = None) -> None:
        if not session_id:
            raise ValueError("Session identity is required")
        root = _lease_root(directory)
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
            previous = json.loads(self.metadata.read_text(encoding="utf-8")) if self.metadata.exists() else {}
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

    def transfer_after_transition(self, session_id: str) -> SessionLease:
        """Move a confirmed native identity transition while caller blocks input.

        Caller must confirm no old-session work remains, checkpoint the returned
        identity and route the retained runtime only after this succeeds.
        Failure never authorizes a new writer.
        """
        if self.closed or self.record.get("phase") != "bound":
            raise SessionOwnedError("Only a bound runtime can transfer ownership")
        child = self.record.get("child")
        try:
            verified = (self.record.get("owner") == _identity(os.getpid())
                        and child and _identity(child["pid"]) == child and _alive(child))
        except (KeyError, TypeError, psutil.Error) as error:
            raise SessionOwnedError("Runtime ownership could not be verified") from error
        if not verified:
            raise SessionOwnedError("Runtime ownership changed before transfer")
        target = SessionLease(session_id, directory=self.metadata.parent)
        previous = deepcopy(self.record)
        try:
            target.launching()
            target.bind(child["pid"])
            if target.record.get("child") != child:
                raise SessionOwnedError("Runtime identity changed during transfer")
            # Publish the new binding before clearing the old binding. A crash
            # between writes may pin both identities, never release both.
            self.record = {}
            self._save()
        except BaseException:
            self.record = previous
            target.release()
            raise
        self.file.close()
        self.closed = True
        return target

    def cancel_before_launch(self) -> None:
        if self.record.get("phase") != "reserved":
            raise SessionOwnedError("Cannot clear a lease after launch began")
        self.release()
