from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.mcp import config as mcp_config
from core.mcp import secrets as mcp_secrets
from core.mcp import writers as mcp_writers
from core.security_policy import (
    DenialCircuitBreaker,
    SecurityPolicyError,
    URLPolicy,
    bounded_output,
    build_child_environment,
    build_execution_command,
    dependency_advisory_plan,
    recovery_advice,
    suggest_approval,
    validate_protected_path,
)


def _resolver(address: str):
    def resolve(_host, port, **_kwargs):
        return [(2, 1, 6, "", (address, port))]

    return resolve


def test_child_environment_is_allowlist_built_and_keeps_named_secrets() -> None:
    inherited = {
        "HOME": "/home/raghav",
        "PATH": "/usr/bin",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
        "NOTES_MODE": "fast",
    }

    result = build_child_environment(
        inherited,
        plain={"FEATURE": "on"},
        secrets={"NOTES_TOKEN": "named-secret"},
        inherit=("NOTES_MODE",),
    )

    assert result == {
        "HOME": "/home/raghav",
        "PATH": os.defpath,
        "NOTES_MODE": "fast",
        "FEATURE": "on",
        "NOTES_TOKEN": "named-secret",
    }
    assert "AWS_SECRET_ACCESS_KEY" not in result


def test_plain_credentials_are_refused() -> None:
    with pytest.raises(SecurityPolicyError, match="named secret reference"):
        build_child_environment({}, plain={"API_TOKEN": "not-allowed"})
    assert build_child_environment({}, plain={"MONKEY_MODE": "calm"}) == {"MONKEY_MODE": "calm"}
    with pytest.raises(SecurityPolicyError, match="alter child execution"):
        build_child_environment({}, plain={"LD_PRELOAD": "/tmp/inject.so"})
    with pytest.raises(SecurityPolicyError, match="alter child execution"):
        build_child_environment(
            {"PYTHONPATH": "/tmp/inject"}, inherit=("PYTHONPATH",)
        )
    with pytest.raises(SecurityPolicyError, match="alter child execution"):
        build_child_environment({}, secrets={"LD_PRELOAD": "named-secret"})


def test_subscription_environment_is_preserved_only_for_native_opt_in() -> None:
    inherited = {"CLAUDE_CODE_OAUTH_TOKEN": "subscription", "OPENAI_API_KEY": "metered"}
    assert build_child_environment(inherited) == {}
    allowed = build_child_environment(inherited, preserve_subscription=True)
    assert allowed == {"CLAUDE_CODE_OAUTH_TOKEN": "subscription"}


def test_url_policy_blocks_private_resolution_and_validates_redirects() -> None:
    policy = URLPolicy(allowed_domains=("example.com",))
    decision = policy.validate("https://api.example.com/mcp", resolver=_resolver("93.184.216.34"))
    assert decision.hostname == "api.example.com"

    with pytest.raises(SecurityPolicyError, match="non-public"):
        policy.validate("https://api.example.com/mcp", resolver=_resolver("127.0.0.1"))
    with pytest.raises(SecurityPolicyError, match="not allowlisted"):
        policy.validate_redirect(
            "https://api.example.com/mcp",
            "https://attacker.invalid/steal",
            resolver=_resolver("93.184.216.34"),
        )


def test_private_network_requires_explicit_opt_in() -> None:
    policy = URLPolicy(allowed_domains=("localhost",), allow_private_network=True, allow_http=True)
    decision = policy.validate("http://localhost:9000/mcp", resolver=_resolver("127.0.0.1"))
    assert decision.private_opt_in is True


def test_url_credentials_and_unresolved_hosts_fail_closed() -> None:
    policy = URLPolicy(allowed_domains=("example.com",))
    with pytest.raises(SecurityPolicyError, match="credentials"):
        policy.validate("https://user:pass@example.com/mcp", resolver=_resolver("93.184.216.34"))

    def unavailable(*_args, **_kwargs):
        raise OSError("dns down")

    with pytest.raises(SecurityPolicyError, match="could not be resolved"):
        policy.validate("https://example.com/mcp", resolver=unavailable)
    with pytest.raises(SecurityPolicyError, match="allowlist is required"):
        URLPolicy().validate("https://example.com/mcp", resolver=_resolver("93.184.216.34"))
    with pytest.raises(SecurityPolicyError, match="plain HTTP"):
        URLPolicy(
            allowed_domains=("example.com",), allow_private_network=True
        ).validate("http://example.com/mcp", resolver=_resolver("93.184.216.34"))


