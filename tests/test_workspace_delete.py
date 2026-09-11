import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.workspace_archive import delete_codex_tree, inspect_codex_delete_tree
from core.workspace_rpc import WorkspaceRpcError


class Lease:
    def __init__(self, identity, calls, *, reject=False):
        calls.append(("lease", identity))
        if reject:
            raise RuntimeError("already owned")
        self.identity = identity
        self.calls = calls

    def launching(self):
        self.calls.append(("launching", self.identity))

    def bind(self, pid):
        self.calls.append(("bind", self.identity, pid))

    def release(self):
        self.calls.append(("release", self.identity))


@pytest.mark.parametrize(
    "case",
    [
        "ok",
        "archived-root",
        "owned-child",
        "owner-guard",
        "family-guard",
        "loaded",
        "ancestry",
        "changed",
        "checkpoint",
        "bad-ack",
        "wrong-notice",
        "leftover",
        "post-descendant",
        "cleanup",
        "external-fork",
    ],
)
def test_native_delete_is_exact_recoverable_and_never_opens_a_writer(tmp_path, case):
    root, child, archived_child = (str(uuid4()) for _ in range(3))
    home = tmp_path / "codex"
    active_store = home / "sessions"
    archived_store = home / "archived_sessions"
    active_store.mkdir(parents=True)
    archived_store.mkdir()
    paths = {
        root: (archived_store if case == "archived-root" else active_store) / f"{root}.jsonl",
        child: active_store / f"{child}.jsonl",
        archived_child: archived_store / f"{archived_child}.jsonl",
    }
    contents = {identity: f'{{"id":"{identity}"}}\n' for identity in paths}
    for identity, path in paths.items():
        path.write_text(contents[identity], encoding="utf-8")
    if case == "external-fork":
        external = str(uuid4())
        (active_store / f"{external}.jsonl").write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": external, "history_base": {"thread_id": root}},
        }) + "\n", encoding="utf-8")
    calls, checkpoints = [], []

    class Rpc:
        process = None
        deleted = None

        def __init__(self):
            self.events = asyncio.Queue()
            self.list_counts = {False: 0, True: 0}
            self.deleted = set()

        async def start(self, argv, **kwargs):
            calls.append(("start", argv))
            assert kwargs["cwd"] == tmp_path
            assert "OPENAI_API_KEY" not in kwargs["env"]
            self.process = SimpleNamespace(pid=451)

        async def notify(self, method, params):
            calls.append((method, params))

        async def request(self, method, params):
            calls.append((method, params))
            if method == "initialize":
                return {}
            if method == "thread/read":
                identity = params["threadId"]
                parents = {root: None, child: root, archived_child: child}
                return {"thread": {
                    "id": identity,
                    "parentThreadId": parents[identity],
                    "cwd": str(tmp_path),
                    "path": str(paths[identity]),
                }}
            if method == "thread/list":
                archived = params["archived"]
                self.list_counts[archived] += 1
                if root in self.deleted:
                    if case == "post-descendant" and not archived:
                        return {"data": [{"id": child, "parentThreadId": root}], "nextCursor": None}
                    return {"data": [], "nextCursor": None}
                if case == "changed" and not archived and self.list_counts[False] > 1:
                    return {"data": [], "nextCursor": None}
                if archived:
                    return {"data": [{"id": archived_child, "parentThreadId": child}], "nextCursor": None}
                parent = str(uuid4()) if case == "ancestry" else root
                return {"data": [{"id": child, "parentThreadId": parent}], "nextCursor": None}
            if method == "thread/loaded/list":
                return {"data": [child] if case == "loaded" else []}
            assert method == "thread/delete"
            identity = params["threadId"]
            self.deleted.add(identity)
            path = paths[identity]
            if not (case == "leftover" and identity == child):
                path.unlink()
            if case == "bad-ack":
                return {"deleted": True}
            notice = str(uuid4()) if case == "wrong-notice" else identity
            await self.events.put({"method": "thread/deleted", "params": {"threadId": notice}})
            return {}

        async def close(self):
            calls.append(("close", None))
            if case != "cleanup":
                self.process = None

    def lease_factory(identity):
        return Lease(identity, calls, reject=case == "owned-child" and identity == child)

    def ownership(thread, pid):
        calls.append(("owner", thread["id"], pid))
        if case == "owner-guard" and thread["id"] == child:
            raise RuntimeError("external writer")

    def family(threads):
        calls.append(("family", [thread["id"] for thread in threads]))
        if case == "family-guard":
            raise RuntimeError("reserved descendant")

    def checkpoint(value):
        checkpoints.append(value)
        if case == "checkpoint":
            raise OSError("checkpoint unavailable")

    recovery = tmp_path / "recovery"

    async def run():
        kwargs = {
            "confirmed": True,
            "binary": "codex",
            "env": {"CODEX_HOME": str(home), "OPENAI_API_KEY": "never"},
            "rpc_factory": Rpc,
            "lease_factory": lease_factory,
            "ownership_guard": ownership,
            "family_guard": family,
            "checkpoint": checkpoint,
            "metadata_reader": lambda identity: {"custom_title": identity},
        }
        if case in {"ok", "archived-root"}:
            result = await delete_codex_tree(root, tmp_path, recovery, **kwargs)
            assert result["deleted"] is True
            assert result["thread_ids"] == [target["session_id"] for target in result["targets"]]
            assert result["thread_ids"][0] == root
            assert set(result["thread_ids"]) == set(paths)
            delete_ids = [call[1]["threadId"] for call in calls if call[0] == "thread/delete"]
            assert delete_ids == [archived_child, child, root]
            assert checkpoints == [{key: result[key] for key in (
                "session_id", "provider", "cwd", "targets", "recovery_dir"
            )}]
            manifest = json.loads((recovery / "recovery.json").read_text(encoding="utf-8"))
            assert manifest["root_session_id"] == root
            assert {target["session_id"] for target in manifest["targets"]} == set(paths)
            for target in result["targets"]:
                saved = Path(target["recovery_path"])
                assert saved.read_text(encoding="utf-8") == contents[target["session_id"]]
                assert target["size"] == saved.stat().st_size
                assert len(target["sha256"]) == 64
                assert manifest["targets"][result["targets"].index(target)]["metadata"] == {
                    "custom_title": target["session_id"]
                }
        else:
            with pytest.raises((OSError, RuntimeError, TimeoutError)):
                await delete_codex_tree(root, tmp_path, recovery, **kwargs)

        methods = [call[0] for call in calls]
        assert not {"thread/resume", "thread/start", "turn/start"} & set(methods)
        mutation_cases = {"ok", "archived-root", "bad-ack", "wrong-notice", "leftover", "post-descendant", "cleanup"}
        expected_mutations = 0
        if case in {"bad-ack", "wrong-notice"}:
            expected_mutations = 1
        elif case in mutation_cases:
            expected_mutations = 3
        assert methods.count("thread/delete") == expected_mutations
        if case in {"owned-child", "owner-guard", "family-guard", "loaded", "ancestry", "changed", "external-fork"}:
            assert not recovery.exists()
        if case == "checkpoint":
            assert recovery.is_dir() and methods.count("thread/delete") == 0
        if case not in {"owned-child"}:
            assert methods[-1] == "release"

    asyncio.run(run())


