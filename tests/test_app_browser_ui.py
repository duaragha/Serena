"""The in-app browser's page side: its tab, its slot, and links routed to it."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ui import web

STATIC = Path(__file__).resolve().parents[1] / "ui" / "static"


def _node(script: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    completed = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def test_terminal_links_open_in_the_app_browser_when_there_is_one():
    _node(f"""
const assert = require('node:assert/strict');
const links = require({json.dumps(str(STATIC / "terminal_links.js"))});
const opened = [];
let windows = 0;
assert.equal(links.openExternalUri('http://localhost:5173/', {{
  inAppBrowser: {{ open: (uri) => opened.push(uri) }},
  openWindow: () => {{ windows++; }},
}}), true);
assert.deepEqual(opened, ['http://localhost:5173/']);
assert.equal(windows, 0);
// Without one (a plain browser tab), links still open in a new window.
assert.equal(links.openExternalUri('https://example.com/', {{
  inAppBrowser: null, openWindow: () => {{ windows++; return {{}}; }},
}}), true);
assert.equal(windows, 1);
""")


def test_slot_reports_hide_the_native_view_whenever_the_pane_is_not_on_screen():
    _node(f"""
const assert = require('node:assert/strict');
const pane = require({json.dumps(str(STATIC / "app_browser.js"))});
const rect = {{ left: 300.4, top: 80.6, width: 900, height: 700 }};
assert.deepEqual(pane.slotReport(rect, true, true), {{ visible: true, x: 300, y: 81, width: 900, height: 700 }});
assert.deepEqual(pane.slotReport(rect, false, true), {{ visible: false }});   // another tab
assert.deepEqual(pane.slotReport(rect, true, false), {{ visible: false }});   // window hidden
assert.deepEqual(pane.slotReport({{ left: 0, top: 0, width: 0, height: 0 }}, true, true), {{ visible: false }});
assert.equal(pane.sameReport({{ visible: false }}, {{ visible: false }}), true);
assert.equal(pane.sameReport(null, {{ visible: false }}), false);
assert.equal(pane.tabLabel({{ title: '', url: 'http://localhost:5173/app' }}), 'localhost:5173');
assert.equal(pane.tabLabel({{ title: 'Vite + React', url: 'x' }}), 'Vite + React');
""")


def test_the_page_loads_the_pane_and_the_code_tabs_know_about_it():
    html = web.app.test_client().get("/").get_data(as_text=True)
    assert '<script src="/static/app_browser.js"></script>' in html
    assert "SerenaAppBrowser.tabHtml(_codeTab === '__browser__')" in html
    assert "SerenaAppBrowser.setActive(id === '__browser__')" in html
    assert web.app.test_client().get("/static/app_browser.js").status_code == 200
