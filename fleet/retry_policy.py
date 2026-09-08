"""Conservative difficult-retry classification from supervisor-owned test receipts."""

from __future__ import annotations

import re
from typing import Any

DIFFICULT_RETRY_MODEL = "gpt-6-astra"
DIFFICULT_RETRY_EFFORT = "xhigh"

_INFRASTRUCTURE = re.compile(
    r"permission denied|operation not permitted|unauthori[sz]ed|forbidden|"
    r"rate.?limit|quota|usage limit|out of usage|connection|network|timed? out|"
    r"no module named|module not found|modulenotfounderror|cannot find module|"
    r"command not found|no such file|enospc|no space left|out of memory|"
    r"missing (?:credential|dependency)|authentication|econn|enotfound",
    re.IGNORECASE,
)
_IMPLEMENTATION = re.compile(
    r"AssertionError|ERR_ASSERTION|assertion .*failed|\bassert .*==|"
    r"\bFAILED\s+[^\n]+::|\bFAIL:\s+test_|\bnot ok\s+\d+|"
    r"error TS\d+:|SyntaxError:",
    re.IGNORECASE,
)


def difficult_retry_reason(
    run: dict[str, Any], leg: dict[str, Any], gate: dict[str, Any] | None
) -> str | None:
    """Only a concrete code/test failure in an actual integration gate qualifies.

    Provider prose, raw provider exit codes and malformed completion envelopes
    are deliberately not classification inputs. Inconclusive failures stay failed.
    """
    policy = run.get("policy") or {}
    phase_name = leg.get("phase") or next(
        (
            phase.get("name")
            for phase in run.get("phases", [])
            if any(item.get("leg_id") == leg.get("leg_id") for item in phase.get("legs", []))
        ),
        "",
    )
    if run.get("activity") != "coding" or phase_name not in {"execute", "finalize"}:
        return None
    if leg.get("access_mode") != "write" or policy.get("requested_provider_mode") in {
        "claude",
        "claude-only",
    }:
        return None
    if policy.get("provider_mode") in {"claude", "claude-only"}:
        return None
    if (
        leg.get("requested_model", leg.get("model")),
        leg.get("requested_effort", leg.get("effort")),
    ) == (DIFFICULT_RETRY_MODEL, DIFFICULT_RETRY_EFFORT):
        return None
    if not gate or gate.get("ran") is not True or gate.get("ok") is not False:
        return None
    code = gate.get("exit_code")
    if type(code) is not int or not 0 < code < 124:
        return None
    output = str(gate.get("output_tail") or "")
    if _INFRASTRUCTURE.search(output) or not _IMPLEMENTATION.search(output):
        return None
    return "integration verification found an implementation failure: " + output[-800:]
