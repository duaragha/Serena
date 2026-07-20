"""Visual MCP server manager.

Serena owns a master config at ~/.config/serena/mcp.json. The writers in
this package render that into Claude's ~/.claude.json and Codex's
~/.codex/config.toml by shelling out to `claude mcp add` / `codex mcp add`
(safer than hand-editing the underlying files).

Self-contained: delete this directory + the /api/mcp routes + the MCP tab
JS block in ui/web.py to remove the feature.
"""
