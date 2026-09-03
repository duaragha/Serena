import threading
import time

from core import frontdoor


def test_greeting_refresh_is_single_flight(monkeypatch, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def fake_turn(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return {"ok": True, "say": "hello"}

    monkeypatch.setattr(frontdoor, "turn", fake_turn)
    monkeypatch.setattr(frontdoor, "GREETING_CACHE", tmp_path / "greeting.json")
    with frontdoor._GREETING_WARM_CONDITION:
        frontdoor._GREETING_WARMING = False

    frontdoor.warm_greeting()
    assert entered.wait(timeout=1)
    for _ in range(10):
        frontdoor.warm_greeting()
    time.sleep(0.02)
    assert calls == 1

    release.set()
    frontdoor.warm_greeting(block=True)
    assert calls == 1
