"""Real subprocess pipes; no provider calls, sessions, credentials or PTYs."""

import asyncio
import contextlib
import os
import sys

import psutil
import pytest

from core.workspace_rpc import WorkspaceRpc, WorkspaceRpcError

PEER = r"""
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if method == 'ask':
        emit({'id':'approval', 'method':'item/commandExecution/requestApproval', 'params':{'threadId':'exact'}})
        emit({'id':msg['id'], 'result':{'turnId':'turn'}})
    elif method == 'ping':
        emit({'id':msg['id'], 'result':msg['params']})
    elif method == 'resolve':
        emit({'method':'serverRequest/resolved', 'params':{'requestId':'approval'}})
        emit({'id':msg['id'], 'result':{}})
    elif method == 'exit':
        sys.exit(0)
    elif method == 'bad':
        print('not json', flush=True)
    elif method == 'wait':
        pass
    elif method == 'noise':
        sys.stderr.write('x' * 200000); sys.stderr.flush()
        emit({'id':msg['id'], 'result':True})
    elif 'result' in msg:
        emit({'method':'answerObserved', 'params':msg})
"""


def test_idle_pause_wakes_same_child_before_rpc_and_close(tmp_path):
    async def run():
        rpc = WorkspaceRpc()
        await rpc.start([sys.executable, "-u", "-c", PEER], cwd=tmp_path, env=dict(os.environ))
        process = rpc.process
        try:
            assert await rpc.request("ping", {"ready": True}) == {"ready": True}
            assert await rpc.pause_idle()
            assert rpc.suspended
            async with asyncio.timeout(2):
                while psutil.Process(process.pid).status() != psutil.STATUS_STOPPED:
                    await asyncio.sleep(.01)
            assert await rpc.request("ping", {"awake": True}, timeout=2) == {"awake": True}
            assert rpc.process is process and not rpc.suspended
            assert await rpc.pause_idle()
        finally:
            await rpc.close()
        assert process.returncode == 0 and not rpc.suspended
    asyncio.run(run())


@pytest.mark.skipif(os.name == "nt", reason="POSIX group suspension")
@pytest.mark.parametrize("blocker", ["pending", "questions", "events", "group"])
def test_pause_refuses_inflight_control_or_unsafe_group(tmp_path, monkeypatch, blocker):
    async def run():
        rpc = WorkspaceRpc()
        await rpc.start([sys.executable, "-u", "-c", PEER], cwd=tmp_path, env=dict(os.environ))
        try:
            assert await rpc.request("ping", {}) == {}
            if blocker == "pending":
                rpc._pending[123] = asyncio.get_running_loop().create_future()
            elif blocker == "questions":
                rpc._questions.add("approval")
            elif blocker == "events":
                await rpc.events.put({"method": "turn/started"})
            else:
                monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())
            if blocker == "group":
                with pytest.raises(WorkspaceRpcError, match="isolated"):
                    await rpc.pause_idle()
            else:
                assert not await rpc.pause_idle()
            assert not rpc.suspended
        finally:
            rpc._pending.clear()
            await rpc.close()
    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Windows assignment gate")
def test_failed_job_assignment_never_executes_provider(tmp_path, monkeypatch):
    from core.workspace_windows_job import WindowsJob

    marker = tmp_path / "must-not-exist"
    def reject(self, pid):
        raise OSError("assignment refused")
    monkeypatch.setattr(WindowsJob, "assign", reject)
    async def run():
        rpc = WorkspaceRpc()
        with pytest.raises(OSError, match="assignment refused"):
            await rpc.start(
                [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)],
                cwd=tmp_path, env=dict(os.environ),
            )
        assert rpc.process is None
        assert rpc._windows_job is None
        assert not marker.exists()
        await rpc.close()
    asyncio.run(run())


