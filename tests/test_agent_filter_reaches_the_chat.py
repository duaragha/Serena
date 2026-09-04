"""Filtering by an agent must show that agent's chats.

A linked thread renders as one row and the rest of its members are folded
underneath, and the head was always the Claude chat. With the Codex filter on
that meant a claude-headed pair answered with a Claude row -- the Codex chat
was in the list, counted, and unreachable.

It looked like older chats had gone missing, because pairing was more common
back then: of Raghav's own 38 Codex chats, 9 are unpaired and render normally,
29 are folded into a thread, and 21 of those threads are headed by a Claude
chat. The recent unpaired ones showed; the older paired ones did not.

The comparator is pulled out of the served page and run, so this tests the
ordering the browser actually applies.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import web

HARNESS = r"""
'use strict';
const assert = require('node:assert/strict');

let _agentFilter = null;

__FUNCTION__

const claude = { session_id: 'c1', agent: 'claude', last_timestamp: '2026-06-04T10:00:00Z' };
const codex  = { session_id: 'x1', agent: 'codex',  last_timestamp: '2026-06-04T12:00:00Z' };
const gemini = { session_id: 'g1', agent: 'gemini', last_timestamp: '2026-06-04T11:00:00Z' };

function head(members) { return members.slice().sort(_groupHeadFirst)[0].agent; }

// Browsing with no filter: one row per thread, headed by Claude, even though
// the Codex chat is the more recent of the two.
_agentFilter = null;
assert.equal(head([codex, claude]), 'claude', 'the default head changed');
assert.equal(head([claude, codex]), 'claude');

// Asking for Codex has to answer with the Codex chat.
_agentFilter = 'codex';
assert.equal(head([claude, codex]), 'codex', 'the Codex filter still shows a Claude row');
assert.equal(head([codex, claude]), 'codex');

// And the same for a three-way thread.
_agentFilter = 'gemini';
assert.equal(head([claude, codex, gemini]), 'gemini');
_agentFilter = 'codex';
assert.equal(head([claude, codex, gemini]), 'codex');

// A filter naming an agent the thread does not contain must not reorder it
// into something surprising; the thread is dropped by the filter afterwards.
_agentFilter = 'gemini';
assert.equal(head([claude, codex]), 'claude', 'an absent agent changed the head');

// Recency still breaks ties within the winning agent.
_agentFilter = null;
const older = { session_id: 'c0', agent: 'claude', last_timestamp: '2026-01-01T00:00:00Z' };
assert.equal(head([older, claude]).length > 0, true);
assert.equal([older, claude].slice().sort(_groupHeadFirst)[0].session_id, 'c1',
  'the most recent chat of the heading agent must lead');

console.log('ok');
"""


def _extract(name: str) -> str:
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run page code")
def test_the_filtered_agent_heads_its_own_thread(tmp_path: Path) -> None:
    script = tmp_path / "fold.cjs"
    script.write_text(HARNESS.replace("__FUNCTION__", _extract("_groupHeadFirst")), encoding="utf-8")

    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("ok")


def test_the_fold_uses_the_comparator_rather_than_its_own_copy() -> None:
    """Two orderings would drift apart, and only one of them is tested."""
    page = web.HTML
    start = page.index("const groupBuckets = new Map()")
    body = page[start : start + 1200]

    assert "arr.sort(_groupHeadFirst)" in body
    assert "=== 'claude' ? 0 : 1" not in body, "an inline copy of the old rule survives"
