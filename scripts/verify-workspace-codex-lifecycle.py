"""Native session lifecycle proof with isolated storage and no inference/auth."""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
            print("PASS: fresh native process resumed exact persisted fork/history without inference")
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
