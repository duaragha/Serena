"""A new chat on several agents links, and looks linked while it waits.

Two faults, both seen on one attempt that opened Claude, Codex and Gemini:

The thread never linked. A pane only becomes a real session once its agent
writes a transcript, and Codex and Gemini write nothing until their first
message. Linking waited for every member to resolve, so a thread only linked
if the user typed into all three -- on the real attempt only the Claude pane
was used, it became an ordinary chat, and the other two expired as
placeholders.

And while it waited, the sidebar folds rows on `group`, which no placeholder
has, so the three showed as three unrelated chats.
"""

from __future__ import annotations

import re

from ui import web


def _function(name: str) -> str:
    start = web.HTML.index(f"function {name}(")
    depth = 0
    for index in range(web.HTML.index("{", start), len(web.HTML)):
        char = web.HTML[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return web.HTML[start : index + 1]
    raise AssertionError(f"{name} is not brace-balanced")


def test_placeholders_share_a_group_before_any_agent_speaks() -> None:
    """Otherwise the thread reads as separate chats until someone types."""
    body = _function("newLinkedChatInline")

    assert "const clientGroup = 'fdg-' + pairId;" in body
    assert "pseudo.group = clientGroup;" in body
    # Every member gets it, not just the one taking focus.
    assert body.index("pseudo.group = clientGroup;") < body.index("const lead = pseudos[0];")


def test_linking_starts_at_the_second_member_not_the_last() -> None:
    page = web.HTML

    assert "if (bucket.length >= 2) _fdLinkPair([...bucket], 0);" in page
    assert "bucket.length >= (pseudo.fd_pair_size || 2)" not in page, (
        "linking still waits for panes that may never write a session"
    )
    assert "if (bucket.length === 2)" not in page


def test_the_bucket_is_kept_so_a_late_member_joins_the_same_thread() -> None:
    """Deleting the bucket on the first link would leave a straggler to start
    a rival group; link_sessions merges, so re-linking the whole bucket grows
    the thread instead."""
    page = web.HTML
    start = page.index("if (pseudo.fd_pair_id) {")
    body = page[start : start + 1200]

    assert "if (!bucket.includes(match.session_id)) bucket.push(match.session_id);" in body
    assert "delete _fdPairResolved[pseudo.fd_pair_id];" not in body, (
        "the bucket is discarded, so a member resolving later cannot join"
    )


def test_a_pane_that_never_writes_a_session_cannot_block_the_others() -> None:
    """The regression in one line: the gate is a count of what arrived, never
    a count of what was asked for."""
    page = web.HTML
    start = page.index("if (pseudo.fd_pair_id) {")
    body = page[start : start + 1200]

    gates = re.findall(r"if \(bucket\.length[^)]*\)", body)
    assert gates == ["if (bucket.length >= 2)"], gates
    assert "fd_pair_size" not in body


def test_a_single_agent_still_takes_the_ordinary_path() -> None:
    body = _function("newLinkedChatInline")

    assert "if (ordered.length < 2) return newChatInline(cwdOverride);" in body


def test_the_thread_size_is_still_recorded_for_anything_else_that_wants_it() -> None:
    """fd_pair_size stops gating the link but still describes the thread."""
    assert "fd_pair_size: pairId ? (pairSize || 2) : null" in web.HTML
