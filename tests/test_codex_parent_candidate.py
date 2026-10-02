from __future__ import annotations

import pytest

from core.indexer import _is_agent_spawned_candidate


@pytest.mark.parametrize("originator,expected", [
    # codex-cli 0.159 tags interactive TUI chats source=vscode; they are still
    # the user's own terminal chats and must never nest under a Claude chat.
    ("codex-tui:vscode", False),
    ("codex-tui:cli", False),
    ("codex_cli_rs:cli", False),
    ("codex_exec:exec", True),
    ("codex_cli_rs:mcp", True),
    ("serena-workspace:vscode", True),
    ("Claude Code", True),
])
def test_agent_spawned_candidate(originator, expected):
    assert _is_agent_spawned_candidate({"originator": originator}) is expected
