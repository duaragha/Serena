"""Native crash status recovery remains bounded and authority-aware."""

import os

import pytest

from fleet.store import FleetStore
from fleet.resources import is_process_crash
from test_fleet_supervision import _run


CRASH_CODES = [0xC0000005, 0xC000001D, 0xC0000094, 0xC0000096,
               0xC00000FD, 0xC000013A, 0xC0000374, 0xC0000409,
               0xC0000602, 0x40000015]


@pytest.mark.parametrize("code", [True, False, None, "3221225477", -9.0, 2**32 + 0xC0000005, -(2**32) - 9])
def test_invalid_exit_codes_cannot_alias_a_crash(code):
    assert not is_process_crash(code)


@pytest.mark.parametrize("code", CRASH_CODES + [x - 2**32 for x in CRASH_CODES if x >= 2**31])
def test_recorded_windows_crash_queues_process_retry(tmp_path, code):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    store.claim_run(run["run_id"])
    leg = run["phases"][0]["legs"][0]
    attempt = store.begin_attempt(leg["leg_id"])
    store.mark_attempt_process(attempt["attempt_id"], os.getpid(), "")
    store.finish_attempt(attempt["attempt_id"], state="failed", exit_code=code,
                         error=f"worker exited with status {code}")
    snapshot = store.get_run(run["run_id"])
    assert snapshot["resource_waits"][0]["resource"] == "process"
    assert snapshot["phases"][0]["legs"][0]["state"] == "waiting_for_resources"


@pytest.mark.parametrize("code", [0, 1, 2, 3, 127, 255, -1, 0xC0000135, 0xDEADBEEF])
def test_ordinary_or_unknown_exit_is_not_a_process_crash(tmp_path, code):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    store.claim_run(run["run_id"])
    leg = run["phases"][0]["legs"][0]
    attempt = store.begin_attempt(leg["leg_id"])
    store.mark_attempt_process(attempt["attempt_id"], os.getpid(), "")
    store.finish_attempt(attempt["attempt_id"], state="failed", exit_code=code,
                         error="ordinary command or configuration failure")
    assert store.get_run(run["run_id"])["resource_waits"] == []


@pytest.mark.parametrize("constraint", ["no_process", "cancelled", "authority"])
def test_crash_code_does_not_override_recovery_constraints(tmp_path, constraint):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _run(store)
    store.claim_run(run["run_id"])
    leg = run["phases"][0]["legs"][0]
    attempt = store.begin_attempt(leg["leg_id"])
    if constraint != "no_process":
        store.mark_attempt_process(attempt["attempt_id"], os.getpid(), "")
    if constraint == "cancelled":
        store.request_cancel(run["run_id"])
    store.finish_attempt(attempt["attempt_id"], state="failed", exit_code=0xC0000005,
                         error="worker interrupted", input_blocker_reason=(
                             "requires explicit authority" if constraint == "authority" else None))
    assert store.get_run(run["run_id"])["resource_waits"] == []
