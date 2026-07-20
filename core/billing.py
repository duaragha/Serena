"""Shared fail-closed subscription billing environment rules."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from typing import Any

METERED_AUTH_ENV_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
    "CODEX_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_AWS_BASE_URL",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_API_BASE_URL",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL",
    "CLAUDE_CODE_CUSTOM_OAUTH_URL",
    "CLAUDE_CODE_ENABLE_PROXY_AUTH_HELPER",
    "CLAUDE_CODE_GB_BASE_URL",
    "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
    "CLAUDE_CODE_HTTP_PROXY",
    "CLAUDE_CODE_HTTPS_PROXY",
    "CLAUDE_CODE_PROXY_AUTHENTICATE",
    "CLAUDE_CODE_PROXY_URL",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_VERTEX",
)

SUBSCRIPTION_OAUTH_ENV_VARS = frozenset(
    {
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    }
)
_SECRET_ENV_MARKERS = (
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "KEY",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)


def _is_metered_auth_env(name: str, value: str) -> bool:
    if not value or name in SUBSCRIPTION_OAUTH_ENV_VARS:
        return False
    if name in METERED_AUTH_ENV_VARS:
        return True
    if name.startswith(("ANTHROPIC_", "FOUNDRY_", "VERTEX_")):
        return True
    # Claude Code adds provider routes faster than this project can safely
    # enumerate them. The zero-metered-cost lane therefore treats every
    # nonempty CLAUDE_CODE_* variable as a routing/auth override unless it is
    # one of the three exact subscription OAuth transports above.
    return name.startswith("CLAUDE_CODE_")


def present_metered_auth_env(environ: Mapping[str, str]) -> list[str]:
    """Return non-secret names of configured metered-provider overrides."""

    return sorted(name for name, value in environ.items() if _is_metered_auth_env(name, value))


def strip_metered_auth_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Copy an environment without any metered-provider override."""

    return {key: value for key, value in environ.items() if not _is_metered_auth_env(key, value)}


def command_fingerprint(command: Sequence[str]) -> str:
    """Hash the exact argv without persisting prompt or command text."""

    encoded = json.dumps(list(command), ensure_ascii=False, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def environment_fingerprint(environ: Mapping[str, str]) -> str:
    """Hash a stable, secret-redacted view of one worker environment."""

    redacted = {
        name: (
            "<redacted-present>"
            if any(marker in name.upper() for marker in _SECRET_ENV_MARKERS)
            else value
        )
        for name, value in sorted(environ.items())
    }
    encoded = json.dumps(redacted, ensure_ascii=False, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def subscription_auth_evidence(
    *,
    environ: Mapping[str, str] | None = None,
    command: Sequence[str] = ("claude", "auth", "status"),
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Return only non-secret evidence for first-party Max subscription auth."""

    env = dict(os.environ if environ is None else environ)
    present = present_metered_auth_env(env)
    if present:
        return {
            "ok": False,
            "api_key_present": "ANTHROPIC_API_KEY" in present,
            "metered_auth_env_present": present,
            "failures": ["metered-provider auth is set in the audit environment"],
        }
    try:
        completed = runner(
            list(command),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "api_key_present": False,
            "metered_auth_env_present": [],
            "failures": [f"Claude subscription auth could not be verified: {exc}"],
        }
    if not isinstance(payload, dict):
        payload = {}
    safe = {
        "logged_in": payload.get("loggedIn") is True,
        "auth_method": str(payload.get("authMethod") or ""),
        "api_provider": str(payload.get("apiProvider") or ""),
        "subscription_type": str(payload.get("subscriptionType") or "").lower(),
        "api_key_present": False,
        "metered_auth_env_present": [],
    }
    failures: list[str] = []
    if completed.returncode != 0:
        failures.append("claude auth status returned a nonzero exit code")
    if not safe["logged_in"]:
        failures.append("Claude is not logged in")
    if safe["auth_method"] != "claude.ai":
        failures.append("Claude auth is not subscription OAuth")
    if safe["api_provider"] != "firstParty":
        failures.append("Claude is not using the first-party subscription provider")
    if safe["subscription_type"] != "max":
        failures.append("Claude Max subscription status is not verified")
    return {"ok": not failures, **safe, "failures": failures}
