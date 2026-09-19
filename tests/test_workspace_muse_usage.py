"""Subscription limits are account snapshots, not context/token estimates."""

import copy
import io
import json
import threading
import time
import urllib.error

import pytest

from core import muse_usage_reader as reader


@pytest.fixture
def snapshot():
    return {"subs_usage": {
        "window": {"used_percent": 0, "window_duration_mins": 300, "resets_at": 2000},
        "weekly": {"used_percent": 9, "resets_at": 9000},
        "tier": "native-tier",
    }, "api_key": "never-expose-this", "user_email": "private@example.test"}


@pytest.fixture(autouse=True)
def isolated_reader(monkeypatch, tmp_path):
    auth = tmp_path / "muse" / "auth.json"
    auth.parent.mkdir()
    auth.write_text(json.dumps({"providers": {"meta": {
        "mechanism": "oauth", "access_token": "test-secret",
    }}}), encoding="utf-8")
    monkeypatch.setenv("MUSE_AUTH_PATH", str(auth))
    monkeypatch.setattr(reader, "_CACHE", {"at": 0.0, "data": None})
    monkeypatch.setattr(reader, "_REFRESHING", False)
    monkeypatch.setattr(reader, "_binary", lambda: "/fake/muse")
    yield auth
    assert not reader._REFRESHING


def test_native_snapshot_preserves_zero_weekly_reset_and_timestamp(snapshot):
    result = reader.parse_usage(json.dumps(snapshot), now=1000)
    assert result["available"]
    assert result["five_hour"] == {"used_percentage": 0, "resets_at": 2000}
    assert result["seven_day"] == {"used_percentage": 9, "resets_at": 9000}
    assert result["window_minutes"] == 300
    assert result["updated_at"] == 1000
    assert "never-expose-this" not in json.dumps(result)
    assert "private@example.test" not in json.dumps(result)


@pytest.mark.parametrize("text", ["", "not json", "null", "[]", "{}", '{"subs_usage":null}'])
def test_absent_usage_is_not_zero(text):
    result = reader.parse_usage(text)
    assert not result["available"]
    assert "five_hour" not in result
    assert "updated_at" not in result


@pytest.mark.parametrize("field,value", [
    ("used_percent", -1), ("used_percent", True), ("used_percent", "12"),
    ("resets_at", 0), ("resets_at", None),
    ("window_duration_mins", 0), ("window_duration_mins", True),
])
def test_malformed_windows_are_not_healthy(snapshot, field, value):
    snapshot["subs_usage"]["window"][field] = value
    assert not reader.parse_usage(json.dumps(snapshot))["available"]


def test_over_quota_and_nonstandard_duration_are_not_clamped(snapshot):
    snapshot["subs_usage"]["window"].update(used_percent=120, window_duration_mins=180)
    result = reader.parse_usage(json.dumps(snapshot))
    assert result["five_hour"]["used_percentage"] == 120
    assert result["window_minutes"] == 180


def test_account_exchange_only_retains_usage(monkeypatch, snapshot, isolated_reader):
    original = isolated_reader.read_bytes()
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.full_url == "https://api.meta.ai/muse-code/key"
            assert request.get_method() == "POST"
            assert request.data == b"{}"
            assert request.get_header("Authorization") == "Bearer test-secret"
            assert timeout == 8
            return io.BytesIO(json.dumps(snapshot).encode())

    monkeypatch.setattr(reader.urllib.request, "build_opener", lambda handler: Opener())
    result = reader.read_muse_usage(now=1000, force=True)
    assert result["available"]
    assert reader.read_muse_usage(now=1100) == result
    assert len(calls) == 1
    assert isolated_reader.read_bytes() == original
    result["five_hour"]["used_percentage"] = 99
    assert reader.read_muse_usage(now=1101)["five_hour"]["used_percentage"] == 0
    assert "test-secret" not in json.dumps(reader._CACHE)
    assert "never-expose-this" not in json.dumps(reader._CACHE)


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_http_errors_are_safe_and_keep_last_observation(monkeypatch, snapshot, status):
    original_fetch = reader._fetch_usage
    monkeypatch.setattr(reader, "_fetch_usage", lambda moment: reader.parse_usage(json.dumps(snapshot), now=moment))
    reader.read_muse_usage(now=1000, force=True)
    monkeypatch.setattr(reader, "_fetch_usage", original_fetch)

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, status, "private-secret", {}, io.BytesIO(b"secret"))

    monkeypatch.setattr(reader.urllib.request, "build_opener", lambda handler: Opener())
    result = reader.read_muse_usage(now=1301, force=True)
    assert result["available"] and result["stale"]
    assert result["updated_at"] == 1000
    assert "secret" not in json.dumps(result)
    assert ("sign-in" in result["reason"]) == (status in (401, 403))


