from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path

import pytest

from core import fleet_capacity

NOW = 2_000_000_000.0


def _write_usage(path: Path, claude: dict) -> None:
    path.write_text(json.dumps({"updated_at": NOW, "claude": claude}), encoding="utf-8")


def _read(
    monkeypatch,
    usage_path: Path,
    *,
    codex_limits: dict | None,
    extra: dict[str, str] | None = None,
):
    monkeypatch.setattr(
        fleet_capacity,
        "_read_codex_app_server",
        lambda _environ: codex_limits,
    )
    # These fixtures exercise quota signals with installed providers. Use an
    # existing binary without depending on which CLIs happen to be on the host;
    # the Codex RPC is mocked above and no provider executable is launched.
    environ = {
        "SERENA_FLEET_LIVE_USAGE_PATH": str(usage_path),
        **{
            f"SERENA_FLEET_{provider}_BIN": sys.executable
            for provider in ("CODEX", "CLAUDE", "MUSE")
        },
    }
    environ.update(extra or {})
    return fleet_capacity.read_fleet_capacity(now=NOW, environ=environ)


def test_fresh_claude_exhaustion_and_codex_headroom_are_detected(tmp_path, monkeypatch):
    monkeypatch.setattr("fleet.workers.shutil.which", lambda _name: None)
    usage = tmp_path / "live-usage.json"
    _write_usage(
        usage,
        {
            "available": True,
            "updated_at": NOW - 5,
            "five_hour": {
                "used_percentage": 111,
                "resets_at": NOW + 900,
                "observed_at": NOW - 5,
            },
            "seven_day": {
                "used_percentage": 40,
                "resets_at": NOW + 86_400,
                "observed_at": NOW - 5,
            },
        },
    )

    states = _read(
        monkeypatch,
        usage,
        codex_limits={
            "primary": {"usedPercent": 79, "resetsAt": NOW + 86_400},
            "credits": {"hasCredits": False, "balance": "0"},
            "rateLimitReachedType": None,
            "spendControlReached": False,
        },
    )

    assert set(states) == {"codex", "claude", "muse"}
    assert states["claude"].status == "unavailable"
    assert states["claude"].usable is False
    assert states["claude"].used_percent == 111
    assert states["codex"].status == "available"
    assert states["codex"].usable is True
    assert states["codex"].used_percent == 79
    assert states["muse"].status == "unknown"
    assert states["muse"].usable is True


def test_stale_or_expired_claude_observation_never_blocks(tmp_path, monkeypatch):
    usage = tmp_path / "live-usage.json"
    _write_usage(
        usage,
        {
            "updated_at": NOW - 31,
            "five_hour": {
                "used_percentage": 105,
                "resets_at": NOW + 900,
                "observed_at": NOW - 31,
            },
        },
    )
    stale = _read(monkeypatch, usage, codex_limits=None)
    assert stale["claude"].status == "unknown"
    assert stale["claude"].usable is True

    _write_usage(
        usage,
        {
            "updated_at": NOW - 1,
            "five_hour": {
                "used_percentage": 105,
                "resets_at": NOW - 1,
                "observed_at": NOW - 1,
            },
        },
    )
    expired = _read(monkeypatch, usage, codex_limits=None)
    assert expired["claude"].status == "available"
    assert expired["claude"].usable is True


@pytest.mark.parametrize(
    ("limits", "reason_fragment"),
    [
        (
            {
                "primary": {"usedPercent": 20, "resetsAt": NOW + 900},
                "rateLimitReachedType": "secondary_window",
            },
            "secondary_window",
        ),
        (
            {
                "primary": {"usedPercent": 20, "resetsAt": NOW + 900},
                "spendControlReached": True,
            },
            "spend control",
        ),
        (
            {
                "primary": {"usedPercent": 100, "resetsAt": NOW + 900},
                "rateLimitReachedType": None,
            },
            "exhausted",
        ),
    ],
)
def test_codex_definitive_limit_signals_block(
    tmp_path,
    monkeypatch,
    limits,
    reason_fragment,
):
    states = _read(monkeypatch, tmp_path / "missing.json", codex_limits=limits)
    codex = states["codex"]
    assert codex.status == "unavailable"
    assert codex.usable is False
    assert reason_fragment in codex.reason


