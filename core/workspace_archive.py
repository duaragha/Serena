"""Native archive maintenance without attaching a coding-session writer.

Callers must claim a durable mutation receipt before invoking archive changes.
An ambiguous native result is never retried here.
"""
import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
from uuid import UUID

import psutil

from core.billing import strip_metered_auth_env
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc, WorkspaceRpcError

_AGENT_SOURCES = ["subAgent", "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther"]


def _maintenance_pids(process_id):
    try:
        process = psutil.Process(process_id)
        return tuple({process_id, *(child.pid for child in process.children(recursive=True))})
    except psutil.Error as error:
        raise WorkspaceRpcError("Archive maintenance process ownership is unconfirmed") from error


async def _descendants(rpc, session_id, archived):
    rows, seen, cursor = [], set(), None
    for _ in range(83):
        params = {"ancestorThreadId": session_id, "archived": archived, "limit": 50,
                  "sourceKinds": _AGENT_SOURCES, "cursor": cursor}
        result = await rpc.request("thread/list", params)
        if not isinstance(result, dict) or not isinstance(result.get("data"), list) or len(result["data"]) > 50:
            raise WorkspaceRpcError("Codex returned an invalid archive family")
        for thread in result["data"]:
            identity = thread.get("id") if isinstance(thread, dict) else None
            parent = thread.get("parentThreadId") if isinstance(thread, dict) else None
            if (not isinstance(identity, str) or str(UUID(identity)) != identity or identity == session_id
                    or identity in seen or not isinstance(parent, str) or not parent):
                raise WorkspaceRpcError("Codex returned an invalid archive descendant")
            seen.add(identity)
            rows.append(thread)
        following = result.get("nextCursor")
        if following is None:
            return rows
        if not isinstance(following, str) or not following or following == cursor:
            raise WorkspaceRpcError("Archive family pagination did not advance")
        cursor = following
    raise WorkspaceRpcError("Archive family exceeds the supported limit")


