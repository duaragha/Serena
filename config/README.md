# Local Configuration Boundary

The files in this directory are templates and schemas only. Live configuration belongs in `~/.config/serena` with directory mode `0700` and file mode `0600`.

## Files

- `brain.env.example`: optional resident-brain overrides. Subscription OAuth remains the only supported authentication path.
- `desk.env.example`: optional desk and call overrides.
- `mcp.example.json`: secret-free shape for the MCP multiplexer configuration.

Copy only the template you need. Never put a real token, credential, runtime database, model, transcript, or acceptance report in this repository.

The bootstrap doctor verifies the boundary. The private backup command handles the live files without adding them to Git.
