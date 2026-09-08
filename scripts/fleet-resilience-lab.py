#!/usr/bin/env python3
"""Run repeatable fault tests and render their real durable receipts as an offline report.

No production database, native provider usage, service restart or live fault controls.
Provider outputs/capacity/time are scripted; SQLite, process fencing, git/test gates
and Fleet recovery code are exercised by the selected tests.
"""

import argparse
import html
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = [
    "test_fleet_autonomy.py",
    "test_fleet_supervision.py",
    "test_fleet_peers.py",
    "test_fleet_capacity.py",
    "test_fleet_difficult_retry.py",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new report directory; existing output is never overwritten",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    traces = output / "traces"
    traces.mkdir()
    env = dict(
        os.environ,
        SERENA_FLEET_LAB_TRACES=str(traces),
        PYTHONPATH=str(ROOT / "tests") + os.pathsep + str(ROOT),
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "fleet_lab_capture",
        "--junitxml=" + str(output / "results.xml"),
        *[str(ROOT / "tests" / name) for name in SUITES],
    ]
    result = subprocess.run(
        command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    (output / "test-output.txt").write_text(result.stdout)
    print(result.stdout)
    cases = []
    if (output / "results.xml").exists():
        for node in ET.parse(output / "results.xml").iter("testcase"):
            bad = node.find("failure")
            if bad is None:
                bad = node.find("error")
            cases.append(
                {
                    "name": node.attrib["name"],
                    "suite": node.attrib.get("classname", ""),
                    "seconds": float(node.attrib.get("time", 0)),
                    "state": "failed"
                    if bad is not None
                    else "skipped"
                    if node.find("skipped") is not None
                    else "passed",
                    "error": (bad.text or "") if bad is not None else "",
                }
            )
    receipts = [json.loads(path.read_text()) for path in sorted(traces.glob("*.json"))]
    data = {
        "created": datetime.now(timezone.utc).isoformat(),
        "exit_code": result.returncode,
        "cases": cases,
        "receipts": receipts,
    }
    (output / "results.json").write_text(json.dumps(data, indent=2))
    passed = sum(c["state"] == "passed" for c in cases)
    blocks = []
    for case in cases:
        events = [
            event
            for receipt in receipts
            if receipt["test"].split("::")[-1] == case["name"]
            for event in receipt["events"]
        ]
        important = [
            e
            for e in events
            if e["type"].startswith(
                ("run.recover", "peer.", "learning.", "worker.progress", "worker.stalled")
            )
            or "capacity" in e["type"]
            or "retry" in e["type"]
        ]
        items = "".join(
            "<details class='event'><summary>"
            + html.escape(e["type"])
            + "</summary><pre>"
            + html.escape(json.dumps(e["payload"], indent=2))
            + "</pre></details>"
            for e in important
        )
        blocks.append(
            "<details class='case "
            + case["state"]
            + "'><summary><span>"
            + case["state"].upper()
            + "</span> "
            + html.escape(case["name"])
            + f" <small>{case['seconds']:.2f}s · {len(important)} transitions</small></summary>"
            + ("<pre>" + html.escape(case["error"]) + "</pre>" if case["error"] else "")
            + (
                items
                or "<p>Assertions passed without retained autonomy events; see the test source and raw results.</p>"
            )
            + "</details>"
        )
    page = (
        """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Fleet resilience lab</title><style>
body{background:#141217;color:#e7e3ed;font:15px system-ui;margin:0;padding:40px;max-width:1300px;margin:auto}
h1{font-size:34px}p{color:#bbb4c5;line-height:1.6}small{color:#aaa;margin-left:12px}.case{border:1px solid #413649;border-radius:8px;margin:10px 0;padding:16px}
summary{cursor:pointer;overflow-wrap:anywhere}.case>summary span{color:#85e8b6;font-weight:700;margin-right:12px}.failed>summary span{color:#ff8395}
.event{border-left:2px solid #ac83ec;margin:14px 0 0 14px;padding:8px 16px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;color:#c6bfce}
.note{background:#25202d;padding:18px;border-radius:8px}button{background:#41304f;border:0;color:white;padding:10px 15px;border-radius:6px;cursor:pointer;margin-right:8px}
</style><h1>Fleet resilience lab</h1>"""
        + f"<h2>{passed}/{len(cases)} test cases passed · process exit {result.returncode}</h2>"
        + """
<p class="note">Deterministic fault injection, not a live-model benchmark. Provider responses, clocks and capacity are scripted.
The tests exercise real Fleet control code and temporary databases; selected scenarios also use real process fencing and git/test gates.
Expand a test to see its committed event receipts. A green test is not evidence that future fleets are faster.</p>
<p>Recovery · independent learning · request outcomes · progress budgets · quota and difficult retries</p>
<button id="expand">Expand tests</button><button id="collapse">Collapse all</button>
"""
        + "".join(blocks)
        + """<script>
document.querySelector('#expand').onclick=()=>document.querySelectorAll('.case').forEach(e=>e.open=true);
document.querySelector('#collapse').onclick=()=>document.querySelectorAll('details').forEach(e=>e.open=false);
</script></html>"""
    )
    (output / "index.html").write_text(page)
    print("Report: " + str(output / "index.html"))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
