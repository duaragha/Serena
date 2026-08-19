import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_terminal_lifecycle_module_has_a_real_two_frame_restore_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    module = (
        Path(__file__).resolve().parents[1] / "ui" / "static" / "terminal_lifecycle.js"
    )
    script = f"""
const assert = require('node:assert/strict');
const lifecycle = require({json.dumps(str(module))});
assert.equal(lifecycle.isRenderable({{
  tab: 'chats', mode: 'live', hidden: false, rect: {{width: 900, height: 600}}
}}), true);
for (const bad of [
  {{tab: 'fleet', mode: 'live', hidden: false, rect: {{width: 900, height: 600}}}},
  {{tab: 'chats', mode: 'read', hidden: false, rect: {{width: 900, height: 600}}}},
  {{tab: 'chats', mode: 'live', hidden: true, rect: {{width: 900, height: 600}}}},
  {{tab: 'chats', mode: 'live', hidden: false, rect: {{width: 0, height: 0}}}},
]) assert.equal(lifecycle.isRenderable(bad), false);
const queue = [];
let called = 0;
lifecycle.afterReveal(() => called++, {{requestAnimationFrame: cb => queue.push(cb)}});
assert.equal(queue.length, 1);
queue.shift()();
assert.equal(called, 0);
assert.equal(queue.length, 1);
queue.shift()();
assert.equal(called, 1);
assert.equal(lifecycle.TAIL_FOLLOW_MS, 6000);
assert.equal(lifecycle.tailDeadline(100), 6100);
assert.equal(lifecycle.shouldFollowTail(6100, 6099), true);
assert.equal(lifecycle.shouldFollowTail(6100, 6100), true);
assert.equal(lifecycle.shouldFollowTail(6100, 6101), false);
assert.equal(lifecycle.shouldFollowTail(0, 0), false);
"""
    completed = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
