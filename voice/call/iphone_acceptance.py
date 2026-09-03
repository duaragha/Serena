"""Stage and verify one physical iPhone call for the v2 call gates."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from core.billing import subscription_auth_evidence
from core.brain_lifetime import write_json_atomic
from voice.call.cost_audit import (
    analyze_cost_objective,
    brain_daemon_evidence,
    brain_health_evidence,
    capture_baseline,
    capture_metrics_cursor,
    load_metrics_append,
)
from voice.call.integrity import IntegrityCriteria, analyze_call
from voice.call.telemetry import DEFAULT_METRICS_PATH

DEFAULT_HANDOFF = Path.home() / ".config" / "serena" / "iphone-call-handoff.json"
DEFAULT_REPORT = Path.home() / ".local" / "state" / "serena" / "iphone-call-acceptance.json"
DEFAULT_APP_URL = "https://raghavslaptop.tail4d6220.ts.net:8445/app"
CHAT_TOKEN = Path.home() / ".config" / "serena" / "chat_token"
REQUIRED_SERVICES = (
    "serena-brain.service",
    "serena-mobile-host.service",
)


def _service_state(name: str) -> str:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", name],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or "unknown"


def _json_url(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError(f"endpoint did not return a JSON object: {url}")
    return value


def _probe_app(url: str) -> int:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read(1024)
        return int(response.status)


def prepare(
    *,
    handoff_path: Path = DEFAULT_HANDOFF,
    metrics_path: Path = DEFAULT_METRICS_PATH,
    app_url: str = DEFAULT_APP_URL,
) -> dict[str, Any]:
    failures: list[str] = []
    services = {name: _service_state(name) for name in REQUIRED_SERVICES}
    failures.extend(name for name, state in services.items() if state != "active")

    token_ok = False
    try:
        metadata = CHAT_TOKEN.stat()
        token_ok = (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600
            and metadata.st_size >= 16
        )
    except OSError:
        pass
    if not token_ok:
        failures.append("private chat token is missing")

    brain: dict[str, Any] = {}
    try:
        brain = _json_url("http://127.0.0.1:8377/health")
        if brain.get("ok") is not True:
            failures.append("resident brain health is not ready")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        failures.append(f"resident brain health failed: {exc}")

    app_status: int | None = None
    try:
        app_status = _probe_app(app_url)
        if app_status != 200:
            failures.append(f"mobile app returned HTTP {app_status}")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        failures.append(f"mobile app is not reachable: {exc}")

    cursor = capture_metrics_cursor(metrics_path)
    if cursor.get("ok") is not True:
        failures.extend(str(item) for item in cursor.get("failures", []))

    auth = subscription_auth_evidence()
    daemon = brain_daemon_evidence()
    health = brain_health_evidence()
    cost_baseline = capture_baseline(
        auth=auth,
        daemon=daemon,
        health=health,
        metrics_cursor=cursor,
    )
    if cost_baseline.get("ok") is not True:
        failures.extend(str(item) for item in cost_baseline.get("failures", []))

    handoff = {
        "schema_version": 2,
        "ok": not failures,
        "prepared_at": time.time(),
        "app_url": app_url,
        "services": services,
        "token_ready": token_ok,
        "brain": {
            "ok": brain.get("ok"),
            "pid": brain.get("pid"),
            "started": brain.get("started"),
            "billing": brain.get("billing"),
        },
        "app_http_status": app_status,
        "metrics": cursor,
        "cost_baseline": cost_baseline,
        "criteria": {
            "min_duration_seconds": 1200,
            "min_user_turns": 3,
            "max_cold_hello_ms": 5000,
            "require_reconnect": True,
            "require_heard_clean": True,
        },
        "failures": failures,
    }
    write_json_atomic(handoff_path, handoff)
    return handoff


def verify(
    *,
    handoff_path: Path = DEFAULT_HANDOFF,
    report_path: Path = DEFAULT_REPORT,
    metrics_path: Path = DEFAULT_METRICS_PATH,
    heard_clean: bool,
    require_reconnect: bool = True,
    billing_dashboard_clear: bool = False,
) -> dict[str, Any]:
    try:
        handoff = json.loads(Path(handoff_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"call handoff could not be read: {exc}") from exc
    if (
        not isinstance(handoff, dict)
        or handoff.get("schema_version") != 2
        or handoff.get("ok") is not True
    ):
        raise ValueError("call handoff is missing or its preflight did not pass")
    rows, window = load_metrics_append(metrics_path, handoff.get("metrics"))
    if window.get("ok") is not True:
        raise ValueError("call metrics changed outside the staged append window")
    call_ids = {
        str(row.get("call_id"))
        for row in rows
        if row.get("event") == "call.start" and str(row.get("call_id") or "")
    }
    if len(call_ids) != 1:
        raise ValueError(
            "the staged window must contain exactly one physical call, found "
            + str(len(call_ids))
        )
    call_id = next(iter(call_ids))
    integrity = analyze_call(
        rows,
        call_id=call_id,
        criteria=IntegrityCriteria(),
        heard_clean=heard_clean,
    )
    failures = list(integrity.get("failures", []))
    reconnects = int(integrity.get("reconnects") or 0)
    if require_reconnect and reconnects < 1:
        failures.append("the call did not prove one network-roam reconnect")
    cost = analyze_cost_objective(
        rows,
        call_id=call_id,
        auth=subscription_auth_evidence(),
        daemon=brain_daemon_evidence(),
        baseline=handoff.get("cost_baseline"),
        health=brain_health_evidence(),
        metrics_window=window,
        billing_dashboard_clear=billing_dashboard_clear,
    )
    failures.extend(str(item) for item in cost.get("failures", []))
    failures = list(dict.fromkeys(failures))
    report = {
        "schema_version": 2,
        "ok": not failures,
        "verified_at": time.time(),
        "call_id": call_id,
        "cold_open_pass": bool(integrity.get("cold_open", {}).get("pass")),
        "call_integrity_pass": bool(integrity.get("acceptance_claim"))
        and (not require_reconnect or reconnects >= 1),
        "cost_pass": bool(cost.get("acceptance_claim")),
        "reconnects": reconnects,
        "metrics_window": window,
        "integrity": integrity,
        "cost": cost,
        "failures": failures,
    }
    write_json_atomic(report_path, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path, default=DEFAULT_HANDOFF)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser("prepare")
    stage.add_argument("--app-url", default=DEFAULT_APP_URL)
    check = subparsers.add_parser("verify")
    check.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    check.add_argument("--heard-clean", action="store_true")
    check.add_argument("--allow-no-reconnect", action="store_true")
    check.add_argument("--billing-dashboard-clear", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(
                handoff_path=args.handoff,
                metrics_path=args.metrics,
                app_url=args.app_url,
            )
        else:
            result = verify(
                handoff_path=args.handoff,
                report_path=args.report,
                metrics_path=args.metrics,
                heard_clean=args.heard_clean,
                require_reconnect=not args.allow_no_reconnect,
                billing_dashboard_clear=args.billing_dashboard_clear,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
