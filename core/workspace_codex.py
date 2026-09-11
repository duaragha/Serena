"""Codex rich-client control, separate from the restricted resident brain.

This adapter holds a session lease shared with the PTY registry.
This module has no UI/socket-disconnect lifecycle and never falls back to a new
thread when resumption fails. Raw protocol events are retained for the renderer.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections import deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from core.billing import strip_metered_auth_env
from core.workspace_codex_auth import CodexLoginLease
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc, WorkspaceRpcError


class CodexWorkspace:
    def __init__(
        self,
        *,
        session_id: str,
        cwd: Path,
        publish: Callable[[dict], Awaitable[None]],
        rpc: WorkspaceRpc | None = None,
        lease_factory: Callable[[str], SessionLease] = SessionLease,
    ) -> None:
        if not session_id or not cwd.is_dir():
            raise ValueError("A persisted session ID and existing project directory are required")
        self.session_id = session_id
        self.cwd = cwd.resolve()
        self.rpc = rpc or WorkspaceRpc()
        self.publish = publish
        self._lease_factory = lease_factory
        self._lease: SessionLease | None = None
        self.thread: dict | None = None
        self.settings: dict = {}
        self.model_catalog: list[dict] | None = None
        self.active_turn: str | None = None
        self.state = "closed"
        self.questions: dict[int | str, dict] = {}
        self._completed: deque[str] = deque(maxlen=64)
        self._control_lock = asyncio.Lock()
        self._events_task: asyncio.Task | None = None
        self._history_ready = asyncio.Event()
        self._fork_ready = asyncio.Event()
        self._fork_ready.set()
        self._fork_ids: set[str] = set()
        self._agent_ids: set[str] = set()
        self.active_agent_threads: set[str] = set()
        self.history_cursor: str | None = None
        self._history_revision = 0
        self._history_cursors: set[str] = set()
        self._mcp_logins: dict[str, dict] = {}
        self._create_attempted = False
        self._account_login = None
        self._account_login_lease = None
        self._early_login_completions = {}
        self._account_update = None
        self._account_updated = asyncio.Event()

    @staticmethod
    def _safe_account_status(result):
        if not isinstance(result, dict) or type(result.get("requiresOpenaiAuth")) is not bool:
            raise WorkspaceRpcError("Codex returned invalid account status")
        account = result.get("account")
        if account is not None and (not isinstance(account, dict) or not isinstance(account.get("type"), str)):
            raise WorkspaceRpcError("Codex returned invalid account identity")
        # Never forward credentials or claim a stored login proves token validity.
        safe = None if account is None else {
            key: account[key]
            for key in ("type", "email", "planType")
            if isinstance(account.get(key), str)
        }
        return {
            "account": safe,
            "requiresOpenaiAuth": result["requiresOpenaiAuth"],
            "credentialsVerified": False,
        }

    async def account_status(self):
        async with self._control_lock:
            if self.state in {"closed", "opening", "unavailable"}:
                raise WorkspaceRpcError("Attach the Codex session before checking its account")
            result = await self.rpc.request("account/read", {"refreshToken": False})
            return {**self._safe_account_status(result), "login": deepcopy(self._account_login)}

    async def logout_account(self, *, notification_timeout=5):
        async with self._control_lock:
            if self.state != "ready" or self.active_turn or self.questions or self.active_agent_threads:
                raise WorkspaceRpcError("Finish Codex work before signing out")
            if (self._account_login
                    and self._account_login.get("status") in {"pending", "uncertain"}):
                raise WorkspaceRpcError("Finish or cancel browser sign-in before signing out")
            if (type(notification_timeout) not in {int, float}
                    or not 0 < notification_timeout <= 30):
                raise ValueError("A bounded account notification timeout is required")
            self._account_update = None
            self._account_updated.clear()
            result = await self.rpc.request("account/logout", None)
            if result != {}:
                raise WorkspaceRpcError("Codex did not confirm account logout")
            try:
                await asyncio.wait_for(self._account_updated.wait(), notification_timeout)
            except TimeoutError as error:
                raise WorkspaceRpcError("Codex did not report the completed account logout") from error
            if self._account_update != {"authMode": None, "planType": None}:
                raise WorkspaceRpcError("Codex reported an unexpected account after logout")
            status = self._safe_account_status(
                await self.rpc.request("account/read", {"refreshToken": False})
            )
            if status["account"] is not None:
                raise WorkspaceRpcError("Codex still reports a signed-in account")
            self._account_login = None
            self._early_login_completions.clear()
            return {"loggedOut": True}

    async def account_rate_limits(self):
        async with self._control_lock:
            if self.state in {"closed", "opening", "unavailable"}:
                raise WorkspaceRpcError("Attach Codex before checking account limits")
            result = await self.rpc.request("account/rateLimits/read", {})
            if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
                raise WorkspaceRpcError("Codex returned invalid account limits")
            buckets = result.get("rateLimitsByLimitId")
            if buckets is None or buckets == {}:
                buckets = {"codex": result["rateLimits"]}
            if not isinstance(buckets, dict) or len(buckets) > 100:
                raise WorkspaceRpcError("Codex returned invalid limit buckets")
            limits = []
            for key, source in buckets.items():
                if not isinstance(key, str) or not isinstance(source, dict):
                    raise WorkspaceRpcError("Codex returned invalid limit bucket")
                item = {"id": key, "name": source.get("limitName") or key}
                if not isinstance(item["name"], str):
                    raise WorkspaceRpcError("Codex returned invalid limit name")
                for name in ("primary", "secondary"):
                    window = source.get(name)
                    item[name] = None
                    if window is None:
                        continue
                    if (not isinstance(window, dict) or type(window.get("usedPercent")) is not int
                            or window["usedPercent"] < 0):
                        raise WorkspaceRpcError("Codex returned invalid usage percentage")
                    item[name] = {"usedPercent": window["usedPercent"]}
                    for field in ("windowDurationMins", "resetsAt"):
                        value = window.get(field)
                        if value is not None and (type(value) is not int or value < 0):
                            raise WorkspaceRpcError("Codex returned invalid usage window")
                        item[name][field] = value
                limits.append(item)
            safe = {"limits": limits, "observedAt": datetime.now(timezone.utc).isoformat()}
            await self.publish({"method": "workspace/accountLimits", "params": deepcopy(safe)})
            return safe

    async def account_token_usage(self):
        async with self._control_lock:
            if self.state in {"closed", "opening", "unavailable"}:
                raise WorkspaceRpcError("Attach Codex before checking account usage")
            result = await self.rpc.request("account/usage/read", {})
            if not isinstance(result, dict) or not isinstance(result.get("summary"), dict):
                raise WorkspaceRpcError("Codex returned invalid account usage")

            def count(value):
                if value is None:
                    return None
                if type(value) is not int or not 0 <= value <= 2**63 - 1:
                    raise WorkspaceRpcError("Codex returned an invalid usage counter")
                # Native int64 totals must not lose precision in JavaScript.
                return str(value)

            fields = ("lifetimeTokens", "peakDailyTokens", "longestRunningTurnSec", "currentStreakDays", "longestStreakDays")
            summary = {key: count(result["summary"].get(key)) for key in fields}
            buckets = result.get("dailyUsageBuckets")
            daily = None
            if buckets is not None:
                if not isinstance(buckets, list) or len(buckets) > 10000:
                    raise WorkspaceRpcError("Codex returned invalid daily usage")
                daily, seen = [], set()
                for bucket in buckets:
                    if not isinstance(bucket, dict) or not isinstance(bucket.get("startDate"), str):
                        raise WorkspaceRpcError("Codex returned an invalid usage date")
                    day = bucket["startDate"]
                    try:
                        valid = datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") == day
                    except ValueError:
                        valid = False
                    if not valid or day in seen:
                        raise WorkspaceRpcError("Codex returned an invalid or duplicate usage date")
                    tokens = count(bucket.get("tokens"))
                    if tokens is None:
                        raise WorkspaceRpcError("Codex returned a missing daily usage counter")
                    seen.add(day)
                    daily.append({"startDate": day, "tokens": tokens})
                daily.sort(key=lambda item: item["startDate"], reverse=True)
            return {"summary": summary, "dailyUsageBuckets": daily,
                    "observedAt": datetime.now(timezone.utc).isoformat()}

    async def thread_token_usage(self):
        async with self._control_lock:
            if self.state in {"closed", "opening", "unavailable"}:
                raise WorkspaceRpcError("Attach Codex before checking session usage")
            result = await self.rpc.request("account/usage/read", {"threadId": self.session_id})
            if not isinstance(result, dict):
                raise WorkspaceRpcError("Codex returned invalid session usage")
            usage = result.get("threadUsage")
            observed = datetime.now(timezone.utc).isoformat()
            if usage is None:
                return {"threadUsage": None, "observedAt": observed}
            if not isinstance(usage, dict) or usage.get("threadId") != self.session_id:
                raise WorkspaceRpcError("Codex returned usage for a different session")

            def count(value, required=False):
                if value is None and not required:
                    return None
                if type(value) is not int or not 0 <= value <= 2**63 - 1:
                    raise WorkspaceRpcError("Codex returned an invalid session usage counter")
                return str(value)

            groups = usage.get("groups")
            if not isinstance(groups, list) or len(groups) > 1000:
                raise WorkspaceRpcError("Codex returned invalid session usage groups")
            safe = {"threadId": self.session_id,
                    "estimatedUsageCreditsMicros": count(usage.get("estimatedUsageCreditsMicros"), True),
                    "estimatedUsageUsdMicros": count(usage.get("estimatedUsageUsdMicros")), "groups": []}
            for group in groups:
                if not isinstance(group, dict):
                    raise WorkspaceRpcError("Codex returned an invalid session usage group")
                row = {key: count(group.get(key), key == "estimatedUsageCreditsMicros") for key in (
                    "estimatedUsageCreditsMicros", "cachedInputTokens", "inputTokens", "netNewInputTokens", "outputTokens", "totalTokens")}
                for key in ("model", "reasoningEffort", "speed"):
                    value = group.get(key)
                    if value is not None and (not isinstance(value, str) or len(value) > 256):
                        raise WorkspaceRpcError("Codex returned invalid usage group metadata")
                    row[key] = value
                safe["groups"].append(row)
            return {"threadUsage": safe, "observedAt": observed}

    def _finish_account_login(self, params):
        if (self._account_login is None or self._account_login["status"] not in {"pending", "uncertain"}
                or not isinstance(params.get("loginId"), str)
                or type(params.get("success")) is not bool):
            return
        login_id = params["loginId"]
        if not self._account_login.get("loginId"):
            if len(self._early_login_completions) < 8:
                self._early_login_completions[login_id] = deepcopy(params)
            return
        if login_id != self._account_login["loginId"]:
            return
        self._account_login = {"loginId": login_id, "status": "succeeded" if params["success"] else "failed"}
        if self._account_login_lease:
            self._account_login_lease.confirmed_finished()
            self._account_login_lease = None

    async def login_account(self):
        async with self._control_lock:
            if self.state != "ready" or self.questions:
                raise WorkspaceRpcError("Finish the current Codex turn before signing in")
            if self._account_login and self._account_login["status"] in {"pending", "uncertain"}:
                return deepcopy(self._account_login)
            # One callback operation across panes/processes. Never kill another
            # login or automatically log out an existing account to acquire it.
            directory = self._lease.metadata.parent if self._lease else None
            self._account_login_lease = CodexLoginLease("codex-browser-login", directory=directory)
            try:
                self._account_login_lease.bind(self.rpc.process.pid)
                self._account_login = {"status": "pending"}
                self._early_login_completions.clear()
                result = await self.rpc.request("account/login/start", {"type": "chatgpt"})
                if not isinstance(result, dict) or result.get("type") != "chatgpt":
                    raise WorkspaceRpcError("Codex returned an invalid browser login")
                login_id, url = result.get("loginId"), result.get("authUrl")
                if not isinstance(login_id, str) or not login_id or not isinstance(url, str):
                    raise WorkspaceRpcError("Codex did not return a browser login identity")
                self._account_login["loginId"] = login_id
                parsed = urlsplit(url)
                if (parsed.scheme != "https" or parsed.hostname not in {"auth.openai.com", "auth0.openai.com", "chatgpt.com"}
                        or parsed.username or parsed.password or parsed.port not in {None, 443}):
                    raise WorkspaceRpcError("Codex returned an unsafe authorization URL")
                self._account_login["authUrl"] = url
                completion = self._early_login_completions.pop(login_id, None)
                if completion:
                    self._finish_account_login(completion)
                return deepcopy(self._account_login)
            except BaseException:
                self._account_login = {**(self._account_login or {}), "status": "uncertain"}
                raise

    async def cancel_account_login(self, login_id):
        async with self._control_lock:
            if (not isinstance(login_id, str) or not login_id or not self._account_login
                    or self._account_login.get("loginId") != login_id
                    or self._account_login["status"] not in {"pending", "uncertain"}):
                raise ValueError("No matching pending browser login")
            result = await self.rpc.request("account/login/cancel", {"loginId": login_id})
            if not isinstance(result, dict) or result.get("status") not in {"canceled", "notFound"}:
                raise WorkspaceRpcError("Codex did not confirm login cancellation")
            if self._account_login["status"] == "succeeded":
                return deepcopy(self._account_login)
            self._finish_account_login({"loginId": login_id, "success": False})
            self._account_login["status"] = "cancelled"
            return deepcopy(self._account_login)

    def _same_project(self, value):
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            return False
        # Do not probe arbitrary provider-supplied paths (including UNC shares).
        # Accept spelling differences only within the already admitted path.
        if os.path.normcase(os.path.normpath(value)) != os.path.normcase(str(self.cwd)):
            return False
        try:
            return Path(value).samefile(self.cwd)
        except (OSError, ValueError):
            return False

    async def _history_page(self, cursor=None):
        params = {"threadId": self.session_id, "limit": 50, "sortDirection": "desc", "itemsView": "full"}
        if cursor is not None:
            params["cursor"] = cursor
        page = await self.rpc.request("thread/turns/list", params)
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise WorkspaceRpcError("Codex returned invalid paginated history")
        ids = set()
        for turn in page["data"]:
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"] or turn["id"] in ids or not isinstance(turn.get("items"), list):
                raise WorkspaceRpcError("Codex returned invalid history turns")
            ids.add(turn["id"])
        following = page.get("nextCursor")
        if following is not None and (not isinstance(following, str) or not following or following == cursor or following in self._history_cursors):
            raise WorkspaceRpcError("Codex history pagination did not advance")
        return {"turns": list(reversed(page["data"])), "historyCursor": following}

    async def load_earlier(self, cursor):
        async with self._control_lock:
            if self.state in {"closed", "opening", "unavailable", "reconciling"} or not cursor or cursor != self.history_cursor:
                raise WorkspaceRpcError("History cursor is stale or session unavailable")
            revision = self._history_revision
            page = await self._history_page(cursor)
            if revision != self._history_revision:
                raise WorkspaceRpcError("History changed while loading earlier turns")
            await self.publish({"method": "workspace/historyPage", "params": {"threadId": self.session_id, 'historyRevision': revision, **deepcopy(page)}})
            if revision != self._history_revision:
                raise WorkspaceRpcError("History changed while publishing earlier turns")
            self._history_cursors.add(cursor)
            self.history_cursor = page["historyCursor"]
            return page

    async def fork_session(self):
        async with self._control_lock:
            if self.state != "ready" or self.questions:
                raise WorkspaceRpcError("Finish the current Codex turn before forking")
            self._fork_ready.clear()
            try:
                result = await self.rpc.request("thread/fork", {"threadId": self.session_id})
                thread = result.get("thread") if isinstance(result, dict) else None
                sid = thread.get("id") if isinstance(thread, dict) else None
                try:
                    valid = isinstance(sid, str) and str(UUID(sid)) == sid
                except ValueError:
                    valid = False
                if not valid or sid == self.session_id or not self._same_project(thread.get("cwd")):
                    raise WorkspaceRpcError("Native fork returned an invalid identity or project")
                self._fork_ids.add(sid)
                return {"session_id": sid, "provider": "codex", "cwd": str(self.cwd)}
            finally:
                self._fork_ready.set()

    async def open(self, *, binary: str | None = None, env: dict[str, str] | None = None) -> dict:
        if self.session_id.startswith("new:"):
            raise WorkspaceRpcError("New sessions require explicit creation")
        return await self._open(binary=binary, env=env)

    async def create(self, *, checkpoint: Callable[[dict], Awaitable[None]], binary=None, env=None) -> dict:
        """Create once under a caller-reserved request identity, never as resume fallback.

        The host must durably reserve the creation request before calling, then
        persist the returned native identity in checkpoint before input is enabled.
        An uncertain attempt must not be retried with another owner instance.
        """
        try:
            valid = self.session_id.startswith("new:") and str(UUID(self.session_id[4:])) == self.session_id[4:]
        except ValueError:
            valid = False
        if not valid or not callable(checkpoint):
            raise ValueError("Creation requires a reserved new UUID and durable checkpoint callback")
        return await self._open(binary=binary, env=env, checkpoint=checkpoint)

    async def _open(self, *, binary=None, env=None, checkpoint=None) -> dict:
        async with self._control_lock:
            if self.state != "closed":
                raise WorkspaceRpcError("Session already opened or awaiting recovery")
            if checkpoint is not None:
                if self._create_attempted:
                    raise WorkspaceRpcError("Creation was already attempted; recover its recorded identity")
                self._create_attempted = True
            executable = binary or shutil.which("codex")
            if not executable:
                raise WorkspaceRpcError("Codex executable is unavailable")
            self.state = "opening"
            self._history_ready.clear()
            try:
                self._lease = self._lease_factory(self.session_id)
                self._lease.launching()
                await self.rpc.start(
                    [executable, "app-server", "--stdio"],
                    cwd=self.cwd,
                    env=strip_metered_auth_env(dict(os.environ if env is None else env)),
                )
                self._lease.bind(self.rpc.process.pid)
                await self.rpc.request(
                    "initialize",
                    {
                        "clientInfo": {"name": "serena-workspace", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                await self.rpc.notify("initialized", {})
                self._events_task = asyncio.create_task(self._events())
                # Do not set baseInstructions, disable coding tools, or override
                # the session's configured model and permission policy here.
                if checkpoint is None:
                    result = await self.rpc.request("thread/resume", {"threadId": self.session_id})
                else:
                    result = await self.rpc.request("thread/start", {"cwd": str(self.cwd), "ephemeral": False})
                    thread = result.get("thread") if isinstance(result, dict) else None
                    sid = thread.get("id") if isinstance(thread, dict) else None
                    try:
                        valid = isinstance(sid, str) and str(UUID(sid)) == sid
                    except ValueError:
                        valid = False
                    if (not valid or not self._same_project(thread.get("cwd"))
                            or thread.get("ephemeral") is not False or thread.get("turns") != []):
                        raise WorkspaceRpcError("Codex returned an invalid new thread")
                    await checkpoint({"session_id": sid, "provider": "codex", "cwd": str(self.cwd)})
                    self._lease = self._lease.transfer_after_transition(sid)
                    self.session_id = sid
                thread = result.get("thread") if isinstance(result, dict) else None
                if not isinstance(thread, dict) or thread.get("id") != self.session_id:
                    raise WorkspaceRpcError(
                        "Codex returned a different session; refusing attachment"
                    )
                if checkpoint is None and thread.get("historyMode") == "paginated":
                    page = await self._history_page()
                    turns = {turn["id"]: turn for turn in page["turns"]}
                    turns.update({turn["id"]: turn for turn in thread.get("turns", [])
                                  if turn["id"] in turns or turn.get("status") == "inProgress"})
                    thread["turns"] = list(turns.values())
                    self.history_cursor = result["historyCursor"] = page["historyCursor"]
                self.thread = deepcopy(thread)
                self.settings = {
                    key: deepcopy(result[key])
                    for key in ("model", "reasoningEffort", "serviceTier")
                    if key in result
                }
                for turn in thread.get("turns", []):
                    if turn.get("status") == "inProgress":
                        self.active_turn = turn["id"]
                if self.state == "opening":
                    self.state = "running" if self.active_turn else "ready"
                await self.publish({"method": "workspace/history", "params": deepcopy(result)})
                self._history_ready.set()
                return deepcopy(result)
            except BaseException:
                await self._close()
                raise

    async def list_agents(self, cursor=None):
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Attach Codex before inspecting agents")
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096):
            raise ValueError("Invalid agent list cursor")
        result = await self.rpc.request("thread/list", {
            "ancestorThreadId": self.session_id, "limit": 50, "cursor": cursor,
            "sourceKinds": ["subAgent", "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther"],
        })
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise WorkspaceRpcError("Codex returned an invalid agent list")
        seen = {self.session_id}
        for thread in result["data"]:
            if (not isinstance(thread, dict) or not isinstance(thread.get("id"), str)
                    or not thread["id"] or thread["id"] in seen or not thread.get("parentThreadId")):
                raise WorkspaceRpcError("Codex returned an invalid agent identity")
            seen.add(thread["id"])
        following = result.get("nextCursor")
        if following is not None and (not isinstance(following, str) or not following or following == cursor):
            raise WorkspaceRpcError("Agent list pagination did not advance")
        return deepcopy(result)

    async def _descendant_thread(self, thread_id):
        if not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 256 or thread_id == self.session_id:
            raise ValueError("An exact child agent is required")
        # A row or caller-supplied ID is not authority to read another conversation.
        # Verify its native ancestry through this existing parent's connection.
        seen = {self.session_id}
        current = thread_id
        selected = None
        for _ in range(32):
            if current in seen:
                raise WorkspaceRpcError("Agent ancestry contains a cycle")
            seen.add(current)
            result = await self.rpc.request("thread/read", {"threadId": current, "includeTurns": False})
            thread = result.get("thread") if isinstance(result, dict) else None
            if not isinstance(thread, dict) or thread.get("id") != current:
                raise WorkspaceRpcError("Codex returned a different agent")
            if selected is None:
                selected = deepcopy(thread)
            parent = thread.get("parentThreadId")
            if parent == self.session_id:
                break
            if not isinstance(parent, str) or not 1 <= len(parent) <= 256:
                raise WorkspaceRpcError("Agent does not belong to this conversation")
            current = parent
        else:
            raise WorkspaceRpcError("Agent ancestry exceeds the inspection limit")
        # Thread ancestry is immutable. Bound the cache; evicted IDs are rechecked.
        if len(self._agent_ids) >= 4096:
            self._agent_ids.clear()
        self._agent_ids.add(thread_id)
        if selected.get("status", {}).get("type") not in {"idle", "notLoaded"}:
            self.active_agent_threads.add(thread_id)
        return selected

    async def inspect_agent(self, thread_id, cursor=None):
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Attach Codex before inspecting agents")
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096):
            raise ValueError("A valid agent history cursor is required")
        selected = await self._descendant_thread(thread_id)
        page = await self.rpc.request("thread/turns/list", {
            "threadId": thread_id, "limit": 50, "sortDirection": "desc", "itemsView": "full", "cursor": cursor,
        })
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise WorkspaceRpcError("Codex returned invalid agent history")
        ids = set()
        for turn in page["data"]:
            if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]
                    or turn["id"] in ids or not isinstance(turn.get("items"), list)):
                raise WorkspaceRpcError("Codex returned invalid agent turns")
            ids.add(turn["id"])
        following = page.get("nextCursor")
        if following is not None and (not isinstance(following, str) or not following or following == cursor):
            raise WorkspaceRpcError("Agent history pagination did not advance")
        selected["turns"] = list(reversed(deepcopy(page["data"])))
        return {"thread": selected, "historyCursor": following}

    async def steer_agent(self, thread_id, expected_turn_id, text=None, *, inputs=None):
        if inputs is not None:
            if text is not None or not isinstance(inputs, list) or not 1 <= len(inputs) <= 17:
                raise ValueError("An agent message or validated attachments are required")
            inputs = deepcopy(inputs)
            text = "".join(part.get("text", "") for part in inputs if part.get("type") == "text")
            has_attachment = any(part.get("type") == "localImage" for part in inputs)
        else:
            has_attachment = False
            inputs = [{"type": "text", "text": text}]
        if (not isinstance(expected_turn_id, str) or not 1 <= len(expected_turn_id) <= 256
                or not isinstance(text, str) or (not text.strip() and not has_attachment)
                or "\0" in text or len(text.encode()) > 65536):
            raise ValueError("An exact active agent turn and message of at most 64 KiB are required")
        self._reject_unrouted_command(inputs)
        async with self._control_lock:
            if self.state not in {"ready", "running"}:
                raise WorkspaceRpcError("Parent connection is not available for agent controls")
            snapshot = await self.inspect_agent(thread_id)
            running = [turn["id"] for turn in snapshot["thread"]["turns"] if turn.get("status") == "inProgress"]
            if running != [expected_turn_id] or self.state not in {"ready", "running"}:
                raise WorkspaceRpcError("Agent turn changed; refresh before sending")
            result = await self.rpc.request("turn/steer", {
                "threadId": thread_id, "expectedTurnId": expected_turn_id, "input": inputs,
            })
            if not isinstance(result, dict) or result.get("turnId") != expected_turn_id:
                raise WorkspaceRpcError("Agent message delivery was not confirmed")
            return {"threadId": thread_id, "turnId": expected_turn_id, "accepted": True}

    async def continue_agent(self, thread_id, expected_latest_turn_id, text, confirmed):
        if (confirmed is not True or not isinstance(expected_latest_turn_id, str)
                or not 1 <= len(expected_latest_turn_id) <= 256
                or not isinstance(text, str) or not text.strip() or "\0" in text or len(text.encode()) > 65536):
            raise ValueError("Confirm the exact idle agent and a message of at most 64 KiB")
        inputs = [{"type": "text", "text": text}]
        self._reject_unrouted_command(inputs)
        async with self._control_lock:
            if self.state != "ready" or self.active_turn or self.questions or self.active_agent_threads:
                raise WorkspaceRpcError("Wait for parent and delegated work before continuing an idle agent")
            snapshot = await self.inspect_agent(thread_id)
            thread = snapshot["thread"]
            turns = thread["turns"]
            if (thread.get("status", {}).get("type") != "idle" or not turns
                    or turns[-1]["id"] != expected_latest_turn_id
                    or any(turn.get("status") not in {"completed", "interrupted", "failed"} for turn in turns)
                    or self.state != "ready" or self.active_turn or self.questions or self.active_agent_threads):
                raise WorkspaceRpcError("Agent changed or is not loaded and idle; refresh before continuing")
            # Reserve before issuing the request: a fast native completion may
            # arrive before the response, and must remain authoritative.
            self.active_agent_threads.add(thread_id)
            try:
                result = await self.rpc.request("turn/start", {"threadId": thread_id, "input": inputs})
                turn = result.get("turn") if isinstance(result, dict) else None
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
                    raise WorkspaceRpcError("Agent continuation was not confirmed")
                return {"accepted": True, "threadId": thread_id, "turnId": turn["id"]}
            except BaseException:
                # Do not make an ambiguous start eligible for a second writer.
                self.state = "uncertain"
                raise

    async def interrupt_agent(self, thread_id, expected_turn_id, confirmed):
        if confirmed is not True or not isinstance(expected_turn_id, str) or not 1 <= len(expected_turn_id) <= 256:
            raise ValueError("An exact agent turn and explicit stop confirmation are required")
        async with self._control_lock:
            if self.state not in {"ready", "running"}:
                raise WorkspaceRpcError("Parent connection is not available for agent controls")
            snapshot = await self.inspect_agent(thread_id)
            running = [turn["id"] for turn in snapshot["thread"]["turns"] if turn.get("status") == "inProgress"]
            if running != [expected_turn_id] or self.state not in {"ready", "running"}:
                raise WorkspaceRpcError("Agent turn changed; refresh before stopping it")
            result = await self.rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": expected_turn_id})
            if not isinstance(result, dict):
                raise WorkspaceRpcError("Agent interruption was not confirmed")
            # The acknowledgment is not completion; only native lifecycle events
            # may release the child's busy state or mark its turn interrupted.
            return {"requested": True, "threadId": thread_id, "turnId": expected_turn_id}

    async def rename(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 1000 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError("A title of 1 to 1000 characters without control characters is required")
        name = name.strip()
        async with self._control_lock:
            if self.state not in {"ready", "running"}:
                raise WorkspaceRpcError("Attach Codex before renaming the session")
            result = await self.rpc.request("thread/name/set", {"threadId": self.session_id, "name": name})
            if not isinstance(result, dict):
                raise WorkspaceRpcError("Codex did not confirm the rename")
            read = await self.rpc.request("thread/read", {"threadId": self.session_id, "includeTurns": False})
            thread = read.get("thread") if isinstance(read, dict) else None
            if not isinstance(thread, dict) or thread.get("id") != self.session_id or thread.get("name") != name:
                raise WorkspaceRpcError("Codex rename could not be verified; the native name may have changed")
            self.thread["name"] = name
            return {"session_id": self.session_id, "name": name}

    async def shell_command(self, command, confirmed):
        async with self._control_lock:
            if confirmed is not True:
                raise ValueError("Running outside the Codex sandbox requires explicit confirmation")
            if not isinstance(command, str) or not command.strip() or len(command) > 32768 or "\0" in command:
                raise ValueError("A non-empty shell command is required")
            if self.state not in {"ready", "running"} or self.questions:
                raise WorkspaceRpcError("Codex session is not available for shell input")
            if self.state == "ready":
                self.state = "submitting"
            try:
                result = await self.rpc.request("thread/shellCommand", {
                    "threadId": self.session_id, "command": command,
                })
                if not isinstance(result, dict):
                    raise WorkspaceRpcError("Shell command delivery was not confirmed")
                if self.state == "submitting":
                    self.state = "running"
                return {"accepted": True}
            except BaseException:
                self.state = "uncertain"
                raise

    async def revert_history(self, before_turn_id, expected_latest_turn_id, confirmed):
        if confirmed is not True or any(not isinstance(value, str) or not value or len(value) > 256
                                        for value in (before_turn_id, expected_latest_turn_id)):
            raise ValueError('An exact turn and explicit rewind confirmation are required')
        async with self._control_lock:
            if self.state != 'ready' or self.questions:
                raise WorkspaceRpcError('Finish the active turn before rewinding history')
            revision = self._history_revision
            if (await self.list_background_tasks())["data"]:
                raise WorkspaceRpcError('Stop background tasks before rewinding history')
            page = await self._history_page()
            if (self.state != 'ready' or revision != self._history_revision or not page['turns']
                    or page['turns'][-1]['id'] != expected_latest_turn_id):
                raise WorkspaceRpcError('History changed; reopen the rewind dialog')
            self.state = 'reverting'
            try:
                result = await self.rpc.request('thread/revert', {
                    'threadId': self.session_id, 'beforeTurnId': before_turn_id})
                thread = result.get('thread') if isinstance(result, dict) else None
                if not isinstance(thread, dict) or thread.get('id') != self.session_id:
                    raise WorkspaceRpcError('Native rewind was not confirmed; do not repeat it')
                async with asyncio.timeout(15):
                    while self._history_revision == revision or self.state == 'reconciling':
                        await asyncio.sleep(.01)
                if self.state != 'ready':
                    raise WorkspaceRpcError('Rewound history could not be loaded')
                return {'session_id': self.session_id, 'before_turn_id': before_turn_id, 'files_changed': False}
            except BaseException:
                self.state = 'uncertain'
                raise

    async def search_files(self, query):
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Codex session is unavailable")
        if not isinstance(query, str) or not query.strip() or len(query) > 200 or "\0" in query:
            raise ValueError("A file search of 1 to 200 characters is required")
        result = await self.rpc.request("fuzzyFileSearch", {"query": query, "roots": [str(self.cwd)]})
        if not isinstance(result, dict) or not isinstance(result.get("files"), list):
            raise WorkspaceRpcError("Codex returned invalid file search results")
        paths = []
        for item in result["files"]:
            if not isinstance(item, dict) or not self._same_project(item.get("root")) or not isinstance(item.get("path"), str):
                raise WorkspaceRpcError("Codex returned files outside the session project")
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise WorkspaceRpcError("Codex returned files outside the session project")
            path = self.cwd / relative
            if item.get("match_type") != "file" or not path.is_file() or not path.resolve().is_relative_to(self.cwd):
                continue
            if item["path"] not in paths:
                paths.append(item["path"])
            if len(paths) >= 100:
                break
        return {"paths": paths}

    async def permissions(self):
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        profiles, seen, cursors, cursor = [], set(), set(), None
        while True:
            params = {"cwd": str(self.cwd), "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self.rpc.request("permissionProfile/list", params)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise WorkspaceRpcError("Codex returned an invalid permission catalog")
            for profile in page["data"]:
                if not isinstance(profile, dict) or not isinstance(profile.get("id"), str) or not profile["id"] or type(profile.get("allowed")) is not bool or profile["id"] in seen:
                    raise WorkspaceRpcError("Codex returned an invalid permission profile")
                seen.add(profile["id"])
                profiles.append({"id": profile["id"], "allowed": profile["allowed"], "description": profile.get("description") or ""})
            cursor = page.get("nextCursor")
            if not cursor:
                return {"mode": self.settings.get("permissionProfile"), "modes": [p["id"] for p in profiles], "profiles": profiles}
            if not isinstance(cursor, str) or cursor in cursors or len(cursors) >= 100:
                raise WorkspaceRpcError("Permission catalog pagination did not advance")
            cursors.add(cursor)

    async def set_permissions(self, mode, confirmed=False):
        async with self._control_lock:
            if self.state != "ready" or self.questions:
                raise WorkspaceRpcError("Finish the current Codex turn before changing permissions")
            if confirmed is not True:
                raise ValueError("Changing a permission profile requires explicit confirmation")
            profiles = (await self.permissions())["profiles"]
            if not isinstance(mode, str) or not any(p["id"] == mode and p["allowed"] for p in profiles):
                raise ValueError("Permission profile is unavailable or blocked by policy")
            if self.state != "ready":
                raise WorkspaceRpcError("Session changed during permission lookup")
            result = await self.rpc.request("thread/settings/update", {"threadId": self.session_id, "permissions": mode})
            if not isinstance(result, dict):
                raise WorkspaceRpcError("Permission profile change was not confirmed")
            self.settings["permissionProfile"] = mode
            await self.publish({"method": "workspace/settings", "params": deepcopy(self.settings)})
            return {"mode": mode, "modes": [p["id"] for p in profiles], "profiles": profiles}

    async def list_session_modes(self):
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Attach Codex before inspecting session modes")
        result = await self.rpc.request("collaborationMode/list", {})
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise WorkspaceRpcError("Codex returned an invalid mode catalog")
        choices, seen = [], set()
        for item in result["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise WorkspaceRpcError("Codex returned an invalid mode preset")
            mode = item.get("mode")
            if mode not in {"plan", "default"}:
                continue
            if mode in seen:
                raise WorkspaceRpcError("Codex returned duplicate mode presets")
            seen.add(mode)
            choices.append({"value": mode, "name": item["name"]})
        return {"currentValue": self.settings.get("collaborationMode"), "options": choices}

    async def personality(self):
        models = (await self.list_models())["data"]
        model = next((item for item in models if item.get("model") == self.settings.get("model")), None)
        supported = bool(model and model.get("supportsPersonality") is True)
        return {"currentValue": self.settings.get("personality"),
                "options": ["none", "friendly", "pragmatic"] if supported else []}

    def _validated_goal(self, result):
        if not isinstance(result, dict) or "goal" not in result:
            raise WorkspaceRpcError("Codex returned no goal state")
        goal = result["goal"]
        if goal is None:
            return {"goal": None}
        if (not isinstance(goal, dict) or goal.get("threadId") != self.session_id
                or not isinstance(goal.get("objective"), str) or not 1 <= len(goal["objective"]) <= 4000
                or goal.get("status") not in ("active", "paused", "blocked", "usageLimited", "budgetLimited", "complete")
                or any(type(goal.get(key)) is not int or goal[key] < 0
                       for key in ("createdAt", "updatedAt", "tokensUsed", "timeUsedSeconds"))
                or (goal.get("tokenBudget") is not None and
                    (type(goal["tokenBudget"]) is not int or not 1 <= goal["tokenBudget"] <= 2**53 - 1))):
            raise WorkspaceRpcError("Codex returned invalid or foreign goal state")
        return {"goal": deepcopy(goal)}

    async def get_goal(self):
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Attach Codex before reading its goal")
        return self._validated_goal(await self.rpc.request("thread/goal/get", {"threadId": self.session_id}))

    async def update_goal(self, changes, expected, confirmed):
        if confirmed is not True or not isinstance(changes, dict) or not changes or changes.keys() - {"objective", "status", "tokenBudget"}:
            raise ValueError("Explicit goal changes and confirmation are required")
        if "objective" in changes and (not isinstance(changes["objective"], str)
                                       or not changes["objective"].strip() or len(changes["objective"]) > 4000):
            raise ValueError("Goal objective must contain 1 to 4000 characters")
        if "status" in changes and changes["status"] not in ("active", "paused", "complete"):
            raise ValueError("Choose active, paused or complete")
        budget = changes.get("tokenBudget")
        if budget is not None and (type(budget) is not int or not 1 <= budget <= 2**53 - 1):
            raise ValueError("Goal budget must be a positive integer or unlimited")
        async with self._control_lock:
            if self.state not in {"ready", "running"}:
                raise WorkspaceRpcError("Session is not available for goal changes")
            current = (await self.get_goal())["goal"]
            if current != expected or self.state not in {"ready", "running"}:
                raise WorkspaceRpcError("Goal changed; refresh before applying changes")
            result = self._validated_goal(await self.rpc.request("thread/goal/set", {"threadId": self.session_id, **changes}))
            if result["goal"] is None or any(result["goal"].get(key) != value for key, value in changes.items()):
                raise WorkspaceRpcError("Native goal change was not confirmed")
            return result

    async def clear_goal(self, expected, confirmed):
        if confirmed is not True:
            raise ValueError("Clearing a goal requires explicit confirmation")
        async with self._control_lock:
            if self.state not in {"ready", "running"}:
                raise WorkspaceRpcError("Session is not available for goal changes")
            if (await self.get_goal())["goal"] != expected or self.state not in {"ready", "running"}:
                raise WorkspaceRpcError("Goal changed; refresh before clearing")
            result = await self.rpc.request("thread/goal/clear", {"threadId": self.session_id})
            if not isinstance(result, dict) or type(result.get("cleared")) is not bool:
                raise WorkspaceRpcError("Native goal clearing was not confirmed")
            state = await self.get_goal()
            if state["goal"] is not None:
                raise WorkspaceRpcError("Goal is still present; refresh before retrying")
            return state

    async def speed_tiers(self):
        catalog = await self.list_models()
        model_id = self.settings.get("model")
        model = next((item for item in catalog["data"] if item.get("id") == model_id or item.get("model") == model_id), None)
        if model is None:
            raise WorkspaceRpcError("Current model is not in the native catalog")
        return {"model": model_id, "currentValue": self.settings.get("serviceTier"),
                "options": deepcopy(model.get("serviceTiers", []))}

    async def set_speed_tier(self, value, expected_model):
        async with self._control_lock:
            if self.state != "ready" or self.questions or self.active_agent_threads:
                raise WorkspaceRpcError("Finish active work before changing session speed")
            catalog = await self.speed_tiers()
            if expected_model != catalog["model"] or expected_model != self.settings.get("model"):
                raise WorkspaceRpcError("Model changed; refresh session speed")
            if value is not None and (not isinstance(value, str) or value not in {tier["id"] for tier in catalog["options"]}):
                raise ValueError("Speed tier is not advertised by this model")
            if self.state != "ready" or self.questions or self.active_agent_threads:
                raise WorkspaceRpcError("Session changed during speed lookup")
            result = await self.rpc.request("thread/settings/update", {"threadId": self.session_id, "serviceTier": value})
            if not isinstance(result, dict):
                raise WorkspaceRpcError("Session speed change was not confirmed")
            self.settings["serviceTier"] = value
            await self.publish({"method": "workspace/settings", "params": deepcopy(self.settings)})
            await self.publish({"method": "workspace/speed", "params": {"model": expected_model, "value": value}})
            return {**catalog, "currentValue": value}

    async def set_personality(self, value):
        async with self._control_lock:
            if self.state != "ready" or self.questions:
                raise WorkspaceRpcError("Finish the current turn before changing personality")
            model = self.settings.get("model")
            catalog = await self.personality()
            if not isinstance(value, str) or value not in catalog["options"]:
                raise ValueError("Personality is unavailable for this model")
            if self.state != "ready" or self.questions or model != self.settings.get("model"):
                raise WorkspaceRpcError("Session changed during personality lookup")
            result = await self.rpc.request("thread/settings/update", {"threadId": self.session_id, "personality": value})
            if not isinstance(result, dict):
                raise WorkspaceRpcError("Personality change was not confirmed")
            self.settings["personality"] = value
            self.settings["personalityConfirmed"] = True
            await self.publish({"method": "workspace/settings", "params": deepcopy(self.settings)})
            return {**catalog, "currentValue": value}

    async def set_session_mode(self, mode):
        async with self._control_lock:
            if self.state != "ready" or self.questions:
                raise WorkspaceRpcError("Finish the current Codex turn before changing mode")
            catalog = await self.list_session_modes()
            if not isinstance(mode, str) or mode not in {item["value"] for item in catalog["options"]}:
                raise ValueError("Codex did not advertise this session mode")
            if self.state != "ready" or self.questions:
                raise WorkspaceRpcError("Session changed during mode lookup")
            model = self.settings.get("model")
            if not isinstance(model, str) or not model:
                raise WorkspaceRpcError("Current Codex model is unavailable")
            result = await self.rpc.request("thread/settings/update", {
                "threadId": self.session_id,
                "collaborationMode": {"mode": mode, "settings": {
                    "model": model, "reasoning_effort": self.settings.get("reasoningEffort"),
                    "developer_instructions": None}},
            })
            if not isinstance(result, dict):
                raise WorkspaceRpcError("Codex mode change was not confirmed")
            self.settings["collaborationMode"] = mode
            await self.publish({"method": "workspace/settings", "params": deepcopy(self.settings)})
            return {**catalog, "currentValue": mode}

    async def list_apps(self) -> dict:
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        snapshot = await self.rpc.request("app/installed", {"threadId": self.session_id, "forceRefresh": True})
        installed = snapshot.get("apps") if isinstance(snapshot, dict) else None
        if not isinstance(installed, list) or len(installed) > 1000:
            raise WorkspaceRpcError("Codex returned an invalid installed app snapshot")
        runtime, apps = {}, []
        for app in installed:
            if (not isinstance(app, dict) or not isinstance(app.get("id"), str) or app["id"] in runtime
                    or type(app.get("enabled")) is not bool or type(app.get("callable")) is not bool):
                raise WorkspaceRpcError("Codex returned invalid app runtime state")
            identity = app['id']
            # Native IDs are opaque, not filesystem paths or URL slugs.
            if not identity or len(identity) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in identity):
                raise WorkspaceRpcError("Codex returned an invalid app ID")
            runtime[app["id"]] = app
        ids = list(runtime)
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            page = await self.rpc.request('app/read', {'appIds': batch, 'includeTools': False})
            if not isinstance(page, dict) or not isinstance(page.get('apps'), list) or not isinstance(page.get('missingAppIds'), list):
                raise WorkspaceRpcError('Codex returned invalid app metadata')
            metadata = {}
            for app in page['apps']:
                if not isinstance(app, dict) or not isinstance(app.get('id'), str) or app['id'] not in batch or app['id'] in metadata:
                    raise WorkspaceRpcError('Codex returned unexpected app metadata')
                name, description = app.get('name'), app.get('description') or ''
                if not isinstance(name, str) or not name or len(name) > 1000 or not isinstance(description, str) or len(description) > 8192:
                    raise WorkspaceRpcError('Codex returned invalid app details')
                metadata[app['id']] = {'name': name, 'description': description}
            missing = page['missingAppIds']
            if any(not isinstance(identity, str) or identity not in batch or identity in metadata for identity in missing) or len(set(missing)) != len(missing) or set(metadata) | set(missing) != set(batch):
                raise WorkspaceRpcError('Codex app metadata did not match the installed snapshot')
            for identity in batch:
                state = runtime[identity]
                info = metadata.get(identity, {'name': identity, 'description': 'App metadata unavailable'})
                apps.append({'id': identity, **info, 'accessible': identity in metadata,
                             'enabled': state['enabled'],
                             'callable': identity in metadata and state['enabled'] and state['callable']})
        return {"data": apps}

    async def _app_inputs(self, apps):
        if not isinstance(apps, list) or len(apps) > 20 or any(not isinstance(app, str) for app in apps) or len(set(apps)) != len(apps):
            raise ValueError("Select distinct apps from the current session")
        if not apps:
            return []
        catalog = {app["id"]: app for app in (await self.list_apps())["data"] if app["callable"]}
        if any(app not in catalog for app in apps):
            raise ValueError("Selected app is no longer callable in this session")
        return [{"type": "mention", "name": catalog[app]["name"], "path": f"app://{app}"} for app in apps]

    async def list_hooks(self) -> dict:
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        result = await self.rpc.request("hooks/list", {"cwds": [str(self.cwd)]})
        pages = result.get("data") if isinstance(result, dict) else None
        if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict) or not self._same_project(pages[0].get("cwd")):
            raise WorkspaceRpcError("Codex returned hooks for a different project")
        page = pages[0]
        hooks, errors, warnings = page.get("hooks"), page.get("errors"), page.get("warnings")
        if not isinstance(hooks, list) or len(hooks) > 1000 or not isinstance(errors, list) or not isinstance(warnings, list):
            raise WorkspaceRpcError("Codex returned an invalid hook catalog")
        safe = []
        for hook in hooks:
            if not isinstance(hook, dict) or type(hook.get("enabled")) is not bool or type(hook.get("isManaged")) is not bool:
                raise WorkspaceRpcError("Codex returned invalid hook state")
            item = {"enabled": hook["enabled"], "isManaged": hook["isManaged"]}
            for key in ("key", "eventName", "handlerType", "source", "sourcePath", "trustStatus"):
                value = hook.get(key)
                if not isinstance(value, str) or not value or len(value) > 8192:
                    raise WorkspaceRpcError("Codex returned invalid hook metadata")
                item[key] = value
            for key in ("command", "server", "tool", "matcher", "pluginId", "statusMessage"):
                value = hook.get(key)
                if value is not None:
                    if not isinstance(value, str) or len(value) > 8192:
                        raise WorkspaceRpcError("Codex returned invalid hook details")
                    item[key] = value
            safe.append(item)
        if len(errors) > 1000 or len(warnings) > 1000 or any(not isinstance(w, str) for w in warnings):
            raise WorkspaceRpcError("Codex returned invalid hook diagnostics")
        if any(not isinstance(e, dict) or not isinstance(e.get("message"), str) or not isinstance(e.get("path"), str) for e in errors):
            raise WorkspaceRpcError("Codex returned invalid hook errors")
        return {"data": safe, "errors": [{"path": e["path"], "message": e["message"]} for e in errors], "warnings": warnings}

    async def list_commands(self) -> dict:
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        result = await self.rpc.request("skills/list", {"cwds": [str(self.cwd)], "forceReload": True})
        pages = result.get("data") if isinstance(result, dict) else None
        if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict) or not self._same_project(pages[0].get("cwd")):
            raise WorkspaceRpcError("Codex returned skills for a different project")
        page = pages[0]
        if page.get("errors"):
            raise WorkspaceRpcError("Codex could not load every skill; fix the skill errors before selecting")
        if not isinstance(page.get("skills"), list):
            raise WorkspaceRpcError("Codex returned an invalid skill catalog")
        commands, paths = [], set()
        for skill in page["skills"]:
            if not isinstance(skill, dict) or any(not isinstance(skill.get(key), str) or not skill[key] for key in ("name", "path")) or not isinstance(skill.get("enabled"), bool):
                raise WorkspaceRpcError("Codex returned an invalid skill")
            if skill["path"] in paths:
                raise WorkspaceRpcError("Codex returned duplicate skill paths")
            paths.add(skill["path"])
            commands.append({"name": skill["name"], "path": skill["path"], "kind": "skill",
                             "enabled": skill["enabled"],
                             "description": skill.get("description", ""),
                             "unavailableReason": "Skill is disabled" if not skill["enabled"] else ""})
        return {"data": commands}

    async def set_skill_enabled(self, path, enabled):
        async with self._control_lock:
            if self.state != "ready" or self.questions:
                raise WorkspaceRpcError("Finish the current Codex turn before changing skills")
            if not isinstance(path, str) or type(enabled) is not bool:
                raise ValueError("An exact skill path and boolean enabled state are required")
            catalog = await self.list_commands()
            if not any(skill["path"] == path for skill in catalog["data"]):
                raise ValueError("Skill is not in this project's native catalog")
            if self.state != "ready":
                raise WorkspaceRpcError("Session changed during skill lookup")
            result = await self.rpc.request("skills/config/write", {"path": path, "enabled": enabled})
            if not isinstance(result, dict) or type(result.get("effectiveEnabled")) is not bool:
                raise WorkspaceRpcError("Skill configuration change was not confirmed")
            catalog = await self.list_commands()
            await self.publish({"method": "workspace/commands", "params": deepcopy(catalog)})
            return {**catalog, "path": path, "effectiveEnabled": result["effectiveEnabled"]}

    async def _mcp_config(self):
        result = await self.rpc.request("config/read", {"includeLayers": True, "cwd": str(self.cwd)})
        if not isinstance(result, dict) or not isinstance(result.get("config"), dict):
            raise WorkspaceRpcError("Codex returned invalid MCP configuration")
        servers = result["config"].get("mcp_servers", {})
        if not isinstance(servers, dict) or any(
            not isinstance(name, str) or not name or not isinstance(value, dict)
            or type(value.get("enabled", True)) is not bool for name, value in servers.items()
        ):
            raise WorkspaceRpcError("Codex returned invalid MCP settings")
        users = [layer for layer in result.get("layers") or [] if isinstance(layer, dict)
                 and isinstance(layer.get("name"), dict) and layer["name"].get("type") == "user"
                 and not layer["name"].get("profile")]
        user = users[0] if len(users) == 1 else None
        if user and (not isinstance(user.get("version"), str) or not user["version"]
                     or not isinstance(user["name"].get("file"), str) or not Path(user["name"]["file"]).is_absolute()):
            user = None
        return servers, user

    async def set_mcp_enabled(self, name, enabled):
        async with self._control_lock:
            if self.state != "ready" or self.questions:
                raise WorkspaceRpcError("Finish the current Codex turn before changing MCP settings")
            if not isinstance(name, str) or type(enabled) is not bool:
                raise ValueError("An exact MCP server name and boolean enabled state are required")
            servers, user = await self._mcp_config()
            if name not in servers or user is None:
                raise ValueError("MCP server has no safely writable Codex user settings")
            if self.state != "ready" or self.questions:
                raise WorkspaceRpcError("Session changed during MCP settings lookup")
            result = await self.rpc.request("config/value/write", {
                "keyPath": f"mcp_servers.{json.dumps(name, ensure_ascii=False)}.enabled",
                "value": enabled, "mergeStrategy": "replace",
                "filePath": user["name"]["file"], "expectedVersion": user["version"],
            })
            if not isinstance(result, dict) or result.get("status") not in {"ok", "okOverridden"}:
                raise WorkspaceRpcError("MCP configuration change was not confirmed")
            await self.rpc.request("config/mcpServer/reload", {})
            inventory = await self.list_mcp_servers()
            current = next((server for server in inventory["data"] if server["name"] == name), {})
            if type(current.get("enabled")) is not bool:
                raise WorkspaceRpcError("MCP settings were saved but their effective state is unavailable")
            notice = ""
            if current["enabled"] != enabled:
                notice = "Saved in Codex user settings; another configuration layer overrides this setting."
            return {**inventory, "effectiveEnabled": current["enabled"], "notice": notice}

    async def list_mcp_servers(self) -> dict:
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        data, cursors, names, cursor = [], set(), set(), None
        while True:
            params = {"threadId": self.session_id, "limit": 100, "detail": "toolsAndAuthOnly"}
            if cursor:
                params["cursor"] = cursor
            page = await self.rpc.request("mcpServerStatus/list", params)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise WorkspaceRpcError("Codex returned an invalid MCP inventory")
            for server in page["data"]:
                if (
                    not isinstance(server, dict)
                    or not isinstance(server.get("name"), str)
                    or not server["name"]
                    or server["name"] in names
                    or not isinstance(server.get("tools"), dict)
                ):
                    raise WorkspaceRpcError("Codex returned an invalid MCP server")
                names.add(server["name"])
                status = server.get("runtimeStatus")
                if status not in {None, "notStarted", "starting", "connected", "authenticationRequired", "failed", "cancelled", "disabled"}:
                    raise WorkspaceRpcError("Codex returned an unknown MCP connection state")
                # Auth state or a nonempty tool catalog does not prove a live connection.
                data.append({"name": server["name"], "status": status or "unknown",
                             "authStatus": server.get("authStatus", "unknown"),
                             "toolCount": len(server["tools"]),
                             **({"login": deepcopy(self._mcp_logins[server["name"]])} if server["name"] in self._mcp_logins else {})})
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in cursors or len(cursors) >= 100:
                raise WorkspaceRpcError("MCP inventory pagination did not advance")
            cursors.add(cursor)
        servers, user = await self._mcp_config()
        for name, settings in servers.items():
            item = next((server for server in data if server["name"] == name), None)
            if item is None:
                item = {"name": name, "status": "unknown",
                        "authStatus": "unknown", "toolCount": 0}
                data.append(item)
            item.update(enabled=settings.get("enabled", True), settingsWritable=user is not None)
        return {"data": data}

    async def reload_mcp(self):
        async with self._control_lock:
            if self.state != "ready":
                raise WorkspaceRpcError("Finish the current Codex turn before reloading MCP")
            await self.rpc.request("config/mcpServer/reload", {})
            return await self.list_mcp_servers()

    async def login_mcp(self, name):
        async with self._control_lock:
            if self.state != "ready":
                raise WorkspaceRpcError("Finish the current Codex turn before MCP login")
            if not isinstance(name, str) or not name:
                raise ValueError("An exact configured MCP server is required")
            prior = self._mcp_logins.get(name)
            if prior and prior["status"] in {"pending", "uncertain"}:
                return deepcopy(prior)
            servers = (await self.list_mcp_servers())["data"]
            server = next((item for item in servers if item["name"] == name), None)
            if server is None or server["authStatus"] not in {"notLoggedIn", "oAuth"}:
                raise ValueError("This configured MCP server does not advertise OAuth login")
            # Reserve before the RPC: a transport timeout must not start another login.
            login = self._mcp_logins[name] = {"status": "pending"}
            try:
                result = await self.rpc.request("mcpServer/oauth/login", {"name": name, "threadId": self.session_id})
                url = result.get("authorizationUrl") if isinstance(result, dict) else None
                parts = urlsplit(url) if isinstance(url, str) else None
                if not parts or parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password or any(char.isspace() for char in url):
                    raise WorkspaceRpcError("Codex returned an invalid MCP authorization URL")
                if login["status"] == "pending":
                    login["authorizationUrl"] = url
                return deepcopy(login)
            except BaseException:
                if login["status"] == "pending":
                    login["status"] = "uncertain"
                raise

    async def list_background_tasks(self) -> dict:
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        data, cursors, cursor = [], set(), None
        while True:
            params = {"threadId": self.session_id, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self.rpc.request("thread/backgroundTerminals/list", params)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise WorkspaceRpcError("Provider returned an invalid background task list")
            for task in page["data"]:
                if not isinstance(task, dict) or any(
                    not isinstance(task.get(key), str)
                    for key in ("processId", "itemId", "command", "cwd")
                ):
                    raise WorkspaceRpcError("Provider returned an invalid background task")
            data.extend(deepcopy(page["data"]))
            cursor = page.get("nextCursor")
            if not cursor:
                return {"data": data}
            if not isinstance(cursor, str) or cursor in cursors or len(cursors) >= 100:
                raise WorkspaceRpcError("Background task pagination did not advance")
            cursors.add(cursor)

    async def terminate_background_task(self, process_id: str) -> dict:
        if not isinstance(process_id, str) or not process_id or len(process_id) > 256:
            raise ValueError("An exact background process ID is required")
        async with self._control_lock:
            current = await self.list_background_tasks()
            if not any(task["processId"] == process_id for task in current["data"]):
                raise ValueError("Background task is no longer running in this session")
            result = await self.rpc.request(
                "thread/backgroundTerminals/terminate",
                {"threadId": self.session_id, "processId": process_id},
            )
            if not isinstance(result, dict) or not isinstance(result.get("terminated"), bool):
                raise WorkspaceRpcError("Background task termination is unconfirmed")
        return result

    async def list_models(self) -> dict:
        if self.state in {"closed", "opening", "unavailable"}:
            raise WorkspaceRpcError("Session is not connected")
        models, seen, cursor = [], set(), None
        while True:
            params = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            page = await self.rpc.request("model/list", params)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise WorkspaceRpcError("Provider returned an invalid model catalog")
            models.extend(deepcopy(page["data"]))
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen or len(seen) >= 100:
                raise WorkspaceRpcError("Provider model pagination did not advance")
            seen.add(cursor)
        self.model_catalog = models
        result = {"data": models, "settings": deepcopy(self.settings)}
        await self.publish({"method": "workspace/models", "params": result})
        return deepcopy(result)

    async def _validate_model_options(self, options):
        if not {"model", "effort", "serviceTier"}.intersection(options):
            return
        if self.model_catalog is None:
            await self.list_models()
        model_id = options.get("model") or self.settings.get("model")
        model = next((m for m in self.model_catalog if m.get("model") == model_id), None)
        if model is None:
            raise ValueError("Selected model is not advertised by this provider")
        if (
            "model" in options
            and options["model"] != self.settings.get("model")
            and "effort" not in options
        ):
            default = model.get("defaultReasoningEffort")
            if not isinstance(default, str):
                raise ValueError("Provider did not advertise a default effort for this model")
            options["effort"] = default
        effort = options.get("effort")
        if effort is not None and effort not in {
            e.get("reasoningEffort") for e in model.get("supportedReasoningEfforts", [])
        }:
            raise ValueError("Selected effort is not supported by this model")
        tier = options.get("serviceTier")
        if tier is not None and tier not in {t.get("id") for t in model.get("serviceTiers", [])}:
            raise ValueError("Selected speed tier is not supported by this model")

    async def _skill_inputs(self, skills):
        if not isinstance(skills, list):
            raise ValueError("Selected skills must be a list")
        if not skills:
            return []
        if len(skills) > 20 or any(not isinstance(path, str) for path in skills) or len(set(skills)) != len(skills):
            raise ValueError("Select distinct skills from the current project")
        catalog = {item["path"]: item for item in (await self.list_commands())["data"] if not item["unavailableReason"]}
        if any(path not in catalog for path in skills):
            raise ValueError("Selected skill is no longer enabled in this project")
        return [{"type": "skill", "name": catalog[path]["name"], "path": path} for path in skills]

    @staticmethod
    def _reject_unrouted_command(inputs):
        text = "".join(part.get("text", "") for part in inputs if part.get("type") == "text")
        command = re.match(r"^/([A-Za-z][A-Za-z0-9_:-]*)(?:\s|$)", text.lstrip())
        if command:
            raise ValueError(f"/{command[1]} requires a native session control; it was not sent to the model")

    async def submit(self, inputs: list[dict], *, options: dict | None = None) -> dict:
        async with self._control_lock:
            if self.state != "ready":
                raise WorkspaceRpcError("Session is not ready for a new turn")
            if not inputs:
                raise ValueError("A message or attachment is required")
            self._reject_unrouted_command(inputs)
            params = deepcopy(options or {})
            selected = await self._skill_inputs(params.pop("skills", []))
            selected += await self._app_inputs(params.pop("apps", []))
            allowed = {
                "model",
                "effort",
                "serviceTier",
                "approvalPolicy",
                "sandboxPolicy",
                "collaborationMode",
            }
            if params.keys() - allowed:
                raise ValueError("Unsupported turn option")
            await self._validate_model_options(params)
            params.update(threadId=self.session_id, input=deepcopy(inputs) + selected)
            self.state = "submitting"
            try:
                result = await self.rpc.request("turn/start", params)
                turn = result.get("turn") if isinstance(result, dict) else None
                if not isinstance(turn, dict) or not turn.get("id"):
                    raise WorkspaceRpcError("Codex returned no turn identity")
                turn_id = turn["id"]
                if turn_id not in self._completed:
                    self.active_turn = turn_id
                    self.state = "running"
                else:
                    self.active_turn = None
                    self.state = "ready"
                for source, target in (
                    ("model", "model"),
                    ("effort", "reasoningEffort"),
                    ("serviceTier", "serviceTier"),
                ):
                    if source in params:
                        self.settings[target] = params[source]
                await self.publish(
                    {"method": "workspace/settings", "params": deepcopy(self.settings)}
                )
                if "serviceTier" in params:
                    await self.publish({"method": "workspace/speed", "params": {
                        "model": self.settings["model"], "value": params["serviceTier"]}})
                return result
            except BaseException:
                # A timeout is not proof that Codex rejected the message. Do not
                # allow a retry to create a second turn until state is reconciled.
                if self.state == "submitting":
                    self.state = "uncertain"
                raise

    async def steer(self, inputs: list[dict], *, expected_turn_id: str | None = None, skills=None, apps=None) -> Any:
        self._reject_unrouted_command(inputs)
        turn_id = self.active_turn
        if not self.active_turn or self.state != "running":
            raise WorkspaceRpcError("No running turn to steer")
        if expected_turn_id is not None and expected_turn_id != self.active_turn:
            raise WorkspaceRpcError("The running turn changed; steering was not sent")
        selected = await self._skill_inputs([] if skills is None else skills)
        selected += await self._app_inputs([] if apps is None else apps)
        if self.active_turn != turn_id or self.state != "running":
            raise WorkspaceRpcError("The running turn changed; steering was not sent")
        return await self.rpc.request(
            "turn/steer",
            {
                "threadId": self.session_id,
                "expectedTurnId": self.active_turn,
                "input": deepcopy(inputs) + selected,
            },
        )

    async def review(self, target: dict) -> dict:
        fields = {
            "uncommittedChanges": set(),
            "baseBranch": {"branch"},
            "commit": {"sha"},
            "custom": {"instructions"},
        }
        if not isinstance(target, dict) or target.get("type") not in fields:
            raise ValueError("Unsupported review target")
        required = fields[target["type"]]
        if set(target) != {"type"} | required or any(
            not isinstance(target.get(key), str) or not target[key].strip() for key in required
        ):
            raise ValueError("Review target fields are incomplete")
        async with self._control_lock:
            if self.state != "ready":
                raise WorkspaceRpcError("Session is not ready for review")
            self.state = "submitting"
            try:
                result = await self.rpc.request(
                    "review/start",
                    {"threadId": self.session_id, "delivery": "inline", "target": deepcopy(target)},
                )
                if result.get("reviewThreadId") != self.session_id or not result.get(
                    "turn", {}
                ).get("id"):
                    raise WorkspaceRpcError("Review did not confirm this exact session and turn")
                turn_id = result["turn"]["id"]
                if turn_id not in self._completed:
                    self.active_turn, self.state = turn_id, "running"
                else:
                    self.active_turn, self.state = None, "ready"
                return result
            except BaseException:
                if self.state == "submitting":
                    self.state = "uncertain"
                raise

    async def compact(self) -> dict:
        async with self._control_lock:
            if self.state != "ready":
                raise WorkspaceRpcError("Session is not ready for compaction")
            self.state = "submitting"
            try:
                await self.publish(
                    {
                        "method": "workspace/activity",
                        "params": {"threadId": self.session_id, "status": "compacting"},
                    }
                )
                return await self.rpc.request("thread/compact/start", {"threadId": self.session_id})
            except BaseException:
                if self.state == "submitting":
                    self.state = "uncertain"
                raise

    async def interrupt(self) -> Any:
        if not self.active_turn:
            raise WorkspaceRpcError("No running turn to interrupt")
        return await self.rpc.request(
            "turn/interrupt",
            {
                "threadId": self.session_id,
                "turnId": self.active_turn,
            },
        )

    async def answer(self, request_id: int | str, answer: dict) -> None:
        question = self.questions.get(request_id)
        if question is None:
            raise WorkspaceRpcError("Question is no longer pending in this session")
        method = question["method"]
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            decisions = {"accept", "acceptForSession", "decline", "cancel"}
            if (
                set(answer) != {"decision"}
                or not isinstance(answer["decision"], str)
                or answer["decision"] not in decisions
            ):
                raise ValueError("Invalid approval decision")
        elif method == "mcpServer/elicitation/request":
            from core.workspace_elicitation import validate_reply

            validate_reply(question["params"], answer)
        elif method == "item/permissions/requestApproval":
            requested = question["params"].get("permissions")
            granted = answer.get("permissions")
            if (
                set(answer) != {"permissions", "scope"}
                or answer["scope"] not in {"turn", "session"}
                or not isinstance(requested, dict)
                or not isinstance(granted, dict)
                or granted.keys() - {"network", "fileSystem"}
            ):
                raise ValueError("Invalid permission grant")
            # Grant only exact requested groups. In particular, never remove a
            # deny entry from a filesystem profile and accidentally widen it.
            for key, value in granted.items():
                if key not in requested or json.dumps(value, sort_keys=True) != json.dumps(
                    requested[key], sort_keys=True
                ):
                    raise ValueError("Permission grant exceeds the requested access")
        elif method == "item/tool/requestUserInput":
            expected = {q["id"] for q in question["params"].get("questions", [])}
            answers = answer.get("answers")
            if (
                set(answer) != {"answers"}
                or not isinstance(answers, dict)
                or set(answers) != expected
            ):
                raise ValueError("Answer must address the pending questions exactly")
            for value in answers.values():
                if (
                    not isinstance(value, dict)
                    or set(value) != {"answers"}
                    or not isinstance(value["answers"], list)
                    or not all(isinstance(a, str) for a in value["answers"])
                ):
                    raise ValueError("Invalid question answer")
        else:
            raise WorkspaceRpcError(f"Response schema not yet implemented for {method}")
        await self.rpc.answer(request_id, answer)
        self.questions.pop(request_id, None)

    async def _events(self) -> None:
        try:
            await self._history_ready.wait()
            while True:
                event = await self.rpc.events.get()
                method, params = event.get("method"), event.get("params") or {}
                thread_id = params.get("threadId") or (params.get("thread") or {}).get("id")
                if thread_id is not None and thread_id != self.session_id:
                    await self._fork_ready.wait()
                    if thread_id in self._fork_ids and "id" not in event:
                        # The native fork loads a dormant thread in this server.
                        # Its lifecycle notifications are not source-chat output.
                        continue
                    if thread_id not in self._agent_ids:
                        await self._descendant_thread(thread_id)
                    if method == "turn/started":
                        self.active_agent_threads.add(thread_id)
                    elif method in {"turn/completed", "thread/closed"}:
                        self.active_agent_threads.discard(thread_id)
                    elif method == "thread/status/changed":
                        if params.get("status", {}).get("type") in {"idle", "notLoaded"}:
                            self.active_agent_threads.discard(thread_id)
                        else:
                            self.active_agent_threads.add(thread_id)
                    if "id" in event:
                        self.questions[event["id"]] = deepcopy(event)
                    elif method == "serverRequest/resolved":
                        self.questions.pop(params.get("requestId"), None)
                    if "id" in event or method == "serverRequest/resolved":
                        routed = {**deepcopy(event), "params": {**deepcopy(params),
                                  "threadId": self.session_id, "agentThreadId": thread_id}}
                    else:
                        routed = {"method": "workspace/agentEvent", "params": {
                            "threadId": self.session_id, "agentThreadId": thread_id,
                            "activeAgentCount": len(self.active_agent_threads), "event": deepcopy(event)}}
                    await self.publish(routed)
                    continue
                if "id" in event:
                    self.questions[event["id"]] = deepcopy(event)
                if method == "thread/reverted":
                    if thread_id != self.session_id:
                        raise WorkspaceRpcError('Reverted history is missing its exact session identity')
                    self.state = 'reconciling'
                    self._history_revision += 1
                    self.history_cursor = None
                    self._history_cursors.clear()
                    self._completed.clear()
                    await self.publish(deepcopy(event))
                    page = await self._history_page()
                    self.thread = {**self.thread, 'turns': page['turns']}
                    running = [turn['id'] for turn in page['turns'] if turn.get('status') == 'inProgress']
                    if len(running) > 1:
                        raise WorkspaceRpcError('Reverted history has ambiguous active turns')
                    self.active_turn = next(iter(running), None)
                    self.history_cursor = page['historyCursor']
                    await self.publish({'method': 'workspace/history', 'params': {
                        **deepcopy(self.settings), 'thread': deepcopy(self.thread),
                        'historyCursor': self.history_cursor, 'historyRevision': self._history_revision,
                        'copyUnavailableAfterRevert': True}})
                    self.state = 'running' if self.active_turn else 'ready'
                    continue
                if method == "account/login/completed":
                    self._finish_account_login(params)
                elif method == "account/updated":
                    auth_mode, plan_type = params.get("authMode"), params.get("planType")
                    if ((auth_mode is not None and (not isinstance(auth_mode, str) or len(auth_mode) > 64))
                            or (plan_type is not None and (not isinstance(plan_type, str) or len(plan_type) > 64))):
                        raise WorkspaceRpcError("Codex returned invalid account update metadata")
                    self._account_update = {"authMode": auth_mode, "planType": plan_type}
                    self._account_updated.set()
                elif method == "thread/settings/updated":
                    settings = params.get("threadSettings")
                    if not isinstance(settings, dict):
                        raise WorkspaceRpcError("Codex returned invalid thread settings")
                    mode = settings.get("collaborationMode")
                    if isinstance(mode, dict) and mode.get("mode") in {"plan", "default"}:
                        self.settings["collaborationMode"] = mode["mode"]
                    for source, target in (("model", "model"), ("effort", "reasoningEffort"),
                                           ("serviceTier", "serviceTier"), ("approvalPolicy", "approvalPolicy"),
                                           ("sandboxPolicy", "sandboxPolicy")):
                        if source in settings:
                            self.settings[target] = deepcopy(settings[source])
                    if settings.get("personality") in {"none", "friendly", "pragmatic"}:
                        self.settings["personality"] = settings["personality"]
                        self.settings.setdefault("personalityConfirmed", False)
                    await self.publish({"method": "workspace/settings", "params": deepcopy(self.settings)})
                elif method == "mcpServer/oauthLogin/completed":
                    login = self._mcp_logins.get(params.get("name"))
                    if login is not None and type(params.get("success")) is bool:
                        login.clear()
                        login.update({"status": "succeeded" if params["success"] else "failed"})
                        if not params["success"]:
                            login["error"] = str(params.get("error") or "MCP login failed")
                elif method == "turn/started":
                    self.active_turn = params["turn"]["id"]
                    self.state = "running"
                elif method == "turn/completed":
                    turn_id = params["turn"]["id"]
                    self._completed.append(turn_id)
                    if self.active_turn == turn_id or (
                        self.active_turn is None and self.state not in {"submitting", "uncertain"}
                    ):
                        self.active_turn = None
                        self.state = "ready"
                elif method == "serverRequest/resolved":
                    self.questions.pop(params.get("requestId"), None)
                elif method == "workspace/transportClosed":
                    self.state = "unavailable"
                    self.questions.clear()
                await self.publish(deepcopy(event))
                if method == "workspace/transportClosed":
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.state = "unavailable"
            await self.publish({"method": "workspace/error", "params": {"reason": str(error)}})

    async def _close(self) -> None:
        if self._events_task:
            self._events_task.cancel()
            await asyncio.gather(self._events_task, return_exceptions=True)
            self._events_task = None
        try:
            await self.rpc.close()
        finally:
            if self._account_login_lease:
                self._account_login_lease.release()
                self._account_login_lease = None
            if self._lease:
                self._lease.release()
                self._lease = None
        self.state = "closed"
        self.active_turn = None
        self.thread = None
        self.settings = {}
        self.model_catalog = None
        self._mcp_logins.clear()
        self._account_login = None
        self._early_login_completions.clear()
        self._account_update = None
        self._account_updated.clear()
        self.questions.clear()
        self._completed.clear()
        self.history_cursor = None
        self._history_cursors.clear()
        self._history_revision = 0
        self._fork_ids.clear()
        self._agent_ids.clear()
        self.active_agent_threads.clear()

    def can_retry_attachment(self) -> bool:
        return (self.state == "closed" and self.rpc.process is None
                and self._lease is None and self._events_task is None)

    async def close(self) -> None:
        """Owner shutdown, not view hide or browser disconnect."""
        async with self._control_lock:
            await self._close()
