"""Persistent same-origin host for Serena's mobile chat and call surface."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)

    from ui.web import run_web

    run_web(host=args.host, port=args.port, open_browser=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