@pytest.mark.parametrize("inherit_output", [False, True])
def test_owner_close_removes_child_even_after_leader_exits(tmp_path, inherit_output):
    peer = r"""
import json, subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
    stdin=subprocess.DEVNULL, stdout=None if sys.argv[1]=='True' else subprocess.DEVNULL,
    stderr=subprocess.DEVNULL)
for line in sys.stdin:
    msg=json.loads(line)
    print(json.dumps({'id':msg['id'],'result':{'pid':child.pid}}),flush=True)
"""
    async def run():
        rpc = WorkspaceRpc()
        child = None
        try:
            await rpc.start([sys.executable, '-u', '-c', peer, str(inherit_output)], cwd=tmp_path, env=dict(os.environ))
            pid = (await rpc.request('child', {}))['pid']
            child = psutil.Process(pid)
            if os.name != "nt":
                assert os.getpgid(pid) == rpc.process.pid
                assert os.getpgid(pid) != os.getpgrp()
            else:
                assert rpc._windows_job.active_processes() >= 2
            await asyncio.wait_for(rpc.close(), 8)
            for _ in range(100):
                if not child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
                    break
                await asyncio.sleep(.01)
            assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        finally:
            if child:
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
            await rpc.close()
    asyncio.run(run())


def test_bidirectional_approval_does_not_block_other_requests(tmp_path):
    async def run():
        rpc = WorkspaceRpc()
        await rpc.start([sys.executable, "-u", "-c", PEER], cwd=tmp_path, env=dict(os.environ))
        try:
            with pytest.raises(WorkspaceRpcError, match="already owns"):
                await rpc.start([sys.executable], cwd=tmp_path, env={})
            assert await rpc.request("ask", {}) == {"turnId": "turn"}
            event = await asyncio.wait_for(rpc.events.get(), 2)
            assert event["params"]["threadId"] == "exact"
            assert await rpc.request("ping", {"text": "hello\nworld"}) == {"text": "hello\nworld"}
            await rpc.answer("approval", {"decision": "decline"})
            answer = await asyncio.wait_for(rpc.events.get(), 2)
            assert answer["params"]["result"] == {"decision": "decline"}
            with pytest.raises(WorkspaceRpcError, match="no longer"):
                await rpc.answer("approval", {"decision": "accept"})
            assert await rpc.request("noise", {}) is True
        finally:
            process = rpc.process
            await rpc.close()
            assert process.returncode == 0

    asyncio.run(run())


def test_resolved_prompt_cannot_be_answered(tmp_path):
    async def run():
        rpc = WorkspaceRpc()
        await rpc.start([sys.executable, "-u", "-c", PEER], cwd=tmp_path, env=dict(os.environ))
        try:
            await rpc.request("ask", {})
            await rpc.request("resolve", {})
            with pytest.raises(WorkspaceRpcError, match="no longer"):
                await rpc.answer("approval", {"decision": "accept"})
        finally:
            await rpc.close()

    asyncio.run(run())


@pytest.mark.parametrize("method", ["exit", "bad"])
def test_transport_failure_releases_waiters(tmp_path, method):
    async def run():
        rpc = WorkspaceRpc()
        await rpc.start([sys.executable, "-u", "-c", PEER], cwd=tmp_path, env=dict(os.environ))
        try:
            with pytest.raises(WorkspaceRpcError):
                await rpc.request(method, {}, timeout=2)
            with pytest.raises(WorkspaceRpcError):
                await rpc.request("ping", {})
        finally:
            await rpc.close()
        assert not rpc._pending

    asyncio.run(run())


def test_timeout_does_not_restart_or_cancel_agent(tmp_path):
    async def run():
        rpc = WorkspaceRpc()
        await rpc.start([sys.executable, "-u", "-c", PEER], cwd=tmp_path, env=dict(os.environ))
        try:
            pid = rpc.process.pid
            with pytest.raises(TimeoutError):
                await rpc.request("wait", {}, timeout=0.1)
            assert await rpc.request("ping", {"same": True}) == {"same": True}
            assert rpc.process.pid == pid
            assert not rpc._pending
        finally:
            await rpc.close()

    asyncio.run(run())
