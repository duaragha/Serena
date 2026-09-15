"""Fail-closed admission for persisted coding sessions, never resume-as-new."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import psutil


def _registered_other_runtime(process, sid: str) -> bool:
    """Recognize leased runtime wrappers and their native child, not tool trees."""
    root = Path(os.environ.get("SERENA_RUNTIME_LEASE_DIR") or Path.home() / ".config/serena/runtime-leases")
    target = hashlib.sha256(sid.encode()).hexdigest() + ".json"
    try:
        identities = {(process.pid, process.create_time())}
        parent = process.parent()
        if parent is not None:
            parent_argv = parent.cmdline()
            wrapper = (len(parent_argv) > 1 and Path(parent_argv[0]).name.lower() in {"node", "node.exe"}
                       and Path(parent_argv[1]).name in {"codex", "codex.js", "workspace_claude_worker.mjs"})
            if wrapper:
                identities.add((parent.pid, parent.create_time()))
            if (process.info.get("name") or "").lower() == "codex-code-mode-host":
                identities.add((parent.pid, parent.create_time()))
                grandparent = parent.parent()
                if grandparent is not None:
                    identities.add((grandparent.pid, grandparent.create_time()))
        for path in root.glob("*.json"):
            if path.name == target:
                continue
            try:
                record = json.loads(path.read_text())
                child, owner = record.get("child", {}), record.get("owner", {})
                if record.get("phase") != "bound" or (child.get("pid"), child.get("born")) not in identities:
                    continue
                host = psutil.Process(owner["pid"])
                if host.create_time() == owner["born"] and host.status() != psutil.STATUS_ZOMBIE:
                    return True
            except (OSError, ValueError, TypeError, KeyError, psutil.Error):
                continue
    except (OSError, psutil.Error, AttributeError):
        pass
    return False


def reject_unregistered_codex(sid: str, cwd: Path, transcript: Path) -> None:
    reject_unregistered_provider(sid, cwd, transcript, "codex")


def reject_unregistered_provider(sid: str, cwd: Path, transcript: Path, provider: str, *, exclude_pids=()) -> None:
    """Detect older/manual owners that do not participate in shared leases.

    A process with an explicit different resume ID is unrelated. An unidentified
    runtime in this project is ambiguous and blocks migration. Verified leased
    owners of other sessions are not ambiguous. This check
    complements leases; it cannot constrain manually launched future processes.
    """
    # Google's native harness may outlive the agy parent. Linux may truncate
    # its process name, so match the stable prefix as well as its argv.
    names = ("agy", "localharness") if provider == "agy" else (provider,)
    excluded = {os.getpid(), *exclude_pids}
    if not all(type(pid) is int and pid > 0 for pid in excluded):
        raise ValueError("Excluded process identities must be positive integers")
    for process in psutil.process_iter(["pid", "name"]):
        if process.pid in excluded:
            continue
        candidate = any(name in (process.info.get("name") or "").lower() for name in names)
        label = provider.capitalize()
        try:
            argv = process.cmdline()
            candidate = candidate or any(name in Path(value).name.lower()
                for index, value in enumerate(argv[:3])
                if index == 0 or argv[index - 1] not in {"-c", "-lc", "-ic", "-e", "--eval"}
                for name in names)
            if not candidate:
                continue
            if sid in argv or f"--resume={sid}" in argv or (provider == 'agy' and f'--conversation={sid}' in argv):
                raise RuntimeError(f"This session already has a {label} process")
            paths = {Path(f.path) for f in process.open_files()}
            if transcript in paths:
                raise RuntimeError(f"This session transcript is already open by a {label} process")
            if (provider == "claude" and len(argv) >= 6
                    and Path(argv[1]).resolve() == Path(__file__).with_name("workspace_claude_worker.mjs").resolve()
                    and argv[4] and argv[4] != sid):
                continue
            if _registered_other_runtime(process, sid):
                continue
            if provider == "codex" and not any(path.suffix == '.jsonl' for path in paths):
                # A bare interactive launcher has no selected transcript. Sharing
                # its working directory is not ownership of this saved session.
                bare_native = len(argv) == 1 and Path(argv[0]).name.lower() in {"codex", "codex.exe"}
                bare_wrapper = (len(argv) == 2 and Path(argv[0]).name.lower() in {"node", "node.exe"}
                                and Path(argv[1]).name in {"codex", "codex.js"})
                if (bare_native or bare_wrapper) and getattr(process, 'terminal', lambda: None)():
                    continue
            switches = ("resume",) if provider == "codex" else (("--conversation",) if provider == "agy" else ("--resume", "-r"))
            explicit = next((value for value in switches if value in argv), None)
            if any(value.startswith(switch + "=") and value.split("=", 1)[1]
                   for value in argv for switch in switches if switch.startswith("-")):
                continue
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
    if provider not in {"codex", "claude", "gemini"}:
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
    if provider == 'gemini':
        from core.gemini_scanner import resumable_conversation_path
        native = resumable_conversation_path(sid)
        if native is None:
            raise ValueError('Exact native Gemini conversation is unavailable')
        transcript = native.resolve()
    reject_unregistered_provider(sid, cwd, transcript, 'agy' if provider == 'gemini' else provider)
    return {"session_id": sid, "provider": provider, "cwd": str(cwd),
            **({"session_directory": str(Path(session.get("cwd") or cwd).resolve())} if provider == "claude" else {}),
            "archived": bool(session.get("is_archived"))}
