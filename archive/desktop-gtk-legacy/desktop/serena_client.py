#!/usr/bin/env python3
"""Serena laptop/desktop CLIENT — a native window onto the PC daemon.

The PC is the single source of truth. This loads the bundled chat client the
daemon serves at /app over Tailscale (valid HTTPS cert → no warning, no URL
bar). The client auto-connects: the daemon injects the ws URL + token, so
there's nothing to configure. Chatting uses the same /ws/chat path the phone
uses (staging-resume + persona/memory/knowledge grounding + slug merge), so it
works where the full web UI's in-container terminal can't resume.

Override the target with SERENA_URL.
"""

import os

URL = os.environ.get("SERENA_URL", "https://pc.tail4d6220.ts.net:8444/")


def main() -> None:
    import webview

    webview.create_window("Serena", URL, width=1100, height=820, min_size=(700, 560))
    webview.start()


if __name__ == "__main__":
    main()
