"""Operator loopback verification is a durable, queryable flag on the attempt.

Provider sandboxes are not Serena-owned config, so a worker that needs
127.0.0.1 reports its result unverified and the host-side operator records
the check here instead of the worker reaching loopback itself.
"""

from __future__ import annotations

import pytest
from test_fleet_policy_store import _create

from fleet.store import FleetStore


def test_verification_is_recorded_readable_and_latest_wins(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store, worker_count=1)
    leg_id = run["phases"][0]["legs"][0]["leg_id"]
    attempt = store.begin_attempt(leg_id)
    attempt_id = attempt["attempt_id"]

    assert store.loopback_verified(attempt_id) is None

    first = store.record_loopback_verification(
        attempt_id, verifier="operator", detail="health endpoint 200 on 127.0.0.1"
    )
    assert first["verifier"] == "operator"
    assert "127.0.0.1" in first["detail"]
    assert store.loopback_verified(attempt_id) == first

    second = store.record_loopback_verification(
        attempt_id, verifier="host-probe", detail="rechecked after restart"
    )
    assert store.loopback_verified(attempt_id) == second

    kinds = [event["type"] for event in store.events(run["run_id"])]
    assert kinds.count("attempt.loopback_verified") == 2

    reopened = FleetStore(store.path)
    assert reopened.loopback_verified(attempt_id) == second


def test_verification_rejects_unknown_attempts_and_empty_verifiers(tmp_path):
    store = FleetStore(tmp_path / "fleet.sqlite3")
    run = _create(store, worker_count=1)
    leg_id = run["phases"][0]["legs"][0]["leg_id"]
    attempt_id = store.begin_attempt(leg_id)["attempt_id"]

    with pytest.raises(KeyError):
        store.record_loopback_verification("no-such-attempt", verifier="operator")
    with pytest.raises(ValueError):
        store.record_loopback_verification(attempt_id, verifier="   ")
    assert store.loopback_verified(attempt_id) is None
    assert store.loopback_verified("no-such-attempt") is None
