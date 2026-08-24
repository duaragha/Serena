"""A program inside a pane must be able to reach the system clipboard.

Claude Code, finding no native clipboard inside Serena's terminal, copies by
emitting OSC 52 and trusting the terminal to complete the write. It even says so
on screen: "sent 386 chars via OSC 52". xterm.js does not handle that sequence
by default, so the bytes were dropped and nothing ever reached the clipboard.

The same sequence can also ASK for the clipboard's contents. Answering that
would let anything running in a pane read whatever is on it, so the reply half
is deliberately not implemented.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import web

FUNCTIONS = ("_decodeOsc52", "_copyPlainText", "_installClipboardBridge")

HARNESS = r"""
'use strict';
const assert = require('node:assert/strict');

const copied = [];
const toasts = [];
const handlers = new Map();

const navigator = { clipboard: { writeText: async (text) => { copied.push(text); } } };
const document = { createElement: () => ({ style: {}, setAttribute() {}, select() {}, remove() {} }), body: { appendChild() {} } };
function showToast(text, opts) { toasts.push({ text, opts }); }
function atob(b64) {
  // Node's Buffer silently ignores invalid base64; a browser throws. The
  // handler's rejection path depends on the browser behaviour, so match it.
  const cleaned = String(b64).replace(/[\t\n\f\r ]/g, '');
  if (cleaned.length % 4 !== 0 || /[^A-Za-z0-9+/=]/.test(cleaned)) {
    throw new Error('InvalidCharacterError');
  }
  return Buffer.from(cleaned, 'base64').toString('binary');
}
const TextDecoder = global.TextDecoder;

const term = { parser: { registerOscHandler: (code, fn) => handlers.set(code, fn) } };

__FUNCTIONS__

_installClipboardBridge(term);
assert.ok(handlers.has(52), 'OSC 52 was never registered');
const handle = handlers.get(52);

const CASES = JSON.parse(process.env.CASES);

// A plain copy reaches the clipboard.
assert.equal(handle(CASES.ascii.sequence), true);
// A clipboard read request is swallowed, never answered.
assert.equal(handle('c;?'), true);
// Non-UTF8-safe payloads are rejected rather than pasting garbage.
assert.equal(handle('c;!!!not base64!!!'), false);
assert.equal(handle('no-semicolon'), false);
// Unicode survives the round trip.
assert.equal(handle(CASES.unicode.sequence), true);

setTimeout(() => {
  assert.deepEqual(copied, [CASES.ascii.text, CASES.unicode.text],
    `clipboard received ${JSON.stringify(copied)}`);
  assert.ok(!copied.includes('?'), 'a read request must never be treated as a copy');
  assert.equal(toasts.length, 2);
  console.log('ok');
}, 10);
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


def _sequence(text: str) -> str:
    return "c;" + base64.b64encode(text.encode("utf-8")).decode("ascii")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run page code")
def test_osc52_copies_reach_the_clipboard_but_reads_are_refused(tmp_path: Path) -> None:
    source = HARNESS.replace("__FUNCTIONS__", "\n\n".join(_extract(name) for name in FUNCTIONS))
    script = tmp_path / "clipboard.cjs"
    script.write_text(source, encoding="utf-8")

    cases = {
        "ascii": {"text": "npm run build", "sequence": _sequence("npm run build")},
        "unicode": {"text": "café — ok", "sequence": _sequence("café — ok")},
    }
    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "CASES": json.dumps(cases)},
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("ok")


def test_the_bridge_is_installed_on_every_terminal() -> None:
    """Registering it anywhere but the constructor path would miss most panes."""
    calls = web.HTML.count("  _installClipboardBridge(term);")
    assert calls == 1, f"expected exactly one call site, found {calls}"
    assert web.HTML.count("function _installClipboardBridge(") == 1
