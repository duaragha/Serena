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
        self._turn_task = None

    @property
    def active_turn(self):
        return self.session.events.turn if self.session.state in {"running", "cancelling"} else None

    @property
    def questions(self):
        return self.session.events.questions

    async def submit(self, inputs, *, options=None):
        async with self._lifecycle:
            if options is not None and (not isinstance(options, dict) or options.keys() - {"model"}):
                raise ValueError("Unsupported Gemini per-turn settings")
            if self.state != "ready" or (self._turn_task and not self._turn_task.done()):
                raise ValueError("Gemini is not ready for input")
            if options and "model" in options:
                await self.session.set_model(options["model"])
            previous_turn = self.session.last_turn_id
            self._turn_task = asyncio.create_task(self._run_prompt(inputs))
            await asyncio.sleep(0)
            if self.session.last_turn_id == previous_turn:
                await self._turn_task
                raise ValueError("Gemini prompt was not admitted")
            return {"turn": {"id": self.session.last_turn_id}}

    async def _run_prompt(self, inputs):
        try:
            return await self.session.prompt(inputs)
        except Exception as error:
            self.session.state = "unavailable"
            await self.publish(self.session.events.event("workspace/transportClosed", {"reason": str(error)}))

    async def interrupt(self):
        await self.session.cancel()
        return {}

    async def list_models(self):
        if self.state not in {"ready", "running", "cancelling"}:
            raise ValueError("Gemini is not attached")
        return await self.session.publish_model_state()

    async def answer(self, request_id, answer):
        if not isinstance(answer, dict) or set(answer) != {"outcome"}:
            raise ValueError("Invalid ACP permission answer")
        outcome = answer["outcome"]
        if outcome == {"outcome": "cancelled"}:
            option = None
        elif isinstance(outcome, dict) and set(outcome) == {"outcome", "optionId"} and outcome["outcome"] == "selected" and isinstance(outcome["optionId"], str):
            option = outcome["optionId"]
        else:
            raise ValueError("Invalid ACP permission outcome")
        await self.session.answer(request_id, option)
        return {}

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
            if self._turn_task is not None:
                if not self._turn_task.done():
                    self._turn_task.cancel()
                await asyncio.gather(self._turn_task, return_exceptions=True)
        finally:
            self.session.state = "unavailable"
            if self._lease is not None:
                self._lease.release()
                self._lease = None

    async def close(self):
        """Explicit native shutdown; never a browser/window-disposal callback."""
        async with self._lifecycle:
            await self._close()
