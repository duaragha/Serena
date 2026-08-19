"""UI edits must show up without restarting the sidecar.

The whole interface is one HTML constant baked into ``ui/web.py`` at import, so
changing it used to mean restarting the backend, which kills every PTY and the
live agent sessions attached to them. In a source checkout the page is sliced
out of the file on disk per request instead, so reloading the window is enough
and the Python process (with its terminals) is never touched.

The packaged sidecar has no source file to re-read, so it must keep serving the
in-memory constant and must not ship the dev poller.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ui import web


@pytest.fixture
def source_backup():
    path = Path(web.__file__).resolve()
    original = path.read_text(encoding="utf-8")
    yield path
    path.write_text(original, encoding="utf-8")
    web._html_disk_cache["mtime"] = None
    web._html_disk_cache["html"] = None


def test_the_disk_slice_matches_the_imported_page_exactly():
    """Hot reload must serve the same bytes, not a lookalike."""

    assert web.ui_hot_reload_enabled() is True
    assert web._live_html() == web.HTML


def test_an_edit_is_served_without_touching_the_running_process(source_backup):
    client = web.app.test_client()
    marker = "<!-- serena-hot-reload-test -->"
    assert marker not in client.get("/").get_data(as_text=True)

    source = source_backup.read_text(encoding="utf-8")
    anchor = '<span class="col-title">Title</span>'
    assert source.count(anchor) == 1
    source_backup.write_text(source.replace(anchor, anchor + marker, 1), encoding="utf-8")

    served = client.get("/").get_data(as_text=True)
    assert marker in served, "an edited page must be served without a restart"
    # The imported constant is untouched, which is what proves nothing reloaded.
    assert marker not in web.HTML


def test_a_broken_slice_falls_back_to_the_imported_page(source_backup):
    """A dev convenience may never serve a broken page."""

    source_backup.write_text("garbage, no template here", encoding="utf-8")
    web._html_disk_cache["mtime"] = None

    assert web._live_html() == web.HTML


def test_a_packaged_build_serves_the_checkout_when_one_is_present(monkeypatch):
    """The installed app is the one that matters, so it must reload too.

    Gating this to unpackaged runs meant every UI tweak cost a full rebuild,
    which is the whole thing hot reload exists to avoid.
    """

    monkeypatch.setattr(web.sys, "frozen", True, raising=False)
    monkeypatch.setenv("SERENA_UI_SOURCE", str(Path(web.__file__).resolve()))

    assert web.ui_hot_reload_enabled() is True
    assert web._live_html() == web.HTML
    body = web.app.test_client().get("/").get_data(as_text=True)
    assert "/api/ui-version" in body


def test_a_build_with_no_checkout_falls_back_to_the_bundled_page(monkeypatch, tmp_path):
    """Shipped to a machine with no source, it must behave like before."""

    monkeypatch.setattr(web.sys, "frozen", True, raising=False)
    monkeypatch.setenv("SERENA_UI_SOURCE", str(tmp_path / "absent" / "web.py"))

    assert web._ui_source_path() is None
    assert web.ui_hot_reload_enabled() is False
    assert web._live_html() == web.HTML
    assert web.ui_source_mtime() == 0.0

    body = web.app.test_client().get("/").get_data(as_text=True)
    assert "/api/ui-version" not in body, "the dev poller must not ship where it cannot work"


def test_the_off_switch_wins_even_with_a_checkout_present(monkeypatch):
    monkeypatch.setenv("SERENA_UI_HOTRELOAD", "0")
    assert web.ui_hot_reload_enabled() is False
    assert web._live_html() == web.HTML


def test_the_version_endpoint_reports_the_source_mtime():
    payload = web.app.test_client().get("/api/ui-version").get_json()

    assert payload["hot_reload"] is True
    assert payload["mtime"] == pytest.approx(web.ui_source_mtime())
    assert payload["mtime"] > 0
