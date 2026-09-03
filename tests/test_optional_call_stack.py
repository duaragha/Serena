"""A missing audio dependency must not take the whole app down.

The Windows desktop build shipped without numpy, and because ``ui.web``
imported the voice call stack unguarded at module scope, the frozen sidecar
died on startup with an unhandled ModuleNotFoundError. The user saw a Python
traceback dialog and no window, over a calling feature they had not asked for.

Chats, memory, knowledge and terminals need no audio at all, so a failure in
that import now disables calling and leaves everything else running.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEB_SOURCE = (REPO / "ui" / "web.py").read_text(encoding="utf-8")


def test_the_call_stack_import_is_guarded():
    block = re.search(
        r"CALL_STACK_ERROR: str \| None = None\ntry:\n(.*?)\nexcept Exception",
        WEB_SOURCE,
        re.DOTALL,
    )
    assert block is not None, "the voice.call import is no longer guarded"
    guarded = block.group(1)
    for module in ("voice.call", "voice.call.orchestrator", "voice.call.browser_auth"):
        assert module in guarded, f"{module} must be imported inside the guard"


def test_every_call_name_still_exists_when_the_import_fails():
    """The fallbacks must cover the names the routes actually use."""

    fallback = WEB_SOURCE.split("except Exception as _call_import_error", 1)[1]
    for name in (
        "CALL_SOCKET_COOKIE",
        "CALL_SOCKET_COOKIE_PATH",
        "CALL_SOCKET_TICKET_TTL_SECONDS",
        "handle_websocket",
        "get_desk_runtime",
        "warm_desk_runtime_background",
        "warm_default_runtime_background",
        "browser_call_tickets",
    ):
        assert name in fallback, f"{name} has no fallback, so import failure still crashes"


def test_a_call_attempt_fails_loudly_rather_than_silently():
    """Disabling calling must not look like calling that quietly does nothing."""

    fallback = WEB_SOURCE.split("except Exception as _call_import_error", 1)[1]
    assert "raise RuntimeError" in fallback
    assert "voice calling is unavailable" in fallback


def test_the_windows_build_requires_numpy():
    """The dependency the frozen sidecar was missing is now declared."""

    requirements = (REPO / "requirements-windows.txt").read_text(encoding="utf-8")
    assert "numpy" in requirements

    spec_path = REPO / "apps" / "desktop" / "windows" / "sidecar-win.spec"
    if not spec_path.is_file():
        spec_path = REPO / "desktop-electron" / "windows" / "sidecar-win.spec"
    spec = spec_path.read_text(encoding="utf-8")
    # A bare hidden import bundles a hollow numpy that fails on its compiled
    # extensions, which is the failure the Linux build already hit.
    assert 'collect_all("numpy")' in spec
    assert "binaries=numpy_binaries" in spec