async def archive_codex_tree(session_id, cwd, *, confirmed=False, binary=None, env=None,
                             rpc_factory=WorkspaceRpc, lease_factory=SessionLease,
                             ownership_guard=None, family_guard=None, checkpoint=None):
    """Archive one exact unloaded Codex tree without starting a coding writer."""
    if confirmed is not True or not isinstance(session_id, str) or str(UUID(session_id)) != session_id:
        raise ValueError("Explicit confirmation and an exact active session ID are required")
    project = Path(cwd)
    if not project.is_absolute() or not project.is_dir():
        raise ValueError("An existing absolute project directory is required")
    project = project.resolve()
    executable = binary or shutil.which("codex")
    if not executable:
        raise WorkspaceRpcError("Codex executable is unavailable")
    runtime_env = strip_metered_auth_env(dict(os.environ if env is None else env))
    home = Path(runtime_env.get("CODEX_HOME") or Path.home() / ".codex").resolve()
    leases, rpc = [], None

    def exact(result, identity, directory, expected_cwd=None):
        thread = result.get("thread") if isinstance(result, dict) else None
        path = thread.get("path") if isinstance(thread, dict) else None
        raw_cwd = thread.get("cwd") if isinstance(thread, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != identity:
            raise WorkspaceRpcError("Native archive identity did not match")
        if not isinstance(path, str) or not Path(path).resolve().is_relative_to(home / directory):
            raise WorkspaceRpcError("Native transcript is not in the expected archive state")
        if (not isinstance(raw_cwd, str) or not Path(raw_cwd).is_absolute()
                or (expected_cwd is not None and Path(raw_cwd).resolve() != expected_cwd)):
            raise WorkspaceRpcError("Native archive project did not match")
        return {**thread, "path": str(Path(path).resolve()), "cwd": str(Path(raw_cwd).resolve())}

    try:
        root_lease = lease_factory(session_id)
        leases.append(root_lease)
        rpc = rpc_factory()
        root_lease.launching()
        await rpc.start([executable, "app-server", "--stdio"], cwd=project, env=runtime_env)
        root_lease.bind(rpc.process.pid)
        await rpc.request("initialize", {"clientInfo": {"name": "serena-workspace-archive", "version": "1"},
                                         "capabilities": {"experimentalApi": True}})
        await rpc.notify("initialized", {})
        root = exact(await rpc.request("thread/read", {"threadId": session_id, "includeTurns": False}),
                     session_id, "sessions", project)
        active = await _descendants(rpc, session_id, False)
        already_archived = await _descendants(rpc, session_id, True)
        active_ids = {thread["id"] for thread in active}
        archived_ids = {thread["id"] for thread in already_archived}
        if active_ids & archived_ids:
            raise WorkspaceRpcError("Archive family state is ambiguous")
        family_ids = {session_id, *active_ids, *archived_ids}
        if any(thread["parentThreadId"] not in family_ids for thread in active + already_archived):
            raise WorkspaceRpcError("Archive descendant ancestry is incomplete")
        active_threads = [root]
        for row in active:
            active_threads.append(exact(await rpc.request("thread/read", {"threadId": row["id"], "includeTurns": False}),
                                        row["id"], "sessions"))
        for thread in sorted(active_threads[1:], key=lambda value: value["id"]):
            lease = lease_factory(thread["id"])
            leases.append(lease)
            lease.launching()
            lease.bind(rpc.process.pid)
        if ownership_guard is None:
            from core import metadata
            from core.workspace_admission import reject_unregistered_provider
            from ui import pty_terminal

            def ownership_guard(thread, process_id):
                if metadata.external_runtime_active(thread["id"]):
                    raise RuntimeError("A Fleet or background worker owns an archive descendant")
                if pty_terminal.tid_for_session(thread["id"]):
                    raise RuntimeError("An archive descendant already has a live terminal")
                reject_unregistered_provider(
                    thread["id"], Path(thread["cwd"]), Path(thread["path"]), "codex",
                    exclude_pids=_maintenance_pids(process_id),
                )
        for thread in active_threads:
            ownership_guard(thread, rpc.process.pid)
        if family_guard is not None:
            family_guard(active_threads)
        repeated = await _descendants(rpc, session_id, False)
        if {thread["id"] for thread in repeated} != active_ids:
            raise WorkspaceRpcError("Archive family changed during ownership admission")
        loaded = await rpc.request("thread/loaded/list", {})
        if not isinstance(loaded, dict) or loaded.get("data") != []:
            raise WorkspaceRpcError("Archive maintenance unexpectedly loaded a coding thread")
        targets = [{"session_id": thread["id"], "provider": "codex", "cwd": thread["cwd"]}
                   for thread in active_threads]
        expected = [target["session_id"] for target in targets]
        if checkpoint is not None:
            checkpoint({"session_id": session_id, "provider": "codex", "cwd": str(project),
                        "targets": targets})
        while not rpc.events.empty():
            rpc.events.get_nowait()
        result = await rpc.request("thread/archive", {"threadId": session_id})
        if result != {}:
            raise WorkspaceRpcError("Native archive acknowledgement was invalid")
        notices = set()
        async with asyncio.timeout(5):
            while not set(expected) <= notices:
                event = await rpc.events.get()
                if event.get("method") == "thread/archived":
                    identity = (event.get("params") or {}).get("threadId")
                    if not isinstance(identity, str) or identity not in expected:
                        raise WorkspaceRpcError("Native archive notification had an unexpected identity")
                    notices.add(identity)
        for thread in active_threads:
            exact(await rpc.request("thread/read", {"threadId": thread["id"], "includeTurns": False}),
                  thread["id"], "archived_sessions")
        if await _descendants(rpc, session_id, False):
            raise WorkspaceRpcError("One or more archive descendants remained active")
        loaded = await rpc.request("thread/loaded/list", {})
        if not isinstance(loaded, dict) or loaded.get("data") != []:
            raise WorkspaceRpcError("Archive maintenance loaded a coding thread")
    finally:
        try:
            if rpc is not None:
                await rpc.close()
                if rpc.process is not None:
                    raise WorkspaceRpcError("Archive maintenance cleanup is unconfirmed")
        finally:
            for lease in reversed(leases):
                lease.release()
    return {"session_id": session_id, "provider": "codex", "cwd": str(project), "archived": True,
            "thread_ids": expected, "targets": targets}


async def inspect_codex_archive_tree(session_id, cwd, targets, *, binary=None, env=None,
                                     rpc_factory=WorkspaceRpc, lease_factory=SessionLease,
                                     ownership_guard=None, family_guard=None):
    """Inspect a previously checkpointed tree without changing native state."""
    if not isinstance(session_id, str) or str(UUID(session_id)) != session_id:
        raise ValueError("An exact archive session ID is required")
    project = Path(cwd)
    if not project.is_absolute() or not project.is_dir():
        raise ValueError("An existing absolute project directory is required")
    project = project.resolve()
    if (not isinstance(targets, list) or not 1 <= len(targets) <= 4150
            or any(not isinstance(target, dict) or set(target) != {"session_id", "provider", "cwd"}
                   or target.get("provider") != "codex"
                   or not isinstance(target.get("session_id"), str)
                   or str(UUID(target["session_id"])) != target["session_id"]
                   or not isinstance(target.get("cwd"), str) or not Path(target["cwd"]).is_absolute()
                   for target in targets)
            or targets[0]["session_id"] != session_id
            or Path(targets[0]["cwd"]).resolve() != project
            or len({target["session_id"] for target in targets}) != len(targets)):
        raise ValueError("An exact checkpointed archive family is required")
    normalized = [{**target, "cwd": str(Path(target["cwd"]).resolve())} for target in targets]
    executable = binary or shutil.which("codex")
    if not executable:
        raise WorkspaceRpcError("Codex executable is unavailable")
    runtime_env = strip_metered_auth_env(dict(os.environ if env is None else env))
    home = Path(runtime_env.get("CODEX_HOME") or Path.home() / ".codex").resolve()
    leases, rpc = [], None

    try:
        for target in normalized:
            leases.append(lease_factory(target["session_id"]))
        rpc = rpc_factory()
        for lease in leases:
            lease.launching()
        await rpc.start([executable, "app-server", "--stdio"], cwd=project, env=runtime_env)
        for lease in leases:
            lease.bind(rpc.process.pid)
        await rpc.request("initialize", {"clientInfo": {"name": "serena-workspace-archive", "version": "1"},
                                         "capabilities": {"experimentalApi": True}})
        await rpc.notify("initialized", {})
        states, threads = set(), []
        for target in normalized:
            result = await rpc.request("thread/read", {"threadId": target["session_id"], "includeTurns": False})
            thread = result.get("thread") if isinstance(result, dict) else None
            path = thread.get("path") if isinstance(thread, dict) else None
            raw_cwd = thread.get("cwd") if isinstance(thread, dict) else None
            if (not isinstance(thread, dict) or thread.get("id") != target["session_id"]
                    or not isinstance(path, str) or not Path(path).is_absolute()
                    or not isinstance(raw_cwd, str) or not Path(raw_cwd).is_absolute()
                    or Path(raw_cwd).resolve() != Path(target["cwd"])):
                raise WorkspaceRpcError("Checkpointed archive identity or project did not match")
            resolved = Path(path).resolve()
            if resolved.is_relative_to(home / "sessions"):
                states.add(False)
            elif resolved.is_relative_to(home / "archived_sessions"):
                states.add(True)
            else:
                raise WorkspaceRpcError("Checkpointed transcript is outside the Codex session store")
            threads.append({**thread, "path": str(resolved), "cwd": str(Path(raw_cwd).resolve())})
        if len(states) != 1:
            raise WorkspaceRpcError("Archive family is partially archived; automatic recovery is unavailable")
        archived = states.pop()
        expected_descendants = {target["session_id"] for target in normalized[1:]}
        active_descendants = {thread["id"] for thread in await _descendants(rpc, session_id, False)}
        if (archived and active_descendants) or (not archived and active_descendants != expected_descendants):
            raise WorkspaceRpcError("Checkpointed archive family no longer matches native state")
        if ownership_guard is None:
            from core import metadata
            from core.workspace_admission import reject_unregistered_provider
            from ui import pty_terminal

            def ownership_guard(thread, process_id):
                if metadata.external_runtime_active(thread["id"]):
                    raise RuntimeError("A Fleet or background worker owns an archive descendant")
                if pty_terminal.tid_for_session(thread["id"]):
                    raise RuntimeError("An archive descendant already has a live terminal")
                reject_unregistered_provider(
                    thread["id"], Path(thread["cwd"]), Path(thread["path"]), "codex",
                    exclude_pids=_maintenance_pids(process_id),
                )
        for thread in threads:
            ownership_guard(thread, rpc.process.pid)
        if family_guard is not None:
            family_guard(threads)
        loaded = await rpc.request("thread/loaded/list", {})
        if not isinstance(loaded, dict) or loaded.get("data") != []:
            raise WorkspaceRpcError("Archive inspection unexpectedly loaded a coding thread")
    finally:
        try:
            if rpc is not None:
                await rpc.close()
                if rpc.process is not None:
                    raise WorkspaceRpcError("Archive inspection cleanup is unconfirmed")
        finally:
            for lease in reversed(leases):
                lease.release()
    return {"session_id": session_id, "provider": "codex", "cwd": str(project), "archived": archived,
            "thread_ids": [target["session_id"] for target in normalized], "targets": normalized}


def _store_state(path, home):
    resolved = Path(path).resolve()
    if resolved.is_relative_to(home / "sessions"):
        return resolved, False
    if resolved.is_relative_to(home / "archived_sessions"):
        return resolved, True
    raise WorkspaceRpcError("Native transcript is outside the Codex session store")


def _durable_backup(source, destination):
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        while chunk := incoming.read(1024 * 1024):
            outgoing.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    os.chmod(destination, 0o600)
    return size, digest.hexdigest()


def _file_digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as incoming:
        while chunk := incoming.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _backup_delete_family(recovery, threads, metadata_reader):
    recovery.mkdir(parents=True, mode=0o700, exist_ok=False)
    rollouts = recovery / "rollouts"
    rollouts.mkdir(mode=0o700)
    targets, manifest_targets = [], []
    for index, thread in enumerate(threads):
        source = Path(thread["path"])
        destination = rollouts / f"{index:04d}-{thread['id']}.jsonl"
        size, digest = _durable_backup(source, destination)
        target = {
            "session_id": thread["id"], "provider": "codex", "cwd": thread["cwd"],
            "path": str(source), "archived": thread["archived"],
            "recovery_path": str(destination), "size": size, "sha256": digest,
        }
        targets.append(target)
        manifest_targets.append({**target, "metadata": metadata_reader(thread["id"])})
    manifest = recovery / "recovery.json"
    with manifest.open("x", encoding="utf-8") as output:
        json.dump({"provider": "codex", "root_session_id": threads[0]["id"],
                   "targets": manifest_targets}, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    if os.name != "nt":
        for directory in (rollouts, recovery):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return targets


def _validate_delete_checkpoint(session_id, cwd, checkpoint):
    targets = checkpoint.get("targets") if isinstance(checkpoint, dict) else None
    recovery = checkpoint.get("recovery_dir") if isinstance(checkpoint, dict) else None
    if (not isinstance(checkpoint, dict)
            or set(checkpoint) != {"session_id", "provider", "cwd", "targets", "recovery_dir"}
            or checkpoint.get("session_id") != session_id or checkpoint.get("provider") != "codex"
            or not isinstance(checkpoint.get("cwd"), str)
            or not Path(checkpoint["cwd"]).is_absolute()
            or Path(checkpoint["cwd"]).resolve() != cwd or not isinstance(recovery, str)
            or not Path(recovery).is_absolute() or not isinstance(targets, list) or not targets
            or not isinstance(targets[0], dict) or targets[0].get("session_id") != session_id):
        raise ValueError("An exact checkpointed delete family is required")
    required = {"session_id", "provider", "cwd", "path", "archived", "recovery_path", "size", "sha256"}
    for target in targets:
        identity = target.get("session_id") if isinstance(target, dict) else None
        if (not isinstance(target, dict) or set(target) != required
                or not isinstance(identity, str) or str(UUID(identity)) != identity
                or target.get("provider") != "codex"
                or not isinstance(target.get("cwd"), str) or not Path(target["cwd"]).is_absolute()
                or not isinstance(target.get("path"), str) or not Path(target["path"]).is_absolute()
                or type(target.get("archived")) is not bool
                or not isinstance(target.get("recovery_path"), str)
                or not Path(target["recovery_path"]).is_absolute()
                or not Path(target["recovery_path"]).resolve().is_relative_to(Path(recovery).resolve())
                or type(target.get("size")) is not int or target["size"] < 0
                or not isinstance(target.get("sha256"), str) or len(target["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in target["sha256"])):
            raise ValueError("An exact checkpointed delete target is required")
    if (len({target["session_id"] for target in targets}) != len(targets)
            or len({str(Path(target["path"]).resolve()) for target in targets}) != len(targets)
            or len({str(Path(target["recovery_path"]).resolve()) for target in targets}) != len(targets)):
        raise ValueError("Checkpointed delete targets must be unique")
    return targets, Path(recovery).resolve()


async def delete_codex_tree(session_id, cwd, recovery_dir, *, confirmed=False, binary=None, env=None,
                            rpc_factory=WorkspaceRpc, lease_factory=SessionLease,
                            ownership_guard=None, family_guard=None, checkpoint=None,
                            metadata_reader=None):
    """Delete one unloaded Codex family after preserving an exact recovery copy."""
    if confirmed is not True or not isinstance(session_id, str) or str(UUID(session_id)) != session_id:
        raise ValueError("Explicit confirmation and an exact session ID are required")
    project = Path(cwd)
    recovery = Path(recovery_dir)
    if not project.is_absolute() or not project.is_dir():
        raise ValueError("An existing absolute project directory is required")
    if not recovery.is_absolute() or recovery.exists():
        raise ValueError("A new absolute recovery directory is required")
    project, recovery = project.resolve(), recovery.resolve()
    executable = binary or shutil.which("codex")
    if not executable:
        raise WorkspaceRpcError("Codex executable is unavailable")
    runtime_env = strip_metered_auth_env(dict(os.environ if env is None else env))
    home = Path(runtime_env.get("CODEX_HOME") or Path.home() / ".codex").resolve()
    if recovery.is_relative_to(home):
        raise ValueError("Delete recovery must be outside the Codex session store")
    if metadata_reader is None:
        from core import metadata

        metadata_reader = metadata.get_meta
    leases, rpc = [], None

    def exact(result, identity, expected_cwd=None, archived=None):
        thread = result.get("thread") if isinstance(result, dict) else None
        path = thread.get("path") if isinstance(thread, dict) else None
        raw_cwd = thread.get("cwd") if isinstance(thread, dict) else None
        if (not isinstance(thread, dict) or thread.get("id") != identity
                or not isinstance(path, str) or not isinstance(raw_cwd, str) or not Path(raw_cwd).is_absolute()
                or (expected_cwd is not None and Path(raw_cwd).resolve() != expected_cwd)):
            raise WorkspaceRpcError("Native delete identity or project did not match")
        resolved, state = _store_state(path, home)
        if archived is not None and state is not archived:
            raise WorkspaceRpcError("Native delete family changed archive state")
        if not resolved.is_file():
            raise WorkspaceRpcError("Native delete transcript is unavailable")
        return {**thread, "path": str(resolved), "cwd": str(Path(raw_cwd).resolve()), "archived": state}

    try:
        root_lease = lease_factory(session_id)
        leases.append(root_lease)
        rpc = rpc_factory()
        root_lease.launching()
        await rpc.start([executable, "app-server", "--stdio"], cwd=project, env=runtime_env)
        root_lease.bind(rpc.process.pid)
        await rpc.request("initialize", {"clientInfo": {"name": "serena-workspace-delete", "version": "1"},
                                         "capabilities": {"experimentalApi": True}})
        await rpc.notify("initialized", {})
        root = exact(await rpc.request("thread/read", {"threadId": session_id, "includeTurns": False}),
                     session_id, project)
        active = await _descendants(rpc, session_id, False)
        archived = await _descendants(rpc, session_id, True)
        active_ids = {thread["id"] for thread in active}
        archived_ids = {thread["id"] for thread in archived}
        if active_ids & archived_ids:
            raise WorkspaceRpcError("Delete family state is ambiguous")
        family_ids = {session_id, *active_ids, *archived_ids}
        if any(thread["parentThreadId"] not in family_ids for thread in active + archived):
            raise WorkspaceRpcError("Delete descendant ancestry is incomplete")
        rows = sorted([*(row | {"archived": False} for row in active),
                       *(row | {"archived": True} for row in archived)], key=lambda row: row["id"])
        threads = [root]
        for row in rows:
            threads.append(exact(
                await rpc.request("thread/read", {"threadId": row["id"], "includeTurns": False}),
                row["id"], archived=row["archived"],
            ))
        for thread in threads[1:]:
            lease = lease_factory(thread["id"])
            leases.append(lease)
            lease.launching()
            lease.bind(rpc.process.pid)
        if ownership_guard is None:
            from core import metadata
            from core.workspace_admission import reject_unregistered_provider
            from ui import pty_terminal

            def ownership_guard(thread, process_id):
                if metadata.external_runtime_active(thread["id"]):
                    raise RuntimeError("A Fleet or background worker owns a delete descendant")
                if pty_terminal.tid_for_session(thread["id"]):
                    raise RuntimeError("A delete descendant already has a live terminal")
                reject_unregistered_provider(
                    thread["id"], Path(thread["cwd"]), Path(thread["path"]), "codex",
                    exclude_pids=_maintenance_pids(process_id),
                )
        for thread in threads:
            ownership_guard(thread, rpc.process.pid)
        if family_guard is not None:
            family_guard(threads)
        repeated_active = await _descendants(rpc, session_id, False)
        repeated_archived = await _descendants(rpc, session_id, True)
        if ({thread["id"] for thread in repeated_active} != active_ids
                or {thread["id"] for thread in repeated_archived} != archived_ids):
            raise WorkspaceRpcError("Delete family changed during ownership admission")
        repeated_root = exact(
            await rpc.request("thread/read", {"threadId": session_id, "includeTurns": False}),
            session_id, project, root["archived"],
        )
        if repeated_root["path"] != root["path"]:
            raise WorkspaceRpcError("Delete root changed during ownership admission")
        loaded = await rpc.request("thread/loaded/list", {})
        if not isinstance(loaded, dict) or loaded.get("data") != []:
            raise WorkspaceRpcError("Delete maintenance unexpectedly loaded a coding thread")
        source_paths = [str(Path(thread["path"]).resolve()) for thread in threads]
        if len(set(source_paths)) != len(source_paths):
            raise WorkspaceRpcError("Delete family contains duplicate native transcripts")
        targets = _backup_delete_family(recovery, threads, metadata_reader)
        prepared = {"session_id": session_id, "provider": "codex", "cwd": str(project),
                    "targets": targets, "recovery_dir": str(recovery)}
        if checkpoint is not None:
            checkpoint(prepared)
        while not rpc.events.empty():
            rpc.events.get_nowait()
        result = await rpc.request("thread/delete", {"threadId": session_id})
        if result != {}:
            raise WorkspaceRpcError("Native delete acknowledgement was invalid")
        expected = [target["session_id"] for target in targets]
        notices = set()
        async with asyncio.timeout(15):
            while not set(expected) <= notices:
                event = await rpc.events.get()
                if event.get("method") == "thread/deleted":
                    identity = (event.get("params") or {}).get("threadId")
                    if not isinstance(identity, str) or identity not in expected:
                        raise WorkspaceRpcError("Native delete notification had an unexpected identity")
                    notices.add(identity)
        if any(Path(target["path"]).exists() for target in targets):
            raise WorkspaceRpcError("One or more native delete transcripts remain")
        if await _descendants(rpc, session_id, False) or await _descendants(rpc, session_id, True):
            raise WorkspaceRpcError("One or more native delete descendants remain")
        loaded = await rpc.request("thread/loaded/list", {})
        if not isinstance(loaded, dict) or loaded.get("data") != []:
            raise WorkspaceRpcError("Delete maintenance loaded a coding thread")
    finally:
        try:
            if rpc is not None:
                await rpc.close()
                if rpc.process is not None:
                    raise WorkspaceRpcError("Delete maintenance cleanup is unconfirmed")
        finally:
            for lease in reversed(leases):
                lease.release()
    return {**prepared, "deleted": True, "thread_ids": expected}


async def inspect_codex_delete_tree(session_id, cwd, checkpoint, *, binary=None, env=None,
                                    rpc_factory=WorkspaceRpc, lease_factory=SessionLease,
                                    ownership_guard=None, family_guard=None):
    """Inspect a checkpointed delete family without deleting or restoring it."""
    if not isinstance(session_id, str) or str(UUID(session_id)) != session_id:
        raise ValueError("An exact delete session ID is required")
    project = Path(cwd)
    if not project.is_absolute() or not project.is_dir():
        raise ValueError("An existing absolute project directory is required")
    project = project.resolve()
    targets, recovery = _validate_delete_checkpoint(session_id, project, checkpoint)
    executable = binary or shutil.which("codex")
    if not executable:
        raise WorkspaceRpcError("Codex executable is unavailable")
    runtime_env = strip_metered_auth_env(dict(os.environ if env is None else env))
    home = Path(runtime_env.get("CODEX_HOME") or Path.home() / ".codex").resolve()
    if recovery.is_relative_to(home):
        raise ValueError("Delete recovery must be outside the Codex session store")
    expected_paths = {}
    for target in targets:
        expected_path, archived = _store_state(target["path"], home)
        if archived is not target["archived"]:
            raise WorkspaceRpcError("Checkpointed delete archive state is invalid")
        expected_paths[target["session_id"]] = expected_path
        saved = Path(target["recovery_path"])
        if not saved.is_file() or _file_digest(saved) != (target["size"], target["sha256"]):
            raise WorkspaceRpcError("Delete recovery copy is unavailable or damaged")
    target_names = {path.name for path in expected_paths.values()}
    stored_by_name = {name: [] for name in target_names}
    for store in (home / "sessions", home / "archived_sessions"):
        if not store.is_dir():
            continue
        for candidate in store.rglob("*.jsonl"):
            if candidate.name in stored_by_name and candidate.is_file():
                stored_by_name[candidate.name].append(candidate.resolve())
    leases, rpc = [], None
    try:
        for target in targets:
            leases.append(lease_factory(target["session_id"]))
        rpc = rpc_factory()
        for lease in leases:
            lease.launching()
        await rpc.start([executable, "app-server", "--stdio"], cwd=project, env=runtime_env)
        for lease in leases:
            lease.bind(rpc.process.pid)
        await rpc.request("initialize", {"clientInfo": {"name": "serena-workspace-delete", "version": "1"},
                                         "capabilities": {"experimentalApi": True}})
        await rpc.notify("initialized", {})
        if ownership_guard is None:
            from core import metadata
            from core.workspace_admission import reject_unregistered_provider
            from ui import pty_terminal

            def ownership_guard(thread, process_id):
                if metadata.external_runtime_active(thread["id"]):
                    raise RuntimeError("A Fleet or background worker owns a delete descendant")
                if pty_terminal.tid_for_session(thread["id"]):
                    raise RuntimeError("A delete descendant already has a live terminal")
                reject_unregistered_provider(
                    thread["id"], Path(thread["cwd"]), Path(thread["path"]), "codex",
                    exclude_pids=_maintenance_pids(process_id),
                )
        states, threads = set(), []
        for target in targets:
            expected_path = expected_paths[target["session_id"]]
            try:
                result = await rpc.request("thread/read", {"threadId": target["session_id"], "includeTurns": False})
            except WorkspaceRpcError as error:
                reason = str(error).lower()
                if stored_by_name[expected_path.name] or target["session_id"].lower() not in reason or not any(
                        phrase in reason for phrase in ("not loaded", "not found", "does not exist")):
                    raise WorkspaceRpcError("Checkpointed delete state is unconfirmed") from error
                states.add(True)
                continue
            thread = result.get("thread") if isinstance(result, dict) else None
            path = thread.get("path") if isinstance(thread, dict) else None
            raw_cwd = thread.get("cwd") if isinstance(thread, dict) else None
            if (not isinstance(thread, dict) or thread.get("id") != target["session_id"]
                    or not isinstance(path, str) or not isinstance(raw_cwd, str)
                    or Path(raw_cwd).resolve() != Path(target["cwd"]).resolve()):
                raise WorkspaceRpcError("Checkpointed delete identity or project did not match")
            resolved, archived = _store_state(path, home)
            if (resolved != expected_path or archived is not target["archived"]
                    or stored_by_name[expected_path.name] != [expected_path] or not resolved.is_file()):
                raise WorkspaceRpcError("Checkpointed delete transcript is unavailable")
            states.add(False)
            threads.append({**thread, "path": str(resolved), "cwd": str(Path(raw_cwd).resolve()),
                            "archived": archived})
        if len(states) != 1:
            raise WorkspaceRpcError("Delete family is partially removed; automatic recovery is unavailable")
        deleted = states.pop()
        expected_descendants = {target["session_id"] for target in targets[1:]}
        descendants = {
            *[row["id"] for row in await _descendants(rpc, session_id, False)],
            *[row["id"] for row in await _descendants(rpc, session_id, True)],
        }
        if deleted:
            if descendants:
                raise WorkspaceRpcError("Checkpointed delete descendants remain")
        else:
            if descendants != expected_descendants:
                raise WorkspaceRpcError("Checkpointed delete family no longer matches native state")
            for thread in threads:
                ownership_guard(thread, rpc.process.pid)
        if family_guard is not None:
            family_guard(threads)
        loaded = await rpc.request("thread/loaded/list", {})
        if not isinstance(loaded, dict) or loaded.get("data") != []:
            raise WorkspaceRpcError("Delete inspection unexpectedly loaded a coding thread")
    finally:
        try:
            if rpc is not None:
                await rpc.close()
                if rpc.process is not None:
                    raise WorkspaceRpcError("Delete inspection cleanup is unconfirmed")
        finally:
            for lease in reversed(leases):
                lease.release()
    return {**checkpoint, "deleted": deleted, "thread_ids": [target["session_id"] for target in targets]}


async def restore_codex_archive(session_id, cwd, *, confirmed=False, binary=None, env=None,
                                rpc_factory=WorkspaceRpc, lease_factory=SessionLease, inspect_only=False):
    if type(inspect_only) is not bool or confirmed is not True or not isinstance(session_id, str) or str(UUID(session_id)) != session_id:
        raise ValueError("Explicit confirmation and an exact archived session ID are required")
    project = Path(cwd)
    if not project.is_absolute() or not project.is_dir():
        raise ValueError("An existing absolute project directory is required")
    project = project.resolve()
    executable = binary or shutil.which('codex')
    if not executable:
        raise WorkspaceRpcError("Codex executable is unavailable")
    runtime_env = strip_metered_auth_env(dict(os.environ if env is None else env))
    home = Path(runtime_env.get('CODEX_HOME') or Path.home() / '.codex').resolve()

    def exact_thread(result, directory):
        thread = result.get('thread') if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get('id') != session_id:
            raise WorkspaceRpcError("Native archive identity did not match")
        if not isinstance(thread.get('cwd'), str) or Path(thread['cwd']).resolve() != project:
            raise WorkspaceRpcError("Native archived session belongs to another project")
        path = thread.get('path')
        if not isinstance(path, str) or not Path(path).resolve().is_relative_to(home / directory):
            raise WorkspaceRpcError("Native transcript is not in the expected archive state")

    lease = lease_factory(session_id)
    rpc = None
    try:
        rpc = rpc_factory()
        lease.launching()
        await rpc.start([executable, 'app-server', '--stdio'], cwd=project, env=runtime_env)
        lease.bind(rpc.process.pid)
        await rpc.request('initialize', {'clientInfo': {'name': 'serena-workspace-archive', 'version': '1'},
                                         'capabilities': {'experimentalApi': True}})
        await rpc.notify('initialized', {})
        read = await rpc.request('thread/read', {'threadId': session_id, 'includeTurns': False})
        archived = False
        if inspect_only:
            thread = read.get('thread') if isinstance(read, dict) else None
            path = thread.get('path') if isinstance(thread, dict) else None
            archived = isinstance(path, str) and Path(path).resolve().is_relative_to(home / 'archived_sessions')
            exact_thread(read, 'archived_sessions' if archived else 'sessions')
        else:
            exact_thread(read, 'archived_sessions')
            result = await rpc.request('thread/unarchive', {'threadId': session_id})
            restored = result.get('thread') if isinstance(result, dict) else None
            if not isinstance(restored, dict) or restored.get('id') != session_id:
                raise WorkspaceRpcError("Native archive restoration was not confirmed")
            exact_thread(await rpc.request('thread/read', {'threadId': session_id, 'includeTurns': False}), 'sessions')
        loaded = await rpc.request('thread/loaded/list', {})
        if not isinstance(loaded, dict) or loaded.get('data') != []:
            raise WorkspaceRpcError("Archive maintenance unexpectedly loaded a coding thread")
    finally:
        try:
            if rpc is not None:
                await rpc.close()
                if rpc.process is not None:
                    raise WorkspaceRpcError("Archive maintenance cleanup is unconfirmed")
        finally:
            lease.release()
    return {'session_id': session_id, 'provider': 'codex', 'cwd': str(project), 'archived': archived}
