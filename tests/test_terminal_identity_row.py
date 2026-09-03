"""Every open pane says which machine it is on and which session it is.

Raghav opened an unlinked chat and found neither: no "Linux · laptop", no
session id. Both only appeared on linked claude+codex pairs, because the render
was reached exclusively through the split-view refresh, which returns early
unless two panes are showing. A solo pane is the common case, and it is the one
where "which machine is this running on" is hardest to answer from memory.

The functions are pulled straight out of the served page and executed, so this
tests the shipped behaviour rather than the presence of a line of source.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import web

FUNCTIONS = (
    "_resetIdentityRowForSwitch",
    "_visibleRuntimeSids",
    "_machineBadge",
    "_renderOpenSessionIds",
    "_refreshGtkRuntimeStatus",
)

HARNESS = r"""
'use strict';
const assert = require('node:assert/strict');

// A DOM small enough to reason about: elements remember their class, text and
// children, which is all the identity row builds.
function el(tag) {
  const node = {
    tag, textContent: '', title: '', type: '', children: [],
    classList: {
      _has: new Set(),
      add(c) { this._has.add(c); },
      remove(c) { this._has.delete(c); },
      toggle(c, on) { on ? this._has.add(c) : this._has.delete(c); },
      contains(c) { return this._has.has(c); },
    },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; },
    addEventListener() {},
  };
  // className and classList are two views of one thing in a real DOM, and the
  // page sets classes through both.
  Object.defineProperty(node, 'className', {
    get() { return [...node.classList._has].join(' '); },
    set(value) {
      node.classList._has = new Set(String(value).split(/\s+/).filter(Boolean));
    },
  });
  return node;
}

const root = el('div');
const document = { getElementById: (id) => (id === 'termSessionIds' ? root : null), createElement: el };
const window = { SERENA: { machine: { os: 'Linux', name: 'laptop' } } };
const navigator = { clipboard: { writeText: () => Promise.resolve() } };
function showToast() {}

const SESSIONS = JSON.parse(process.env.SESSIONS);
function _findClientSession(sid) { return SESSIONS[sid] || null; }
const termSessions = new Map();
const _gtkRuntimeStates = new Map();
let termStatus = '';
function setTermStatus(text) { termStatus = text === undefined ? '' : text; }
function _findClientSessionAgent(sid) { return (SESSIONS[sid] || {}).agent; }

let _gtkSplitActive = false;
let _gtkSplitSids = null;
let _gtkCodeSid = null;
let activeTermSid = null;

__FUNCTIONS__

// Exactly what the running page calls when runtime state changes.
function render() { _refreshGtkRuntimeStatus(); return root; }
function pills() { return root.children.filter((c) => c.tag === 'button').map((c) => c.textContent); }
function badges() { return root.children.filter((c) => c.classList.contains('term-machine')); }

// An unlinked claude pane.
activeTermSid = 'c660e9ce-1111-2222-3333-444444444444';
render();
assert.equal(badges().length, 1, 'an unlinked pane must name its machine');
assert.equal(badges()[0].textContent, 'Linux · laptop');
assert.deepEqual(pills(), ['claude c660e9ce'], 'an unlinked claude pane must show its session id');
assert.ok(!root.classList.contains('hidden'), 'the row must be visible');

// An unlinked codex pane.
activeTermSid = '019fdfc8-5555-6666-7777-888888888888';
render();
assert.deepEqual(pills(), ['codex 019fdfc8'], 'an unlinked codex pane must show its session id');
assert.equal(badges().length, 1);

// A linked pair still shows both, which already worked.
_gtkSplitActive = true;
_gtkSplitSids = ['c660e9ce-1111-2222-3333-444444444444', '019fdfc8-5555-6666-7777-888888888888'];
render();
assert.deepEqual(pills(), ['claude c660e9ce', 'codex 019fdfc8']);
assert.equal(badges().length, 1, 'one machine badge, not one per session');

// Nothing open: the row gets out of the way entirely.
_gtkSplitActive = false;
_gtkSplitSids = null;
activeTermSid = null;
render();
assert.deepEqual(pills(), []);
assert.ok(root.classList.contains('hidden'), 'an empty row must hide itself');

// The native GTK shell tracks its focused pane in a different variable.
window.__nativeTerminalBridge = true;
_gtkCodeSid = 'c660e9ce-1111-2222-3333-444444444444';
render();
assert.deepEqual(pills(), ['claude c660e9ce'], 'the native shell must render the row too');

