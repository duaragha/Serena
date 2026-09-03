"""Resolve the intended opposite-agent session inside a linked group."""

from __future__ import annotations

from collections.abc import Callable


def _open_split_target(source_sid: str, candidates: set[str]) -> str | None:
    """Prefer the target visibly paired with ``source_sid`` in this app."""
    try:
        from desktop.app_gtk import ChatsApp
    except Exception:
        return None

    inst = ChatsApp.INSTANCE
    if inst is None or not getattr(inst, "_split_active", False):
        return None
    split_sids = tuple(sid for sid in getattr(inst, "_split_sids", ()) if sid)
    if source_sid not in split_sids:
        return None
    return next((sid for sid in split_sids if sid in candidates), None)


def find_linked_session(
    source_sid: str,
    target_agent: str,
    *,
    fallback_match: Callable[[str], bool] | None = None,
) -> str | None:
    """Return the linked target, preferring the pair open in the current app.

    Groups can contain stale or duplicate members after metadata sync. The
    visible split is the user's explicit pairing. Outside a visible split,
    choose the most recently active matching session instead of JSON order.
    """
    try:
        from core import metadata as meta
        from core.indexer import get_session
    except ImportError:
        return None

    gid = meta.get_group(source_sid)
    if not gid:
        return None

    target_agent = target_agent.lower()
    candidates: list[tuple[str, dict]] = []
    for member in meta.list_group_members(gid):
        if member == source_sid:
            continue
        if meta.get_meta(member).get("fleet_worker"):
            continue
        if meta.external_runtime_active(member):
            continue
        session = get_session(member)
        matches_index = bool(
            session and (session.get("agent") or "").lower() == target_agent
        )
        matches_fallback = bool(fallback_match and fallback_match(member))
        if matches_index or matches_fallback:
            candidates.append((member, session or {}))

    if not candidates:
        return None

    visible = _open_split_target(source_sid, {sid for sid, _ in candidates})
    if visible:
        return visible

    candidates.sort(
        key=lambda item: (item[1].get("last_timestamp") or "", item[0]),
        reverse=True,
    )
    return candidates[0][0]
