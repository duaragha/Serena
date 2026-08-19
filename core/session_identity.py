"""Exact launching-session discovery for Serena's long-lived local services."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence

import psutil

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_ENV_KEYS = {
    "codex": ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_COMPANION_SESSION_ID"),
    "claude": ("CLAUDE_CODE_SESSION_ID",),
}


def resolve_origin_session(
    session_id: str | None,
    agent: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    ancestors: Iterable[tuple[Mapping[str, str], Sequence[str]]] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve an exact origin without guessing from recency or cwd.

    MCP servers can outlive the chat that invokes them, so their own
    environment may not contain the current session id. Provider-filtered
    parent environments and resume argv are safe exact fallbacks.
    """

    clean_id = _clean(session_id)
    clean_agent = _clean(agent)
    if clean_agent not in {None, "claude", "codex"}:
        clean_agent = None
    contexts = [(environ if environ is not None else os.environ, ())]
    contexts.extend(list(ancestors) if ancestors is not None else _ancestor_contexts())

    if clean_id:
        if clean_agent is None:
            clean_agent = _agent_for_id(clean_id, contexts)
        return clean_id, clean_agent

    providers = (clean_agent,) if clean_agent else ("codex", "claude")
    for provider in providers:
        assert provider is not None
        for context_env, _argv in contexts:
            for key in _ENV_KEYS[provider]:
                candidate = _clean(context_env.get(key))
                if candidate:
                    return candidate, provider
        for _context_env, argv in contexts[1:]:
            candidate = _session_from_argv(provider, argv)
            if candidate:
                return candidate, provider
    return None, clean_agent


def _ancestor_contexts(limit: int = 12) -> list[tuple[Mapping[str, str], Sequence[str]]]:
    contexts: list[tuple[Mapping[str, str], Sequence[str]]] = []
    try:
        process = psutil.Process(os.getpid()).parent()
    except psutil.Error:
        return contexts
    for _ in range(max(1, int(limit))):
        if process is None:
            break
        try:
            contexts.append((process.environ(), process.cmdline()))
            process = process.parent()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            break
    return contexts


def _agent_for_id(
    session_id: str,
    contexts: Sequence[tuple[Mapping[str, str], Sequence[str]]],
) -> str | None:
    for provider, keys in _ENV_KEYS.items():
        for context_env, argv in contexts:
            if any(session_id == _clean(context_env.get(key)) for key in keys):
                return provider
            if session_id == _session_from_argv(provider, argv):
                return provider
    return None


def _session_from_argv(provider: str, argv: Sequence[str]) -> str | None:
    values = [str(value) for value in argv]
    if provider == "codex":
        for index, value in enumerate(values[:-2]):
            if value.rsplit("/", 1)[-1] == "codex" and values[index + 1] == "resume":
                candidate = values[index + 2]
                if _UUID.fullmatch(candidate):
                    return candidate
        return None
    for index, value in enumerate(values):
        if value in {"-r", "--resume"} and index + 1 < len(values):
            candidate = values[index + 1]
            if _UUID.fullmatch(candidate):
                return candidate
        if value.startswith("--resume="):
            candidate = value.split("=", 1)[1]
            if _UUID.fullmatch(candidate):
                return candidate
    return None


def _clean(value: object) -> str | None:
    clean = str(value or "").strip()
    return clean or None
