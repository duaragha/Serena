"""ACP JSON-RPC transport; initialization never authenticates or opens a session."""

from __future__ import annotations

from copy import deepcopy

from core.workspace_rpc import WorkspaceRpc, WorkspaceRpcError


class WorkspaceAcpRpc(WorkspaceRpc):
    async def _write(self, message):
        await super()._write({**message, "jsonrpc": "2.0"})

    async def initialize(self, *, timeout: float = 30) -> dict:
        result = await self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "serena-workspace", "version": "0.1.0"},
        }, timeout=timeout)
        if not isinstance(result, dict) or type(result.get("protocolVersion")) is not int or result["protocolVersion"] != 1:
            raise WorkspaceRpcError("ACP server did not negotiate protocol version 1")
        if not isinstance(result.get("agentCapabilities"), dict):
            raise WorkspaceRpcError("ACP server did not advertise valid capabilities")
        methods = result.get("authMethods")
        if not isinstance(methods, list) or any(
            not isinstance(method, dict) or not isinstance(method.get("id"), str)
            or not method["id"] or not isinstance(method.get("name"), str)
            for method in methods
        ):
            raise WorkspaceRpcError("ACP server did not advertise valid authentication methods")
        return deepcopy(result)
