from __future__ import annotations

from core.session_identity import resolve_origin_session

CODEX_SID = "019fc491-f1d5-7fb2-8aba-b8e4846b1e82"
CLAUDE_SID = "979a60b0-1111-4222-8333-123456789abc"


def test_explicit_origin_always_wins() -> None:
    assert resolve_origin_session(
        "explicit-session",
        "codex",
        environ={"CODEX_THREAD_ID": CODEX_SID},
        ancestors=[],
    ) == ("explicit-session", "codex")


def test_provider_filtered_parent_environment_recovers_codex_origin() -> None:
    resolved = resolve_origin_session(
        None,
        "codex",
        environ={},
        ancestors=[({"CODEX_THREAD_ID": CODEX_SID}, ["codex", "mcp-server"])],
    )
    assert resolved == (CODEX_SID, "codex")


def test_codex_resume_argv_is_an_exact_fallback() -> None:
    resolved = resolve_origin_session(
        None,
        "codex",
        environ={},
        ancestors=[({}, ["/usr/local/bin/codex", "resume", CODEX_SID])],
    )
    assert resolved == (CODEX_SID, "codex")


def test_provider_filter_never_captures_the_opposite_host() -> None:
    assert resolve_origin_session(
        None,
        "claude",
        environ={"CODEX_THREAD_ID": CODEX_SID},
        ancestors=[({"CODEX_THREAD_ID": CODEX_SID}, ["codex", "resume", CODEX_SID])],
    ) == (None, "claude")
    assert resolve_origin_session(
        None,
        "claude",
        environ={},
        ancestors=[({"CLAUDE_CODE_SESSION_ID": CLAUDE_SID}, ["claude"])],
    ) == (CLAUDE_SID, "claude")