def test_codex_expired_limit_and_missing_credit_balance_do_not_block(tmp_path, monkeypatch):
    states = _read(
        monkeypatch,
        tmp_path / "missing.json",
        codex_limits={
            "primary": {"usedPercent": 100, "resetsAt": NOW - 1},
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "rateLimitReachedType": None,
            "spendControlReached": False,
        },
    )
    assert states["codex"].status == "available"
    assert states["codex"].usable is True


def test_codex_jsonl_fallback_respects_freshness_and_reset(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    rollout = sessions / "2026/01/01/rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2033-05-18T03:33:15Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {
                            "used_percent": 100,
                            "window_minutes": 10080,
                            "resets_at": NOW + 900,
                        },
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fleet_capacity, "_read_codex_app_server", lambda _environ: None)

    states = fleet_capacity.read_fleet_capacity(
        now=NOW,
        environ={
            "SERENA_FLEET_LIVE_USAGE_PATH": str(tmp_path / "missing.json"),
            "SERENA_FLEET_CODEX_SESSIONS_DIR": str(sessions),
            "SERENA_FLEET_CODEX_FRESH_SECONDS": "120",
        },
    )

    assert states["codex"].source == "codex-jsonl"
    assert states["codex"].status == "unavailable"
    assert states["codex"].resets_at == NOW + 900


def test_complete_json_override_is_deterministic_and_truthful():
    override = json.dumps(
        {
            "codex": {
                "status": "unknown",
                "reason": "probe timed out",
                "usable": False,
            },
            "claude": {
                "status": "unavailable",
                "reason": "session limit",
                "used_percent": 100,
                "resets_at": NOW + 60,
            },
        }
    )

    states = fleet_capacity.read_fleet_capacity(
        now=NOW,
        environ={"SERENA_FLEET_CAPACITY_JSON": override},
    )

    assert states["codex"].status == "unknown"
    assert states["codex"].usable is True
    assert states["claude"].status == "unavailable"
    assert states["claude"].usable is False
    assert states["claude"].to_dict()["resets_at"] == NOW + 60


def test_invalid_override_fails_closed_as_configuration_error():
    with pytest.raises(ValueError, match="requires a claude object"):
        fleet_capacity.read_fleet_capacity(
            environ={"SERENA_FLEET_CAPACITY_JSON": json.dumps({"codex": {"status": "available"}})}
        )


# ---- a provider this machine cannot run is not "usable" --------------------


def test_a_provider_whose_cli_is_absent_is_reported_unavailable(monkeypatch):
    """The PC has no muse CLI, and Fleet called muse usable anyway.

    `unknown` is usable by design, so a muse leg was scheduled onto a machine
    that cannot run one and only found out when the worker raised
    FileNotFoundError mid-run. Reported unavailable, the automatic capacity
    handoff routes around it before any work is handed over.
    """

    from fleet import capacity

    monkeypatch.setattr("fleet.workers.provider_binary", lambda *_a, **_k: None)

    for reader, provider in (
        (capacity._read_muse_capacity, "muse"),
        (capacity._read_codex_capacity, "codex"),
        (capacity._read_claude_capacity, "claude"),
    ):
        result = reader(1_000.0, {})
        assert result.usable is False, provider
        assert result.status == "unavailable", provider
        assert "not installed on this machine" in result.reason, provider


