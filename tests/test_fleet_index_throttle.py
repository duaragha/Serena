import sys
import types

import fleet.supervisor as supervisor


def _fake_indexer(monkeypatch, calls):
    module = types.ModuleType("core.indexer")
    module.update_index = lambda: calls.append(1)
    monkeypatch.setitem(sys.modules, "core.indexer", module)


def test_throttled_refresh_runs_at_most_once_per_window(monkeypatch):
    calls = []
    _fake_indexer(monkeypatch, calls)
    clock = [1000.0]
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(supervisor, "_last_index_refresh", 0.0)

    supervisor._refresh_index_throttled()
    for _ in range(50):
        clock[0] += supervisor.POLL_SECONDS
        supervisor._refresh_index_throttled()
    assert len(calls) == 1

    clock[0] += supervisor.INDEX_REFRESH_SECONDS
    supervisor._refresh_index_throttled()
    assert len(calls) == 2


def test_direct_refresh_is_never_throttled_and_resets_the_window(monkeypatch):
    calls = []
    _fake_indexer(monkeypatch, calls)
    clock = [1000.0]
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(supervisor, "_last_index_refresh", 0.0)

    supervisor._refresh_index()
    supervisor._refresh_index()
    assert len(calls) == 2

    clock[0] += 1.0
    supervisor._refresh_index_throttled()
    assert len(calls) == 2


def test_failed_refresh_still_throttles(monkeypatch):
    attempts = []
    module = types.ModuleType("core.indexer")

    def boom():
        attempts.append(1)
        raise RuntimeError("index locked")

    module.update_index = boom
    monkeypatch.setitem(sys.modules, "core.indexer", module)
    clock = [1000.0]
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(supervisor, "_last_index_refresh", 0.0)

    supervisor._refresh_index_throttled()
    clock[0] += supervisor.POLL_SECONDS
    supervisor._refresh_index_throttled()
    assert len(attempts) == 1
