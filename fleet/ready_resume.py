"""Recover ready peer work parked by an older scheduler without retrying blockers."""

from contextlib import suppress


def _ready_peer_review(snapshot: dict) -> str | None:
    from fleet.supervisor import _prior_turn_blocks_dispatch, _worker_key

    for phase in snapshot["phases"]:
        for leg in phase["legs"]:
            if leg["state"] != "queued" or leg["access_mode"] != "review":
                continue
            if not any(
                _worker_key(prior) == _worker_key(leg)
                and _prior_turn_blocks_dispatch(leg, prior)
                for previous in snapshot["phases"] if previous["index"] < phase["index"]
                for prior in previous["legs"]
            ):
                return str(leg["leg_id"])
    return None


def resume_ready_input_runs(store) -> list[str]:
    with store._connect() as db:
        ids = [str(row[0]) for row in db.execute("SELECT run_id FROM fleet_runs WHERE state='waiting_for_input' AND cancel_requested=0 ORDER BY created_at")]
    resumed = []
    for run_id in ids:
        try:
            if store.resume_ready_input_work(run_id, _ready_peer_review):
                resumed.append(run_id)
        except Exception as error:
            # One malformed legacy run must not starve other ready work.
            with suppress(Exception):
                store.append_event(run_id, "run.ready_work_probe_failed", {"error_type": type(error).__name__})
    return resumed
