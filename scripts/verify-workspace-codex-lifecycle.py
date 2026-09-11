"""Native session lifecycle proof with isolated storage and no inference/auth."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.codex_records import read_messages
from core.workspace_codex import CodexWorkspace
from core.workspace_lease import SessionLease
from core.workspace_rpc import WorkspaceRpc


async def main():
    binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Installed Codex unavailable")
    with tempfile.TemporaryDirectory(prefix="serena-codex-lifecycle-") as temporary:
        root = Path(temporary)
        home, project = root / "home", root / "project"
        home.mkdir()
        (home / ".codex").mkdir()
        project.mkdir()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
               "CODEX_HOME": str(home / ".codex"), "XDG_CONFIG_HOME": str(home / ".config"),
               "OPENAI_BASE_URL": "http://127.0.0.1:9/v1"}
        rpc = WorkspaceRpc()
        try:
            await rpc.start([binary, "app-server", "--stdio"], cwd=project, env=env)
            await rpc.request("initialize", {
                "clientInfo": {"name": "serena-lifecycle-proof", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            await rpc.notify("initialized", {})
            source = (await rpc.request("thread/start", {
                "cwd": str(project), "sandbox": "read-only", "approvalPolicy": "never",
            }))["thread"]
            sid = source["id"]
            await rpc.request("thread/inject_items", {"threadId": sid, "items": [{
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "SERENA_LIFECYCLE_FIXTURE"}],
            }]})
            before = await rpc.request("thread/read", {"threadId": sid, "includeTurns": True})
            fork = (await rpc.request("thread/fork", {"threadId": sid}))["thread"]
            assert fork["id"] != sid and fork["cwd"] == str(project), fork
            def messages(thread):
                path = Path(thread["path"]).resolve()
                assert path.is_relative_to(home.resolve())
                return [row["payload"] for line in path.read_text().splitlines()
                        if (row := json.loads(line)).get("type") == "response_item"]

            source_messages = messages(source)
            assert "SERENA_LIFECYCLE_FIXTURE" in json.dumps(source_messages)
            # Current native forks reference a bounded prefix rather than copying
            # response records. Verify that exact persisted prefix, not a guessed
            # concatenation of all present/future parent messages.
            def inherited_messages(thread):
                rows = [json.loads(line) for line in Path(thread["path"]).read_text().splitlines()]
                metadata = next(row["payload"] for row in rows if row["type"] == "session_meta")
                base = metadata.get("history_base")
                if not base:
                    return messages(thread)
                assert base["thread_id"] == sid
                parent = Path(source["path"]).read_bytes()[:base["end_byte_offset"]]
                prefix = [json.loads(line) for line in parent.splitlines()]
                assert all(row["ordinal"] < base["end_ordinal_exclusive"] for row in prefix)
                return [row["payload"] for row in prefix if row["type"] == "response_item"]

            assert inherited_messages(fork) == source_messages
            assert [text for _, text, _ in read_messages(Path(fork["path"]))] == ["SERENA_LIFECYCLE_FIXTURE"]
            after = await rpc.request("thread/read", {"threadId": sid, "includeTurns": True})
            assert before["thread"]["turns"] == after["thread"]["turns"]
            assert messages(source) == source_messages
            print("PASS: native fork preserved fixture history via exact bounded parent prefix; source unchanged")
            process = rpc.process
            await rpc.close()
            assert process.returncode is not None
            await rpc.start([binary, "app-server", "--stdio"], cwd=project, env=env)
            await rpc.request("initialize", {
                "clientInfo": {"name": "serena-lifecycle-proof", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            await rpc.notify("initialized", {})
            resumed = (await rpc.request("thread/resume", {"threadId": fork["id"]}))["thread"]
            assert resumed["id"] == fork["id"]
            assert inherited_messages(resumed) == source_messages
            assert [text for _, text, _ in read_messages(Path(resumed["path"]))] == ["SERENA_LIFECYCLE_FIXTURE"]
            print("PASS: fresh native process resumed exact persisted fork/history without inference")
            await rpc.close()
            published = []
            async def publish(event):
                published.append(event)
            owner = CodexWorkspace(session_id=sid, cwd=project, publish=publish,
                                   lease_factory=lambda session: SessionLease(session, directory=root / "leases"))
            try:
                await owner.open(binary=binary, env=env)
                pid = owner.rpc.process.pid
                created = await owner.fork_session()
                await owner.rpc.request("thread/read", {"threadId": sid})
                await asyncio.sleep(0.05)
                assert owner.state == "ready" and owner.session_id == sid
                assert owner.rpc.process.pid == pid and owner.rpc.process.returncode is None
                assert created["session_id"] != sid and created["provider"] == "codex"
                assert all((event.get("params", {}).get("thread") or {}).get("id") != created["session_id"] for event in published)
                catalog = subprocess.run([sys.executable, "-c", """
import json, sys
from core.workspace_catalog import register_fork
from core.indexer import _get_db
target = json.loads(sys.argv[1])
register_fork(target)
register_fork(target)
with _get_db() as connection:
    rows = connection.execute('SELECT session_id, agent, first_message, message_count FROM sessions').fetchall()
assert len(rows) == 1
assert tuple(rows[0]) == (target['session_id'], 'codex', 'SERENA_LIFECYCLE_FIXTURE', 1), tuple(rows[0])
print('PASS: native fork registered idempotently in isolated real catalog with inherited title/count')
""", json.dumps(created)], cwd=project,
                    env={**env, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                    text=True, capture_output=True, timeout=30)
                assert catalog.returncode == 0, catalog.stderr
                print(catalog.stdout.strip())
                print("PASS: real workspace owner forked without changing source identity/PID or publishing fork lifecycle as source output")
            finally:
                await owner.close()
        except BaseException:
            print("Native stderr:", "".join(rpc.stderr), file=sys.stderr)
            raise
        finally:
            process = rpc.process
            await rpc.close()
            assert process is None or process.returncode is not None
        assert not list(project.iterdir())
        print("PASS: native processes reaped; project untouched; no credentials or model turns used")


if __name__ == "__main__":
    asyncio.run(main())
