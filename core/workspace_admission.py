"""Fail-closed admission for persisted coding sessions, never resume-as-new."""

from __future__ import annotations

import os
from pathlib import Path

import psutil


def reject_unregistered_codex(sid: str, cwd: Path, transcript: Path) -> None:
    """Detect older/manual owners that do not participate in shared leases.

    A process with an explicit different resume ID is unrelated. An unidentified
    Codex runtime in this project is ambiguous and blocks migration. This check
    complements leases; it cannot constrain manually launched future processes.
    """
    for process in psutil.process_iter(["pid", "name"]):
        if process.pid == os.getpid():
            continue
        try:
            argv = process.cmdline()
            candidate = any("codex" in Path(value).name.lower() for value in argv[:3])
            if not candidate:
                continue
            if sid in argv:
                raise RuntimeError("This session already has a Codex process")
            paths = {Path(f.path) for f in process.open_files()}
            if transcript in paths:
                raise RuntimeError("This session transcript is already open by a Codex process")
            if "resume" in argv:
                index = argv.index("resume") + 1
                if index < len(argv) and not argv[index].startswith("-"):
                    continue
            if Path(process.cwd()).resolve() == cwd:
                raise RuntimeError(
                    "An unregistered Codex process in this project prevents safe attachment"
                )
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as error:
            if "codex" in (process.info.get("name") or "").lower():
                raise RuntimeError(
                    "Cannot verify ownership of an existing Codex process"
                ) from error


def resolve_workspace_session(sid: str) -> dict:
    from core import metadata
    from core.indexer import get_session
    from ui import pty_terminal

    session = get_session(sid)
    if not session or session.get("session_id") != sid:
        raise ValueError("Exact persisted session was not found")
    provider = str(session.get("agent") or "").lower()
    if provider != "codex":
        raise ValueError("This provider's structured workspace is not implemented yet")
    meta = metadata.get_meta(sid)
    if meta.get("fleet_worker") or metadata.external_runtime_active(sid):
        raise RuntimeError("A Fleet or background worker owns this session")
    if pty_terminal.tid_for_session(sid):
        raise RuntimeError("This session already has a live terminal; it will not be duplicated")
    raw_cwd = session.get("last_cwd") or session.get("cwd")
    raw_file = session.get("file_path")
    if not raw_cwd or not raw_file:
        raise ValueError("Session project or native transcript is missing")
    cwd, transcript = Path(raw_cwd), Path(raw_file)
    if not cwd.is_dir() or not transcript.is_file():
        raise ValueError("Session project or native transcript is unavailable on this machine")
    cwd, transcript = cwd.resolve(), transcript.resolve()
    reject_unregistered_codex(sid, cwd, transcript)
    return {"session_id": sid, "provider": provider, "cwd": str(cwd)}
