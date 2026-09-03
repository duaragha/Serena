"""Regression coverage for a linked renderer terminal exiting.

The dead pane used to disappear from ``termSessions`` while the split globals
and status map still described two live panes. The survivor therefore stayed
half-width beside an empty black mount until the whole chat was reopened.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ui import web


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_dead_linked_pane_collapses_to_the_surviving_runtime(tmp_path: Path) -> None:
    helper = _extract("_collapseWebSplitAfterTerminalExit")
    source = f"""
'use strict';
const assert = require('node:assert/strict');
const dead = 'dead-codex';
const survivor = 'live-claude';
const termSessions = new Map([[survivor, {{}}]]);
const _gtkRuntimeStates = new Map([[dead, 'live'], [survivor, 'live']]);
let _gtkSplitActive = true;
let _gtkSplitSids = [survivor, dead];
let _gtkCurrentGroup = 'linked-group';
const container = {{}};
const document = {{ getElementById: id => id === 'termMounts' ? container : null }};
let dividerArgs = null;
let pinSyncs = 0;
let rendered = null;
function _layoutWebSplitDivider(gotContainer, split) {{ dividerArgs = [gotContainer, split]; }}
function _syncRuntimePinButton() {{ pinSyncs += 1; }}
function _renderOpenSessionIds(sids) {{ rendered = sids; }}

{helper}

assert.equal(_collapseWebSplitAfterTerminalExit(dead), survivor);
assert.equal(_gtkRuntimeStates.has(dead), false);
assert.equal(_gtkRuntimeStates.get(survivor), 'live');
assert.equal(_gtkSplitActive, false);
assert.equal(_gtkSplitSids, null);
assert.equal(_gtkCurrentGroup, null);
assert.deepEqual(dividerArgs, [container, false]);
assert.equal(pinSyncs, 1);
assert.equal(rendered, null, 'the survivor activation redraws its own identity row');
console.log('ok');
"""
    script = tmp_path / "split-exit.cjs"
    script.write_text(source, encoding="utf-8")

    result = subprocess.run(
        [shutil.which("node") or "node", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "ok"


def test_teardown_reactivates_the_survivor_after_removing_dead_mount() -> None:
    body = _extract("teardownLiveTerminal")

    assert "const survivorSid = _collapseWebSplitAfterTerminalExit(sid);" in body
    assert body.index("termSessions.delete(sid);") < body.index(
        "_collapseWebSplitAfterTerminalExit(sid)"
    )
    assert body.index("s.mount.parentNode.removeChild(s.mount)") < body.index(
        "_activateTermPane(survivorSid)"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_resumed_linked_pane_rebuilds_the_split(tmp_path: Path) -> None:
    helper = _extract("_restoreWebSplitAfterTerminalOpen")
    source = f"""
'use strict';
const assert = require('node:assert/strict');
const claude = 'live-claude';
const codex = 'resumed-codex';
const termSessions = new Map([[claude, {{}}], [codex, {{}}]]);
let activeTermSid = codex;
const siblings = new Map([[claude, codex], [codex, claude]]);
const activated = [];
function _linkedSiblingSid(sid) {{ return siblings.get(sid) || null; }}
function _activateTermPane(sid) {{ activated.push(sid); }}

{helper}

assert.equal(_restoreWebSplitAfterTerminalOpen(codex, false), codex);
assert.deepEqual(activated, [codex], 'foreground resume must rebuild its linked split');

activated.length = 0;
activeTermSid = claude;
assert.equal(_restoreWebSplitAfterTerminalOpen(codex, true), claude);
assert.deepEqual(activated, [claude], 'normal background attach keeps foreground focus');

activated.length = 0;
activeTermSid = 'unrelated';
assert.equal(_restoreWebSplitAfterTerminalOpen(codex, true), null);
assert.deepEqual(activated, [], 'a background runtime must not steal unrelated focus');
console.log('ok');
"""
    script = tmp_path / "split-resume.cjs"
    script.write_text(source, encoding="utf-8")

    result = subprocess.run(
        [shutil.which("node") or "node", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip() == "ok"
