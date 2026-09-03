"""The scrollbar has to be usable as a control, not a decoration.

A scroll wheel is not a given. On the Windows PC the wheel is dead and there is
no trackpad to two-finger with, so dragging the bar is the only way to move
through a chat. It was a 6px sliver on a transparent track, and xterm.js only
showed its own when scrollback existed, so the control was both hard to hit and
intermittently absent.

Both platforms are covered by the same rules: the ::-webkit- properties drive
Chromium in the Windows build and WebKitGTK on Linux, and scrollbar-width and
scrollbar-color cover Gecko.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "ui" / "web.py"

# Below this a thumb is fiddly to hit with a mouse; the platform default is
# ~15px and the old value here was 6.
MIN_GRABBABLE_PX = 12


def _page() -> str:
    return SOURCE.read_text(encoding="utf-8")


def _rule(page: str, selector: str) -> str:
    """The declaration block for a selector, so assertions are scoped to it."""
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", page)
    assert match, f"no rule found for {selector}"
    return match.group(1)


def test_the_bar_is_wide_enough_to_grab() -> None:
    body = _rule(_page(), "::-webkit-scrollbar")

    width = re.search(r"width:\s*(\d+)px", body)
    assert width, "the scrollbar has no explicit width, so it may be an overlay"
    assert int(width.group(1)) >= MIN_GRABBABLE_PX, (
        f"a {width.group(1)}px scrollbar is a hint, not a control"
    )


def test_the_track_is_visible() -> None:
    """A transparent track gives no target and no sense of position."""
    body = _rule(_page(), "::-webkit-scrollbar-track")

    assert "transparent" not in body, "an invisible track cannot be aimed at"
    assert re.search(r"background:\s*#[0-9a-fA-F]{3,6}", body)


def test_the_thumb_never_shrinks_to_nothing() -> None:
    """In a long transcript the thumb would otherwise be a few pixels tall."""
    body = _rule(_page(), "::-webkit-scrollbar-thumb")

    height = re.search(r"min-height:\s*(\d+)px", body)
    assert height, "no minimum thumb height"
    assert int(height.group(1)) >= 24


def test_the_terminal_scrollbar_is_always_present() -> None:
    """xterm.js shows its own only when scrollback exists, so it comes and goes."""
    body = _rule(_page(), ".term-pane .xterm-viewport")

    assert "overflow-y: scroll !important" in body, (
        "the terminal scrollbar still appears and disappears"
    )


def test_gecko_gets_the_same_treatment() -> None:
    """WebKitGTK and Chromium read the ::-webkit- rules; Gecko needs its own."""
    page = _page()

    assert "scrollbar-width: auto" in page
    assert re.search(r"scrollbar-color:\s*#[0-9a-fA-F]{3,6}\s+#[0-9a-fA-F]{3,6}", page), (
        "no scrollbar-color, so a Gecko build would fall back to a thin default"
    )


def test_the_horizontal_tab_strip_keeps_its_hidden_bar() -> None:
    """It scrolls sideways by drag and would look broken with a bar."""
    assert ".code-tabs::-webkit-scrollbar { height: 0; }" in _page()
