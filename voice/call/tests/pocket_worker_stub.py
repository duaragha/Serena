from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.parse_args()
    send(
        {
            "ok": True,
            "event": "ready",
            "meta": {
                "provider": "CPUExecutionProvider",
                "sample_rate": 24_000,
                "pid": os.getpid(),
                "asset_manifest_sha256": "0" * 64,
                "asset_count": 2,
                "asset_bytes": 2,
                "backchannel_text": "yeah.",
                "backchannel_pcm_b64": base64.b64encode(b"\x01\x00" * 480).decode(
                    "ascii"
                ),
                "backchannel_sample_rate": 24_000,
            },
        }
    )
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("text") == "hang":
            time.sleep(5)
        generation = int(request.get("generation", 0))
        wide = request.get("text") == "wide"
        for value in ((1,) if wide else (1, 2)):
            pcm = bytes((value, 0)) * (3_600 if wide else 480)
            send(
                {
                    "ok": True,
                    "event": "chunk",
                    "generation": generation,
                    "sample_rate": 24_000,
                    "pcm_b64": base64.b64encode(pcm).decode("ascii"),
                }
            )
        send({"ok": True, "event": "done", "generation": generation})


if __name__ == "__main__":
    main()
