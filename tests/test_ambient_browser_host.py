from __future__ import annotations

import io
import json
import struct
import sys
from pathlib import Path

EXTENSION_DIR = Path(__file__).resolve().parent.parent / "integrations" / "browser-extension"
sys.path.insert(0, str(EXTENSION_DIR))

import serena_ambient_host as host
from core.ambient_store import AmbientStore


def make_context(tmp_path, monkeypatch):
    db = tmp_path / "ambient.sqlite3"
    monkeypatch.setenv("SERENA_AMBIENT_DB_PATH", str(db))
    return host.HostContext(store=AmbientStore(db), extension_id="test-extension-id")


def test_tab_message_records_url_and_title(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, monkeypatch)

    handled = host.handle_message(
        {"extensionId": "test-extension-id", "type": "tab",
         "url": "https://example.com/docs", "title": "Docs", "active": True},
        ctx, now=1_000.0,
    )

    assert handled is True
    events = ctx.store.recent(now=1_100.0)
    assert len(events) == 1
    assert events[0].kind == "tab"
    assert events[0].url == "https://example.com/docs"
    assert events[0].title == "Docs"


def test_incognito_window_records_nothing(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, monkeypatch)

    handled = host.handle_message(
        {"extensionId": "test-extension-id", "type": "tab",
         "url": "https://example.com/", "title": "x", "incognito": True},
        ctx, now=1_000.0,
    )

    assert handled is False
    assert ctx.store.recent(now=1_100.0) == []
    assert ctx.store.dump_for_test() == ""


def test_host_refuses_any_origin_but_the_extension(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, monkeypatch)

    handled = host.handle_message(
        {"extensionId": "some-other-extension", "type": "tab",
         "url": "https://example.com/", "title": "x"},
        ctx, now=1_000.0,
    )

    assert handled is False
    assert ctx.store.recent(now=1_100.0) == []


def test_denylisted_url_from_tab_records_nothing(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, monkeypatch)

    handled = host.handle_message(
        {"extensionId": "test-extension-id", "type": "tab",
         "url": "https://chase.com/login", "title": "Sign in"},
        ctx, now=1_000.0,
    )

    assert handled is False
    assert ctx.store.recent(now=1_100.0) == []
    assert ctx.store.dump_for_test() == ""


def test_rapid_identical_tab_updates_are_debounced(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, monkeypatch)
    message = {"extensionId": "test-extension-id", "type": "tab",
               "url": "https://example.com/", "title": "x"}

    assert host.handle_message(message, ctx, now=1_000.0) is True
    assert host.handle_message(message, ctx, now=1_000.5) is False
    assert host.handle_message(message, ctx, now=1_005.0) is True


def test_malformed_messages_are_ignored_not_fatal(tmp_path, monkeypatch):
    ctx = make_context(tmp_path, monkeypatch)

    assert host.handle_message({"type": "tab"}, ctx, now=1_000.0) is False
    assert host.handle_message("not a dict", ctx, now=1_000.0) is False
    assert host.handle_message({"extensionId": "test-extension-id"}, ctx, now=1_000.0) is False
    assert ctx.store.recent(now=1_100.0) == []


def test_native_framing_round_trips_length_prefixed_json():
    payload = {"type": "tab", "url": "https://example.com/"}
    stream = io.BytesIO()
    host.write_message(stream, payload)

    stream.seek(0)
    assert host.read_message(stream) == payload


def test_read_message_returns_none_on_eof():
    assert host.read_message(io.BytesIO(b"")) is None
    assert host.read_message(io.BytesIO(b"\x05\x00")) is None


def test_host_manifest_template_pins_exactly_one_origin(tmp_path):
    template = (EXTENSION_DIR / "native-host-manifest.json").read_text()
    rendered = host.render_host_manifest(template, extension_id="abc123", host_path="/x/host.py")

    manifest = json.loads(rendered)
    assert manifest["allowed_origins"] == ["chrome-extension://abc123/"]
    assert manifest["path"] == "/x/host.py"
    assert "EXTENSION_ID" not in rendered


def test_extension_manifest_is_mv3_with_native_messaging():
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())

    assert manifest["manifest_version"] == 3
    assert "nativeMessaging" in manifest["permissions"]
    assert "service_worker" in manifest["background"]


def test_extension_reports_foreground_tab_updates_only():
    """background.js has no JS harness; pin the guard textually instead."""

    source = (EXTENSION_DIR / "background.js").read_text()
    activated = source.index("chrome.tabs.onActivated")
    updated = source.index("chrome.tabs.onUpdated")
    window_focus = source.index("chrome.windows.onFocusChanged")

    # Both tab paths gate on the focused window (tab.active alone is
    # per-window and would still report minimized windows' active tabs).
    assert "isForeground(tab)" in source[activated:updated]
    assert "isForeground(tab)" in source[updated:window_focus]
    assert "getLastFocused" in source
    assert "tab.active" in source
