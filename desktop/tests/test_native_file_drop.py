"""Regression coverage for file drops onto the native GTK terminal."""

from types import MethodType, SimpleNamespace

import pytest


app_gtk = pytest.importorskip("desktop.app_gtk")
ChatsApp = app_gtk.ChatsApp


class _Widget:
    def __init__(self):
        self.fed = []

    def feed_child(self, value):
        self.fed.append(value)


class _DropData:
    def get_uris(self):
        return ["file:///tmp/screenshot.png", "file:///tmp/photo%20one.jpg"]

    def get_text(self):
        return None


class _Context:
    def __init__(self):
        self.finished = None

    def finish(self, *args):
        self.finished = args


def test_image_paths_are_fed_into_the_native_terminal():
    widget = _Widget()
    app = SimpleNamespace(_input_drafts={}, _vtes={"session-sid": widget})
    app._runtime_last_activity = {}
    app._sid_for_vte = MethodType(ChatsApp._sid_for_vte, app)
    app._draft_append = MethodType(ChatsApp._draft_append, app)
    context = _Context()

    ChatsApp._on_drag_data_received(
        app, widget, context, 0, 0, _DropData(), 0, 123
    )

    assert widget.fed == [
        b"'/tmp/screenshot.png' ",
        b"'/tmp/photo one.jpg' ",
    ]
    assert app._input_drafts["session-sid"] == (
        "/tmp/screenshot.png /tmp/photo one.jpg "
    )
    assert context.finished == (True, False, 123)
