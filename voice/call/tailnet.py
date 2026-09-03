"""Classify the live Tailscale route to a call peer without blocking audio."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


@dataclass(frozen=True, slots=True)
class TailnetPathMeasurement:
    path: str
    source: str
    evidence: str
    measured_ns: int

    @classmethod
    def unknown(cls, evidence: str = "not measured") -> TailnetPathMeasurement:
        return cls("unknown", "unknown", evidence, time.monotonic_ns())


def normalize_tailnet_peer(raw_peer: str | None) -> str | None:
    """Return a canonical Tailscale IP, rejecting every other peer address."""
    value = (raw_peer or "").strip()
    if not value:
        return None
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    value = value.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address in _TAILSCALE_V4 or address in _TAILSCALE_V6:
        return str(address)
    return None


def classify_tailscale_ping(output: str) -> tuple[str, str]:
    """Classify the last concrete tailscale ping route and retain its evidence."""
    pongs = [line.strip() for line in output.splitlines() if "pong from " in line]
    if not pongs:
        return "unknown", output.strip()[-500:]
    evidence = pongs[-1]
    if " via derp(" in evidence.lower():
        return "relay", evidence
    if re.search(r"\svia\s+\S+\s+in\s+", evidence, flags=re.IGNORECASE):
        return "direct", evidence
    return "unknown", evidence


def find_tailscale_cli() -> str | None:
    configured = os.environ.get("SERENA_TAILSCALE_CLI", "").strip()
    candidates = [configured, shutil.which("tailscale") or ""]
    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(variable, "").strip()
            if root:
                candidates.append(str(Path(root) / "Tailscale" / "tailscale.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


async def probe_tailscale_path(
    raw_peer: str | None,
    *,
    timeout_seconds: float = 2.0,
) -> TailnetPathMeasurement:
    """Read the current server-to-phone route from local Tailscale state."""
    peer = normalize_tailnet_peer(raw_peer)
    if peer is None:
        return TailnetPathMeasurement.unknown("websocket peer is not a Tailscale IP")
    executable = find_tailscale_cli()
    if executable is None:
        return TailnetPathMeasurement.unknown("tailscale CLI is unavailable")

    timeout_seconds = max(0.5, min(float(timeout_seconds), 10.0))
    process = await asyncio.create_subprocess_exec(
        executable,
        "status",
        "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    communication = asyncio.create_task(process.communicate())
    try:
        output, _ = await asyncio.wait_for(
            asyncio.shield(communication), timeout=timeout_seconds
        )
    except asyncio.CancelledError:
        await _terminate_and_reap(process, communication)
        raise
    except asyncio.TimeoutError:
        await _terminate_and_reap(process, communication)
        return TailnetPathMeasurement.unknown("tailscale status timed out")

    try:
        status = json.loads(output.decode("utf-8-sig", errors="replace"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        return TailnetPathMeasurement.unknown(f"tailscale status was invalid: {exc}")
    peers = status.get("Peer", {})
    if not isinstance(peers, dict):
        return TailnetPathMeasurement.unknown("tailscale status has no peer map")
    for value in peers.values():
        if not isinstance(value, dict):
            continue
        addresses = value.get("TailscaleIPs", [])
        if not isinstance(addresses, list) or peer not in {
            normalize_tailnet_peer(str(address)) for address in addresses
        }:
            continue
        active = value.get("Active") is True
        current_address = str(value.get("CurAddr") or "").strip()
        relay = str(value.get("Relay") or "").strip()
        peer_relay = str(value.get("PeerRelay") or "").strip()
        if active and current_address:
            path = "direct"
            evidence = f"active peer via {current_address}"
        elif active and peer_relay:
            path = "relay"
            evidence = f"active peer via peer relay {peer_relay}"
        elif active and relay:
            path = "relay"
            evidence = f"active peer via DERP({relay})"
        else:
            path = "unknown"
            evidence = "peer route is not active"
        return TailnetPathMeasurement(
            path=path,
            source="tailscale_probe" if path != "unknown" else "unknown",
            evidence=evidence,
            measured_ns=time.monotonic_ns(),
        )
    return TailnetPathMeasurement(
        path="unknown",
        source="unknown",
        evidence="websocket peer is absent from tailscale status",
        measured_ns=time.monotonic_ns(),
    )


async def _terminate_and_reap(
    process: asyncio.subprocess.Process,
    communication: asyncio.Task[tuple[bytes, bytes | None]],
) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=0.5)
    except asyncio.TimeoutError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await process.wait()
    await asyncio.gather(communication, return_exceptions=True)
