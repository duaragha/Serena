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


async def main(browser_login=False):
    binary = shutil.which("codex")
    assert binary, "Codex is not installed"
    with tempfile.TemporaryDirectory(prefix="serena-account-proof-") as directory:
        root = Path(directory)
        home = root / "home"
        project = root / "project"
        (home / ".codex").mkdir(parents=True)
        project.mkdir()
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
        try:
            await owner.create(checkpoint=checkpoint, binary=binary, env=env)
            process = owner.rpc.process
            sid = owner.session_id
            result = await owner.account_status()
            assert result == {"account": None, "requiresOpenaiAuth": True, "credentialsVerified": False, "login": None}, result
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
                      "signedIn": False, "loginStarted": browser_login, "loginCancelled": browser_login,
                      "browserOpened": False, "inference": False,
                      "childReaped": True, "temporaryProfileRemoved": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-login", action="store_true", help="Start and cancel native OAuth without opening a browser")
    asyncio.run(main(parser.parse_args().browser_login))
