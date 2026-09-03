"""The wake listener must be able to reach Serena's own loopback services.

2026-08-20: every cold wake logged wake.hot_greeting_failed with an OSError,
2 accepted wakes and 2 failures, because the unit shipped
RestrictAddressFamilies=AF_UNIX. The hot greeting is fetched over HTTP from the
mobile host and the dot field is driven over a loopback websocket, so both
raised errno 97 the instant Raghav said her name.

The consequence was invisible in the metrics and very visible to him: the
greeting exists precisely to cover the app's cold start with her voice, so
losing it meant roughly five seconds of silence before she answered.
"""

from __future__ import annotations

import re
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "systemd" / "serena-wake-listener.service"


def _directive(name: str) -> str:
    for line in UNIT.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return ""


def test_the_listener_can_open_loopback_tcp_for_its_greeting() -> None:
    families = _directive("RestrictAddressFamilies").split()
    assert "AF_INET" in families, (
        "the hot greeting is an HTTP fetch; without AF_INET it fails with "
        "errno 97 and the app boots in silence"
    )
    assert "AF_UNIX" in families, "PipeWire audio still needs the unix socket"


def test_the_sandbox_is_still_a_sandbox() -> None:
    """Widening one directive must not quietly drop the rest of the hardening."""
    text = UNIT.read_text()
    assert "NoNewPrivileges=true" in text
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=read-only" in text
    assert "RestrictSUIDSGID=true" in text
    # raw packet access was never needed and must not appear
    assert not re.search(r"RestrictAddressFamilies=.*AF_PACKET", text)
