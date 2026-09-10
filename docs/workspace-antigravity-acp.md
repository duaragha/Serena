# Antigravity ACP Integration

Research and native handshake verified 2026-09-09. This supersedes the earlier
assumption that Antigravity has no suitable interactive interface. The installed
`agy` CLI is still unsuitable, but Google distributes a separate ACP server.

## Primary Evidence

All URLs accessed 2026-09-09:

- https://antigravity.google/docs/ide/extensions/zed/ directs users to install
  Antigravity from the external-agent registry and documents `oauth-personal`
  for individual Free/Pro/Ultra accounts. This establishes a subscription route,
  not proof that existing CLI credentials or session stores are interchangeable.
- https://zed.dev/acp/agent/antigravity-acp identifies the executable as
  `agy_acp_server.par`, separate from `agy`.
- https://raw.githubusercontent.com/agentclientprotocol/registry/main/antigravity-acp/agent.json
  identifies Google LLC, version 1.1.1, proprietary licensing, Linux/macOS/Windows
  distributions and the Linux argument `--uid=`.
- https://antigravity.google/docs/cli/headless/ still documents rejection of
  control request/response events, CLI slash commands and non-text input blocks.
  Do not build the rich pane on that print-mode protocol.
- https://antigravity.google/docs/sdk/overview/ documents API-key or enterprise
  platform authentication for the Python SDK; it is not the selected integration.
- https://antigravity.google/docs/remote-control/ describes a registered OS daemon
  and hosted Google dashboard. No daemon was installed or started.
- https://agentclientprotocol.com/protocol/v1/initialization defines JSON-RPC 2.0
  framing, version negotiation and advertised capabilities. Initialization does
  not require creating a coding session.
- https://agentclientprotocol.com/protocol/v1/session-setup requires checking
  load/resume support and using the exact persisted ID. Load replays history;
  resume does not. Neither implies compatibility with a different native store.

## Native Probe

Downloaded only into ignored development build artifacts:

`https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-agy_acp_server_1.1.1-linux-x86_64.zip`

Observed archive SHA-256:
`38f62d01b32deb0907b3d39a71ec301fd36369f6ffd1cf262d4af385177f79df`

Archive contains `agy_acp_server.par` and `localharness_external`. The digest is
a reproducibility record of the downloaded artifact, not an independently
published signature. No binary is committed or added to application packaging.

The production `WorkspaceAcpRpc` transport successfully negotiated version 1
with `antigravity-acp`, version `agy_acp_server_1.1.1`. Observed capabilities:

- `loadSession: true`; session `list` and `resume` supported.
- Prompt image, audio and embedded context supported.
- MCP HTTP and SSE supported.
- `oauth-personal`, `oauth-business`, `gemini-api-key`, `agent-platform` advertised.

The probe uses a temporary empty HOME, explicit minimal environment and no user
credentials. It sends only `initialize`, then closes and reaps the process.
It does not call authenticate, new, load, resume, prompt or a provider tool.

Exact verification:

- `SERENA_EVIDENCE_KIND=live agy --help`: exit 0; no ACP mode on installed CLI.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_acp.py -q --tb=short`: exit 0, 7 passed. Real subprocess framing, explicit reverse response, duplicate-response refusal and invalid negotiation; no automatic auth/session request.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_acp.py tests/test_workspace_acp.py scripts/verify-workspace-antigravity-acp.py`: exit 0.
- `SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-antigravity-acp.py apps/desktop/build/proof-tools/antigravity-acp/agy_acp_server.par`: exit 0; capability response above, isolated native server exit 0. Repeated after replacing the probe-local transport with production transport.

## Next Integration Work

The JSON-RPC foundation is implemented, not the provider adapter or UI admission.
Gemini remains unavailable in the development rich pane until these checks pass:

1. Establish native session storage and whether an existing `agy` conversation
   can be loaded by this server without conversion, duplication or context loss.
2. Verify supported subscription authentication without copying credentials,
   introducing API billing or silently triggering browser sign-in.
3. Map ACP updates, permission choices, config options, models, commands,
   images and cancellation to the shared owner/journal/pane contracts.
4. Bind exact session leases before load; fail closed if a PTY or other worker
   already owns it. Never fall back from failed load to session/new.
5. Verify configured instructions, skills, plugins, MCP and tools are retained.
6. Exercise native session round trips and Linux/Windows packaging before enabling.

The previous CLI limitation is no longer a reason to declare the overall Gemini
integration impossible. Advertised capability support is also not a substitute
for the still-missing end-to-end proof.
