"""Three or more chats split into columns, not a 2x2 square.

The square was chosen on the reasoning that a third of the width is not enough
columns for an agent TUI to lay out its input box. On a real window that does
not hold up: quarters halve the HEIGHT as well, so every cell loses scrollback,
and with three chats -- the common case, one per agent -- the fourth quadrant
sits empty. Width is the cheaper axis to spend.

The geometry is pulled out of the served page and run, so this exercises the
arithmetic the browser actually applies.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import web

HARNESS = r"""
'use strict';
const assert = require('node:assert/strict');

let _gtkSplitActive = true;
let _gtkSplitSids = null;
let _gtkSplitRatio = 0.5;
const termSessions = new Map();

__FUNCTIONS__

function layout(count) {
  _gtkSplitSids = [];
  termSessions.clear();
  for (let i = 0; i < count; i++) {
    const sid = 's' + i;
    _gtkSplitSids.push(sid);
    termSessions.set(sid, { mount: { style: {} } });
  }
  const container = { querySelector: () => null };
  _applyWebSplitGeometry(container);
  return _gtkSplitSids.map(sid => termSessions.get(sid).mount.style);
}

const out = {};
for (const n of [3, 4, 5]) out[n] = layout(n);
out.isColumn = { 2: _isColumnSplit.call(null), };
_gtkSplitSids = ['a', 'b'];
out.twoIsNotColumns = _isColumnSplit();
_gtkSplitSids = ['a', 'b', 'c'];
out.threeIsColumns = _isColumnSplit();
console.log(JSON.stringify(out));
"""

NEEDED = ("_isColumnSplit", "_applyWebSplitGeometry")


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


def _constants() -> str:
    out = []
    for line in web.HTML.splitlines():
        if re.match(r"const _(COLUMN_MIN_PANES|SPLIT_EDGE_PX|SPLIT_GAP_PX) = ", line.strip()):
            out.append(line.strip())
    return "\n".join(out)


@pytest.fixture(scope="module")
def geometry(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node is required to run page code")
    script = tmp_path_factory.mktemp("split") / "layout.cjs"
    body = _constants() + "\n" + "\n".join(_extract(name) for name in NEEDED)
    script.write_text(HARNESS.replace("__FUNCTIONS__", body), encoding="utf-8")
    done = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout)


def test_two_panes_keep_the_draggable_split(geometry) -> None:
    assert geometry["twoIsNotColumns"] is False
    assert geometry["threeIsColumns"] is True


@pytest.mark.parametrize("count", [3, 4, 5])
def test_every_pane_is_full_height(geometry, count: int) -> None:
    """The point of the change: no pane loses half its scrollback."""
    for style in geometry[str(count)]:
        assert style["top"] == "6px"
        assert style["bottom"] == "2px"


@pytest.mark.parametrize("count", [3, 4, 5])
def test_the_columns_are_equal_and_in_order(geometry, count: int) -> None:
    styles = geometry[str(count)]
    assert len(styles) == count

    def percent(value: str) -> float:
        return float(re.search(r"([\d.]+)%", value).group(1))

    lefts = [percent(s["left"]) for s in styles]
    rights = [percent(s["right"]) for s in styles]

    assert lefts == sorted(lefts), "panes are not laid out left to right"
    assert lefts[0] == pytest.approx(0.0)
    assert rights[-1] == pytest.approx(0.0)
    # Each column occupies exactly its share of the width.
    for index in range(count):
        width = 100.0 - lefts[index] - rights[index]
        assert width == pytest.approx(100.0 / count, abs=0.01)


@pytest.mark.parametrize("count", [3, 4, 5])
def test_outer_edges_and_inner_gaps_match_the_two_pane_layout(geometry, count: int) -> None:
    """8px at the window edge, 6px between neighbours -- 3px from each side."""
    styles = geometry[str(count)]

    def pixels(value: str) -> float:
        return float(re.search(r"\+ ([\d.]+)px", value).group(1))

    assert pixels(styles[0]["left"]) == 8
    assert pixels(styles[-1]["right"]) == 8
    for style in styles[1:]:
        assert pixels(style["left"]) == 3
    for style in styles[:-1]:
        assert pixels(style["right"]) == 3


def test_no_pane_is_dropped_when_a_thread_grows(geometry) -> None:
    """The square only ever positioned four cells, so a fifth pane got no
    geometry at all and landed wherever the previous layout left it."""
    assert len(geometry["5"]) == 5
    assert all(style.get("left") for style in geometry["5"])


def test_the_square_layout_is_gone() -> None:
    page = web.HTML
    assert "_isQuadSplit" not in page
    assert "_QUAD_MIN_PANES" not in page
    assert "calc(50% + 3px)" not in page, "a 2x2 cell definition survives"