def test_failure_keeps_timestamp_and_retries(monkeypatch, snapshot):
    observation = reader.parse_usage(json.dumps(snapshot), now=1000)
    calls = []

    def fetch(moment):
        calls.append(moment)
        if len(calls) == 1:
            return observation
        raise RuntimeError("Authorization: secret must not escape")

    monkeypatch.setattr(reader, "_fetch_usage", fetch)
    reader.read_muse_usage(now=1000, force=True)
    stale = reader.read_muse_usage(now=1301, force=True)
    assert stale["available"] and stale["stale"]
    assert stale["updated_at"] == 1000
    assert "secret" not in json.dumps(stale)
    assert reader._CACHE["retry"]
    assert reader.read_muse_usage(now=1320) == stale
    assert calls == [1000, 1301]


def test_slow_refresh_is_nonblocking_and_single_flight(monkeypatch, snapshot):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    refresh = reader._refresh

    def fetch(moment):
        calls.append(moment)
        started.set()
        assert release.wait(3)
        return reader.parse_usage(json.dumps(snapshot), now=moment)

    def finish(*args):
        try:
            refresh(*args)
        finally:
            finished.set()

    monkeypatch.setattr(reader, "_fetch_usage", fetch)
    monkeypatch.setattr(reader, "_refresh", finish)
    try:
        before = time.monotonic()
        assert reader.read_muse_usage(now=1000)["loading"]
        assert time.monotonic() - before < 0.2
        assert started.wait(1)
        for _ in range(10):
            assert reader.read_muse_usage(now=1000)["loading"]
        assert calls == [1000]
    finally:
        release.set()
        assert finished.wait(3)
    assert reader.read_muse_usage(now=1001)["available"]


def test_changed_account_cannot_reuse_previous_quota(monkeypatch, snapshot, isolated_reader):
    monkeypatch.setattr(reader, "_fetch_usage", lambda moment: reader.parse_usage(json.dumps(snapshot), now=moment))
    assert reader.read_muse_usage(now=1000, force=True)["available"]
    isolated_reader.unlink()
    monkeypatch.setattr(reader, "_fetch_usage", lambda moment: reader._unavailable("Sign in"))
    result = reader.read_muse_usage(now=1001, force=True)
    assert not result["available"]
    assert "five_hour" not in result


def test_redirect_is_never_followed():
    assert reader._NoRedirect().redirect_request(None, None, 307, "", {}, "https://other.test") is None


@pytest.mark.parametrize("failure", [TimeoutError("secret"), urllib.error.URLError("secret")])
def test_network_failure_does_not_escape_or_report_zero(monkeypatch, failure):
    class Opener:
        def open(self, request, timeout):
            raise failure

    monkeypatch.setattr(reader.urllib.request, "build_opener", lambda handler: Opener())
    result = reader.read_muse_usage(now=1000, force=True)
    assert not result["available"]
    assert "five_hour" not in result
    assert "secret" not in json.dumps(result)


def test_success_after_failure_clears_staleness(monkeypatch, snapshot):
    monkeypatch.setattr(reader, "_fetch_usage", lambda moment: reader.parse_usage(json.dumps(snapshot), now=moment))
    reader.read_muse_usage(now=1000, force=True)
    reader._CACHE["data"]["stale"] = True
    reader._CACHE["data"]["reason"] = "Offline"
    result = reader.read_muse_usage(now=1500, force=True)
    assert result["available"] and not result.get("stale")
    assert "reason" not in result
    assert result["updated_at"] == 1500


def test_auth_change_during_request_discards_observation(monkeypatch, snapshot, isolated_reader):
    def fetch(moment):
        isolated_reader.unlink()
        return reader.parse_usage(json.dumps(snapshot), now=moment)

    monkeypatch.setattr(reader, "_fetch_usage", fetch)
    assert not reader.read_muse_usage(now=1000, force=True)["available"]
    assert reader._CACHE["data"] is None


def test_xdg_auth_discovery(monkeypatch, tmp_path):
    monkeypatch.delenv("MUSE_AUTH_PATH")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert reader._auth_path() == tmp_path / "muse" / "auth.json"


def test_api_preserves_muse_values_and_staleness(monkeypatch, snapshot):
    from ui import web

    observation = reader.parse_usage(json.dumps(snapshot), now=time.time())
    observation["stale"] = True
    monkeypatch.setattr(reader, "read_muse_usage", lambda **kwargs: copy.deepcopy(observation))
    monkeypatch.setattr(web, "_LIVE_USAGE_CACHE", {"at": 0.0, "data": None})
    monkeypatch.setattr(web, "_read_live_usage_state", lambda: {})
    monkeypatch.setattr(web, "_latest_codex_usage", lambda: {})
    monkeypatch.setattr(web, "read_gemini_usage", lambda **kwargs: {})
    monkeypatch.setitem(web.app.extensions, "workspace_host", None)
    result = web.app.test_client().get("/api/live-usage").get_json()["muse"]
    assert result["five_hour"]["used_percentage"] == 0
    assert result["seven_day"]["used_percentage"] == 9
    assert result["window_minutes"] == 300
    assert result["stale"]
