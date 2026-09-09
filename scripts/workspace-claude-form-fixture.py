"""Local MCP fixture requesting a form during tool discovery, without inference."""
import asyncio
import json
import sys
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def main():
    receipt = Path(sys.argv[1])
    server = Server("Serena form proof")

    @server.list_tools()
    async def tools():
        receipt.with_suffix(".started").write_text("list_tools entered", encoding="utf-8")
        result = await server.request_context.session.elicit_form(
            "Choose proof count",
            {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1}}, "required": ["count"]},
        )
        receipt.write_text(json.dumps(result.model_dump()), encoding="utf-8")
        assert result.action == "accept" and result.content == {"count": 2}
        return []

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
