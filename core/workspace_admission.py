"""Fail-closed admission for persisted coding sessions, never resume-as-new."""

from __future__ import annotations

import os
from pathlib import Path

import psutil


def reject_unregistered_codex(sid: str, cwd: Path, transcript: Path) -> None:
    reject_unregistered_provider(sid, cwd, transcript, "codex")


def reject_unregistered_provider(sid: str, cwd: Path, transcript: Path, provider: str) -> None:
    """Detect older/manual owners that do not participate in shared leases.

    A process with an explicit different resume ID is unrelated. An unidentified
    Codex runtime in this project is ambiguous and blocks migration. This check
    complements leases; it cannot constrain manually launched future processes.
    """
    for process in psutil.process_iter(["pid", "name"]):
        if process.pid == os.getpid():
            continue
        candidate = provider in (process.info.get("name") or "").lower()
        label = provider.capitalize()
        try:
            argv = process.cmdline()
            candidate = candidate or any(provider in Path(value).name.lower() for value in argv[:3])
            if not candidate:
                continue
            if sid in argv or f"--resume={sid}" in argv:
                raise RuntimeError(f"This session already has a {label} process")
            paths = {Path(f.path) for f in process.open_files()}
            if transcript in paths:
                raise RuntimeError(f"This session transcript is already open by a {label} process")
            switches = ("resume",) if provider == "codex" else ("--resume", "-r")
            explicit = next((value for value in switches if value in argv), None)
            if explicit:
                index = argv.index(explicit) + 1
                if index < len(argv) and not argv[index].startswith("-"):
                    continue
            if Path(process.cwd()).resolve() == cwd:
                raise RuntimeError(
                    f"An unregistered {label} process in this project prevents safe attachment"
                )
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as error:
            if candidate:
                raise RuntimeError(
                    f"Cannot verify ownership of an existing {label} process"
                ) from error


def resolve_workspace_session(sid: str) -> dict:
    from core import metadata
    from core.indexer import get_session
    from ui import pty_terminal

    session = get_session(sid)
    if not session or session.get("session_id") != sid:
        raise ValueError("Exact persisted session was not found")
    provider = str(session.get("agent") or "").lower()
    if provider == "gemini":
        raise ValueError(
            "Antigravity's streaming interface does not support interactive approvals "
            "or image input. A full-fidelity custom Gemini pane is not available yet; "
            "this session has not been opened or changed."
        )
    if provider not in {"codex", "claude"}:
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
    reject_unregistered_provider(sid, cwd, transcript, provider)
    return {"session_id": sid, "provider": provider, "cwd": str(cwd)}