// --- switching chats ----------------------------------------------------
// A linked claude+codex pair is open and the row describes it.
window.__nativeTerminalBridge = false;
_gtkCodeSid = null;
_gtkSplitActive = true;
_gtkSplitSids = ['c660e9ce-1111-2222-3333-444444444444', '019fdfc8-5555-6666-7777-888888888888'];
activeTermSid = 'c660e9ce-1111-2222-3333-444444444444';
setTermStatus('claude live  ·  codex live');
render();
assert.deepEqual(pills(), ['claude c660e9ce', 'codex 019fdfc8']);

// Now open a gemini chat that is in no group at all. Its runtime has not
// spawned yet, so nothing in the terminal lifecycle will redraw the row.
const GEMINI = '9984a527-9999-aaaa-bbbb-cccccccccccc';
_resetIdentityRowForSwitch(GEMINI, false);

assert.deepEqual(pills(), ['gemini 9984a527'],
  'an ungrouped chat inherited the previous pair session ids');
assert.equal(_gtkSplitActive, false, 'the previous split must not survive the switch');
assert.equal(_gtkSplitSids, null);
assert.equal(termStatus, '',
  'the status still described two runtimes from a different conversation');

// Opening a chat in read mode shows no runtime at all.
_resetIdentityRowForSwitch('019fdfc8-5555-6666-7777-888888888888', true);
assert.deepEqual(pills(), [], 'read view has no live runtime to name');
assert.ok(root.classList.contains('hidden'));

// Clicking the other half of the pair you are already viewing is not a switch
// away from the split, and must not tear it down.
_gtkSplitActive = true;
_gtkSplitSids = ['c660e9ce-1111-2222-3333-444444444444', '019fdfc8-5555-6666-7777-888888888888'];
_resetIdentityRowForSwitch('019fdfc8-5555-6666-7777-888888888888', false);
assert.equal(_gtkSplitActive, true, 'focusing a sibling collapsed the split');
assert.deepEqual(_gtkSplitSids.length, 2);

console.log('ok');
"""


def _extract(name: str) -> str:
    """Lift one top-level function out of the served page by brace balance."""
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
def test_the_identity_row_names_the_machine_and_session_for_every_pane(tmp_path: Path) -> None:
    source = HARNESS.replace("__FUNCTIONS__", "\n\n".join(_extract(name) for name in FUNCTIONS))
    script = tmp_path / "identity-row.cjs"
    script.write_text(source, encoding="utf-8")

    sessions = {
        "c660e9ce-1111-2222-3333-444444444444": {"agent": "claude"},
        "019fdfc8-5555-6666-7777-888888888888": {"agent": "codex"},
        "9984a527-9999-aaaa-bbbb-cccccccccccc": {"agent": "gemini"},
    }
    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "SESSIONS": json.dumps(sessions)},
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("ok")


def test_the_render_is_not_gated_behind_the_split_view() -> None:
    """The bug was structural: the only caller sat behind an is-split guard."""
    body = _extract("_refreshGtkRuntimeStatus")
    before_guard = body[: body.index("if (!_gtkSplitActive")]

    assert "_renderOpenSessionIds" in before_guard, (
        "the identity row must render before the split-only early return"
    )


def test_leaving_the_terminal_clears_the_row() -> None:
    assert "_renderOpenSessionIds([])" in _extract("_hideAllTermPanes")


def test_switching_chats_redraws_the_row_rather_than_waiting_for_a_runtime() -> None:
    """openConv is where the row goes stale, so it is where it must be reset.

    Raghav opened a Gemini chat and the row read "claude 979a60b0 · codex
    019de3ff" — the linked pair he had open before it. Those ids were correct
    for a conversation he was no longer looking at.
    """
    page = web.HTML
    start = page.index("async function openConv(")
    body = page[start : start + 2600]

    assert "_resetIdentityRowForSwitch(sid, showReadView)" in body, (
        "switching chats leaves the previous chat's session ids on screen"
    )


def test_a_resumed_chat_is_not_announced_as_claude_regardless_of_agent() -> None:
    """"Starting claude --resume" was printed for codex and gemini too."""
    page = web.HTML
    start = page.index("async function startLiveTerminal(")
    body = page[start : page.index("setTermStatus(opts.isNew", start) + 400]

    assert "'Starting claude --resume '" not in body
    assert "localSession && localSession.agent" in body
