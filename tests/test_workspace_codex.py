import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.workspace_codex import CodexWorkspace
from core.workspace_rpc import WorkspaceRpcError


@pytest.fixture
def tmp_path(tmp_path):
    # Owners canonicalize their project path before sending native requests.
    return tmp_path.resolve()


@pytest.mark.parametrize("account", [None, {"type": "chatgpt", "email": "person@example.test", "planType": "pro", "accessToken": "never-forward"}])
def test_account_status_uses_exact_owner_without_refresh_or_inference(tmp_path, account):
    async def run():
        client, rpc, _ = await make(tmp_path)
        with pytest.raises(WorkspaceRpcError, match="Attach"):
            await client.account_status()
        await client.open(binary="codex")
        calls = []
        async def request(method, params):
            calls.append((method, params))
            return {"account": account, "requiresOpenaiAuth": True, "refreshToken": "never-forward"}
        rpc.request = request
        try:
            client.state, client.active_turn = "running", "preserved"
            result = await client.account_status()
            assert result["credentialsVerified"] is False
            assert "never-forward" not in str(result)
            assert result["account"] == (None if account is None else {k: account[k] for k in ("type", "email", "planType")})
            assert calls == [("account/read", {"refreshToken": False})]
            assert client.state == "running" and client.active_turn == "preserved"
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["complete", "early", "cancel", "timeout", "unsafe"])
def test_browser_login_is_single_owner_subscription_only_and_exact(tmp_path, outcome):
    from core.workspace_codex_auth import CodexLoginLease
    from core.workspace_lease import SessionOwnedError
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        client._lease.metadata = tmp_path / "session.json"
        rpc.process.pid = os.getpid()
        calls = []
        completion = {"loginId": "native-login", "success": True}
        async def request(method, params):
            calls.append((method, params))
            if method == "account/login/start":
                assert params == {"type": "chatgpt"}
                with pytest.raises(SessionOwnedError):
                    CodexLoginLease("codex-browser-login", directory=tmp_path)
                if outcome == "timeout":
                    raise WorkspaceRpcError("timed out")
                if outcome == "early":
                    client._finish_account_login(completion)
                return {"type": "chatgpt", "loginId": "native-login",
                        "authUrl": "https://evil.example/" if outcome == "unsafe" else "https://auth.openai.com/authorize?state=proof"}
            assert method == "account/login/cancel" and params == {"loginId": "native-login"}
            return {"status": "canceled"}
        rpc.request = request
        try:
            client.state = "running"
            with pytest.raises(WorkspaceRpcError, match="Finish"):
                await client.login_account()
            assert not calls
            client.state = "ready"
            if outcome in {"timeout", "unsafe"}:
                with pytest.raises(WorkspaceRpcError):
                    await client.login_account()
                assert (await client.login_account())["status"] == "uncertain"
            else:
                result = await client.login_account()
                assert result["status"] == ("succeeded" if outcome == "early" else "pending")
                if outcome != "early":
                    assert await client.login_account() == result
                    client._finish_account_login({"loginId": "other", "success": True})
                    assert client._account_login["status"] == "pending"
                    with pytest.raises(ValueError):
                        await client.cancel_account_login("other")
                    if outcome == "cancel":
                        assert (await client.cancel_account_login("native-login"))["status"] == "cancelled"
                    else:
                        await rpc.events.put({"method": "account/login/completed", "params": completion})
                        await asyncio.sleep(0)
                        assert client._account_login["status"] == "succeeded"
                assert client._account_login_lease is None
                prior = dict(client._account_login)
                client._finish_account_login({"loginId": "native-login", "success": False})
                assert client._account_login == prior
                lease = CodexLoginLease("codex-browser-login", directory=tmp_path)
                lease.release()
            assert len([call for call in calls if call[0] == "account/login/start"]) == 1
            assert client.state == "ready" and client.active_turn is None
            assert not any(method in {"turn/start", "account/logout"} for method, _ in calls)
        finally:
            await client.close()
    asyncio.run(run())


def test_project_identity_accepts_alias_spelling_not_other_directory(tmp_path):
    async def run():
        client, _, _ = await make(tmp_path)
        assert client._same_project(str(tmp_path / "."))
        assert client._same_project(str(tmp_path) + os.sep)
        if os.name == "nt":
            assert client._same_project(str(tmp_path).swapcase())
        other = tmp_path / "other"
        other.mkdir()
        for value in (str(other), str(tmp_path / "missing"), ".", None, {}, "\0"):
            assert not client._same_project(value)
    asyncio.run(run())


