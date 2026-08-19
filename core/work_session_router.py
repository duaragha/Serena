"""Choose an existing Chats session for spoken coding work when that is safe.

Selection and execution are deliberately separate. This module freezes one
exact session id at acceptance time; the resident supervisor or bridge may not
guess again from a title, linked-group membership, or later recency.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import psutil

RouteMode = Literal["private", "reuse", "refused"]
RoutePreference = Literal["auto", "new", "existing"]

SOL_MODEL = "gpt-5.6-sol"
SOL_REUSE_EFFORTS = frozenset({"high", "xhigh", "max", "ultra"})
RUNTIME_CONTEXT_PATH = "/api/runtime-context"
MAX_RUNTIME_CONTEXT_BYTES = 256 * 1024
_SESSION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_NO_NEW_CHAT = re.compile(
    r"\b(?:do\s+not|don'?t|never)\s+(?:start|open|make|create)\s+"
    r"(?:me\s+)?(?:a\s+)?(?:brand\s+)?(?:new|fresh|separate|another)\s+"
    r"(?:coding\s+)?(?:chat|conversation|thread|session)\b",
    re.IGNORECASE,
)
_NO_EXISTING_CHAT = re.compile(
    r"\b(?:do\s+not|don'?t|never)\s+(?:use|reuse|continue(?:\s+in)?)\s+"
    r"(?:this|the\s+(?:current|existing)|an?\s+existing)\s+"
    r"(?:chat|conversation|thread|session)\b",
    re.IGNORECASE,
)
_NEW_CHAT = re.compile(
    r"(?:\b(?:new|fresh|separate|another)\s+(?:coding\s+)?(?:chat|conversation|thread|session)\b|"
    r"\b(?:start|open|make|create)\s+(?:me\s+)?(?:a\s+)?(?:brand\s+)?"
    r"(?:new|fresh|separate)\s+(?:coding\s+)?(?:chat|conversation|thread|session)\b)",
    re.IGNORECASE,
)
_EXISTING_CHAT = re.compile(
    r"(?:\b(?:use|reuse|continue(?:\s+(?:in|using))?|keep\s+(?:using|working\s+in))\s+"
    r"(?:this|the\s+(?:current|existing)|the\s+same|my\s+current)\s+"
    r"(?:chat|conversation|thread|session)\b|"
    r"\b(?:use|continue)\s+(?:the\s+)?(?:currently\s+)?(?:open|focused|linked)\s+"
    r"(?:chat|conversation|thread|session)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class WorkRoute:
    mode: RouteMode
    preference: RoutePreference
    project_root: str
    session_id: str = ""
    group_id: str = ""
    bridge_port: int | None = None
    title: str = ""
    reason: str = ""
    bound_focus: bool = False
    effort: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_route_preference(spoken: object) -> RoutePreference:
    """Read only an explicit chat-routing instruction from Raghav's words."""

    text = " ".join(str(spoken or "").split())
    if _NO_NEW_CHAT.search(text):
        return "existing"
    if _NO_EXISTING_CHAT.search(text):
        return "new"
    if _NEW_CHAT.search(text):
        return "new"
    if _EXISTING_CHAT.search(text):
        return "existing"
    return "auto"


def strip_route_instruction(spoken: object) -> str:
    """Remove explicit chat placement words before intent cancellation checks."""

    text = " ".join(str(spoken or "").split())
    for pattern in (_NO_NEW_CHAT, _NO_EXISTING_CHAT, _NEW_CHAT, _EXISTING_CHAT):
        text = pattern.sub(" ", text)
    return " ".join(text.split())