def test_protected_paths_are_refused_even_inside_an_allowed_root(tmp_path: Path) -> None:
    safe = validate_protected_path(tmp_path / "notes.txt", allowed_roots=(tmp_path,))
    assert safe == (tmp_path / "notes.txt").resolve()
    with pytest.raises(SecurityPolicyError, match="protected"):
        validate_protected_path(tmp_path / ".ssh" / "id_rsa", allowed_roots=(tmp_path,))
    with pytest.raises(SecurityPolicyError, match="outside"):
        validate_protected_path(tmp_path.parent / "elsewhere", allowed_roots=(tmp_path,))


def test_approval_suggestions_never_grant_authority() -> None:
    suggestion = suggest_approval("deploy", "production write", ("tool.invoke",))
    assert suggestion.requires_explicit_approval is True
    assert suggestion.auto_granted is False


def test_consecutive_denials_trip_until_explicit_approval(tmp_path: Path) -> None:
    breaker = DenialCircuitBreaker(tmp_path / "security.sqlite3", threshold=3)
    assert breaker.record_denial("mcp:deploy")["tripped"] is False
    assert breaker.record_denial("mcp:deploy")["tripped"] is False
    assert breaker.record_denial("mcp:deploy")["tripped"] is True
    assert DenialCircuitBreaker(tmp_path / "security.sqlite3").is_tripped("mcp:deploy")
    breaker.record_explicit_approval("mcp:deploy")
    assert breaker.is_tripped("mcp:deploy") is False
    breaker.record_denial("mcp:preview")
    assert breaker.record_success("mcp:preview") is True
    assert breaker.is_tripped("mcp:preview") is False


