"""Acceptance gate for one explicit mid-call draft-to-link job."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from core.artifacts import ArtifactRegistry
from core.work_jobs import WorkJobStore

from .integrity import latest_call_id, load_metrics


def analyze_tasking(
    rows: Iterable[dict[str, Any]],
    *,
    call_id: str,
    store: WorkJobStore,
    artifacts: ArtifactRegistry,
    projects_root: Path | None = None,
) -> dict[str, Any]:
    selected = [row for row in rows if row.get("call_id") == call_id]
    end_times = [
        row.get("monotonic_us")
        for row in selected
        if row.get("event") == "call.end"
        and row.get("clean_hangup") is True
        and isinstance(row.get("monotonic_us"), int)
        and not isinstance(row.get("monotonic_us"), bool)
    ]
    final_end = max(end_times) if end_times else None
    earlier_clean_ends = [
        value for value in end_times if final_end is not None and value < final_end
    ]
    previous_clean_end = max(earlier_clean_ends) if earlier_clean_ends else -1

    def in_final_lifecycle(row: dict[str, Any]) -> bool:
        value = row.get("monotonic_us")
        return bool(
            final_end is not None
            and isinstance(value, int)
            and not isinstance(value, bool)
            and previous_clean_end < value < final_end
        )

    accepted_job_ids = {
        row.get("job_id")
        for row in selected
        if row.get("event") == "task.accepted"
        and isinstance(row.get("job_id"), str)
        and in_final_lifecycle(row)
    }
    jobs = [job for job in store.jobs_for_call(call_id) if job.job_id in accepted_job_ids]
    events = [event for event in store.events_for_call(call_id) if event.job_id in accepted_job_ids]
    ready_events = {event.job_id: event for event in events if event.type == "artifact.ready"}
    acknowledged = {
        row.get("event_seq"): row
        for row in selected
        if row.get("event") == "task.event_acknowledged"
        and row.get("event_type") == "artifact.ready"
        and isinstance(row.get("event_seq"), int)
        and not isinstance(row.get("event_seq"), bool)
        and in_final_lifecycle(row)
    }
    opened = {
        (row.get("event_seq"), row.get("job_id")): row
        for row in selected
        if row.get("event") == "task.artifact_opened"
        and row.get("event_type") == "artifact.ready"
        and row.get("receipt_verified") is True
        and isinstance(row.get("event_seq"), int)
        and not isinstance(row.get("event_seq"), bool)
        and isinstance(row.get("job_id"), str)
        and in_final_lifecycle(row)
    }
    root = (projects_root or (Path.home() / ".claude" / "projects")).expanduser()

    verified: list[str] = []
    failures: list[str] = []
    for job in jobs:
        event = ready_events.get(job.job_id)
        if job.state != "artifact_ready" or event is None:
            failures.append(f"job {job.job_id} did not reach artifact_ready")
            continue
        url = event.payload.get("url")
        token = (
            url.removeprefix("/artifacts/")
            if isinstance(url, str) and url.startswith("/artifacts/")
            else ""
        )
        artifact = artifacts.resolve(token) if token else None
        if artifact is None or artifact.job_id != job.job_id:
            failures.append(f"job {job.job_id} has no valid scoped artifact")
            continue
        ack = acknowledged.get(event.event_seq)
        ack_time = ack.get("monotonic_us") if ack else None
        if (
            final_end is None
            or not isinstance(ack_time, int)
            or isinstance(ack_time, bool)
            or ack_time >= final_end
        ):
            failures.append(f"job {job.job_id} was not acknowledged by the phone before call end")
            continue
        opened_row = opened.get((event.event_seq, job.job_id))
        opened_time = opened_row.get("monotonic_us") if opened_row else None
        if (
            not isinstance(opened_time, int)
            or isinstance(opened_time, bool)
            or opened_time >= final_end
        ):
            failures.append(f"job {job.job_id} was not opened in-app before call end")
            continue
        if not job.origin_session_id:
            failures.append(f"job {job.job_id} is not linked to the call session")
            continue
        if not job.worker_session_id:
            failures.append(f"job {job.job_id} has no worker session")
            continue
        worker_files = list(root.rglob(f"{job.worker_session_id}.jsonl")) if root.is_dir() else []
        if not worker_files:
            failures.append(f"job {job.job_id} worker session is missing")
            continue
        verified.append(job.job_id)

    if not jobs:
        failures.append("no explicit call job was recorded")
    passed = bool(jobs) and len(verified) == len(jobs) and not failures
    return {
        "ok": passed,
        "acceptance_claim": passed,
        "call_id": call_id,
        "jobs": len(jobs),
        "verified_jobs": verified,
        "clean_call_end": final_end is not None,
        "lifecycle_started_after_us": previous_clean_end,
        "phone_acknowledged": len(acknowledged),
        "opened_in_app": len(opened),
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call-id")
    parser.add_argument("--metrics", type=Path)
    parser.add_argument("--jobs", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--artifact-db", type=Path)
    parser.add_argument("--artifact-key", type=Path)
    parser.add_argument("--projects-root", type=Path)
    args = parser.parse_args(argv)
    rows = load_metrics(args.metrics) if args.metrics else load_metrics()
    call_id = args.call_id or latest_call_id(rows)
    if not call_id:
        print(json.dumps({"ok": False, "error": "no call.start telemetry found"}))
        return 2
    store = WorkJobStore(args.jobs) if args.jobs else WorkJobStore()
    artifact_options = {
        key: value
        for key, value in {
            "root": args.artifact_root,
            "db_path": args.artifact_db,
            "key_path": args.artifact_key,
        }.items()
        if value is not None
    }
    report = analyze_tasking(
        rows,
        call_id=call_id,
        store=store,
        artifacts=ArtifactRegistry(**artifact_options),
        projects_root=args.projects_root,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
