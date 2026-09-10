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


async def main(browser_login=False, pause=False, modes=False, limits=False, signed_limits=False, hooks=False, command_guard=False):
    binary = shutil.which("codex")
    assert binary, "Codex is not installed"
    with tempfile.TemporaryDirectory(prefix="serena-account-proof-") as directory:
        root = Path(directory)
        home = root / "home"
        project = root / "project"
        (home / ".codex").mkdir(parents=True)
        project.mkdir()
        if signed_limits:
            source = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
            auth = json.loads(source.read_text())
            if auth.get("auth_mode") != "chatgpt" or not auth.get("tokens"):
                raise RuntimeError("Existing ChatGPT subscription authentication is required")
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
        wake_ms = None
        try:
            await owner.create(checkpoint=checkpoint, binary=binary, env=env)
            process = owner.rpc.process
            sid = owner.session_id
            result = await owner.account_status()
            if signed_limits:
                assert result.get("account") is not None, "Copied subscription login was not recognized"
                snapshot = await owner.account_rate_limits()
                assert snapshot["limits"] and snapshot["observedAt"]
                assert any(event.get("method") == "workspace/accountLimits" for event in events)
            else:
                assert result == {"account": None, "requiresOpenaiAuth": True, "credentialsVerified": False, "login": None}, result
            if limits:
                from core.workspace_rpc import WorkspaceRpcError

                try:
                    await owner.account_rate_limits()
                except WorkspaceRpcError as error:
                    assert "auth" in str(error).lower() or "access token" in str(error).lower(), str(error)
                else:
                    raise AssertionError("Unsigned profile unexpectedly returned account limits")
                assert not any(event.get("method") == "workspace/accountLimits" for event in events)
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
            assert owner.session_id == sid and owner.rpc.process is process and process.returncode is None
            assert owner.state == "ready" and owner.active_turn is None
            assert not any(event.get("method") == "turn/started" for event in events)
        finally:
            await owner.close()
        assert process is not None and process.returncode is not None
    assert not root.exists()
    print(json.dumps({"ok": True, "nativeAccountRead": True, "sameOwner": True,
                      "signedIn": signed_limits, "loginStarted": browser_login, "loginCancelled": browser_login,
                      "browserOpened": False, "inference": False,
                      "nativePauseWake": pause, "wakeAccountRoundTripMs": wake_ms,
                      "readOnlyRuntimeSnapshot": pause,
                      "nativePlanAndDefaultConfirmed": modes,
                      "nativeUnsignedLimitsRefused": limits,
                      "nativeSignedLimitsRead": signed_limits,
                      "nativeEmptyHookCatalogRead": hooks,
                      "unroutedCommandsRefusedOnNativeOwner": command_guard,
                      "closedViewRetiredWithoutWaking": pause,
                      "childReaped": True, "temporaryProfileRemoved": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-login", action="store_true", help="Start and cancel native OAuth without opening a browser")
    parser.add_argument("--pause", action="store_true", help="Prove POSIX native pause and wake without inference")
    parser.add_argument("--modes", action="store_true", help="Switch native plan/default on the disposable owner without inference")
    parser.add_argument("--limits", action="store_true", help="Verify native unsigned account-limit refusal without inference")
    parser.add_argument("--signed-limits", action="store_true", help="Read real limits with an isolated subscription login copy; no inference")
    parser.add_argument("--hooks", action="store_true", help="Read native empty hook inventory without running hooks")
    parser.add_argument("--command-guard", action="store_true", help="Reject unsupported slash input on a disposable native owner")
    args = parser.parse_args()
    if args.signed_limits and (args.browser_login or args.pause or args.modes or args.limits or args.hooks or args.command_guard):
        parser.error("--signed-limits must run alone")
    asyncio.run(main(args.browser_login, args.pause, args.modes, args.limits, args.signed_limits, args.hooks, args.command_guard))
