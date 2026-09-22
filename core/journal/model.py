"""One model call for the journal, that survives a dead login.

The first live run on the PC lost everything the chats could say -- who he
spent Sunday with -- because the PC's Claude login had expired ("OAuth
session expired and could not be refreshed") and the extraction had nowhere
else to go. Her brain rides out the same failure on its Codex fallback; this
gives the journal the same second path, so an expired login costs a draft its
polish, not its facts.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

CLAUDE_MODEL = "sonnet"
CODEX_TIMEOUT_SECONDS = 300
# Every one-shot call is a saved session, and his chat list shows every saved
# session -- the first week of drafts put a column of "You are reading
# Raghav's own chat messages..." chats in his sidebar. A cwd under
# ~/.cache/serena-headless-* is the scanner's skip convention (core/scanner.py).
HEADLESS_CWD = Path.home() / ".cache" / "serena-headless-journal"


class ModelUnavailable(RuntimeError):
    """Neither provider answered."""


async def _claude(prompt: str, system: str) -> str:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    HEADLESS_CWD.mkdir(parents=True, exist_ok=True)
    options = ClaudeAgentOptions(model=CLAUDE_MODEL, tools=[], allowed_tools=[], setting_sources=[],
                                 max_turns=1, system_prompt=system, cwd=str(HEADLESS_CWD))
    chunks: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            chunks.extend(b.text for b in message.content if isinstance(b, TextBlock))
        elif isinstance(message, ResultMessage) and message.is_error:
            raise ModelUnavailable(f"claude: {message.result}")
    return "".join(chunks).strip()


def _codex(prompt: str, system: str) -> str:
    codex = shutil.which("codex") or shutil.which("codex.cmd")
    if not codex:
        raise ModelUnavailable("codex is not installed on this machine")
    with tempfile.TemporaryDirectory() as scratch:
        # The prompt goes on stdin ("-"), never argv: a day of chats is well
        # past Windows' 32K command-line limit, and the first fallback run on
        # the PC died on exactly that ("filename or extension is too long").
        done = subprocess.run(
            # --ephemeral: no saved rollout, so no chat in his sidebar.
            [codex, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
             "-C", scratch, "-"],
            input=f"{system}\n\n{prompt}",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CODEX_TIMEOUT_SECONDS, check=False)
    last = ""
    for line in done.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            last = str(item.get("text") or "")
    if not last:
        raise ModelUnavailable(f"codex returned nothing (exit {done.returncode}): {done.stderr[-300:]}")
    return last.strip()


def ask(prompt: str, *, system: str) -> str:
    """Claude first; Codex when Claude cannot answer at all."""

    try:
        text = asyncio.run(_claude(prompt, system))
        if text:
            return text
    except Exception:
        pass
    return _codex(prompt, system)
