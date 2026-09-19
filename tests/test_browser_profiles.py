"""Tests for saved browser auth profiles. No real browser launched."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

import core.browser_profiles as bp
from core.browser_profiles import ReenrollRequired, verify_seal


@pytest.fixture()
def store(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    monkeypatch.setenv("SERENA_BROWSER_PROFILE_DIR", str(root))
    auth = tmp_path / "auth.sqlite3"
    monkeypatch.setenv("SERENA_ACTION_DB_PATH", str(auth))
    return root


def _sealed(slug="demo"):
    (bp._slug_dir(slug)).mkdir(parents=True, exist_ok=True)
    bp._write_manifest(
        bp.Manifest(
            slug=slug,
            sealed=True,
            marker=bp.SealMarker(url_prefix="https://x.test/", title_regex="Inbox"),
        )
    )


def test_slug_validation(store):
    with pytest.raises(bp.BrowserProfileError):
        bp._slug_dir("../evil")
    with pytest.raises(bp.BrowserProfileError):
        bp._slug_dir("UPPER")


def test_attach_verifies_marker_before_handoff(store):
    _sealed()
    out = bp.attach("demo", confirm_fn=lambda prompt: True, _launcher=lambda: 1234, _verifier=lambda: (True, "cdp"))
    assert out == {"slug": "demo", "pid": 1234, "verified": True, "method": "cdp"}


def test_attach_gives_the_launched_browser_a_startup_budget(store, monkeypatch):
    """Verification follows the launch, so it must wait out a cold browser start."""
    _sealed()
    order, seen = [], {}

    def verify(manifest, **kwargs):
        order.append("verify")
        seen.update(kwargs)
        return True, "cdp"

    monkeypatch.setattr(bp, "_verify_marker", verify)
    out = bp.attach(
        "demo", confirm_fn=lambda prompt: True, _launcher=lambda: order.append("launch") or 4242
    )
    assert out["pid"] == 4242 and order == ["launch", "verify"]
    assert seen["cdp_dir"].name == bp.PROFILE_DIR_NAME
    assert seen["startup_timeout"] >= 5  # a 0.5s fast-fail budget would false-fail


def test_attach_refuses_unsealed(store):
    (bp._slug_dir("raw")).mkdir(parents=True, exist_ok=True)
    bp._write_manifest(bp.Manifest(slug="raw"))
    with pytest.raises(bp.BrowserProfileError, match="not sealed"):
        bp.attach("raw", _launcher=lambda: 1, _verifier=lambda: (True, "x"))


def test_expired_session_yields_reenroll_signal(store):
    _sealed()
    with pytest.raises(bp.BrowserProfileError, match="run browser enroll again"):
        bp.attach("demo", confirm_fn=lambda prompt: True, _launcher=lambda: 99, _verifier=lambda: (False, "title-mismatch"))


def test_audit_is_metadata_only(store):
    _sealed()
    bp.attach("demo", confirm_fn=lambda prompt: True, _launcher=lambda: 7, _verifier=lambda: (True, "cdp"))
    rows = (bp._slug_dir("demo") / "audit.jsonl").read_text(encoding="utf-8")
    assert "cookie" not in rows.lower()
    for line in rows.splitlines():
        assert set(json.loads(line)) <= {"ts", "slug", "action", "ok", "method", "signal", "pid", "marker_hint"}


def test_authority_lock_denies_attach(store, tmp_path):
    from core.action_authority import ActionAuthority

    auth = ActionAuthority(path=tmp_path / "lock.sqlite3")
    auth.engage_lock(reason="test")
    _sealed()
    with pytest.raises(bp.BrowserProfileError, match="denied by action authority"):
        bp.attach(
            "demo",
            authority_path=tmp_path / "lock.sqlite3",
            _launcher=lambda: 1,
            _verifier=lambda: (True, "x"),
        )


def test_remove_wipes_everything(store):
    _sealed()
    (bp._slug_dir("demo") / "Cookies").write_text("secret-bytes", encoding="utf-8")
    assert bp.remove("demo") == {"slug": "demo", "removed": True}
    assert not (store / "demo").exists()


def test_status_lists_metadata_only(store):
    _sealed("a")
    out = bp.status()
    assert out["profiles"]["a"]["sealed"] is True
    blob = json.dumps(out)
    assert "Inbox" not in blob  # regex content never leaves the manifest


def test_attach_denied_without_confirmation(store):
    _sealed()
    with pytest.raises(bp.BrowserProfileError, match="live confirmation or grant"):
        bp.attach("demo", _launcher=lambda: 1, _verifier=lambda: (True, "x"))


def test_attach_declined_confirmation(store):
    _sealed()
    with pytest.raises(bp.BrowserProfileError, match="not approved"):
        bp.attach(
            "demo",
            confirm_fn=lambda prompt: False,
            _launcher=lambda: 1,
            _verifier=lambda: (True, "x"),
        )


def test_sealed_marker_uses_only_browser_checks():
    class Browser:
        def __init__(self):
            self.kinds = []

        async def check(self, check):
            self.kinds.append(check.kind)
            value = {"url": "https://fixture.test/home", "title": "Dashboard — fixture"}[check.kind]
            assert check.match_mode == ("prefix" if check.kind == "url" else "regex")
            return {
                "value": value,
                "match": value.startswith(check.expected)
                if check.kind == "url"
                else bool(re.search(check.expected, value)),
                "elapsed_ms": 1,
            }

    async def run():
        browser = Browser()
        seal = {
            "slug": "fixture",
            "enrolled_at": "2026-09-17",
            "url_prefix": "https://fixture.test/",
            "title_regex": "^Dashboard",
        }
        assert (await verify_seal(browser, seal))["verified"] is True
        assert browser.kinds == ["url", "title"]
        with pytest.raises(ReenrollRequired, match="re-enroll"):
            await verify_seal(browser, {**seal, "url_prefix": "https://other.test/"})
        with pytest.raises(ReenrollRequired, match="re-enroll"):
            await verify_seal(browser, {**seal, "enrolled_at": ""})

    asyncio.run(run())

def test_cdp_verification_never_falls_back_to_desktop(monkeypatch, tmp_path):
    monkeypatch.setattr(bp, "_cdp_marker", lambda *args: (False, "cdp-unavailable"))
    monkeypatch.setattr(bp, "_focused_window_title", lambda: pytest.fail("no desktop fallback"))
    manifest = bp.Manifest("fixture", sealed=True, marker=bp.SealMarker(title_regex="Ready"))
    assert bp._verify_marker(manifest, cdp_dir=tmp_path) == (False, "cdp-unavailable")


def test_profile_launch_enables_only_loopback_cdp(monkeypatch, tmp_path):
    monkeypatch.setattr(bp, "_chromium", lambda: "chromium")
    args = bp._launch_args(tmp_path)
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--remote-debugging-port=0" in args


def test_cdp_errors_are_metadata_only(monkeypatch, tmp_path):
    from core import computer_browser
    def failed(*args, **kwargs):
        raise RuntimeError("secret page content")
    monkeypatch.setattr(computer_browser, "check_session", failed)
    assert bp._cdp_marker("https://fixture.test/", "Ready", tmp_path) == (False, "cdp-error")


def test_existing_profile_uses_scripted_checks_without_screenshots(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    from core import computer_browser
    from core.computer_browser import BrowserChecks
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    (profile / "DevToolsActivePort").write_text("12345\n/devtools/browser/fixture\n", encoding="utf-8")
    seen, bound = [], []
    # Discovery is only trusted once the port belongs to this profile's browser.
    monkeypatch.setattr(
        computer_browser, "_verify_profile_listener", lambda port, d: bound.append((port, d))
    )
    class Browser:
        async def check(self, check):
            seen.append((check.kind, check.match_mode, check.expected))
            return {"value": "fixture", "match": True, "elapsed_ms": 0}
    @asynccontextmanager
    async def attach(port_file):
        assert port_file.stat().st_mode & 0o777 == 0o600
        assert json.loads(port_file.read_text(encoding="utf-8")) == {"port": 12345}
        yield Browser()
    monkeypatch.setattr(BrowserChecks, "attach", attach)
    monkeypatch.setattr(bp, "_focused_window_title", lambda: pytest.fail("no screenshots or desktop"))
    manifest = bp.Manifest("fixture", sealed=True, marker=bp.SealMarker("https://fixture.test/", "^Ready"))
    assert bp._verify_marker(manifest, cdp_dir=profile) == (True, "cdp")
    assert seen == [("url", "prefix", "https://fixture.test/"), ("title", "regex", "^Ready")]
    assert bound == [(12345, profile)]
