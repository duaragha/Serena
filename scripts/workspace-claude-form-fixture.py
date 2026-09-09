"""Local MCP fixture requesting a form during tool discovery, without inference."""
import asyncio
import json
import sys
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


async def main():
    receipt = Path(sys.argv[1])
    server = Server("Serena form proof")

    async def ask():
        receipt.with_suffix(".started").write_text("list_tools entered", encoding="utf-8")
        result = await server.request_context.session.elicit_form(
            "Choose proof count",
            {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1}}, "required": ["count"]},
        )
        receipt.write_text(json.dumps(result.model_dump()), encoding="utf-8")
        assert result.action == "accept" and result.content == {"count": 2}
        return [TextContent(type="text", text=result.model_dump_json())]

    @server.list_tools()
    async def tools():
        if "--tool" in sys.argv:
            return [Tool(name="ask", description="Ask the user for the proof count", inputSchema={"type": "object", "properties": {}})]
        await ask()
        return []

    @server.call_tool()
    async def call(name, arguments):
        assert name == "ask" and not arguments
        return await ask()

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
