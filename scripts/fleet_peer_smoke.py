"""Prove a frozen desktop can serve peer MCP without starting its GUI or a Fleet."""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke(binary: str, script: str | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="serena-peer-smoke-") as temporary:
        parameters = StdioServerParameters(
            command=binary,
            args=([script] if script else []) + ["--fleet-peer-mcp"],
            cwd=temporary,
            env={
                **os.environ,
                "SERENA_FLEET_DB_PATH": str(Path(temporary) / "fleet.sqlite3"),
                "SERENA_FLEET_PEER_TOKEN": "invalid-smoke-capability",
            },
        )
        async with (
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {
                "read_messages", "send_message", "request_help", "resolve_request", "propose_lesson", "review_lesson"
            }, names
            response = await session.call_tool("read_messages", {})
            assert response.isError, "an invalid capability must be refused"
            detail = " ".join(item.text for item in response.content if item.type == "text")
            assert "expired or invalid worker capability" in detail, detail
    print("Fleet peer MCP startup, tool contract and capability refusal passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--script", help="Source entrypoint for development smoke tests")
    args = parser.parse_args()
    asyncio.run(smoke(args.binary, args.script))