def _clean_root(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return os.path.normcase(os.path.normpath(raw)).replace("\\", "/")


def _same_root(left: object, right: object) -> bool:
    return bool(_clean_root(left) and _clean_root(left) == _clean_root(right))


def _truthy(record: Mapping[str, Any], *keys: str) -> bool:
    return any(bool(record.get(key)) for key in keys)


def _session_id(record: Mapping[str, Any]) -> str:
    return str(record.get("session_id") or record.get("sid") or "").strip()


def _context_members(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    members = (
        context.get("sessions")
        or context.get("runtime_sessions")
        or context.get("runtimes")
        or context.get("members")
        or []
    )
    result = [dict(value) for value in members if isinstance(value, Mapping)]
    focused = context.get("focused_session")
    if isinstance(focused, Mapping):
        focused_record = dict(focused)
        focused_sid = _session_id(focused_record)
        if focused_sid and not any(_session_id(item) == focused_sid for item in result):
            result.append(focused_record)
    return result


def _context_focus_sid(context: Mapping[str, Any]) -> str:
    focused = context.get("focused_session")
    if isinstance(focused, Mapping):
        sid = _session_id(focused)
        if sid:
            return sid
    for key in (
        "focused_session_id",
        "focused_sid",
        "runtime_focus_sid",
        "focus_sid",
        "current_session_id",
    ):
        sid = str(context.get(key) or "").strip()
        if sid:
            return sid
    return ""


def _context_split_sids(context: Mapping[str, Any]) -> list[str]:
    split = (
        context.get("split_session_ids")
        or context.get("split_pair")
        or context.get("split_sids")
        or context.get("visible_session_ids")
        or []
    )
    if isinstance(split, Mapping):
        split = list(split.values())
    values = [str(value).strip() for value in split if str(value).strip()]
    direct = str(context.get("split_codex_session_id") or "").strip()
    if direct and direct not in values:
        values.append(direct)
    return values


def _context_port(context: Mapping[str, Any]) -> int | None:
    try:
        value = int(context.get("bridge_port") or context.get("port") or 0)
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= 65535 else None


def _time_score(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    clean = str(value or "").strip()
    if not clean:
        return 0.0
    try:
        return float(clean)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(clean.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _context_time(context: Mapping[str, Any]) -> float:
    return max(
        _time_score(context.get(key))
        for key in ("focused_at", "updated_at", "observed_at", "started_at")
    )


def _context_is_foreground(context: Mapping[str, Any]) -> bool:
    return _truthy(context, "window_active", "foreground", "is_active")


def _merge_session(
    sid: str,
    context: Mapping[str, Any] | None,
    indexed: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    record = dict(indexed.get(sid) or {})
    if context is not None:
        for member in _context_members(context):
            if _session_id(member) == sid:
                record.update(member)
                break
    record["session_id"] = sid
    return record


def _metadata_for(
    sid: str,
    get_metadata: Callable[[str], Mapping[str, Any]] | None,
    cache: dict[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    if sid not in cache:
        try:
            value = get_metadata(sid) if get_metadata is not None else {}
        except Exception:
            value = {}
        cache[sid] = value if isinstance(value, Mapping) else {}
    return cache[sid]


def _binding_for(
    sid: str,
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
    get_project_binding: Callable[[str], str | None] | None,
) -> str:
    direct = str(record.get("work_project_root") or metadata.get("work_project_root") or "").strip()
    if direct or get_project_binding is None:
        return direct
    try:
        return str(get_project_binding(sid) or "").strip()
    except Exception:
        return ""


def _candidate_blocker(
    sid: str,
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
    external_runtime_active: Callable[[str], bool] | None,
) -> str:
    agent = str(record.get("agent") or "").strip().casefold()
    if agent == "serena-voice" or _truthy(record, "serena_voice"):
        return "voice transcripts are read-only"
    if agent != "codex":
        return "not a Codex chat"
    if "alive" in record and not bool(record.get("alive")):
        return "the chat has no live terminal owner"
    runtime_state = str(record.get("state") or record.get("runtime_state") or "").strip()
    if runtime_state and runtime_state not in {"live", "paused"}:
        return f"the chat runtime is {runtime_state}"
    if record.get("fleet_worker") or metadata.get("fleet_worker"):
        return "Fleet workers are not reusable chats"
    if "external_runtime_active" in record:
        external = bool(record.get("external_runtime_active"))
    elif external_runtime_active is not None:
        try:
            external = bool(external_runtime_active(sid))
        except Exception:
            external = True
    else:
        external = bool(record.get("external_runtime"))
    if external:
        return "another runtime owns the chat"
    if _truthy(record, "done", "is_done") or bool(metadata.get("done")):
        return "the chat is marked done"
    if _truthy(record, "busy", "runtime_busy", "turn_active", "active_turn", "working"):
        return "the chat already has an active turn"
    if _truthy(
        record,
        "draft",
        "has_draft",
        "draft_text",
        "input_draft",
    ):
        return "the chat has an unsent draft"
    if _truthy(
        record,
        "reserved",
        "bridge_reserved",
        "runtime_reserved",
        "lease_active",
    ):
        return "the chat is reserved by another bridge call"

    model = (
        str(record.get("model") or record.get("reported_model") or record.get("actual_model") or "")
        .strip()
        .casefold()
    )
    effort = (
        str(
            record.get("effort")
            or record.get("reasoning_effort")
            or record.get("reported_effort")
            or record.get("actual_effort")
            or ""
        )
        .strip()
        .casefold()
    )
    if model != SOL_MODEL:
        return "the chat is not running Sol 5.6"
    if effort not in SOL_REUSE_EFFORTS:
        return "the chat is not running an allowed high-reasoning effort"
    return ""


def _project_affinity(
    project_root: str,
    binding: str,
    record: Mapping[str, Any],
) -> Literal["exact", "unbound", "different"]:
    canonical = str(record.get("canonical_project_root") or record.get("git_root") or "").strip()
    observed = [value for value in (binding, canonical) if value]
    if any(not _same_root(value, project_root) for value in observed):
        return "different"
    if observed:
        return "exact"
    if _indexed_transcript_mentions_project(record, project_root):
        return "exact"
    return "unbound"


def _indexed_transcript_mentions_project(
    record: Mapping[str, Any],
    project_root: str,
) -> bool:
    """Accept only a literal absolute project path from indexed chat text.

    Headless chats intentionally start in a neutral cache directory, so their
    cwd cannot identify the repository. The index still retains the first user
    message from the durable rollout. A literal root or a file below it is
    strong enough to bind once; titles and fuzzy project-name matches are not.
    """

    root = str(project_root or "").strip().rstrip("/")
    if not root or not os.path.isabs(root):
        return False
    pattern = re.compile(
        rf"(?<![A-Za-z0-9._~/-]){re.escape(root)}(?=$|/|[^A-Za-z0-9._~/-])"
    )
    return any(
        bool(pattern.search(str(record.get(key) or "")))
        for key in ("first_message", "indexed_excerpt")
    )


def _route_for_session(
    *,
    preference: RoutePreference,
    project_root: str,
    sid: str,
    record: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    bound_focus: bool,
    reason: str,
) -> WorkRoute:
    return WorkRoute(
        mode="reuse",
        preference=preference,
        project_root=project_root,
        session_id=sid,
        group_id=str(
            record.get("group_id") or record.get("group") or ((context or {}).get("group_id")) or ""
        ),
        bridge_port=_context_port(context or {}),
        title=str(
            record.get("display_title") or record.get("custom_title") or record.get("title") or ""
        ),
        reason=reason,
        bound_focus=bound_focus,
        effort=str(
            record.get("effort")
            or record.get("reasoning_effort")
            or record.get("reported_effort")
            or record.get("actual_effort")
            or ""
        )
        .strip()
        .casefold(),
    )


def _ambiguous_route(preference: RoutePreference, project_root: str, reason: str) -> WorkRoute:
    return WorkRoute(
        mode="refused",
        preference=preference,
        project_root=project_root,
        reason=reason,
    )


def _focused_codex_targets(
    context: Mapping[str, Any],
    indexed: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    focus_sid = _context_focus_sid(context)
    if not focus_sid:
        return []
    focused = _merge_session(focus_sid, context, indexed)
    agent = str(focused.get("agent") or "").strip().casefold()
    if agent == "codex":
        return [focus_sid]
    if agent != "claude":
        return []

    targets: list[str] = []
    for sid in _context_split_sids(context):
        if sid == focus_sid:
            continue
        member = _merge_session(sid, context, indexed)
        if str(member.get("agent") or "").strip().casefold() == "codex":
            targets.append(sid)
    return list(dict.fromkeys(targets))


def _default_bridge_context(
    runtime_contexts: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, bool]:
    usable = [context for context in runtime_contexts if _context_port(context)]
    foreground = [context for context in usable if _context_is_foreground(context)]
    pool = foreground or usable
    if not pool:
        return None, False
    unique_ports = {_context_port(context) for context in pool}
    if len(foreground) > 1 and len(unique_ports) > 1:
        return None, True
    if len(pool) == 1:
        return pool[0], False
    best_time = max(_context_time(context) for context in pool)
    newest = [context for context in pool if _context_time(context) == best_time]
    if len({_context_port(context) for context in newest}) != 1:
        return None, True
    return newest[0], False


FocusedCandidate = tuple[Mapping[str, Any], str, dict[str, Any], bool]


def _pick_focused_candidate(
    candidates: Sequence[FocusedCandidate],
) -> tuple[FocusedCandidate | None, str]:
    foreground = [candidate for candidate in candidates if _context_is_foreground(candidate[0])]
    pool = foreground or list(candidates)
    unique = {candidate[1] for candidate in pool}
    if len(foreground) > 1 and len(unique) > 1:
        return None, "more than one foreground Chats window has an eligible focused Codex chat"
    if len(pool) == 1:
        return pool[0], ""
    newest_time = max(_context_time(candidate[0]) for candidate in pool)
    newest = [candidate for candidate in pool if _context_time(candidate[0]) == newest_time]
    if len({candidate[1] for candidate in newest}) > 1:
        return None, "more than one Chats window has an equally recent eligible focus"
    return newest[0], ""


def choose_work_route(
    project_root: str | Path,
    spoken: object,
    runtime_contexts: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    *,
    get_metadata: Callable[[str], Mapping[str, Any]] | None = None,
    get_project_binding: Callable[[str], str | None] | None = None,
    external_runtime_active: Callable[[str], bool] | None = None,
    active_session_ids: Collection[str] = (),
) -> WorkRoute:
    """Purely choose one frozen target from supplied context and session data.

    The optional metadata callables are reads. This function performs no index,
    network, metadata, process, or filesystem mutation.
    """

    root = str(project_root).strip()
    preference = parse_route_preference(spoken)
    if preference == "new":
        return WorkRoute(
            mode="private",
            preference="new",
            project_root=root,
            reason="Raghav explicitly asked for a new chat",
        )

    indexed = {
        sid: dict(record)
        for record in sessions
        if isinstance(record, Mapping) and (sid := _session_id(record))
    }
    metadata_cache: dict[str, Mapping[str, Any]] = {}

    focused_candidates: list[FocusedCandidate] = []
    focused_blockers: list[str] = []
    for context in runtime_contexts:
        targets = _focused_codex_targets(context, indexed)
        if len(targets) > 1:
            return _ambiguous_route(
                preference,
                root,
                "the focused Claude pane has more than one visible Codex partner",
            )
        for sid in targets:
            record = _merge_session(sid, context, indexed)
            metadata = _metadata_for(sid, get_metadata, metadata_cache)
            blocker = _candidate_blocker(
                sid,
                record,
                metadata,
                external_runtime_active,
            )
            binding = _binding_for(
                sid,
                record,
                metadata,
                get_project_binding,
            )
            affinity = _project_affinity(root, binding, record)
            if blocker:
                focused_blockers.append(blocker)
                continue
            if affinity == "different":
                focused_blockers.append("the focused chat belongs to a different Git project")
                continue
            if not _context_port(context):
                focused_blockers.append("the focused chat has no live bridge")
                continue
            focused_candidates.append((context, sid, record, affinity == "unbound"))

    exact_focused = [candidate for candidate in focused_candidates if not candidate[3]]
    immediate_focused = exact_focused
    if preference == "existing" and not immediate_focused:
        immediate_focused = focused_candidates
    if immediate_focused:
        selected, ambiguity = _pick_focused_candidate(immediate_focused)
        if ambiguity:
            return _ambiguous_route(preference, root, ambiguity)
        assert selected is not None
        context, sid, record, bind_focus = selected
        return _route_for_session(
            preference=preference,
            project_root=root,
            sid=sid,
            record=record,
            context=context,
            bound_focus=bind_focus,
            reason=(
                "reusing and binding the focused Codex chat to this Git project"
                if bind_focus
                else "reusing the focused Codex chat for this Git project"
            ),
        )

    bridge_context, bridge_ambiguous = _default_bridge_context(runtime_contexts)
    if bridge_ambiguous:
        return _ambiguous_route(
            preference,
            root,
            "more than one Chats window could own the reused chat",
        )

    recent: list[tuple[float, str, dict[str, Any]]] = []
    if bridge_context is not None:
        live_sids = {
            _session_id(member)
            for member in _context_members(bridge_context)
            if _session_id(member)
            and bool(member.get("alive", True))
            and str(member.get("state") or "live") in {"live", "paused"}
        }
        for sid, base in indexed.items():
            if sid not in live_sids:
                continue
            record = _merge_session(sid, bridge_context, indexed)
            metadata = _metadata_for(sid, get_metadata, metadata_cache)
            blocker = _candidate_blocker(
                sid,
                record,
                metadata,
                external_runtime_active,
            )
            if blocker:
                continue
            binding = _binding_for(
                sid,
                record,
                metadata,
                get_project_binding,
            )
            if _project_affinity(root, binding, record) != "exact":
                continue
            timestamp = _time_score(
                base.get("last_timestamp") or base.get("updated_at") or base.get("first_timestamp")
            )
            recent.append((timestamp, sid, record))

    if recent and bridge_context is not None:
        recent.sort(key=lambda item: (item[0], item[1]), reverse=True)
        top_time = recent[0][0]
        tied = [item for item in recent if item[0] == top_time]
        if len(tied) > 1:
            return _ambiguous_route(
                preference,
                root,
                "more than one existing Codex chat is equally recent for this project",
            )
        _timestamp, sid, record = recent[0]
        return _route_for_session(
            preference=preference,
            project_root=root,
            sid=sid,
            record=record,
            context=bridge_context,
            bound_focus=False,
            reason="reusing the most recent exact-project Codex chat",
        )

    # A durable rollout does not need an open pane to be resumable. Exclude
    # every session that still has an interactive or headless process owner,
    # then rank the remaining exact-project transcripts by their real indexed
    # activity. The supervisor resumes the frozen id through `codex exec` and
    # claims external ownership before it can be opened in Chats.
    owned_sids = {
        _session_id(member)
        for context in runtime_contexts
        for member in _context_members(context)
        if _session_id(member)
        and bool(member.get("alive", True))
        and str(member.get("state") or "live") in {"live", "paused"}
    }
    owned_sids.update(str(value).strip() for value in active_session_ids if str(value).strip())
    historical: list[tuple[float, str, dict[str, Any]]] = []
    for sid, base in indexed.items():
        if sid in owned_sids:
            continue
        record = dict(base)
        metadata = _metadata_for(sid, get_metadata, metadata_cache)
        blocker = _candidate_blocker(
            sid,
            record,
            metadata,
            external_runtime_active,
        )
        if blocker:
            continue
        binding = _binding_for(
            sid,
            record,
            metadata,
            get_project_binding,
        )
        if _project_affinity(root, binding, record) != "exact":
            continue
        timestamp = _time_score(
            base.get("last_timestamp") or base.get("updated_at") or base.get("first_timestamp")
        )
        historical.append((timestamp, sid, record))

    if historical:
        historical.sort(key=lambda item: (item[0], item[1]), reverse=True)
        top_time = historical[0][0]
        tied = [item for item in historical if item[0] == top_time]
        if len(tied) > 1:
            return _ambiguous_route(
                preference,
                root,
                "more than one indexed Codex chat is equally recent for this project",
            )
        _timestamp, sid, record = historical[0]
        return _route_for_session(
            preference=preference,
            project_root=root,
            sid=sid,
            record=record,
            context=None,
            bound_focus=False,
            reason="resuming the most recent indexed exact-project Codex chat",
        )

    # An unbound home/cache pane can become the project chat, but only when no
    # exact indexed transcript exists. Otherwise focus would erase the very
    # continuity the history search is meant to preserve.
    if focused_candidates:
        selected, ambiguity = _pick_focused_candidate(focused_candidates)
        if ambiguity:
            return _ambiguous_route(preference, root, ambiguity)
        assert selected is not None
        context, sid, record, bind_focus = selected
        return _route_for_session(
            preference=preference,
            project_root=root,
            sid=sid,
            record=record,
            context=context,
            bound_focus=bind_focus,
            reason="binding the focused Codex chat because no exact project history exists",
        )

    if preference == "existing":
        detail = (
            focused_blockers[0] if focused_blockers else "no exact-project Sol chat is available"
        )
        return WorkRoute(
            mode="refused",
            preference="existing",
            project_root=root,
            reason=f"Raghav explicitly asked for an existing chat, but {detail}",
        )
    return WorkRoute(
        mode="private",
        preference="auto",
        project_root=root,
        reason="no safe exact-project Sol chat is available",
    )


@lru_cache(maxsize=512)
def _git_root_for_cwd(raw_cwd: str) -> str:
    raw = str(raw_cwd or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_dir():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    try:
        return str(Path(result.stdout.strip()).resolve(strict=True))
    except OSError:
        return ""


def _codex_identity(file_path: object) -> tuple[str, str]:
    path = Path(str(file_path or "")).expanduser()
    if not path.is_file():
        return "", ""
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            start = max(0, size - 2 * 1024 * 1024)
            handle.seek(start)
            if start:
                handle.readline()
            lines = handle.readlines()
    except OSError:
        return "", ""

    model = ""
    effort = ""
    for raw in lines:
        try:
            event = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        if event.get("type") == "turn_context":
            model = str(payload.get("model") or model)
            effort = str(payload.get("reasoning_effort") or payload.get("effort") or effort)
        settings = payload.get("thread_settings")
        if isinstance(settings, Mapping):
            model = str(settings.get("model") or model)
            effort = str(settings.get("reasoning_effort") or settings.get("effort") or effort)
    return model, effort


def _enrich_session(
    raw: Mapping[str, Any],
    *,
    get_metadata: Callable[[str], Mapping[str, Any]],
    get_project_binding: Callable[[str], str | None],
    external_runtime_active: Callable[[str], bool],
) -> dict[str, Any]:
    record = dict(raw)
    sid = _session_id(record)
    if not sid:
        return record
    record["session_id"] = sid
    try:
        metadata = get_metadata(sid)
    except Exception:
        metadata = {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    for key in ("fleet_worker", "done", "group"):
        if key not in record and key in metadata:
            record[key] = metadata[key]
    try:
        binding = str(get_project_binding(sid) or "").strip()
    except Exception:
        binding = ""
    if binding:
        record["work_project_root"] = binding
    try:
        record["external_runtime_active"] = bool(external_runtime_active(sid))
    except Exception:
        record["external_runtime_active"] = True

    if str(record.get("agent") or "").casefold() == "codex":
        if not binding:
            cwd = str(record.get("last_cwd") or record.get("cwd") or "")
            canonical = _git_root_for_cwd(cwd)
            if canonical:
                record["canonical_project_root"] = canonical
        model = str(record.get("model") or "").strip()
        effort = str(record.get("effort") or record.get("reasoning_effort") or "").strip()
        if (not model or model.casefold() == SOL_MODEL) and not effort:
            identity_path = record.get("file_path")
            if not identity_path:
                try:
                    from core.codex_bridge import find_codex_jsonl

                    identity_path = find_codex_jsonl(sid)
                except Exception:
                    identity_path = None
            found_model, found_effort = _codex_identity(identity_path)
            record["model"] = found_model or model
            record["effort"] = found_effort
    return record


def _configured_ports() -> list[int]:
    raw = os.environ.get("SERENA_CHATS_PORTS") or os.environ.get("SERENA_CHATS_PORT") or ""
    ports: list[int] = []
    for value in re.split(r"[,\s]+", raw.strip()):
        if not value:
            continue
        try:
            port = int(value)
        except ValueError:
            continue
        if 0 < port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def discover_active_codex_sessions() -> set[str]:
    """Return exact session ids currently owned by a Codex process.

    Interactive Chats owners are normally visible through the runtime endpoint,
    but an ordinary terminal or a temporarily unreachable WebKit endpoint is
    not. The provider's resume argv is an exact, privacy-safe ownership signal.
    """

    active: set[str] = set()
    processes = psutil.process_iter(["cmdline"])
    for process in processes:
        try:
            info = getattr(process, "info", {}) or {}
            argv = [str(value) for value in (info.get("cmdline") or process.cmdline() or [])]
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            continue
        if not any(Path(value).name == "codex" for value in argv):
            continue
        for index, value in enumerate(argv[:-1]):
            candidate = argv[index + 1].strip()
            if value == "resume" and _SESSION_ID.fullmatch(candidate):
                active.add(candidate)
    return active


def _chats_process_started(process: psutil.Process) -> float | None:
    """Recognize a Chats listener even when WebKit owns the inherited fd."""

    current: Any = process
    for _depth in range(4):
        try:
            command = " ".join(current.cmdline()).casefold()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            return None
        if any(
            marker in command
            for marker in (
                "desktop/app_gtk.py",
                "desktop.app_gtk",
                "ui.web",
                "/bin/chats desktop",
                " chats desktop",
            )
        ):
            try:
                return float(current.create_time())
            except (psutil.Error, OSError, TypeError, ValueError):
                return 0.0
        parent_getter = getattr(current, "parent", None)
        if not callable(parent_getter):
            return None
        try:
            current = parent_getter()
        except (psutil.Error, OSError):
            return None
        if current is None:
            return None
    return None


def discover_runtime_ports() -> list[int]:
    """Find local Chats HTTP listeners, newest process first."""

    configured = _configured_ports()
    if configured:
        return configured
    candidates: dict[int, float] = {}
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return []
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.pid or not connection.laddr:
            continue
        try:
            host = str(connection.laddr.ip)
            port = int(connection.laddr.port)
        except AttributeError:
            host = str(connection.laddr[0])
            port = int(connection.laddr[1])
        if host not in {"127.0.0.1", "::1"}:
            continue
        try:
            process = psutil.Process(connection.pid)
            started = _chats_process_started(process)
            if started is None:
                continue
            candidates[port] = started
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            continue
    return [
        port
        for port, _started in sorted(candidates.items(), key=lambda item: item[1], reverse=True)
    ]


def fetch_runtime_context(port: int, *, timeout: float = 1.0) -> dict[str, Any] | None:
    """Read one bounded, same-host Chats focus snapshot."""

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}{RUNTIME_CONTEXT_PATH}",
            timeout=max(0.1, float(timeout)),
        ) as response:
            payload = response.read(MAX_RUNTIME_CONTEXT_BYTES + 1)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if len(payload) > MAX_RUNTIME_CONTEXT_BYTES:
        return None
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict) or decoded.get("ok") is False:
        return None
    decoded.setdefault("bridge_port", int(port))
    return decoded


def discover_work_route(
    project_root: str | Path,
    spoken: object,
    *,
    runtime_contexts: Sequence[Mapping[str, Any]] | None = None,
    sessions: Sequence[Mapping[str, Any]] | None = None,
    runtime_ports: Sequence[int] | None = None,
    context_fetcher: Callable[[int], Mapping[str, Any] | None] | None = None,
    session_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    get_metadata: Callable[[str], Mapping[str, Any]] | None = None,
    get_project_binding: Callable[[str], str | None] | None = None,
    bind_project: Callable[[str, str | Path], str] | None = None,
    external_runtime_active: Callable[[str], bool] | None = None,
    active_session_ids: Collection[str] | None = None,
    active_session_loader: Callable[[], Collection[str]] | None = None,
) -> WorkRoute:
    """Read live Chats/index state, choose once, then bind only that session."""

    from core import metadata as meta_sync
    from core.coding_job_contract import validate_repository_root

    canonical_root = str(validate_repository_root(project_root))
    get_metadata = get_metadata or meta_sync.get_meta
    get_project_binding = get_project_binding or meta_sync.get_work_project_root
    bind_project = bind_project or meta_sync.set_work_project_root
    external_runtime_active = external_runtime_active or meta_sync.external_runtime_active

    if runtime_contexts is None:
        fetcher = context_fetcher or fetch_runtime_context
        contexts: list[Mapping[str, Any]] = []
        for port in runtime_ports if runtime_ports is not None else discover_runtime_ports():
            try:
                context = fetcher(int(port))
            except Exception:
                context = None
            if isinstance(context, Mapping):
                materialized = dict(context)
                materialized.setdefault("bridge_port", int(port))
                contexts.append(materialized)
        runtime_contexts = contexts

    if sessions is None:
        if session_loader is None:
            from core.indexer import list_sessions

            def session_loader() -> Sequence[Mapping[str, Any]]:
                return list_sessions(limit=500)

        sessions = session_loader()

    if active_session_ids is None:
        loader = active_session_loader or discover_active_codex_sessions
        try:
            active_session_ids = loader()
        except Exception:
            # An unavailable process inventory must fail safe for historical
            # resumes. Live bridge routing still works from exact owner state.
            active_session_ids = {
                _session_id(record)
                for record in sessions
                if isinstance(record, Mapping) and _session_id(record)
            }

    enriched = [
        _enrich_session(
            record,
            get_metadata=get_metadata,
            get_project_binding=get_project_binding,
            external_runtime_active=external_runtime_active,
        )
        for record in sessions
        if isinstance(record, Mapping)
    ]
    enriched_contexts: list[dict[str, Any]] = []
    indexed = {_session_id(record): record for record in enriched if _session_id(record)}
    for raw_context in runtime_contexts:
        context = dict(raw_context)
        members: list[dict[str, Any]] = []
        for raw_member in _context_members(context):
            sid = _session_id(raw_member)
            merged = dict(indexed.get(sid) or {})
            merged.update(raw_member)
            members.append(
                _enrich_session(
                    merged,
                    get_metadata=get_metadata,
                    get_project_binding=get_project_binding,
                    external_runtime_active=external_runtime_active,
                )
            )
        context["sessions"] = members
        enriched_contexts.append(context)

    route = choose_work_route(
        canonical_root,
        spoken,
        enriched_contexts,
        enriched,
        get_metadata=get_metadata,
        get_project_binding=get_project_binding,
        external_runtime_active=external_runtime_active,
        active_session_ids=active_session_ids,
    )
    if route.mode != "reuse":
        return route
    try:
        bind_project(route.session_id, canonical_root)
    except Exception as error:
        return WorkRoute(
            mode="refused",
            preference=route.preference,
            project_root=canonical_root,
            reason=f"the selected chat could not be safely bound: {error}",
        )
    return replace(route, project_root=canonical_root)


__all__ = [
    "SOL_MODEL",
    "SOL_REUSE_EFFORTS",
    "WorkRoute",
    "choose_work_route",
    "discover_active_codex_sessions",
    "discover_runtime_ports",
    "discover_work_route",
    "fetch_runtime_context",
    "parse_route_preference",
    "strip_route_instruction",
]
