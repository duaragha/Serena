"""The server has to be able to say it is running yesterday's code.

Serena runs from a checkout that changes under her, and the desktop app usually
attaches to a long-lived systemd server rather than owning one. A fix lands on
disk, every request keeps being served by the process that started hours
earlier, and nothing says so: the fix simply appears not to work. That is what
this endpoint exists to end.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ui import web


@pytest.fixture()
def client():
    return web.app.test_client()


def _clear_cache():
    web._FRESHNESS_CACHE.clear()


def test_a_current_process_reports_itself_current(client, monkeypatch) -> None:
    _clear_cache()
    monkeypatch.setattr(web, "_PROCESS_STARTED_AT", time.time())

    body = client.get("/api/backend-freshness").get_json()

    assert body["ok"] is True
    assert body["stale"] is False
    assert body["pid"] == os.getpid()


def test_a_process_older_than_the_source_is_stale(client, monkeypatch) -> None:
    """The actual failure: code changed after this process started."""
    _clear_cache()
    monkeypatch.setattr(web, "_PROCESS_STARTED_AT", time.time() - 7200)

    body = client.get("/api/backend-freshness").get_json()

    assert body["stale"] is True
    assert body["stale_by_seconds"] > 0


def test_it_names_the_checkout_so_the_shell_need_not_guess(client) -> None:
    """A packaged app has no idea where the source lives; a frozen one has none."""
    _clear_cache()
    body = client.get("/api/backend-freshness").get_json()

    root = Path(body["source_root"])
    assert (root / "core").is_dir()
    assert (root / "ui" / "web.py").is_file()
    assert body["frozen"] is False


def test_virtualenvs_and_caches_do_not_count_as_source(tmp_path, monkeypatch) -> None:
    """Those churn on their own and would report staleness that means nothing."""
    _clear_cache()
    root = tmp_path / "repo"
    (root / "ui").mkdir(parents=True)
    (root / "core").mkdir(parents=True)

    real = root / "core" / "real.py"
    real.write_text("x = 1", encoding="utf-8")
    source_time = time.time() - 500
    os.utime(real, (source_time, source_time))

    far_future = time.time() + 10_000
    for noise in (
        root / "core" / "__pycache__" / "cached.py",
        root / "core" / ".venv" / "lib" / "dep.py",
        root / "core" / "vendor" / "site-packages" / "dep.py",
    ):
        noise.parent.mkdir(parents=True, exist_ok=True)
        noise.write_text("y = 2", encoding="utf-8")
        os.utime(noise, (far_future, far_future))

    monkeypatch.setattr(web, "_FRESHNESS_ROOTS", ("core",))
    monkeypatch.setattr(web, "__file__", str(root / "ui" / "web.py"))

    newest = web._newest_source_mtime()

    assert newest == pytest.approx(source_time, abs=2), (
        "the newest source should be the real file, not a churning dependency"
    )


def test_the_answer_is_cached_so_a_menu_poll_is_cheap(monkeypatch) -> None:
    """It is polled on a timer; walking the tree every second would be silly."""
    _clear_cache()
    calls = {"n": 0}
    real_rglob = Path.rglob

    def counting_rglob(self, pattern):
        calls["n"] += 1
        return real_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", counting_rglob)

    web._newest_source_mtime()
    first = calls["n"]
    web._newest_source_mtime()

    assert calls["n"] == first, "the second call inside the window must not walk the tree"
