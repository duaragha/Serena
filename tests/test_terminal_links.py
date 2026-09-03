import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_terminal_link_module_routes_http_links_without_window_open():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    module = Path(__file__).resolve().parents[1] / "ui" / "static" / "terminal_links.js"
    script = f"""
const assert = require('node:assert/strict');
const links = require({json.dumps(str(module))});
assert.equal(links.normalizeExternalUri(' https://example.com/docs '), 'https://example.com/docs');
assert.equal(links.normalizeExternalUri('http://example.com'), 'http://example.com');
for (const bad of ['javascript:alert(1)', 'file:///tmp/x', 'https://', 'https://bad host']) {{
  assert.equal(links.normalizeExternalUri(bad), null);
}}
const sent = [];
let opened = 0;
assert.equal(links.openExternalUri('https://example.com/a', {{
  gtkSend: payload => sent.push(payload),
  openWindow: () => {{ opened++; }},
}}), true);
assert.deepEqual(sent, [{{type: 'open-external-uri', uri: 'https://example.com/a'}}]);
assert.equal(opened, 0);
assert.equal(links.openExternalUri('javascript:alert(1)', {{
  gtkSend: payload => sent.push(payload),
}}), false);
assert.equal(sent.length, 1);
"""
    completed = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_renderer_installs_explicit_handlers_for_plain_and_osc8_links():
    from ui import web

    assert '<script src="/static/terminal_links.js"></script>' in web.HTML
    handler = "window.SerenaTerminalLinks.openExternalUri(uri)"
    assert web.HTML.count(handler) == 2
    assert "linkHandler: {" in web.HTML
    assert "new WebLinksAddon.WebLinksAddon(" in web.HTML


def test_gtk_bridge_opens_only_safe_web_uris(monkeypatch):
    app_gtk = pytest.importorskip("desktop.app_gtk")

    opened = []
    monkeypatch.setattr(
        app_gtk.Gtk,
        "show_uri_on_window",
        lambda parent, uri, timestamp: opened.append((parent, uri, timestamp)) or True,
    )
    harness = object()

    assert (
        app_gtk.ChatsApp._open_external_uri(harness, "https://example.com/docs")
        is True
    )
    assert opened == [
        (harness, "https://example.com/docs", app_gtk.Gdk.CURRENT_TIME)
    ]
    assert (
        app_gtk.ChatsApp._open_external_uri(harness, "javascript:alert(1)")
        is False
    )
    assert len(opened) == 1


def test_gtk_message_dispatches_external_link_to_native_opener():
    app_gtk = pytest.importorskip("desktop.app_gtk")
    opened = []

    class _Value:
        def to_string(self):
            return json.dumps(
                {"type": "open-external-uri", "uri": "https://example.com/docs"}
            )

    class _Result:
        def get_js_value(self):
            return _Value()

    class _Harness:
        def _open_external_uri(self, uri):
            opened.append(uri)

    app_gtk.ChatsApp._on_js_message(_Harness(), None, _Result())
    assert opened == ["https://example.com/docs"]
