"""Explicit owner for existing Google ACP sessions, not CLI-session migration."""

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from core.billing import strip_metered_auth_env
from core.workspace_acp import WorkspaceAcpRpc
from core.workspace_acp_session import AcpSession
from core.workspace_admission import reject_unregistered_provider
from core.workspace_lease import SessionLease


class GeminiWorkspace:
    def __init__(self, *, session_id, cwd, gemini_home, binary, publish,
                 rpc=None, lease_factory=SessionLease):
        if str(UUID(session_id)) != session_id:
            raise ValueError("Canonical persisted Gemini session ID required")
        self.session_id = session_id
        self.cwd = Path(cwd).resolve(strict=True)
        self.home = Path(gemini_home).resolve(strict=True)
        self.binary = Path(binary).resolve(strict=True)
        self.publish = publish
        self.rpc = rpc or WorkspaceAcpRpc()
        self.session = AcpSession(session_id=session_id, cwd=self.cwd, rpc=self.rpc, publish=publish)
        self._lease_factory, self._lease = lease_factory, None
        self._lifecycle = asyncio.Lock()
        self._attempted = False

    @property
    def state(self):
        return self.session.state

    def _validate_native_target(self):
        root = self.home / "antigravity-acp"
        transcript = root / "conversations" / f"{self.session_id}.db"
        metadata = root / "conversations" / f"{self.session_id}.meta"
        for path in (transcript, metadata, root / "settings.json", root / "acp_token.json"):
            if not path.is_file() or path.resolve() != path:
                raise ValueError("Exact ACP session, subscription sign-in or native settings unavailable; no migration or login was attempted")
        if transcript.stat().st_nlink != 1:
            raise ValueError("ACP trajectory has another filesystem alias; migration is not verified")
        settings = json.loads((root / "settings.json").read_text())
        if settings.get("auth", {}).get("type") != "oauth-personal":
            raise ValueError("Gemini workspace requires existing Google-account subscription settings")
        saved = json.loads(metadata.read_text())
        if not isinstance(saved.get("cwd"), str) or Path(saved["cwd"]).resolve() != self.cwd:
            raise ValueError("ACP session belongs to a different project")
        return transcript

    async def open(self, *, env=None):
        async with self._lifecycle:
            if self._attempted:
                raise ValueError("Gemini attachment was already attempted; explicit recovery required")
            transcript = self._validate_native_target()
            reject_unregistered_provider(self.session_id, self.cwd, transcript, "agy")
            self._attempted = True
            try:
                self._lease = self._lease_factory(self.session_id)
                self._lease.launching()
                child_env = strip_metered_auth_env(dict(os.environ if env is None else env))
                for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                             "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
                    child_env.pop(name, None)
                child_env["GEMINI_HOME"] = str(self.home)
                await self.rpc.start([str(self.binary), *(["--uid="] if os.name != "nt" else [])], cwd=self.cwd, env=child_env)
                self._lease.bind(self.rpc.process.pid)
                initialization = await self.rpc.initialize()
                if initialization.get("agentInfo", {}).get("name") != "antigravity-acp":
                    raise ValueError("Unexpected Gemini ACP server")
                self.session.start_event_reader()
                return await self.session.load(initialization, mcp_servers=[])
            except BaseException:
                await self._close()
                raise

    async def _close(self):
        await self.session.stop_event_reader()
        try:
            await self.rpc.close()
        finally:
            self.session.state = "unavailable"
            if self._lease is not None:
                self._lease.release()
                self._lease = None

    async def close(self):
        """Explicit native shutdown; never a browser/window-disposal callback."""
        async with self._lifecycle:
            await self._close()
