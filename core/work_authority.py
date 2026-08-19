"""Broker for Serena starting coding work from her own judgment.

Why this exists: the spoken path used to decide by regex, before the brain
ever saw the words. Pattern matching cannot tell "fix the phev tracker" from
"why is the phev tracker broken", cannot check memory first, and cannot ask
one clarifying question. So ordinary asks silently never became jobs while
Serena sat there sounding willing.

The authority moves to her, with a broker underneath: she calls the tool when
she judges that Raghav asked for work, and this module independently re-reads
his ACTUAL spoken turn before anything is queued. She cannot invent a job out
of a question, a hypothetical, or her own enthusiasm, and she cannot queue
work from a turn he never spoke.

Deliberately NOT enforced here: which verb he used, or what order the words
came in. That judgement is hers now. This layer only proves the request is
grounded in something he really said, on a live spoken turn, once.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

from core.coding_job_contract import (
    CodingJobBrief,
    RepositoryResolutionError,
    capture_git_snapshot,
    implement_effort_for,
    normalise_complexity,
    resolve_repository_root,
)
from core.coding_model_preferences import (
    normalise_coding_model,
    read_coding_model_preference,
)
from core.work_session_router import (
    WorkRoute,
    parse_route_preference,
    strip_route_instruction,
)

DEFAULT_AUDIT_PATH = Path.home() / ".local" / "state" / "serena" / "work-authority.jsonl"
MAX_REQUEST_CHARS = 4_000
MIN_REQUEST_CHARS = 3

# Spoken surfaces may start work. A front-door or plain text turn is not a
# live spoken authority and must go through the existing pane protocol.
SPOKEN_PROTOCOLS = frozenset({"voice", "desk"})

# She may not convert a pure question into a job. This mirrors the language
# the old gate refused, kept as a floor under her judgement rather than as
# the decision itself.
_INTERROGATIVE = re.compile(
    r"^\s*(?:what|why|when|where|who|which|how|is|are|was|were|do|does|did|"
    r"can|could|would|should|will|have|has|had)\b",
    re.IGNORECASE,
)
_QUESTION_ONLY = re.compile(r"\?\s*$")
_MODAL_WORK_REQUEST = re.compile(
    r"^\s*(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    r"(?:fix|build|implement|change|update|add|remove|replace|refactor|debug|"
    r"investigate|test|verify|review|finish|wire|create|code|continue|clean|"
    r"patch|repair|migrate|rename|optimi[sz]e|connect|move|split|merge|deploy|ship)\b",
    re.IGNORECASE,
)
# Withdrawal phrases only. A bare "don't" or "stop" is not a withdrawal:
# "add a comment, don't commit" and "stop the panel from opening" are both
# instructions ABOUT the work, and both were refused as cancellations. The
# words that survive here are ones that cannot mean anything except call it
# off, so this stays a floor and does not start guessing at intent again.
_CANCELLATION = re.compile(
    r"\b(?:never\s*mind|forget\s+it|leave\s+it|drop\s+it|call\s+it\s+off|"
    r"cancel\s+(?:that|it|the\s+job|the\s+work)|abort\s+(?:that|it)|"
    r"hold\s+off|no\s+need\s+to|scratch\s+that|don'?t\s+bother|"
    r"do\s+not\s+bother|forget\s+about\s+(?:it|that))\b",
    re.IGNORECASE,
)
# A spoken turn that authorizes work mentions doing something to something.
# Broad on purpose: her judgement narrows it, this only proves intent exists.
_WORK_SIGNAL = re.compile(
    r"\b(?:fix|build|implement|change|update|add|remove|replace|refactor|"
    r"debug|investigate|test|verify|review|finish|wire|hook|work|create|"
    r"write|code|start|continue|keep|going|look|dig|jump|tackle|sort|handle|"
    r"deal|clean|set|make|patch|repair|migrate|rename|polish|optimi[sz]e|"
    r"document|connect|move|split|merge|extract|do|done|sort(?:ed)?|"
    r"broken|bug|issue|error|failing|deploy|ship)\b",
    re.IGNORECASE,
)

# Generic verbs such as create, write, make, and document are not proof of a
# coding request. The old call fast path turned "make a memory list in Word"
# into a coding job before the resident brain could use its document tools.
# Keep real software requests working when they actually name software context.
_PERSONAL_DOCUMENT_SIGNAL = re.compile(
    r"\b(?:documents?|word\s+(?:documents?|files?)|microsoft\s+word|docx|"
    r"notepad|text\s+files?|txt|"
    r"notes?|lists?|telegram|beeper|memories)\b",
    re.IGNORECASE,
)
_SOFTWARE_CONTEXT_SIGNAL = re.compile(
    r"\b(?:code|coding|software|app|application|repo|repository|codebase|"
    r"project|bug|feature|function|class|module|endpoint|api|ui|website|web|"
    r"backend|frontend|database|tests?|script|automation|readme|deploy)\b",
    re.IGNORECASE,
)
_MEMORY_LIST_FOLLOWUP = re.compile(
    r"^\s*(?:(?:okay|ok|then|now|and|actually|let'?s)\s+)?"
    r"(?:start|begin|go)\s+with\s+"
    r"(?:memory\s+)?#?\d+\s*[.!]?\s*$",
    re.IGNORECASE,
)

_BRIEF_LIST_FIELDS = (
    "relevant_conversation",
    "project_context",
    "memory_guidance",
    "ledger_guidance",
    "handoff_guidance",
    "acceptance_criteria",
    "authority_boundaries",
)
# Validated the same way, but never required. A file map she could not build is
# a worse brief, not an illegal one, and refusing the job over a missing guess
# would trade a slow start for no start at all.
_OPTIONAL_BRIEF_LIST_FIELDS = ("likely_files",)


@dataclass(frozen=True, slots=True)
class WorkAuthorityResult:
    allowed: bool
    reason: str
    request: str
    item_id: str = ""
    route_mode: str = ""
    session_id: str = ""
    title: str = ""


def _private_route(root: Path, spoken: object, reason: str) -> WorkRoute:
    return WorkRoute(
        mode="private",
        preference=parse_route_preference(spoken),
        project_root=str(root),
        reason=reason,
    )


def _route_receipt(route: Mapping[str, object]) -> tuple[str, str, str, str]:
    mode = str(route.get("mode") or "private")
    session_id = str(route.get("session_id") or "")
    title = _clean(route.get("title"))
    if mode == "reuse":
        label = title or f"Codex chat {session_id[:8]}"
        return f"continuing in {label}", mode, session_id, title
    reason = _clean(route.get("reason"))
    if str(route.get("preference") or "") == "new":
        return "starting a new coding chat because you asked for one", mode, "", ""
    if reason:
        return f"starting a new coding chat because {reason}", mode, "", ""
    return "starting a new coding chat", mode, "", ""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def is_personal_document_request(text: object) -> bool:
    """True when words describe a personal artifact, not software work."""

    clean = _clean(text)
    return bool(
        _PERSONAL_DOCUMENT_SIGNAL.search(clean)
        and not _SOFTWARE_CONTEXT_SIGNAL.search(clean)
    )


def authority_denial(request: str, origin: Mapping[str, object]) -> str | None:
    """Return why this request may not start work, or None when it may."""

    request = _clean(request)
    spoken = _clean(origin.get("text"))
    spoken_without_route = strip_route_instruction(spoken)
    protocol = str(origin.get("protocol") or "").strip().lower()

    if not request or len(request) < MIN_REQUEST_CHARS:
        return "the work request was empty"
    if len(request) > MAX_REQUEST_CHARS:
        return "the work request was too long to queue safely"
    if not spoken:
        return "no originating spoken turn is bound to this tool call"
    if protocol not in SPOKEN_PROTOCOLS:
        return (
            "coding work starts only from a live spoken turn, "
            f"not a {protocol or 'unknown'} turn"
        )
    if _CANCELLATION.search(spoken_without_route):
        return "the spoken turn withdrew or postponed the work"
    if (
        _INTERROGATIVE.match(spoken)
        and _QUESTION_ONLY.search(spoken)
        and not _MODAL_WORK_REQUEST.match(spoken)
    ):
        return "the spoken turn was a question, not an instruction"
    # These two stay. Both are narrow blacklists, not vocabularies: one keeps
    # a document request out of the coding queue, the other stops a bare
    # "start with 381" becoming a job, which once deleted memory 381.
    if is_personal_document_request(spoken):
        return "the spoken turn asked for a personal document, not coding work"
    if _MEMORY_LIST_FOLLOWUP.fullmatch(spoken):
        return "the spoken turn is a list or memory follow-up, not coding work"
    # Nothing below this line judges what he MEANT. That is hers.
    #
    # There used to be a required vocabulary here: his turn had to contain one
    # of about fifty approved words or the job was refused. On 2026-08-04 he
    # reported the coding panel opening on every typed message and she read it
    # correctly, called this tool, and was vetoed because the list held "keep"
    # and he had written "keeps", and held "code" while he wrote "coding". A
    # whole bug report killed by one letter. It was the fourth refusal of that
    # exact shape in a week, against zero false starts ever recorded.
    #
    # A whitelist of approved phrasings can never be finished, because it is a
    # guess about his words made in advance without him. Cancellation above is
    # a blacklist and stays: "don't" and "never mind" are narrow, and a person
    # who is told to stop should stop. Everything else is judgement, and the
    # model making it has the conversation, his memory and the ledger, which
    # no pattern here will ever have.
    return None


def ground_likely_files(entries: object, root: Path) -> list[str]:
    """Check her guessed paths against the real tree before the worker sees them.

    The map is worth having because the worker starts cold, but a guess she
    cannot check is how a worker gets sent somewhere confidently wrong. The
    first live run of this named core/coding_provider.py for a thread that
    lives in core/voice_work_supervisor.py, which is exactly the false path
    the labelling was supposed to make cheap.

    So the broker stats each one. A path that really exists is marked as
    checked, which is worth more than her word for it. A path that does not
    exist is kept and marked plainly as wrong, rather than dropped, because
    "she thought it was here and it is not" tells the worker something. Prose
    with no path in it passes through untouched. Nothing here decides whether
    the guess is *right*, only whether the file is there.
    """

    grounded: list[str] = []
    for raw in _json_list_of_text(entries):
        entry = _clean(raw)
        if not entry:
            continue
        token = _path_token(entry)
        if not token:
            grounded.append(entry)
            continue
        try:
            exists = (root / token).exists()
        except OSError:
            exists = False
        marker = "verified to exist" if exists else "NOT FOUND, this guess is wrong"
        grounded.append(f"{entry} [{marker}]")
    return grounded


def _json_list_of_text(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _path_token(entry: str) -> str:
    """The repository-relative path an entry names, if it names one.

    Deliberately conservative. It takes the first token that looks like a real
    file path, drops a ``::symbol`` suffix and trailing punctuation, and gives
    up on anything else rather than inventing a path to go and stat.
    """

    for candidate in entry.replace(",", " ").split():
        token = candidate.split("::", 1)[0].strip("`'\"()[]<>").rstrip(":;.")
        if token.startswith(("/", "~")) or ".." in token:
            return ""
        if "/" in token or _FILE_SUFFIX.search(token):
            return token
    return ""


_FILE_SUFFIX = re.compile(
    r"\.(?:py|js|mjs|cjs|ts|tsx|jsx|json|css|html|md|toml|yaml|yml|sh|service|"
    r"socket|timer|sql|rs|go|java|rb|c|h|cpp|swift)$",
    re.IGNORECASE,
)


def _validated_brief_context(
    context: Mapping[str, object] | None,
) -> tuple[dict[str, object] | None, str]:
    """Validate the complete brain-authored brief before snapshotting Git."""

    if not isinstance(context, Mapping):
        return None, "the structured coding brief is missing"
    supplied = dict(context)
    required = {"project", "requested_outcome", *_BRIEF_LIST_FIELDS}
    missing = sorted(field for field in required if field not in supplied)
    if missing:
        return None, "the structured coding brief is missing: " + ", ".join(missing)
    for field_name in ("project", "requested_outcome"):
        value = supplied[field_name]
        if not isinstance(value, str) or not value.strip():
            return None, f"brief field {field_name} must be non-empty text"
    for field_name in _BRIEF_LIST_FIELDS:
        value = supplied[field_name]
        if not isinstance(value, list):
            return None, f"brief field {field_name} must be a list of text"
        if field_name == "relevant_conversation":
            normalised: list[str] = []
            for item in value:
                if isinstance(item, str):
                    text = _clean(item)
                    if text:
                        normalised.append(text)
                    continue
                if isinstance(item, Mapping):
                    role = _clean(item.get("role") or item.get("speaker"))
                    text = _clean(item.get("text") or item.get("message"))
                    if text:
                        normalised.append(f"{role}: {text}" if role else text)
                        continue
                return None, f"brief field {field_name} must be a list of text"
            supplied[field_name] = normalised
        elif any(not isinstance(item, str) for item in value):
            return None, f"brief field {field_name} must be a list of text"
    for field_name in ("acceptance_criteria", "authority_boundaries"):
        if not supplied[field_name]:
            return None, f"brief field {field_name} cannot be empty"
    for field_name in _OPTIONAL_BRIEF_LIST_FIELDS:
        if field_name not in supplied:
            supplied[field_name] = []
            continue
        value = supplied[field_name]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return None, f"brief field {field_name} must be a list of text"
        supplied[field_name] = [text for text in map(_clean, value) if text]
    complexity = supplied.get("complexity")
    if complexity is not None and not isinstance(complexity, str):
        return None, "brief field complexity must be text"
    supplied["complexity"] = normalise_complexity(complexity)
    risk = supplied.get("risk")
    if risk is not None and not isinstance(risk, str):
        return None, "brief field risk must be text"
    if "coding_model" in supplied:
        try:
            supplied["coding_model"] = normalise_coding_model(
                supplied["coding_model"], strict=True
            )
        except ValueError as error:
            return None, str(error)
    else:
        supplied["coding_model"] = read_coding_model_preference()
    if "commit_authorized" in supplied and not isinstance(
        supplied["commit_authorized"], bool
    ):
        return None, "brief field commit_authorized must be true or false"
    supplied.setdefault("commit_authorized", False)
    return supplied, ""


def start_coding_work(
    request: str,
    *,
    origin: Mapping[str, object],
    brief_context: Mapping[str, object] | None = None,
    inbox=None,
    audit_path: Path = DEFAULT_AUDIT_PATH,
    route_resolver: Callable[[Path, object], WorkRoute] | None = None,
) -> WorkAuthorityResult:
    """Queue one coding job in a frozen existing or new Codex chat."""

    request = _clean(request)
    denial = authority_denial(request, origin)
    if denial is not None:
        result = WorkAuthorityResult(False, denial, request)
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result

    supplied, brief_error = _validated_brief_context(brief_context)
    if supplied is None:
        result = WorkAuthorityResult(False, brief_error, request)
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result

    if inbox is None:
        from core.voice_inbox import get_default_voice_inbox

        inbox = get_default_voice_inbox()
    if not inbox.resident_lease_active():
        result = WorkAuthorityResult(
            False, "my private coding runtime is not running", request
        )
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result

    call_id = str(origin.get("call_id") or "").strip() or "desk"
    turn_id = str(origin.get("turn_id") or "").strip() or f"{call_id}:tool"
    existing_lookup = getattr(inbox, "item_for_turn", None)
    if callable(existing_lookup):
        existing = existing_lookup(call_id=call_id, turn_id=turn_id)
        if existing is not None:
            route_lookup = getattr(inbox, "route_record", None)
            frozen_route = route_lookup(existing.item_id) if callable(route_lookup) else None
            reason, mode, session_id, title = _route_receipt(frozen_route or {})
            result = WorkAuthorityResult(
                True,
                f"already queued, {reason}",
                request,
                existing.item_id,
                mode,
                session_id,
                title,
            )
            _append_audit(result, origin=origin, audit_path=audit_path)
            return result

    project_context = supplied.get("project_context") or []
    try:
        root = resolve_repository_root(
            "\n".join((request, str(origin.get("text") or ""))),
            project_hint=str(supplied.get("project") or ""),
            project_context=[str(value) for value in project_context],
        )
    except RepositoryResolutionError as error:
        result = WorkAuthorityResult(False, str(error), request)
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result

    from core.coding_provider import choose_providers
    from core.serena_policy import classify_risk

    risk_level, _risk_reason = classify_risk(
        request,
        paths=supplied.get("likely_files") or [],
        explicit=supplied.get("risk"),
    )
    assignment = choose_providers(
        preferred_model=supplied.get("coding_model"),
        complexity=supplied.get("complexity"),
        risk=risk_level,
        request=request,
    )
    if not assignment.usable:
        result = WorkAuthorityResult(False, assignment.reason, request)
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result
    supplied["risk"] = assignment.risk
    supplied["model_policy"] = assignment.to_dict()

    spoken = str(origin.get("text") or "")
    try:
        if route_resolver is None:
            from core.work_session_router import discover_work_route

            route = discover_work_route(root, spoken)
        else:
            route = route_resolver(root, spoken)
    except Exception:
        preference = parse_route_preference(spoken)
        if preference == "existing":
            route = WorkRoute(
                mode="refused",
                preference="existing",
                project_root=str(root),
                reason="the existing-chat check was unavailable",
            )
        else:
            route = _private_route(
                root,
                spoken,
                "the existing-chat check was unavailable",
            )

    if route.mode == "refused":
        result = WorkAuthorityResult(False, route.reason, request, route_mode="refused")
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result
    if Path(route.project_root).expanduser().resolve() != root:
        result = WorkAuthorityResult(
            False,
            "the selected coding chat was bound to a different Git project",
            request,
            route_mode="refused",
        )
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result

    implement_effort = assignment.implement_effort
    reuse_mismatch = ""
    if route.mode == "reuse" and assignment.implement_provider != "codex":
        reuse_mismatch = (
            f"the selected chat is Codex, but this job froze "
            f"{assignment.implement_model} on {assignment.implement_provider}"
        )
    elif route.mode == "reuse" and assignment.implement_model != "gpt-5.6-sol":
        reuse_mismatch = (
            f"the selected chat uses gpt-5.6-sol, but this job froze "
            f"{assignment.implement_model}"
        )
    elif route.mode == "reuse" and route.effort != implement_effort:
        actual = route.effort or "an unknown effort"
        reuse_mismatch = (
            f"the selected chat uses {actual}, but this job froze "
            f"{implement_effort} implementation effort"
        )
    if reuse_mismatch:
        reason = reuse_mismatch
        if route.preference == "existing":
            result = WorkAuthorityResult(
                False,
                reason,
                request,
                route_mode="refused",
            )
            _append_audit(result, origin=origin, audit_path=audit_path)
            return result
        route = _private_route(root, spoken, reason)

    item_id = str(uuid.uuid4())
    try:
        initial_git = capture_git_snapshot(root, item_id=item_id, label="baseline")
    except Exception as error:
        result = WorkAuthorityResult(
            False, f"i could not freeze the initial Git state: {_clean(error)}", request
        )
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result

    if not supplied.get("relevant_conversation"):
        try:
            from core.brain_laptop_tools import recent_turn_texts

            supplied["relevant_conversation"] = recent_turn_texts(limit=6)
        except Exception:
            supplied["relevant_conversation"] = []
    supplied["likely_files"] = ground_likely_files(supplied.get("likely_files"), root)
    brief = CodingJobBrief.create(
        item_id=item_id,
        exact_request=request,
        triggering_request=str(origin.get("text") or ""),
        project_root=root,
        initial_git=initial_git,
        context=supplied,
        work_route=route.to_dict(),
    )
    enqueue_accepted = getattr(inbox, "enqueue_accepted", None)
    if not callable(enqueue_accepted):
        result = WorkAuthorityResult(
            False, "the coding inbox cannot persist structured accepted briefs", request
        )
        _append_audit(result, origin=origin, audit_path=audit_path)
        return result
    if route.mode == "reuse":
        try:
            from core.metadata import set_work_project_root

            set_work_project_root(route.session_id, root)
        except Exception as error:
            result = WorkAuthorityResult(
                False,
                f"the selected chat could not be safely bound: {_clean(error)}",
                request,
                route_mode="refused",
            )
            _append_audit(result, origin=origin, audit_path=audit_path)
            return result
    item = enqueue_accepted(brief.to_dict(), call_id=call_id, turn_id=turn_id)
    reason, mode, session_id, title = _route_receipt(route.to_dict())
    result = WorkAuthorityResult(
        True,
        reason,
        request,
        item.item_id,
        mode,
        session_id,
        title,
    )
    _append_audit(result, origin=origin, audit_path=audit_path)
    return result


def _append_audit(
    result: WorkAuthorityResult,
    *,
    origin: Mapping[str, object],
    audit_path: Path,
) -> None:
    """Record every decision, including refusals, without storing raw speech.

    Best effort by design: the job is already durable in the inbox once
    enqueued, so a failed audit write must never turn a started job into a
    reported failure. Serena telling the truth about what happened outranks
    this file.
    """

    with suppress(OSError):
        _write_audit(result, origin=origin, audit_path=Path(audit_path))


def _write_audit(
    result: WorkAuthorityResult,
    *,
    origin: Mapping[str, object],
    audit_path: Path,
) -> None:
    audit_path = audit_path.expanduser()
    audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        **asdict(result),
        "created_at": time.time(),
        "protocol": str(origin.get("protocol") or ""),
        "call_id": str(origin.get("call_id") or ""),
        "turn_id": str(origin.get("turn_id") or ""),
        "origin_sha256": hashlib.sha256(
            str(origin.get("text") or "").encode("utf-8")
        ).hexdigest(),
    }
    descriptor = os.open(audit_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(
            descriptor,
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
