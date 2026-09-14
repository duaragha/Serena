"""Alt+W closes the chat you are in, and the row leaves Active.

Two regressions, both from the structured panes taking over.

A pane reports its own state, and _sessionHasActiveRuntime trusts that reading
in preference to _activeTerms. Nothing cleared the reading when the pane was
closed, so the last "ok" stayed on the row: the runtime was gone, the pane was
gone, and the chat sat in Active until the sidebar was reloaded from the
server.

And the parent only acted on a pane's close request while the parent's own
activeElement was the frame. Clicking into the composer left focus inside the
iframe, the request arrived, and the guard dropped it -- so the shortcut worked
only when focus was outside the chat, which is the opposite of what it is for.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from ui.web import HTML


def _function(name: str) -> str:
    start = HTML.index(f"function {name}(")
    depth = 0
    for index in range(HTML.index("{", start), len(HTML)):
        if HTML[index] == "{":
            depth += 1
        elif HTML[index] == "}":
            depth -= 1
            if depth == 0:
                return HTML[start : index + 1]
    raise AssertionError(f"{name} is not brace-balanced")


def _node(script: str) -> None:
    if not shutil.which("node"):
        pytest.skip("node is required")
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr or result.stdout


def test_a_closed_pane_stops_claiming_the_row_is_active() -> None:
    """The reading outranks _activeTerms, so clearing it is what frees the row."""
    _node("""
const assert=require('node:assert/strict');
const _activeTerms=new Set();
""" + _function("_sessionHasActiveRuntime") + """
// A live pane: its own report decides.
assert.equal(_sessionHasActiveRuntime({session_id:'s',workspace_runtime:{ok:true}}),true);
assert.equal(_sessionHasActiveRuntime({session_id:'s',workspace_runtime:{ok:false}}),false);
// Closed: the reading is gone, so the terminal set decides again.
assert.equal(_sessionHasActiveRuntime({session_id:'s',workspace_runtime:null}),false);
_activeTerms.add('s');
assert.equal(_sessionHasActiveRuntime({session_id:'s',workspace_runtime:null}),true,
  'a cleared reading must fall through to _activeTerms, not stay false');
// A plain terminal chat is unaffected.
assert.equal(_sessionHasActiveRuntime({session_id:'other'}),false);
""")


def test_a_stale_ok_cannot_outlive_the_pane() -> None:
    """The exact regression: unmarking the terminal alone left the row Active."""
    _node("""
const assert=require('node:assert/strict');
const _activeTerms=new Set(['s']);
""" + _function("_sessionHasActiveRuntime") + """
const row={session_id:'s',workspace_runtime:{ok:true}};
_activeTerms.delete('s');                       // what teardown used to do alone
assert.equal(_sessionHasActiveRuntime(row),true,'reproduces the stuck row');
row.workspace_runtime=null;                     // what teardown does now
assert.equal(_sessionHasActiveRuntime(row),false,'the row leaves Active');
""")


def test_teardown_clears_the_reading_before_unmarking_the_terminal() -> None:
    body = _function("teardownLiveTerminal")

    assert "workspace_runtime: null" in body, "a closed pane still reports itself active"
    assert body.index("workspace_runtime: null") < body.index("_unmarkActive(sid)"), (
        "unmarking first cannot free the row, because the pane's reading outranks it"
    )


def test_the_close_request_no_longer_depends_on_where_focus_sits() -> None:
    start = HTML.index("if(event.data.type==='serena-workspace-close-request'){")
    body = HTML[start : start + 700]

    assert "document.activeElement!==frame" not in body, (
        "Alt+W is gated on focus again, so it only works from outside the chat"
    )
    # The modal guard is the one that must stay: a dialog owns the keyboard.
    assert "modalBackdrop" in body
    assert "window.__gtkShortcut('close-terminal',sid)" in body


def test_only_this_pane_can_ask_this_pane_to_close() -> None:
    """Dropping the focus check is safe because the sender is already proven;
    without that, any frame could close any chat."""
    start = HTML.index("const receive = async event => {")
    body = HTML[start : start + 400]

    assert "event.source !== frame.contentWindow" in body
    assert "event.origin !== location.origin" in body
    assert "event.data?.sid !== sid" in body