def test_dependency_advisories_are_bounded_plans_not_installers(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    plans = dependency_advisory_plan(tmp_path)
    assert {item["ecosystem"] for item in plans} == {"python", "npm"}
    assert all("install" not in item["command"] for item in plans)


def test_risky_execution_needs_opt_in_and_never_wraps_subscription_clis(tmp_path: Path) -> None:
    assert build_execution_command(["codex", "exec"], mode="native", project_root=tmp_path) == [
        "codex",
        "exec",
    ]
    with pytest.raises(SecurityPolicyError, match="cannot be wrapped"):
        build_execution_command(
            ["claude", "-p", "hi"],
            mode="container",
            project_root=tmp_path,
            explicit_opt_in=True,
            container_image="python@sha256:abc",
        )
    with pytest.raises(SecurityPolicyError, match="explicit opt-in"):
        build_execution_command(["pytest"], mode="sandbox", project_root=tmp_path)
    command = build_execution_command(
        ["pytest", "-q"],
        mode="container",
        project_root=tmp_path,
        explicit_opt_in=True,
        container_image="python@sha256:" + "a" * 64,
    )
    assert "--network=none" in command
    assert command[-2:] == ["pytest", "-q"]
    with pytest.raises(SecurityPolicyError, match="broad filesystem root"):
        build_execution_command(
            ["pytest"],
            mode="sandbox",
            project_root=Path.home(),
            explicit_opt_in=True,
        )


def test_output_recovery_is_actionable_and_credentials_are_filtered() -> None:
    output = bounded_output("API_TOKEN=top-secret\n" + "x" * 3_000, limit=1_000)
    assert output["truncated"] is True
    assert "top-secret" not in output["text"]
    assert "never repeat a write" in output["recovery"]
    assert recovery_advice("timed out", idempotent=False)["automatic_retry"] is False
    assert recovery_advice("timed out", idempotent=True)["automatic_retry"] is True
    assert "do not repeat" in recovery_advice("", idempotent=False, truncated=True)["action"]


def test_mcp_config_persists_explicit_environment_and_url_policy(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "mcp.json"
    monkeypatch.setattr(mcp_config, "CONFIG_PATH", path)
    record = mcp_config.upsert_server(
        {
            "name": "local-notes",
            "transport": "http",
            "url": "http://127.0.0.1:9000/mcp",
            "allowed_domains": ["127.0.0.1"],
            "allow_private_network": True,
            "allow_http": True,
            "env": {"NOTES_MODE": "safe"},
            "secrets": ["NOTES_TOKEN"],
            "headers": {"Authorization": "Bearer ${NOTES_TOKEN}"},
        }
    )
    assert record["env_allowlist"] == ["NOTES_MODE", "NOTES_TOKEN"]
    assert record["allow_private_network"] is True
    assert record["allow_http"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["servers"]["local-notes"]


def test_mcp_config_rejects_inline_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_config, "CONFIG_PATH", tmp_path / "mcp.json")
    with pytest.raises(ValueError, match="named secret"):
        mcp_config.upsert_server(
            {
                "name": "bad",
                "transport": "stdio",
                "command": "demo",
                "env": {"API_TOKEN": "inline"},
            }
        )

    with pytest.raises(ValueError, match="alter child execution"):
        mcp_config.upsert_server(
            {
                "name": "unsafe-env",
                "transport": "stdio",
                "command": "demo",
                "env_allowlist": ["LD_PRELOAD"],
            }
        )
    with pytest.raises(ValueError, match="alter child execution"):
        mcp_config.upsert_server(
            {
                "name": "unsafe-secret-env",
                "transport": "stdio",
                "command": "demo",
                "secrets": ["LD_PRELOAD"],
            }
        )

    with pytest.raises(ValueError, match="protected"):
        mcp_config.upsert_server(
            {
                "name": "protected-cwd",
                "transport": "stdio",
                "command": "demo",
                "cwd": str(Path.home() / ".ssh"),
            }
        )

    with pytest.raises(ValueError, match="undeclared secrets"):
        mcp_config.upsert_server(
            {
                "name": "bad-header",
                "transport": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${MISSING_TOKEN}"},
            }
        )

    with pytest.raises(ValueError, match="may contain only"):
        mcp_config.upsert_server(
            {
                "name": "mixed-header",
                "transport": "http",
                "url": "https://example.com/mcp",
                "secrets": ["NOTES_TOKEN"],
                "headers": {"Authorization": "Bearer ${NOTES_TOKEN} inline"},
            }
        )


def test_mcp_server_environment_does_not_inherit_ambient_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_secrets,
        "get_secret",
        lambda server, name: "resolved" if (server, name) == ("notes", "NOTES_TOKEN") else None,
    )
    result = mcp_secrets.server_environment(
        {
            "name": "notes",
            "env": {"NOTES_MODE": "safe"},
            "secrets": ["NOTES_TOKEN"],
            "env_allowlist": ["NOTES_MODE", "NOTES_TOKEN"],
        },
        inherited={"PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "ambient"},
    )
    assert result == {
        "PATH": os.defpath,
        "NOTES_MODE": "safe",
        "NOTES_TOKEN": "resolved",
    }

    monkeypatch.setattr(mcp_secrets, "get_secret", lambda _server, _name: None)
    with pytest.raises(SecurityPolicyError, match="unavailable"):
        mcp_secrets.server_environment({"name": "notes", "secrets": ["NOTES_TOKEN"]}, inherited={})


def test_mcp_headers_resolve_only_declared_secret_placeholders(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_secrets,
        "get_secret",
        lambda server, name: (
            "private-value" if (server, name) == ("notes", "NOTES_TOKEN") else None
        ),
    )
    server = {
        "name": "notes",
        "secrets": ["NOTES_TOKEN"],
        "headers": {"Authorization": "Bearer ${NOTES_TOKEN}"},
    }

    assert mcp_secrets.resolved_headers(server) == {"Authorization": "Bearer private-value"}
    assert "private-value" not in json.dumps(server)

    monkeypatch.setattr(mcp_secrets, "get_secret", lambda _server, _name: None)
    with pytest.raises(SecurityPolicyError, match="unavailable"):
        mcp_secrets.resolved_headers(server)


def test_native_mcp_config_writer_filters_unrelated_credentials(monkeypatch) -> None:
    observed = {}

    def run(argv, **kwargs):
        observed.update(kwargs["env"])

        class Completed:
            returncode = 0
            stdout = "configured"
            stderr = ""

        return Completed()

    monkeypatch.setattr(mcp_writers.subprocess, "run", run)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unrelated-secret")

    ok, message = mcp_writers._run(["claude", "mcp", "remove", "notes"])

    assert ok is True and message == "configured"
    assert observed["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription-token"
    assert "AWS_SECRET_ACCESS_KEY" not in observed


def test_native_mcp_config_writer_redacts_subscription_token(monkeypatch) -> None:
    class Completed:
        returncode = 1
        stdout = ""
        stderr = "subscription-token"

    monkeypatch.setattr(mcp_writers.subprocess, "run", lambda *_args, **_kwargs: Completed())
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-token")

    ok, message = mcp_writers._run(["claude", "mcp", "remove", "notes"])

    assert ok is False
    assert message == "[REDACTED]"
