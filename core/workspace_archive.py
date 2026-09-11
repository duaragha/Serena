"""Native archive maintenance without attaching a coding-session writer.

Callers must claim a durable mutation receipt before invoking restoration.
An ambiguous native result is never retried here.
"""
import os
import shutil
from pathlib import Path
from uuid import UUID

from core.billing import strip_metered_auth_env
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc, WorkspaceRpcError


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
