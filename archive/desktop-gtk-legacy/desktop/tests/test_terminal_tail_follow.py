"""Regression coverage for opening native VTE chats at the newest output."""

import pytest

app_gtk = pytest.importorskip("desktop.app_gtk")
ChatsApp = app_gtk.ChatsApp


class _Adjustment:
    def __init__(self, upper=120.0, page=20.0, value=0.0):
        self.upper = upper
        self.page = page
        self.value = value

    def get_lower(self):
        return 0.0

    def get_upper(self):
        return self.upper

    def get_page_size(self):
        return self.page

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value


class _Vte:
    def __init__(self):
        self.adjustment = _Adjustment()
        self.scroll_on_output = False

    def get_vadjustment(self):
        return self.adjustment

    def set_scroll_on_output(self, enabled):
        self.scroll_on_output = bool(enabled)


class _Harness:
    _queue_vte_tail_scroll = ChatsApp._queue_vte_tail_scroll
    _stop_vte_tail_follow = ChatsApp._stop_vte_tail_follow
    _arm_vte_tail_follow = ChatsApp._arm_vte_tail_follow
    _on_vte_scroll = ChatsApp._on_vte_scroll

    def __init__(self):
        self.sid = "session-1"
        self.vte = _Vte()
        self._vtes = {self.sid: self.vte}
        self._vte_tail_follow_until = {}
        self._vte_tail_scroll_pending = set()

    def _sid_for_vte(self, vte):
        return self.sid if vte is self.vte else None


class _Event:
    direction = app_gtk.Gdk.ScrollDirection.UP


def test_opening_chat_follows_resume_redraw_to_bottom(monkeypatch):
    harness = _Harness()
    idle_callbacks = []
    timeout_callbacks = []
    monkeypatch.setattr(app_gtk.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(
        app_gtk.GLib, "idle_add", lambda callback: idle_callbacks.append(callback) or 1
    )
    monkeypatch.setattr(
        app_gtk.GLib,
        "timeout_add",
        lambda _delay, callback: timeout_callbacks.append(callback) or 2,
    )

    harness._arm_vte_tail_follow(harness.sid, harness.vte)
    assert harness.vte.scroll_on_output is True
    idle_callbacks.pop()()
    assert harness.vte.adjustment.value == 100.0

    timeout_callbacks.pop()()
    assert harness.vte.scroll_on_output is False
    assert harness.sid not in harness._vte_tail_follow_until


def test_scrolling_up_cancels_startup_tail_follow(monkeypatch):
    harness = _Harness()
    monkeypatch.setattr(app_gtk.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(app_gtk.GLib, "idle_add", lambda _callback: 1)
    monkeypatch.setattr(app_gtk.GLib, "timeout_add", lambda *_args: 2)
    harness._arm_vte_tail_follow(harness.sid, harness.vte)

    assert harness._on_vte_scroll(harness.vte, _Event()) is True
    assert harness.vte.scroll_on_output is False
    assert harness.sid not in harness._vte_tail_follow_until
    assert harness.vte.adjustment.value == 0.0
