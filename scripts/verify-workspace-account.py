"""Prove account controls on a disposable owner without browser opening or inference."""
import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.billing import strip_metered_auth_env
from core.workspace_codex import CodexWorkspace
from core.workspace_lease import SessionLease
from scripts.workspace_proof_auth import read_test_auth


async def main(browser_login=False, pause=False, modes=False, limits=False, signed_limits=False, hooks=False, command_guard=False, signed_apps=False, auth_home=None, usage=False, logout=False, login_sync=False):
    auth = read_test_auth(auth_home) if signed_limits or signed_apps else None
    binary = shutil.which("codex")
    assert binary, "Codex is not installed"
    with tempfile.TemporaryDirectory(prefix="serena-account-proof-") as directory:
        root = Path(directory)
        home = root / "home"
        project = root / "project"
        (home / ".codex").mkdir(parents=True)
        project.mkdir()
        if signed_apps:
            (home / '.codex' / 'config.toml').write_text('[features]\napps = true\n')
        if signed_limits or signed_apps:
            with open(home / ".codex" / "auth.json", "x",
                      opener=lambda path, flags: os.open(path, flags, 0o600)) as output:
                json.dump({"auth_mode": "chatgpt", "tokens": auth["tokens"],
                           "last_refresh": auth.get("last_refresh")}, output)
        env = strip_metered_auth_env(dict(os.environ))
        env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / ".codex"),
                   XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".state"))
        events = []
        async def publish(event):
            events.append(event)
        async def checkpoint(identity):
            (root / "identity.json").write_text(json.dumps(identity))
        owner = CodexWorkspace(session_id="new:" + str(uuid4()), cwd=project, publish=publish,
                               lease_factory=lambda sid: SessionLease(sid, directory=root / "leases"))
        process = None
        secondary = None
        secondary_process = None
        reconnected = None
        reconnected_process = None
        stale_login_cache = None
        wake_ms = None
        try:
            await owner.create(checkpoint=checkpoint, binary=binary, env=env)
            process = owner.rpc.process
            sid = owner.session_id
            result = await owner.account_status()
            if signed_limits or signed_apps:
                assert result.get("account") is not None, "Copied subscription login was not recognized"
            else:
                assert result == {"account": None, "requiresOpenaiAuth": True, "credentialsVerified": False, "login": None}, result
            if signed_limits:
                snapshot = await owner.account_rate_limits()
                assert snapshot["limits"] and snapshot["observedAt"]
                assert any(event.get("method") == "workspace/accountLimits" for event in events)
            if signed_apps:
                catalog = await owner.list_apps()
                assert isinstance(catalog['data'], list)
                selected = [app['id'] for app in catalog['data'] if app['callable']][:1]
                if selected:
                    mentions = await owner._app_inputs(selected)
                    assert mentions[0]['path'] == 'app://' + selected[0]
                assert owner.session_id == sid and owner.rpc.process is process and owner.state == 'ready'
            if limits:
                from core.workspace_rpc import WorkspaceRpcError

                try:
                    await owner.account_rate_limits()
                except WorkspaceRpcError as error:
                    assert "auth" in str(error).lower() or "access token" in str(error).lower(), str(error)
                else:
                    raise AssertionError("Unsigned profile unexpectedly returned account limits")
                assert not any(event.get("method") == "workspace/accountLimits" for event in events)
            if usage:
                from core.workspace_rpc import WorkspaceRpcError

                for reader in (owner.account_token_usage, owner.thread_token_usage):
                    try:
                        await reader()
                    except WorkspaceRpcError as error:
                        assert any(word in str(error).lower() for word in ("auth", "access token", "logged in")), str(error)
                    else:
                        raise AssertionError("Unsigned profile unexpectedly returned token activity")
            if hooks:
                catalog = await owner.list_hooks()
                assert catalog == {"data": [], "errors": [], "warnings": []}, catalog
            if command_guard:
                for command in ("/plugins", "/delete", "/debug-config", "/unknown"):
                    try:
                        await owner.submit([{"type": "text", "text": command}])
                    except ValueError as error:
                        assert "not sent to the model" in str(error)
                    else:
                        raise AssertionError("Unrouted command was accepted")
            if modes:
                from core.workspace_host import WorkspaceHost
                from core.workspace_journal import WorkspaceJournal

                mode_observer = WorkspaceHost(journal=WorkspaceJournal(root / "modes.db"), resolve=None)
                mode_observer._sessions[sid] = (owner, "codex")
                before = dict(owner.settings)
                catalog = await owner.list_session_modes()
                assert {item["value"] for item in catalog["options"]} >= {"plan", "default"}
                for mode in ("plan", "default"):
                    start = len(events)
                    assert (await owner.set_session_mode(mode))["currentValue"] == mode
                    async with asyncio.timeout(5):
                        while not any(event.get("method") == "thread/settings/updated"
                                      and event["params"]["threadSettings"]["collaborationMode"]["mode"] == mode
                                      for event in events[start:]):
                            await asyncio.sleep(.01)
                    native = next(event["params"]["threadSettings"] for event in events[start:]
                                  if event.get("method") == "thread/settings/updated")
                    assert native["model"] == before["model"]
                    assert native["effort"] == before.get("reasoningEffort")
                    assert owner.settings["collaborationMode"] == mode
                    assert (mode_observer._work_admission_error(sid) == "Native session is in Plan mode") == (mode == "plan")
            if pause:
                import psutil

                from core.workspace_host import WorkspaceHost
                from core.workspace_journal import WorkspaceJournal

                observer = WorkspaceHost(journal=WorkspaceJournal(root / "observer.db"), resolve=None)
                observer._sessions[sid] = (owner, "codex")
                old_view = {"view_id": str(uuid4()), "sequence": 1, "visible": False,
                            "focused": False, "draft": True, "pinned": False}
                await observer._note_view_context(sid, old_view)
                observer._views[sid][old_view["view_id"]]["seen"] -= 10
                await observer._note_view_context(sid, {**old_view, "view_id": str(uuid4()), "draft": False})
                assert observer._work_admission_error(sid)
                async with asyncio.timeout(5):
                    while not await owner.rpc.pause_idle():
                        await asyncio.sleep(.01)
                    while psutil.Process(process.pid).status() != psutil.STATUS_STOPPED:
                        await asyncio.sleep(.01)
                assert observer.events(sid)["runtime"] == {"session_id": sid, "sleeping": True}
                assert observer._loop is None and owner.rpc.suspended
                assert (await observer._note_view_context(sid, {**old_view, "sequence": 2, "closed": True}))["closed"]
                assert not observer._work_admission_error(sid) and not observer._sleep_blocker(sid)
                assert owner.rpc.suspended and owner.rpc.process is process
                started = asyncio.get_running_loop().time()
                assert (await owner.account_status())["account"] is None
                wake_ms = round((asyncio.get_running_loop().time() - started) * 1000, 2)
                assert not owner.rpc.suspended
                assert observer.events(sid)["runtime"] == {"session_id": sid, "sleeping": False}
                assert observer._loop is None
            if browser_login:
                login = await owner.login_account()
                assert login["status"] == "pending" and login["loginId"] and login["authUrl"].startswith("https://")
                assert await owner.login_account() == login
                assert (await owner.cancel_account_login(login["loginId"]))["status"] == "cancelled"
                assert (await owner.account_status())["account"] is None
            if logout:
                fake_key = "sk-test-serena-disposable-only"
                assert await owner.rpc.request(
                    "account/login/start", {"type": "apiKey", "apiKey": fake_key}
                ) == {"type": "apiKey"}
                assert (await owner.account_status())["account"] == {"type": "apiKey"}
                secondary = CodexWorkspace(
                    session_id="new:" + str(uuid4()),
                    cwd=project,
                    publish=publish,
                    lease_factory=lambda identity: SessionLease(
                        identity, directory=root / "leases"
                    ),
                )
                await secondary.create(checkpoint=checkpoint, binary=binary, env=env)
                secondary_process = secondary.rpc.process
                assert (await secondary.account_status())["account"] == {"type": "apiKey"}
                assert await owner.logout_account() == {"loggedOut": True}
                assert (await owner.account_status())["account"] is None
                assert (await secondary.account_status())["account"] == {"type": "apiKey"}
                assert await secondary.logout_account() == {"loggedOut": True}
                assert (await secondary.account_status())["account"] is None
                assert await owner.logout_account() == {"loggedOut": True}
            if login_sync:
                secondary = CodexWorkspace(
                    session_id="new:" + str(uuid4()),
                    cwd=project,
                    publish=publish,
                    lease_factory=lambda identity: SessionLease(
                        identity, directory=root / "leases"
                    ),
                )
                await secondary.create(checkpoint=checkpoint, binary=binary, env=env)
                secondary_process = secondary.rpc.process
                secondary_sid = secondary.session_id
                assert (await secondary.account_status())["account"] is None
                await secondary.rpc.request(
                    "thread/inject_items",
                    {
                        "threadId": secondary_sid,
                        "items": [{
                            "type": "message",
                            "role": "user",
                            "content": [{
                                "type": "input_text",
                                "text": "SERENA_ACCOUNT_RECONNECT_FIXTURE",
                            }],
                        }],
                    },
                )
                assert await owner.rpc.request(
                    "account/login/start",
                    {"type": "apiKey", "apiKey": "sk-test-serena-disposable-only"},
                ) == {"type": "apiKey"}
                assert (await owner.account_status())["account"] == {"type": "apiKey"}
                stale_login_cache = (await secondary.account_status())["account"] is None
                assert stale_login_cache
                refreshed = await secondary.rpc.request(
                    "account/read", {"refreshToken": True}
                )
                assert refreshed.get("account") is None
                await secondary.close()
                assert secondary_process.returncode is not None
                secondary = None
                reconnected = CodexWorkspace(
                    session_id=secondary_sid,
                    cwd=project,
                    publish=publish,
                    lease_factory=lambda identity: SessionLease(
                        identity, directory=root / "leases"
                    ),
                )
                await reconnected.open(binary=binary, env=env)
                reconnected_process = reconnected.rpc.process
                assert reconnected.session_id == secondary_sid
                assert (await reconnected.account_status())["account"] == {"type": "apiKey"}
            assert owner.session_id == sid and owner.rpc.process is process and process.returncode is None
            assert owner.state == "ready" and owner.active_turn is None
            assert not any(event.get("method") == "turn/started" for event in events)
        finally:
            if reconnected is not None:
                await reconnected.close()
            if secondary is not None:
                await secondary.close()
            await owner.close()
        assert process is not None and process.returncode is not None
        assert secondary_process is None or secondary_process.returncode is not None
        assert reconnected_process is None or reconnected_process.returncode is not None
    assert not root.exists()
    print(json.dumps({"ok": True, "nativeAccountRead": True, "sameOwner": True,
                      "signedIn": signed_limits or signed_apps, "loginStarted": browser_login, "loginCancelled": browser_login,
                      "browserOpened": False, "inference": False,
                      "nativePauseWake": pause, "wakeAccountRoundTripMs": wake_ms,
                      "readOnlyRuntimeSnapshot": pause,
                      "nativePlanAndDefaultConfirmed": modes,
                      "nativeUnsignedLimitsRefused": limits,
                      "nativeSignedLimitsRead": signed_limits,
                      "nativeUnsignedTokenUsageRefused": usage,
                      "nativeUnsignedSessionUsageRefused": usage,
                      "nativeAllOwnerLogoutConfirmed": logout,
                      "repeatLogoutIdempotent": logout,
                      "nativeStaleLoginCacheConfirmed": stale_login_cache,
                      "exactPersistedSessionReloadedAccount": login_sync,
                      "persistedWithoutInference": login_sync,
                      "duplicateConversationCreated": False,
                      "fakeDisposableApiKeyOnly": logout or login_sync,
                      "nativeEmptyHookCatalogRead": hooks,
                      "nativeAppCatalogRead": signed_apps,
                      "appsEnabledOnlyInDisposableProfile": signed_apps,
                      "appCount": len(catalog['data']) if signed_apps else None,
                      "nativeAppSelectionValidated": bool(selected) if signed_apps else False,
                      "unroutedCommandsRefusedOnNativeOwner": command_guard,
                      "closedViewRetiredWithoutWaking": pause,
                      "childReaped": True, "temporaryProfileRemoved": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-login", action="store_true", help="Start and cancel native OAuth without opening a browser")
    parser.add_argument("--pause", action="store_true", help="Prove POSIX native pause and wake without inference")
    parser.add_argument("--modes", action="store_true", help="Switch native plan/default on the disposable owner without inference")
    parser.add_argument("--limits", action="store_true", help="Verify native unsigned account-limit refusal without inference")
    parser.add_argument("--usage", action="store_true", help="Verify native unsigned token-activity refusal without inference")
    parser.add_argument("--signed-limits", action="store_true", help="Read real limits with an isolated subscription login copy; no inference")
    parser.add_argument("--hooks", action="store_true", help="Read native empty hook inventory without running hooks")
    parser.add_argument("--command-guard", action="store_true", help="Reject unsupported slash input on a disposable native owner")
    parser.add_argument("--signed-apps", action="store_true", help="Read apps with an isolated subscription login copy; no tool calls or inference")
    parser.add_argument("--auth-home", type=Path, help="Separate disposable ChatGPT test profile; never the normal or active login")
    parser.add_argument("--logout", action="store_true", help="Sign out two disposable native owners carrying a fake local API key")
    parser.add_argument("--login-sync", action="store_true", help="Prove exact-session reconnect reloads login cached by an old owner")
    args = parser.parse_args()
    if args.signed_limits and (args.browser_login or args.pause or args.modes or args.limits or args.hooks or args.command_guard or args.usage or args.logout or args.login_sync):
        parser.error("--signed-limits must run alone")
    if args.signed_apps and any(value for key, value in vars(args).items() if key not in {'signed_apps', 'auth_home'}):
        parser.error("--signed-apps must run alone")
    if bool(args.auth_home) != bool(args.signed_limits or args.signed_apps):
        parser.error("--auth-home is required only with --signed-limits or --signed-apps")
    if args.logout and any(value for key, value in vars(args).items() if key != "logout"):
        parser.error("--logout must run alone")
    if args.login_sync and any(value for key, value in vars(args).items() if key != "login_sync"):
        parser.error("--login-sync must run alone")
    asyncio.run(main(args.browser_login, args.pause, args.modes, args.limits, args.signed_limits, args.hooks, args.command_guard, args.signed_apps, args.auth_home, args.usage, args.logout, args.login_sync))