def test_capacity_resolves_a_binary_the_same_way_a_worker_will(monkeypatch, tmp_path):
    """They disagreed: capacity asked shutil.which, a worker searched further.

    A provider installed under ~/.local/bin ran perfectly well and was still
    reported missing, because only one of the two knew to look there.
    """

    from fleet import capacity
    from fleet.workers import provider_binary

    monkeypatch.setattr(capacity.shutil, "which", lambda _name: None)
    found = tmp_path / "muse"
    found.write_text("#!/bin/sh\n", encoding="utf-8")
    found.chmod(0o755)

    seen = provider_binary("muse", {"SERENA_FLEET_MUSE_BIN": str(found)})
    assert seen == str(found)
    assert capacity._read_muse_capacity(
        1_000.0, {"SERENA_FLEET_MUSE_BIN": str(found)}
    ).usable is True


def test_an_unknown_provider_is_not_quietly_handed_claude(monkeypatch):
    """A typo in a policy used to run the wrong model instead of saying so."""

    from fleet.workers import provider_binary

    assert provider_binary("gpt-6-astra") is None
    assert provider_binary("") is None


def test_codex_capacity_rpc_launches_the_shared_worker_resolver_result(tmp_path, monkeypatch):
    from fleet import capacity, workers

    environ = {"SERENA_FLEET_CODEX_BIN": str(tmp_path / "npm" / "codex.cmd")}
    native = str(tmp_path / "npm" / "native" / "codex.exe")
    resolved = []
    spawned = []
    written = []
    stdout_eof = threading.Event()
    limits = {"primary": {"usedPercent": 17, "resetsAt": NOW + 900}}

    def resolve(provider, source):
        resolved.append((provider, source))
        return native

    class Input(io.StringIO):
        def write(self, text):
            written.append(text)
            return super().write(text)

    class Output(io.StringIO):
        def readline(self, *args, **kwargs):
            line = super().readline(*args, **kwargs)
            if not line:
                stdout_eof.set()
            return line

    class Process:
        def __init__(self):
            self.stdin = Input()
            self.stdout = Output(json.dumps({"id": 2, "result": {"rateLimits": limits}}) + "\n")

        def poll(self):
            return 0

        def wait(self, **_kwargs):
            # Model process completion only after the reader can observe EOF,
            # so fake stdout cannot close between its response and next read.
            assert stdout_eof.wait(timeout=1)
            return 0

    process = Process()

    def spawn(command, **kwargs):
        spawned.append((command, kwargs))
        return process

    monkeypatch.setattr(workers, "provider_binary", resolve)
    monkeypatch.setattr(capacity.subprocess, "Popen", spawn)

    assert capacity._read_codex_app_server(environ) == limits
    assert resolved == [("codex", environ)]
    assert spawned[0][0] == [native, "app-server", "--listen", "stdio://"]
    messages = [json.loads(line) for line in "".join(written).splitlines()]
    assert [message["method"] for message in messages] == [
        "initialize", "initialized", "account/rateLimits/read"
    ]
    assert process.stdin.closed
    assert process.stdout.closed


def test_codex_capacity_rpc_does_not_spawn_when_shared_resolver_finds_no_binary(monkeypatch):
    from fleet import capacity, workers

    monkeypatch.setattr(workers, "provider_binary", lambda *_args: None)

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("A missing Codex binary must not start a capacity process")

    monkeypatch.setattr(capacity.subprocess, "Popen", unexpected_spawn)

    assert capacity._read_codex_app_server({}) is None


@pytest.mark.parametrize("suffix", [".cmd", ".bat", ".ps1"])
def test_unresolved_windows_codex_shim_never_spawns_and_capacity_stays_unknown(tmp_path, monkeypatch, suffix):
    from fleet import capacity, workers

    shim = str(tmp_path / f"codex{suffix}")
    environ = {"SERENA_FLEET_CODEX_SESSIONS_DIR": str(tmp_path / "no-sessions")}
    monkeypatch.setattr(workers, "provider_binary", lambda *_args: shim)
    monkeypatch.setattr(workers, "_is_windows", lambda: True)

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("An unresolved Windows shim must not create a capacity process")

    monkeypatch.setattr(capacity.subprocess, "Popen", unexpected_spawn)

    assert capacity._read_codex_app_server(environ) is None
    state = capacity._read_codex_capacity(NOW, environ)
    assert state.status == "unknown"
    assert state.usable is True
