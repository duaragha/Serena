from __future__ import annotations

from core.ambient_denylist import AmbientDenylist


def test_denylisted_password_manager_app_is_denied():
    denylist = AmbientDenylist.default()

    assert denylist.denies(app="1password")
    assert denylist.denies(app="Bitwarden")
    assert denylist.denies(app="KeePassXC")
    assert denylist.denies(app="tor browser")


def test_ordinary_apps_are_allowed():
    denylist = AmbientDenylist.default()

    assert not denylist.denies(app="microsoft-edge", title="Example Domain")
    assert not denylist.denies(app="code", title="ambient_store.py — serena")
    assert not denylist.denies(app="org.gnome.Terminal", title="raghav@laptop")


def test_incognito_and_private_titles_are_denied():
    denylist = AmbientDenylist.default()

    assert denylist.denies(app="microsoft-edge", title="New Incognito window")
    assert denylist.denies(app="microsoft-edge", title="New InPrivate window")
    assert denylist.denies(app="firefox", title="Private Browsing")


def test_bank_and_login_urls_are_denied_before_capture():
    denylist = AmbientDenylist.default()

    assert denylist.denies(app="microsoft-edge", url="https://chase.com/login")
    assert denylist.denies(app="microsoft-edge", url="https://example-bank.com/signin")
    assert denylist.denies(app="microsoft-edge", url="http://expyuzz4wqqh33r.onion/")
    assert denylist.denies(app="microsoft-edge", url="https://shop.example/checkout")


def test_ordinary_urls_are_allowed():
    denylist = AmbientDenylist.default()

    assert not denylist.denies(app="microsoft-edge", url="https://example.com/docs")
    assert not denylist.denies(app="microsoft-edge", url="https://github.com/duaragha/serena")


def test_denial_gives_a_reason_for_the_audit_log():
    denylist = AmbientDenylist.default()

    reason = denylist.denies(app="1password")
    assert isinstance(reason, str) and reason
