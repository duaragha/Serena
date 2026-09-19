#!/usr/bin/env python3
"""Native messaging host: browser tabs into the ambient store.

The MV3 extension connects to `com.serena.ambient`; the browser launches this
script with the tab on stdin and reads acknowledgements on stdout. Every
message is JSON, UTF-8, prefixed with a 32-bit native-endian length.

Trust: the browser enforces `allowed_origins` before we ever spawn, and we
additionally require our own extension id on every message, so a stray local
sender speaking our framing to a reused pipe is still refused. Incognito tabs
and denylisted URLs are dropped before the store sees them.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HOST_NAME = "com.serena.ambient"
TAB_DEBOUNCE_SECONDS = 2.0


@dataclass
class HostContext:
    store: object
    extension_id: str
    _last_tab: tuple[str, float] = field(default=("", 0.0))


def read_message(stream) -> dict | None:
    """One length-prefixed JSON message, or None on EOF/truncation."""

    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("@I", header)
    if length > 1024 * 1024:
        return None
    body = stream.read(length)
    if len(body) < length:
        return None
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def write_message(stream, payload: dict) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("@I", len(body)))
    stream.write(body)
    stream.flush()


def render_host_manifest(template: str, *, extension_id: str, host_path: str) -> str:
    return (
        template.replace("EXTENSION_ID", extension_id)
        .replace("HOST_PATH", host_path)
    )


def handle_message(message: object, ctx: HostContext, *, now: float) -> bool:
    """Validate, filter, and record one tab report. False means dropped."""

    if not isinstance(message, dict):
        return False
    if message.get("extensionId") != ctx.extension_id:
        return False
    if message.get("type") != "tab":
        return False
    if message.get("incognito") is True:
        return False
    url = " ".join(str(message.get("url") or "").split())
    title = " ".join(str(message.get("title") or "").split())
    if not url or url in {"about:blank"}:
        return False
    last_url, last_at = ctx._last_tab
    if url == last_url and now - last_at < TAB_DEBOUNCE_SECONDS:
        return False

    from core.ambient_denylist import AmbientDenylist

    if AmbientDenylist.default().denies(app="microsoft-edge", title=title, url=url):
        return False
    recorded = ctx.store.record(
        "tab", app="microsoft-edge", title=title, url=url, now=now
    )
    if recorded is None:
        return False
    ctx._last_tab = (url, now)
    return True


def main(argv: list[str]) -> int:
    extension_id = (
        argv[1]
        if len(argv) > 1
        else os.environ.get("SERENA_AMBIENT_EXTENSION_ID", "")
    ).strip()
    if not extension_id:
        print("usage: serena_ambient_host.py <extension-id>", file=sys.stderr)
        return 2

    from core.ambient_store import AmbientStore

    ctx = HostContext(store=AmbientStore(), extension_id=extension_id)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            message = read_message(stdin)
        except Exception:
            return 0
        if message is None:
            return 0
        try:
            accepted = handle_message(message, ctx, now=time.time())
        except Exception:
            accepted = False
        try:
            write_message(stdout, {"accepted": bool(accepted)})
        except Exception:
            return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
