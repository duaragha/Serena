"""System info tool — CPU, memory, disk, battery, and top processes via psutil."""

from __future__ import annotations

import logging
import time
from typing import Any

import psutil

logger = logging.getLogger(__name__)


def _format_uptime(boot_time: float) -> str:
    """Convert boot timestamp to a human-readable uptime string."""
    elapsed = int(time.time() - boot_time)
    days, remainder = divmod(elapsed, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


class SystemTool:
    """Provides system resource information."""

    def get_status(self) -> dict[str, Any]:
        """Return a snapshot of system resource usage.

        Keys: cpu_percent, memory_percent, disk_percent, battery_percent
        (None if no battery), uptime.
        """
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        battery_percent: float | None = None
        battery = psutil.sensors_battery()
        if battery is not None:
            battery_percent = battery.percent

        uptime = _format_uptime(psutil.boot_time())

        return {
            "cpu_percent": cpu,
            "memory_percent": mem.percent,
            "disk_percent": disk.percent,
            "battery_percent": battery_percent,
            "uptime": uptime,
        }

    def get_summary(self) -> str:
        """Return a voice-friendly summary of system status."""
        status = self.get_status()

        parts = [
            f"CPU is at {status['cpu_percent']:.0f}%,",
            f"memory {status['memory_percent']:.0f}% used,",
            f"disk is at {status['disk_percent']:.0f}%.",
        ]

        if status["battery_percent"] is not None:
            parts.append(f"Battery is at {status['battery_percent']:.0f}%.")

        parts.append(f"System uptime is {status['uptime']}.")

        return " ".join(parts)

    def get_processes(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return the top processes sorted by CPU usage.

        Each entry: pid, name, cpu_percent, memory_percent.
        """
        # First call to cpu_percent returns 0.0 for all procs; a small
        # interval is needed. We collect attrs in one pass, then sort.
        procs: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                info = proc.info  # type: ignore[attr-defined]
                procs.append({
                    "pid": info["pid"],
                    "name": info["name"] or "unknown",
                    "cpu_percent": info["cpu_percent"] or 0.0,
                    "memory_percent": round(info["memory_percent"] or 0.0, 1),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs.sort(key=lambda p: p["cpu_percent"], reverse=True)
        return procs[:limit]
