"""Resuming a Codex chat must not stop to ask which directory to use.

Chats sync between the laptop and the PC, so a session's recorded cwd is very
often a path belonging to the other machine. `codex resume <sid>` notices that
the recorded cwd is not the one it was launched in and stops on a "Choose
working directory to resume this session" prompt, waiting for a keypress.

In a terminal that reads as the chat never opening, and the choice it offers is
a trap: the session directory is a Windows path that does not exist on Linux,
and "always use session directory" would be wrong on one machine forever.

Serena already resolves the cwd cross-platform and spawns the process there, so
it can answer the question itself with --cd. Measured against the real CLI:

    codex resume <sid>              prompts for a directory
    codex resume <sid> --cd <dir>   goes straight to the input line
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "ui" / "web.py"


def _spawn_source() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    start = text.index("def api_spawn_terminal(")
    return text[start : start + 4000]


def test_resume_passes_the_resolved_working_directory() -> None:
    body = _spawn_source()

    match = re.search(r'argv = \["codex", "resume", sid([^\]]*)\]', body)
    assert match, "the codex resume argv is no longer recognisable"

    tail = match.group(1)
    assert '"--cd"' in tail, "codex will stop and ask which directory to resume in"
    assert "cwd" in tail, "--cd must carry the resolved cwd, not a literal"


def test_it_uses_the_cwd_that_was_already_resolved() -> None:
    """Passing anything else would run the agent somewhere the pane is not."""
    body = _spawn_source()

    resolved_first = body.index("cwd = resolve_session_cwd")
    argv_line = body.index('argv = ["codex", "resume", sid')

    assert resolved_first < argv_line, "argv must be built after the cwd is resolved"
    assert '"--cd", cwd]' in body, "the flag should pass the resolved cwd verbatim"


def test_a_fresh_codex_chat_is_left_alone() -> None:
    """A new session has no recorded cwd to disagree with, so it never prompts."""
    body = _spawn_source()

    assert 'argv = ["codex"]' in body, "the fresh-session branch changed shape"