def test_native_skills_accept_same_directory_with_alternate_spelling(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        async def request(method, params):
            assert method == "skills/list"
            alternate = str(tmp_path).swapcase() if os.name == "nt" else str(tmp_path) + os.sep
            return {"data": [{"cwd": alternate, "skills": [], "errors": []}]}
        rpc.request = request
        try:
            assert (await client.list_commands())["data"] == []
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("url", ["https://auth.example/authorize?state=one", "javascript:alert(1)"])
def test_mcp_login_exact_server_pending_guard_and_native_completion(tmp_path, url):
    async def run():
        client, rpc, events = await make(tmp_path)
        await client.open(binary="codex")
        calls = []
        async def request(method, params):
            calls.append((method, params))
            if method == "config/read":
                return {"config": {}}
            if method == "mcpServerStatus/list":
                return {"data": [{"name": "local", "tools": {}, "authStatus": "notLoggedIn"}]}
            if method == "mcpServer/oauth/login":
                assert params == {"name": "local", "threadId": "exact-session"}
                return {"authorizationUrl": url}
            assert method == "config/mcpServer/reload" and params == {}
            return {}
        rpc.request = request
        try:
            with pytest.raises(ValueError):
                await client.login_mcp("other")
            if url.startswith("https"):
                assert (await client.login_mcp("local"))["authorizationUrl"] == url
                assert (await client.login_mcp("local"))["status"] == "pending"
            else:
                with pytest.raises(WorkspaceRpcError, match="authorization URL"):
                    await client.login_mcp("local")
                assert (await client.login_mcp("local"))["status"] == "uncertain"
            assert len([call for call in calls if call[0] == "mcpServer/oauth/login"]) == 1
            await rpc.events.put({"method": "mcpServer/oauthLogin/completed", "params": {"threadId": "exact-session", "name": "local", "success": False, "error": "Denied"}})
            await asyncio.sleep(0)
            assert (await client.list_mcp_servers())["data"][0]["login"] == {"status": "failed", "error": "Denied"}
            assert (await client.reload_mcp())["data"][0]["name"] == "local"
            client.state = "running"
            with pytest.raises(WorkspaceRpcError, match="Finish"):
                await client.reload_mcp()
            with pytest.raises(WorkspaceRpcError, match="Finish"):
                await client.login_mcp("local")
            assert not any(method == "turn/start" for method, _ in calls)
        finally:
            await client.close()
        assert not client._mcp_logins
    asyncio.run(run())


def test_skill_configuration_is_exact_explicit_and_uses_effective_native_state(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        path = str(tmp_path / "SKILL.md")
        calls, enabled = [], True
        async def request(method, params):
            nonlocal enabled
            calls.append((method, params))
            if method == "skills/list":
                return {"data": [{"cwd": str(tmp_path), "errors": [], "skills": [{"name": "proof", "path": path, "enabled": enabled}]}]}
            assert method == "skills/config/write" and params == {"path": path, "enabled": False}
            enabled = False
            return {"effectiveEnabled": False}
        rpc.request = request
        try:
            for bad_path, value in [("/not-discovered", False), (path, "false"), (path, 0)]:
                with pytest.raises(ValueError):
                    await client.set_skill_enabled(bad_path, value)
            assert not any(method == "skills/config/write" for method, _ in calls)
            result = await client.set_skill_enabled(path, False)
            assert result["effectiveEnabled"] is False
            assert result["data"][0]["unavailableReason"] == "Skill is disabled"
            assert result["data"][0]["enabled"] is False
            client.state = "running"
            with pytest.raises(WorkspaceRpcError, match="Finish"):
                await client.set_skill_enabled(path, True)
            assert len([call for call in calls if call[0] == "skills/config/write"]) == 1
        finally:
            await client.close()
    asyncio.run(run())


def test_native_file_search_is_bound_to_owned_project(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        (tmp_path / "selected file.py").write_text("fixture")
        async def request(method, params):
            assert method == "fuzzyFileSearch"
            assert params == {"query": "selected", "roots": [str(tmp_path)]}
            return {"files": [{"root": str(tmp_path), "path": "selected file.py", "match_type": "file"}]}
        rpc.request = request
        try:
            assert await client.search_files("selected") == {"paths": ["selected file.py"]}
            with pytest.raises(ValueError):
                await client.search_files("")
            async def escaped(method, params):
                return {"files": [{"root": str(tmp_path), "path": "../outside", "match_type": "file"}]}
            rpc.request = escaped
            with pytest.raises(WorkspaceRpcError, match="outside"):
                await client.search_files("selected")
            assert client.state == "ready"
        finally:
            await client.close()
    asyncio.run(run())


def test_shell_command_requires_confirmation_and_preserves_exact_input(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        try:
            with pytest.raises(ValueError, match="confirmation"):
                await client.shell_command("printf hello", False)
            with pytest.raises(ValueError, match="non-empty"):
                await client.shell_command("", True)
            command = "printf '%s' 'hello world'"
            assert await client.shell_command(command, True) == {"accepted": True}
            assert rpc.calls[-1] == ("thread/shellCommand", {"threadId": "exact-session", "command": command})
            assert client.state == "running"
            assert not any(method == "turn/start" for method, _ in rpc.calls)
        finally:
            await client.close()
    asyncio.run(run())


def test_shell_command_timeout_is_uncertain_not_retried(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        calls = []
        async def request(method, params):
            calls.append(method)
            raise TimeoutError()
        rpc.request = request
        try:
            with pytest.raises(TimeoutError):
                await client.shell_command("printf hello", True)
            assert client.state == "uncertain"
            with pytest.raises(WorkspaceRpcError):
                await client.shell_command("printf hello", True)
            assert calls == ["thread/shellCommand"]
        finally:
            await client.close()
    asyncio.run(run())


def test_paginated_resume_and_explicit_older_page(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        original = rpc.request
        requests = []
        async def request(method, params):
            if method == "thread/resume":
                return {"thread": {"id": rpc.sid, "historyMode": "paginated", "turns": []}}
            if method == "thread/turns/list":
                requests.append(params)
                ids = ["newer", "middle"] if "cursor" not in params else ["oldest"]
                return {"data": [{"id": sid, "status": "completed", "items": []} for sid in ids],
                        "nextCursor": None if "cursor" in params else "older"}
            return await original(method, params)
        rpc.request = request
        try:
            history = await client.open(binary="codex")
            assert [t["id"] for t in history["thread"]["turns"]] == ["middle", "newer"]
            assert history["historyCursor"] == "older" and len(requests) == 1
            with pytest.raises(WorkspaceRpcError, match="stale"):
                await client.load_earlier("wrong")
            await client.load_earlier("older")
            assert events[-1]["method"] == "workspace/historyPage"
            assert events[-1]["params"]["turns"][0]["id"] == "oldest"
            assert client.history_cursor is None and client.state == "ready"
            assert requests == [{"threadId": rpc.sid, "limit": 50, "sortDirection": "desc", "itemsView": "full"},
                                {"threadId": rpc.sid, "limit": 50, "sortDirection": "desc", "itemsView": "full", "cursor": "older"}]
            await client.close()
            reopened = await client.open(binary="codex")
            assert reopened["historyCursor"] == "older"
            await client.load_earlier("older")
            assert client.history_cursor is None and client.state == "ready"
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("bad", [{"data": [] , "nextCursor": "same"}, {"data": [{}]}, {"data": None}])
def test_history_page_rejection_does_not_advance_cursor(tmp_path, bad):
    async def run():
        client, rpc, events = await make(tmp_path)
        await client.open(binary="codex")
        client.history_cursor = "same"
        async def request(method, params):
            return bad
        rpc.request = request
        before = len(events)
        try:
            with pytest.raises(WorkspaceRpcError):
                await client.load_earlier("same")
            assert client.history_cursor == "same" and len(events) == before
        finally:
            await client.close()
    asyncio.run(run())


def test_fork_preserves_owner_and_filters_only_created_thread(tmp_path):
    async def run():
        client, rpc, published = await make(tmp_path)
        await client.open(binary="codex")
        fork_id = str(uuid4())
        original = rpc.request
        async def request(method, params):
            if method == "thread/fork":
                assert params == {"threadId": "exact-session"}
                await rpc.events.put({"method": "thread/started", "params": {"thread": {"id": fork_id}}})
                await asyncio.sleep(0)
                return {"thread": {"id": fork_id, "cwd": str(tmp_path)}}
            return await original(method, params)
        rpc.request = request
        try:
            client.state = "running"
            with pytest.raises(WorkspaceRpcError, match="current Codex turn"):
                await client.fork_session()
            client.state = "ready"
            assert await client.fork_session() == {"session_id": fork_id, "cwd": str(tmp_path), "provider": "codex"}
            await asyncio.sleep(0)
            assert client.session_id == "exact-session" and client.state == "ready"
            assert not any(e.get("method") == "thread/started" for e in published)
            assert not any(m == "turn/start" for m, _ in rpc.calls)
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["same", "invalid", "project"])
def test_fork_rejects_invalid_native_identity(tmp_path, case):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        async def request(method, params):
            return {"thread": {"id": "exact-session" if case == "same" else "invalid" if case == "invalid" else str(uuid4()),
                               "cwd": str(tmp_path / "wrong") if case == "project" else str(tmp_path)}}
        rpc.request = request
        try:
            with pytest.raises(WorkspaceRpcError, match="invalid identity"):
                await client.fork_session()
            assert not client._fork_ids and client._fork_ready.is_set()
        finally:
            await client.close()
    asyncio.run(run())


class Rpc:
    def __init__(self):
        self.events = asyncio.Queue()
        self.calls = []
        self.sid = "exact-session"
        self.closed = False
        self.race = False
        self.timeout = False
        self.process = SimpleNamespace(pid=12345)

    async def start(self, command, **kwargs):
        self.command, self.options = command, kwargs

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "initialize":
            return {}
        if method == "thread/resume":
            return {
                "thread": {"id": self.sid, "turns": []},
                "model": "chosen-model",
                "reasoningEffort": "high",
            }
        if method == "model/list":
            return {
                "data": [
                    {
                        "model": "chosen-model",
                        "displayName": "Chosen model",
                        "defaultReasoningEffort": "high",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "high"},
                            {"reasoningEffort": "low"},
                        ],
                    }
                ],
                "nextCursor": None,
            }
        if method == "turn/start":
            if self.timeout:
                raise TimeoutError()
            if self.race:
                await self.events.put(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": self.sid,
                            "turn": {"id": "turn-1", "status": "completed"},
                        },
                    }
                )
                await asyncio.sleep(0)
            return {"turn": {"id": "turn-1"}}
        return {}

    async def notify(self, method, params):
        self.calls.append((method, params))

    async def answer(self, request_id, answer):
        self.calls.append(("answer", (request_id, answer)))

    async def close(self):
        self.closed = True


def test_permission_profiles_enforce_policy_confirmation_and_exact_thread(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        original = rpc.request
        async def request(method, params):
            if method == "permissionProfile/list":
                assert params["cwd"] == str(tmp_path)
                return {"data": [{"id": ":read-only", "allowed": True}, {"id": "blocked", "allowed": False}], "nextCursor": None}
            return await original(method, params)
        rpc.request = request
        try:
            with pytest.raises(ValueError, match="confirmation"):
                await client.set_permissions(":read-only")
            with pytest.raises(ValueError, match="blocked"):
                await client.set_permissions("blocked", True)
            assert not any(method == "thread/settings/update" for method, _ in rpc.calls)
            assert (await client.set_permissions(":read-only", True))["mode"] == ":read-only"
            updates = [params for method, params in rpc.calls if method == "thread/settings/update"]
            assert updates == [{"threadId": "exact-session", "permissions": ":read-only"}]
            client.state = "running"
            with pytest.raises(WorkspaceRpcError, match="current Codex turn"):
                await client.set_permissions(":read-only", True)
            assert not any(method == "turn/start" for method, _ in rpc.calls)
        finally:
            await client.close()
    asyncio.run(run())


def test_skill_steering_preserves_turn_and_rechecks_after_discovery(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        client.state, client.active_turn = "running", "original"
        async def skills():
            return {"data": [{"name": "proof", "path": "/skill", "unavailableReason": ""}]}
        client.list_commands = skills
        try:
            await client.steer([{"type": "text", "text": ""}], expected_turn_id="original", skills=["/skill"])
            request = [params for method, params in rpc.calls if method == "turn/steer"][-1]
            assert request["expectedTurnId"] == "original"
            assert request["input"][-1] == {"type": "skill", "name": "proof", "path": "/skill"}
            async def raced():
                client.active_turn = "replacement"
                return await skills()
            client.list_commands = raced
            with pytest.raises(WorkspaceRpcError, match="turn changed"):
                await client.steer([{"type": "text", "text": ""}], expected_turn_id="original", skills=["/skill"])
            assert len([m for m, _ in rpc.calls if m == "turn/steer"]) == 1
            assert not any(m == "turn/start" for m, _ in rpc.calls)
        finally:
            await client.close()
    asyncio.run(run())


def test_selected_skills_are_revalidated_and_sent_as_native_skill_inputs(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        original = rpc.request
        path = str(tmp_path / "SKILL.md")
        async def request(method, params):
            if method == "skills/list":
                assert params == {"cwds": [str(tmp_path)], "forceReload": True}
                return {"data": [{"cwd": str(tmp_path), "errors": [], "skills": [{"name": "proof", "path": path, "enabled": True, "description": "Proof"}]}]}
            return await original(method, params)
        rpc.request = request
        try:
            assert (await client.list_commands())["data"][0]["kind"] == "skill"
            for selected in (["/foreign/SKILL.md"], [path, path], {}):
                with pytest.raises(ValueError):
                    await client.submit([{"type": "text", "text": "Run"}], options={"skills": selected})
            assert not any(method == "turn/start" for method, _ in rpc.calls)
            await client.submit([{"type": "text", "text": "Run"}], options={"skills": [path]})
            turn = [params for method, params in rpc.calls if method == "turn/start"][-1]
            assert turn["threadId"] == "exact-session"
            assert turn["input"][-1] == {"type": "skill", "name": "proof", "path": path}
            assert "skills" not in turn
        finally:
            await client.close()
    asyncio.run(run())


def test_mcp_inventory_paginates_exact_thread_without_inventing_connection_status(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        calls = []
        async def request(method, params):
            if method == "config/read":
                return {"config": {}}
            calls.append((method, params))
            if not params.get("cursor"):
                return {"data": [{"name": "first", "tools": {"tool": {}}, "authStatus": "oAuth", "runtimeStatus": None}], "nextCursor": "next"}
            return {"data": [{"name": "second", "tools": {}, "authStatus": "unknown", "runtimeStatus": "failed"}], "nextCursor": None}
        rpc.request = request
        try:
            result = await client.list_mcp_servers()
            assert result["data"][0] == {"name": "first", "status": "unknown", "authStatus": "oAuth", "toolCount": 1}
            assert result["data"][1]["status"] == "failed"
            assert len(calls) == 2
            assert all(method == "mcpServerStatus/list" and params["threadId"] == "exact-session" for method, params in calls)
            assert calls[1][1]["cursor"] == "next"
            async def stuck(method, params):
                return {"data": [], "nextCursor": "same"}
            rpc.request = stuck
            with pytest.raises(WorkspaceRpcError, match="pagination"):
                await client.list_mcp_servers()
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("overridden", [False, True])
def test_mcp_setting_uses_native_versioned_write_and_effective_state(tmp_path, overridden):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        name, enabled, calls = 'proof.dot"quoted', True, []
        config_path = str(tmp_path / "config.toml")
        async def request(method, params):
            nonlocal enabled
            calls.append((method, params))
            if method == "config/read":
                assert params == {"includeLayers": True, "cwd": str(tmp_path)}
                return {"config": {"mcp_servers": {name: {"enabled": enabled, "env": {"TOKEN": "private-token"}}}}, "layers": [
                    {"name": {"type": "user", "file": config_path}, "version": "version-one"}]}
            if method == "config/value/write":
                assert params == {"keyPath": 'mcp_servers."proof.dot\\"quoted".enabled', "value": False,
                                  "mergeStrategy": "replace", "filePath": config_path, "expectedVersion": "version-one"}
                enabled = overridden
                return {"status": "okOverridden" if overridden else "ok"}
            if method == "mcpServerStatus/list":
                return {"data": []}
            assert method == "config/mcpServer/reload" and params == {}
            return {}
        rpc.request = request
        try:
            for bad_name, value in [("other", False), (name, "false"), (name, 0)]:
                with pytest.raises(ValueError):
                    await client.set_mcp_enabled(bad_name, value)
            assert not any(method == "config/value/write" for method, _ in calls)
            result = await client.set_mcp_enabled(name, False)
            assert result["effectiveEnabled"] is overridden
            assert bool(result["notice"]) is overridden
            assert result["data"][0]["enabled"] is overridden
            assert result["data"][0]["settingsWritable"] is True
            assert "private-token" not in str(result)
            client.state = "running"
            with pytest.raises(WorkspaceRpcError, match="Finish"):
                await client.set_mcp_enabled(name, True)
            assert len([call for call in calls if call[0] == "config/value/write"]) == 1
        finally:
            await client.close()
    asyncio.run(run())


def test_mcp_setting_version_conflict_does_not_reload_or_retry(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        calls = []
        async def request(method, params):
            calls.append(method)
            if method == "config/read":
                return {"config": {"mcp_servers": {"proof": {}}}, "layers": [
                    {"name": {"type": "user", "file": str(tmp_path / "config.toml")}, "version": "old"}]}
            assert method == "config/value/write"
            raise WorkspaceRpcError("Version conflict")
        rpc.request = request
        try:
            with pytest.raises(WorkspaceRpcError, match="Version conflict"):
                await client.set_mcp_enabled("proof", False)
            assert calls == ["config/read", "config/value/write"]
        finally:
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["missing", "profile", "relative", "no-version", "busy"])
def test_mcp_setting_refuses_ambiguous_writer_or_changed_session(tmp_path, mode):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        calls = []
        async def request(method, params):
            calls.append(method)
            assert method == "config/read"
            layer = {"name": {"type": "user", "file": str(tmp_path / "config.toml")}, "version": "one"}
            if mode == "profile":
                layer["name"]["profile"] = "selected"
            if mode == "relative":
                layer["name"]["file"] = "config.toml"
            if mode == "no-version":
                layer.pop("version")
            if mode == "busy":
                client.state = "running"
            return {"config": {"mcp_servers": {"proof": {}}}, "layers": [] if mode == "missing" else [layer]}
        rpc.request = request
        try:
            with pytest.raises((ValueError, WorkspaceRpcError)):
                await client.set_mcp_enabled("proof", False)
            assert calls == ["config/read"]
        finally:
            await client.close()
    asyncio.run(run())


def test_background_tasks_paginate_and_stop_only_exact_session_process(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        calls = []

        async def request(method, params):
            calls.append((method, params))
            assert params["threadId"] == "exact-session"
            if method.endswith("/terminate"):
                return {"terminated": True}
            if params.get("cursor") == "next":
                return {"data": [], "nextCursor": None}
            return {
                "data": [
                    {"processId": "p1", "itemId": "i1", "command": "sleep 30", "cwd": "/project"}
                ],
                "nextCursor": "next",
            }

        rpc.request = request
        try:
            assert len((await client.list_background_tasks())["data"]) == 1
            assert (await client.terminate_background_task("p1"))["terminated"]
            with pytest.raises(ValueError, match="no longer running"):
                await client.terminate_background_task("foreign-process")
            stops = [p for m, p in calls if m.endswith("/terminate")]
            assert stops == [{"threadId": "exact-session", "processId": "p1"}]
            assert client.state == "ready"
            assert not any(m in {"turn/start", "turn/interrupt"} for m, _ in calls)

            async def looping(method, params):
                return {"data": [], "nextCursor": "loop"}

            rpc.request = looping
            with pytest.raises(WorkspaceRpcError, match="pagination"):
                await client.list_background_tasks()
        finally:
            await client.close()

    asyncio.run(run())


async def make(tmp_path):
    events = []

    async def publish(event):
        events.append(event)

    rpc = Rpc()
    lease = SimpleNamespace(launching=lambda: None, bind=lambda pid: None, release=lambda: None)
    client = CodexWorkspace(
        session_id=rpc.sid, cwd=tmp_path, publish=publish, rpc=rpc, lease_factory=lambda sid: lease
    )
    return client, rpc, events


@pytest.mark.parametrize("failure", [None, "identity", "cwd", "ephemeral", "turns", "checkpoint", "transport"])
def test_new_thread_checkpoints_before_ownership_and_never_retries_creation(tmp_path, failure):
    async def run():
        client, rpc, events = await make(tmp_path)
        client.session_id = "new:" + str(uuid4())
        native_sid = str(uuid4())
        order = []
        lease = SimpleNamespace(launching=lambda: None, bind=lambda pid: None,
                                release=lambda: order.append("release"))
        def transfer(sid):
            assert sid == native_sid and order == ["checkpoint"]
            order.append("transfer")
            return lease
        lease.transfer_after_transition = transfer
        client._lease_factory = lambda sid: lease
        original = rpc.request
        async def request(method, params):
            if method != "thread/start":
                assert method != "thread/resume"
                return await original(method, params)
            rpc.calls.append((method, params))
            assert params == {"cwd": str(tmp_path), "ephemeral": False}
            if failure == "transport":
                raise WorkspaceRpcError("lost response")
            thread = {"id": native_sid, "cwd": str(tmp_path), "ephemeral": False, "turns": [], "historyMode": "paginated"}
            if failure == "identity":
                thread["id"] = "unknown"
            elif failure == "cwd":
                thread["cwd"] = "/wrong"
            elif failure == "ephemeral":
                thread["ephemeral"] = True
            elif failure == "turns":
                thread["turns"] = [{"id": "existing"}]
            return {"thread": thread, "model": "configured-model"}
        rpc.request = request
        async def checkpoint(target):
            assert target == {"session_id": native_sid, "provider": "codex", "cwd": str(tmp_path)}
            assert events == [] and client.state == "opening"
            order.append("checkpoint")
            if failure == "checkpoint":
                raise OSError("durable write failed")
        try:
            with pytest.raises(WorkspaceRpcError, match="explicit creation"):
                await client.open(binary="codex")
            assert rpc.calls == []
            if failure:
                with pytest.raises((OSError, WorkspaceRpcError)):
                    await client.create(checkpoint=checkpoint, binary="codex")
                assert events == [] and "transfer" not in order
                with pytest.raises(WorkspaceRpcError, match="already attempted"):
                    await client.create(checkpoint=checkpoint, binary="codex")
            else:
                result = await client.create(checkpoint=checkpoint, binary="codex")
                assert client.session_id == result["thread"]["id"] == native_sid
                assert client.state == "ready" and order == ["checkpoint", "transfer"]
                assert events[0]["method"] == "workspace/history"
                with pytest.raises(ValueError):
                    await client.create(checkpoint=checkpoint, binary="codex")
            assert len([call for call in rpc.calls if call[0] == "thread/start"]) == 1
            assert not any(method in {"turn/start", "thread/resume"} for method, _ in rpc.calls)
        finally:
            await client.close()
    asyncio.run(run())


def test_model_discovery_and_unsupported_effort_never_starts_turn(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        try:
            await client.open(binary="codex")
            catalog = await client.list_models()
            assert catalog["settings"] == {"model": "chosen-model", "reasoningEffort": "high"}
            assert catalog["data"][0]["model"] == "chosen-model"
            assert events[-1]["method"] == "workspace/models"
            for options in (
                {"model": "invented"},
                {"model": "chosen-model", "effort": "unsupported"},
                {"serviceTier": "invented-fast"},
            ):
                with pytest.raises(ValueError):
                    await client.submit([{"type": "text", "text": "message"}], options=options)
            assert not any(method == "turn/start" for method, _ in rpc.calls)
            assert client.state == "ready"
            await client.submit([{"type": "text", "text": "message"}], options={"effort": "low"})
            assert rpc.calls[-1][1]["effort"] == "low"
            assert "model" not in rpc.calls[-1][1]
            assert events[-1] == {
                "method": "workspace/settings",
                "params": {"model": "chosen-model", "reasoningEffort": "low"},
            }
        finally:
            await client.close()

    asyncio.run(run())


def test_changed_model_without_effort_uses_advertised_default(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        try:
            await client.open(binary="codex")
            client.model_catalog = [
                {
                    "model": "new-model",
                    "defaultReasoningEffort": "low",
                    "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                    "serviceTiers": [{"id": "fast"}],
                }
            ]
            await client.submit(
                [{"type": "text", "text": "hello"}],
                options={"model": "new-model", "serviceTier": "fast"},
            )
            assert rpc.calls[-1][1]["effort"] == "low"
            assert rpc.calls[-1][1]["serviceTier"] == "fast"
        finally:
            await client.close()

    asyncio.run(run())


def test_model_catalog_pagination_and_loop_rejection(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        try:
            await client.open(binary="codex")
            original = rpc.request
            repeat = False

            async def paged(method, params):
                if method != "model/list":
                    return await original(method, params)
                if params.get("cursor"):
                    return {"data": [{"model": "second"}], "nextCursor": "next" if repeat else None}
                return {"data": [{"model": "first"}], "nextCursor": "next"}

            rpc.request = paged
            assert [m["model"] for m in (await client.list_models())["data"]] == ["first", "second"]
            repeat = True
            with pytest.raises(WorkspaceRpcError, match="pagination"):
                await client.list_models()
        finally:
            await client.close()

    asyncio.run(run())


def test_exact_resume_and_real_turn_controls(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        try:
            await client.open(binary="codex", env={"OPENAI_API_KEY": "must-not-pass"})
            assert "OPENAI_API_KEY" not in rpc.options["env"]
            assert "--disable" not in rpc.command
            assert rpc.calls[2] == ("thread/resume", {"threadId": "exact-session"})
            inputs = [
                {"type": "text", "text": "hello"},
                {"type": "localImage", "path": "/photo.png"},
            ]
            await client.submit(inputs, options={"model": "chosen-model", "effort": "high"})
            assert rpc.calls[-1][1]["input"] == inputs
            assert rpc.calls[-1][1]["threadId"] == "exact-session"
            with pytest.raises(WorkspaceRpcError):
                await client.submit(inputs)
            previous_calls = list(rpc.calls)
            with pytest.raises(WorkspaceRpcError, match="turn changed"):
                await client.steer(
                    [{"type": "text", "text": "correction"}], expected_turn_id="old-turn"
                )
            assert rpc.calls == previous_calls
            await client.steer([{"type": "text", "text": "correction"}], expected_turn_id="turn-1")
            assert rpc.calls[-1][1]["expectedTurnId"] == "turn-1"
            await client.interrupt()
            assert rpc.calls[-1] == (
                "turn/interrupt",
                {"threadId": "exact-session", "turnId": "turn-1"},
            )
            assert not rpc.closed
            assert events[0]["method"] == "workspace/history"
        finally:
            await client.close()

    asyncio.run(run())


def test_attachment_retry_requires_transport_and_lease_cleanup(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        await client.open(binary="codex")
        assert not client.can_retry_attachment()
        client.state = "unavailable"
        assert not client.can_retry_attachment()
        await client.close()
        assert not client.can_retry_attachment()  # Fixture still exposes a process.
        rpc.process = None
        assert client.can_retry_attachment()
    asyncio.run(run())


def test_wrong_resume_never_creates_fallback_session(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        rpc.sid = "wrong-session"
        with pytest.raises(WorkspaceRpcError, match="different session"):
            await client.open(binary="codex", env={})
        assert rpc.closed
        assert not any(method == "thread/start" for method, _ in rpc.calls)

    asyncio.run(run())


def test_review_stays_inline_and_rejects_busy_or_unknown_targets(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        try:
            await client.open(binary="codex", env={})
            original = rpc.request

            async def request(method, params):
                if method == "review/start":
                    rpc.calls.append((method, params))
                    return {"reviewThreadId": "exact-session", "turn": {"id": "review-1"}}
                return await original(method, params)

            rpc.request = request
            with pytest.raises(ValueError):
                await client.review({"type": "custom", "instructions": ""})
            result = await client.review({"type": "baseBranch", "branch": "main"})
            assert result["reviewThreadId"] == "exact-session"
            assert rpc.calls[-1] == (
                "review/start",
                {
                    "threadId": "exact-session",
                    "delivery": "inline",
                    "target": {"type": "baseBranch", "branch": "main"},
                },
            )
            assert client.active_turn == "review-1"
            with pytest.raises(WorkspaceRpcError, match="not ready"):
                await client.review({"type": "uncommittedChanges"})
        finally:
            await client.close()

    asyncio.run(run())


def test_compaction_ack_does_not_make_session_ready_before_completion(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        try:
            await client.open(binary="codex", env={})

            async def request(method, params):
                rpc.calls.append((method, params))
                return {}

            rpc.request = request
            await client.compact()
            assert rpc.calls[-1] == ("thread/compact/start", {"threadId": "exact-session"})
            assert client.state == "submitting"
            with pytest.raises(WorkspaceRpcError, match="not ready"):
                await client.submit([{"type": "text", "text": "too early"}])
            await rpc.events.put(
                {
                    "method": "turn/started",
                    "params": {"threadId": "exact-session", "turn": {"id": "compact-1"}},
                }
            )
            await rpc.events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "exact-session",
                        "turn": {"id": "compact-1", "status": "completed"},
                    },
                }
            )
            await asyncio.sleep(0.01)
            assert client.state == "ready"
        finally:
            await client.close()

    asyncio.run(run())


def test_fast_completion_is_not_overwritten_by_start_reply(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        try:
            rpc.race = True
            await client.open(binary="codex", env={})
            await client.submit([{"type": "text", "text": "go"}])
            assert client.state == "ready"
            assert client.active_turn is None
        finally:
            await client.close()

    asyncio.run(run())


def test_ambiguous_submission_cannot_be_retried_as_new_turn(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        try:
            await client.open(binary="codex", env={})
            rpc.timeout = True
            with pytest.raises(TimeoutError):
                await client.submit([{"type": "text", "text": "go"}])
            assert client.state == "uncertain"
            with pytest.raises(WorkspaceRpcError):
                await client.submit([{"type": "text", "text": "go"}])
            assert sum(method == "turn/start" for method, _ in rpc.calls) == 1
            assert not rpc.closed
        finally:
            await client.close()

    asyncio.run(run())


def test_permission_grants_cannot_expand_profile_or_drop_deny_entries(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        permissions = {
            "network": {"enabled": True},
            "fileSystem": {
                "entries": [
                    {"access": "write", "path": {"type": "path", "path": "/project"}},
                    {"access": "deny", "path": {"type": "path", "path": "/project/private"}},
                ]
            },
        }
        question = {
            "method": "item/permissions/requestApproval",
            "params": {"permissions": permissions},
        }
        for granted in (
            {"network": {"enabled": False}},
            {"fileSystem": {"write": ["/"]}},
            {"fileSystem": {"entries": permissions["fileSystem"]["entries"][:1]}},
            {"unexpected": {}},
        ):
            client.questions[7] = question
            with pytest.raises(ValueError):
                await client.answer(7, {"permissions": granted, "scope": "turn"})
        assert not rpc.calls
        for scope, granted in (
            ("turn", {}),
            ("turn", {"network": permissions["network"]}),
            ("session", permissions),
        ):
            client.questions[7] = question
            await client.answer(7, {"permissions": granted, "scope": scope})
            assert rpc.calls[-1] == ("answer", (7, {"permissions": granted, "scope": scope}))
            assert 7 not in client.questions
        with pytest.raises(WorkspaceRpcError, match="no longer pending"):
            await client.answer(7, {"permissions": permissions, "scope": "session"})

    asyncio.run(run())


def test_mcp_answer_validates_before_reply_and_rejects_stale_request(tmp_path):
    async def run():
        client, rpc, _ = await make(tmp_path)
        client.questions[8] = {
            "method": "mcpServer/elicitation/request",
            "params": {
                "mode": "form",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            },
        }
        with pytest.raises(ValueError):
            await client.answer(8, {"action": "accept", "content": {"count": "wrong"}})
        assert not rpc.calls and 8 in client.questions
        answer = {"action": "accept", "content": {"count": 3}}
        await client.answer(8, answer)
        assert rpc.calls == [("answer", (8, answer))]
        with pytest.raises(WorkspaceRpcError, match="no longer pending"):
            await client.answer(8, answer)

    asyncio.run(run())


def test_approval_validation_and_stale_resolution(tmp_path):
    async def run():
        client, rpc, events = await make(tmp_path)
        try:
            await client.open(binary="codex", env={})
            event = {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": rpc.sid, "command": "git status"},
            }
            await rpc.events.put(event)
            await asyncio.sleep(0)
            assert events[-1] == event
            with pytest.raises(ValueError):
                await client.answer(7, {"decision": "yes"})
            assert 7 in client.questions
            await client.answer(7, {"decision": "decline"})
            with pytest.raises(WorkspaceRpcError):
                await client.answer(7, {"decision": "accept"})
            await rpc.events.put(event)
            await rpc.events.put(
                {
                    "method": "serverRequest/resolved",
                    "params": {"threadId": rpc.sid, "requestId": 7},
                }
            )
            await asyncio.sleep(0)
            assert 7 not in client.questions
        finally:
            await client.close()

    asyncio.run(run())
