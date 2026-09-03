from core.billing import (
    METERED_AUTH_ENV_VARS,
    present_metered_auth_env,
    strip_metered_auth_env,
)


def test_metered_provider_overrides_are_detected_and_stripped() -> None:
    environment = {name: "configured" for name in METERED_AUTH_ENV_VARS}
    environment["CLAUDE_CODE_OAUTH_TOKEN"] = "subscription-token"

    assert present_metered_auth_env(environment) == sorted(METERED_AUTH_ENV_VARS)
    stripped = strip_metered_auth_env(environment)
    assert set(METERED_AUTH_ENV_VARS).isdisjoint(stripped)
    assert stripped["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription-token"


def test_empty_override_values_do_not_block_subscription_oauth() -> None:
    environment = {name: "" for name in METERED_AUTH_ENV_VARS}
    assert present_metered_auth_env(environment) == []


def test_future_provider_routing_names_fail_closed() -> None:
    environment = {
        "ANTHROPIC_FUTURE_GATEWAY": "configured",
        "CLAUDE_CODE_FUTURE_GATEWAY": "configured",
        "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token",
    }
    assert present_metered_auth_env(environment) == [
        "ANTHROPIC_FUTURE_GATEWAY",
        "CLAUDE_CODE_FUTURE_GATEWAY",
    ]
    assert strip_metered_auth_env(environment) == {"CLAUDE_CODE_OAUTH_TOKEN": "subscription-token"}


def test_all_non_oauth_claude_code_variables_fail_closed() -> None:
    environment = {
        "CLAUDE_CODE_USE_ANTHROPIC_AWS": "1",
        "CLAUDE_CODE_FUTURE_PROVIDER": "configured",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "subscription-refresh",
    }
    assert present_metered_auth_env(environment) == [
        "CLAUDE_CODE_FUTURE_PROVIDER",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    ]
    assert strip_metered_auth_env(environment) == {
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "subscription-refresh"
    }
