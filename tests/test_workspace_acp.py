"""ACP framing and negotiation without authentication or coding sessions."""

import asyncio
import json
import os
import sys

import pytest

from core.workspace_acp import WorkspaceAcpRpc
from core.workspace_rpc import WorkspaceRpcError


def test_real_acp_pipe_framing_and_explicit_reverse_response(tmp_path):
    async def run():
        rpc = WorkspaceAcpRpc()
        code = """
import json, sys
def read():
    value = json.loads(sys.stdin.readline())
    assert value.pop('jsonrpc') == '2.0'
    return value
message = read()
assert message['method'] == 'initialize'
assert message['params']['clientCapabilities'] == {}
print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':{
 'protocolVersion':1,'agentCapabilities':{'loadSession':True},
 'authMethods':[{'id':'oauth-personal','name':'Google'}]}}), flush=True)
print(json.dumps({'jsonrpc':'2.0','id':'permission','method':'session/request_permission',
 'params':{'sessionId':'exact'}}), flush=True)
assert read() == {'id':'permission','result':{'outcome':{'outcome':'cancelled'}}}
assert read() == {'method':'session/cancel','params':{'sessionId':'exact'}}
assert sys.stdin.read() == ''
"""
        try:
            await rpc.start([sys.executable, "-c", code], cwd=tmp_path, env=dict(os.environ))
            result = await rpc.initialize()
            assert result["agentCapabilities"]["loadSession"] is True
            question = await asyncio.wait_for(rpc.events.get(), 2)
            assert question["params"]["sessionId"] == "exact"
            await rpc.answer("permission", {"outcome": {"outcome": "cancelled"}})
            with pytest.raises(WorkspaceRpcError, match="no longer"):
                await rpc.answer("permission", {})
            await rpc.notify("session/cancel", {"sessionId": "exact"})
        finally:
            process = rpc.process
            await rpc.close()
        assert process.returncode == 0, list(rpc.stderr)
    asyncio.run(run())


@pytest.mark.parametrize("result", [None, {}, {"protocolVersion": True},
    {"protocolVersion": 2}, {"protocolVersion": 1, "agentCapabilities": []},
    {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": [{}]}])
def test_invalid_negotiation_never_sends_auth_or_session_request(tmp_path, result):
    async def run():
        rpc = WorkspaceAcpRpc()
        code = """
import json, sys
message = json.loads(sys.stdin.readline())
assert message['method'] == 'initialize'
print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':json.loads(sys.argv[1])}),flush=True)
assert sys.stdin.read() == ''
"""
        try:
            await rpc.start([sys.executable, "-c", code, json.dumps(result)], cwd=tmp_path, env=dict(os.environ))
            with pytest.raises(WorkspaceRpcError, match="ACP server"):
                await rpc.initialize()
        finally:
            process = rpc.process
            await rpc.close()
        assert process.returncode == 0, list(rpc.stderr)
    asyncio.run(run())
