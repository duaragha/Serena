"""Prove one native terminal widget can host multiple cold-resumed children."""

import pytest


gi = pytest.importorskip("gi")
gi.require_version("Vte", "2.91")
from gi.repository import GLib, Vte  # noqa: E402


def _wait_for_exit(terminal, timeout_ms=3000):
    loop = GLib.MainLoop()
    result = {"status": None}

    def _exited(_terminal, status):
        result["status"] = status
        loop.quit()

    def _timeout():
        loop.quit()
        return False

    handler = terminal.connect("child-exited", _exited)
    GLib.timeout_add(timeout_ms, _timeout)
    loop.run()
    terminal.disconnect(handler)
    assert result["status"] is not None


def test_vte_can_spawn_again_after_a_child_exits():
    terminal = Vte.Terminal()
    pids = []
    for command in ("exit 0", "sleep 0.05"):
        ok, pid = terminal.spawn_sync(
            Vte.PtyFlags.DEFAULT,
            "/tmp",
            ["/bin/sh", "-c", command],
            [],
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            None,
        )
        assert ok
        pids.append(pid)
        _wait_for_exit(terminal)

    assert pids[0] != pids[1]
