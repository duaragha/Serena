"""Push the canonical active-state snapshot to the home brain over Tailnet SSH."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys

from core.brain_state import build_snapshot, encode_snapshot


DEFAULT_TARGET = "docker-pc"


def _encoded_powershell() -> str:
    script = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$dest = Join-Path $env:USERPROFILE '.config\serena\canonical_state.json'
$dir = [IO.Path]::GetDirectoryName($dest)
[IO.Directory]::CreateDirectory($dir) | Out-Null
$temp = "$dest.$PID.tmp"
try {
    $data = [Console]::In.ReadToEnd().Trim()
    if ([String]::IsNullOrWhiteSpace($data)) {
        throw 'canonical state payload was empty'
    }
    $bytes = [Convert]::FromBase64String($data)
    [IO.File]::WriteAllBytes($temp, $bytes)
    $parsed = Get-Content -LiteralPath $temp -Raw | ConvertFrom-Json
    if ($parsed.version -ne 1) {
        throw "unsupported canonical state version $($parsed.version)"
    }
    Move-Item -LiteralPath $temp -Destination $dest -Force
    $hash = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash.ToLowerInvariant()
    [pscustomobject]@{sha256=$hash; path=$dest; bytes=(Get-Item $dest).Length} |
        ConvertTo-Json -Compress
}
finally {
    if ([IO.File]::Exists($temp)) {
        [IO.File]::Delete($temp)
    }
}
exit 0
""".strip()
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def push_snapshot(target: str, payload: bytes, timeout: float = 30.0) -> dict:
    executable = shutil.which("ssh")
    if not executable:
        raise RuntimeError("ssh is not installed")
    command = [
        executable,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        target,
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        _encoded_powershell(),
    ]
    wire_payload = base64.b64encode(payload)
    result = subprocess.run(
        command,
        input=wire_payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        output = result.stdout.decode("utf-8-sig", errors="replace").strip()
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"state sync to {target} failed with exit {result.returncode}: "
            f"{detail or output or 'no diagnostic output'}"
        )
    raw = result.stdout.decode("utf-8-sig", errors="replace").strip()
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"state sync returned invalid JSON: {raw[:300]}") from exc
    local_hash = hashlib.sha256(payload).hexdigest()
    remote_hash = str(response.get("sha256") or "").lower()
    if not remote_hash or remote_hash != local_hash:
        raise RuntimeError(
            f"state sync hash mismatch: local {local_hash}, remote {remote_hash or 'missing'}"
        )
    response["target"] = target
    response["state_hash"] = build_snapshot_hash(payload)
    return response


def build_snapshot_hash(payload: bytes) -> str:
    try:
        snapshot = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return str(snapshot.get("state_hash") or "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        default=os.environ.get("SERENA_BRAIN_SYNC_TARGET", DEFAULT_TARGET),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    snapshot = build_snapshot()
    payload = encode_snapshot(snapshot)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "bytes": len(payload),
                    "records": len(snapshot["records"]),
                    "state_hash": snapshot["state_hash"],
                    "target": args.target,
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        response = push_snapshot(args.target, payload, timeout=args.timeout)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"[brain-state-sync] {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **response}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
