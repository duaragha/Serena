"""The brain's uptime ledger says why she went down, not just that she did.

On 2026-09-19 the brain stopped at 15:37 and stayed dead two days, and the only
record was end_reason "shutdown" -- the same word for a crash, a kill and a
clean stop -- in a log with no timestamps.
"""

import io

from core import brain_downtime, doctor

DAY = 86400.0


def test_a_clean_stop_is_remembered_with_its_reason_and_the_gap(tmp_path):
    ledger = tmp_path / "uptime.jsonl"
    brain_downtime.record_start(100, path=ledger, now=1000.0, booted_at=500.0)
    brain_downtime.record_stop(100, "crashed", detail="RuntimeError: provider gone",
                               started_at=1000.0, path=ledger, now=1000.0 + 2 * 3600)

    start = brain_downtime.record_start(200, path=ledger, now=1000.0 + 2 * DAY,
                                        booted_at=500.0)

    assert start["previous_stop"] == "crashed"
    assert round(start["down_for"]) == round(2 * DAY - 2 * 3600)


def test_no_stop_line_and_a_newer_boot_means_the_machine_went_down(tmp_path):
    ledger = tmp_path / "uptime.jsonl"
    brain_downtime.record_start(100, path=ledger, now=1000.0, booted_at=500.0)
    # Power cut: no stop line, and the PC booted after that start.
    start = brain_downtime.record_start(200, path=ledger, now=9000.0, booted_at=8000.0)

    assert start["previous_stop"] == "machine_rebooted"
    assert start["down_for"] == 1000.0


def test_no_stop_line_and_no_reboot_means_it_was_killed(tmp_path):
    ledger = tmp_path / "uptime.jsonl"
    brain_downtime.record_start(100, path=ledger, now=1000.0, booted_at=500.0)
    start = brain_downtime.record_start(200, path=ledger, now=9000.0, booted_at=500.0)

    assert start["previous_stop"] == "killed_without_a_trace"


def test_a_torn_line_from_a_dying_process_is_skipped_not_fatal(tmp_path):
    ledger = tmp_path / "uptime.jsonl"
    brain_downtime.record_start(100, path=ledger, now=1000.0, booted_at=500.0)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "stop", "at": 12')  # died mid-write

    assert [e["event"] for e in brain_downtime.read(ledger)] == ["start"]


def test_history_reads_as_sentences(tmp_path):
    ledger = tmp_path / "uptime.jsonl"
    brain_downtime.record_start(100, path=ledger, now=1000.0, booted_at=500.0)
    brain_downtime.record_stop(100, "windows:windows_shutdown", started_at=1000.0,
                               path=ledger, now=4600.0)
    brain_downtime.record_start(200, path=ledger, now=4600.0 + DAY, booted_at=500.0)

    lines = brain_downtime.explain(brain_downtime.read(ledger))

    assert "stopped after 1h 0m: windows:windows_shutdown" in lines[1]
    assert "down 1d 0h, last stop: windows:windows_shutdown" in lines[2]


def _doctor_on(monkeypatch, tmp_path, events, alive=True):
    ledger = tmp_path / "uptime.jsonl"
    for event in events:
        brain_downtime._append(event, ledger)
    monkeypatch.setattr(brain_downtime, "LEDGER", ledger)
    import psutil

    monkeypatch.setattr(psutil, "pid_exists", lambda pid: alive)
    return doctor.check_brain_alive(now=1000.0 + 2 * DAY)


def test_the_doctor_catches_a_brain_that_stopped(monkeypatch, tmp_path):
    """This check did not exist, so two dead days read as nothing broken."""
    [finding] = _doctor_on(monkeypatch, tmp_path, [
        {"event": "start", "at": 1000.0, "pid": 100},
        {"event": "stop", "at": 1000.0 + 3600, "pid": 100, "reason": "crashed",
         "detail": "RuntimeError: provider gone"},
    ])
    assert not finding.ok and finding.severity == "fail"
    assert "crashed" in finding.detail and "provider gone" in finding.detail


def test_the_doctor_catches_a_brain_that_died_silently(monkeypatch, tmp_path):
    [finding] = _doctor_on(monkeypatch, tmp_path,
                           [{"event": "start", "at": 1000.0, "pid": 100}], alive=False)
    assert not finding.ok
    assert "without recording why" in finding.detail


def test_a_running_brain_is_healthy(monkeypatch, tmp_path):
    [finding] = _doctor_on(monkeypatch, tmp_path,
                           [{"event": "start", "at": 1000.0, "pid": 100}], alive=True)
    assert finding.ok


def test_every_log_line_carries_the_wall_clock():
    from core.brain_daemon import _Stamped

    out = io.StringIO()
    stamped = _Stamped(out)
    stamped.write("[brain] one\n[brain] two")
    stamped.write(" continued\n")

    lines = out.getvalue().splitlines()
    assert len(lines) == 2
    assert all(line[:4].isdigit() and line[4] == "-" for line in lines)
    assert lines[1].endswith("[brain] two continued")