def _delete_checkpoint(tmp_path):
    root, child = str(uuid4()), str(uuid4())
    home = tmp_path / "codex"
    paths = {
        root: home / "sessions" / f"{root}.jsonl",
        child: home / "archived_sessions" / f"{child}.jsonl",
    }
    recovery = tmp_path / "recovery"
    (recovery / "rollouts").mkdir(parents=True)
    targets = []
    for index, (identity, path) in enumerate(paths.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f'{{"id":"{identity}"}}\n'.encode()
        saved = recovery / "rollouts" / f"{index:04d}-{identity}.jsonl"
        saved.write_bytes(content)
        targets.append({
            "session_id": identity,
            "provider": "codex",
            "cwd": str(tmp_path),
            "path": str(path),
            "archived": path.is_relative_to(home / "archived_sessions"),
            "recovery_path": str(saved),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    return root, child, home, {
        "session_id": root,
        "provider": "codex",
        "cwd": str(tmp_path),
        "targets": targets,
        "recovery_dir": str(recovery),
    }


@pytest.mark.parametrize(
    "case",
    [
        "deleted",
        "present",
        "mixed",
        "bad-recovery",
        "read-error-with-file",
        "transport-error",
        "wrong-path",
        "changed",
        "owner-guard",
        "family-guard",
        "loaded",
        "cleanup",
    ],
)
def test_delete_reconciliation_is_strict_read_only_and_rejects_ambiguous_state(tmp_path, case):
    all_present = case not in {"deleted", "mixed", "transport-error"}
    root, child, home, checkpoint = _delete_checkpoint(tmp_path)
    if all_present:
        for target in checkpoint["targets"]:
            Path(target["path"]).write_bytes(Path(target["recovery_path"]).read_bytes())
    elif case == "mixed":
        target = checkpoint["targets"][0]
        Path(target["path"]).write_bytes(Path(target["recovery_path"]).read_bytes())
    if case == "bad-recovery":
        Path(checkpoint["targets"][0]["recovery_path"]).write_text("damaged", encoding="utf-8")
    calls = []

    class Rpc:
        process = None

        async def start(self, argv, **kwargs):
            calls.append(("start", argv))
            self.process = SimpleNamespace(pid=991)

        async def notify(self, method, params):
            calls.append((method, params))

        async def request(self, method, params):
            calls.append((method, params))
            if method == "initialize":
                return {}
            if method == "thread/read":
                identity = params["threadId"]
                target = next(item for item in checkpoint["targets"] if item["session_id"] == identity)
                if case in {"deleted", "transport-error"} or (case == "mixed" and identity == child):
                    if case == "transport-error":
                        raise WorkspaceRpcError("connection closed")
                    raise WorkspaceRpcError(f"thread not loaded: {identity}")
                if case == "read-error-with-file":
                    raise WorkspaceRpcError(f"thread not loaded: {identity}")
                path = target["path"]
                if case == "wrong-path" and identity == root:
                    path = str(home / "sessions" / f"moved-{identity}.jsonl")
                    Path(path).write_text("moved", encoding="utf-8")
                return {"thread": {"id": identity, "cwd": target["cwd"], "path": path}}
            if method == "thread/list":
                if case in {"deleted", "transport-error"}:
                    return {"data": [], "nextCursor": None}
                if case == "changed":
                    return {"data": [], "nextCursor": None}
                data = [{"id": child, "parentThreadId": root}] if params["archived"] else []
                return {"data": data, "nextCursor": None}
            if method == "thread/loaded/list":
                return {"data": [root] if case == "loaded" else []}
            raise AssertionError(f"Unexpected mutation: {method}")

        async def close(self):
            calls.append(("close", None))
            if case != "cleanup":
                self.process = None

    def guard(thread, pid):
        calls.append(("owner", thread["id"], pid))
        if case == "owner-guard":
            raise RuntimeError("owned")

    def family(threads):
        calls.append(("family", [thread["id"] for thread in threads]))
        if case == "family-guard":
            raise RuntimeError("reserved")

    async def run():
        kwargs = {
            "binary": "codex",
            "env": {"CODEX_HOME": str(home), "OPENAI_API_KEY": "never"},
            "rpc_factory": Rpc,
            "lease_factory": lambda identity: Lease(identity, calls),
            "ownership_guard": guard,
            "family_guard": family,
        }
        if case in {"deleted", "present"}:
            result = await inspect_codex_delete_tree(root, tmp_path, checkpoint, **kwargs)
            assert result["deleted"] is (case == "deleted")
            assert result["thread_ids"] == [root, child]
            family_calls = [call for call in calls if call[0] == "family"]
            assert family_calls == [("family", [] if case == "deleted" else [root, child])]
        else:
            with pytest.raises((RuntimeError, ValueError)):
                await inspect_codex_delete_tree(root, tmp_path, checkpoint, **kwargs)
        methods = [call[0] for call in calls]
        assert not {
            "thread/delete", "thread/archive", "thread/unarchive", "thread/resume", "thread/start", "turn/start"
        } & set(methods)
        if case == "bad-recovery":
            assert calls == []
        else:
            assert methods[-1] == "release"

    asyncio.run(run())
