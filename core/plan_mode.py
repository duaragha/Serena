"""Plan mode: read-only evidence gathering + fleet-prompt builder.

`gather_evidence()` composes the existing structured retrieval layers (chat
FTS, memory retrieval, knowledge FTS) without new retrieval code, then
normalizes the three citation dialects into one. `build_fleet_prompt()`
turns evidence + clarifier answers into a `start_run`-ready artifact.
Deterministic throughout: no model calls, golden-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXCERPT_CHARS = 200
FINDINGS_PER_SOURCE = 5


@dataclass
class Citation:
    source: str  # chat | kb | mem | ledger
    key: str  # sid8 | slug[/file] | type:id | ledger key
    title: str = ""
    excerpt: str = ""

    def line(self) -> str:
        head = f"{self.source}:{self.key}"
        if self.title:
            head += f" — {self.title}"
        if self.excerpt:
            head += f" ({self.excerpt})"
        return head


@dataclass
class Evidence:
    query: str
    chats: list[Citation] = field(default_factory=list)
    memory: list[Citation] = field(default_factory=list)
    knowledge: list[Citation] = field(default_factory=list)
    ledgers: list[Citation] = field(default_factory=list)
    receipt_ids: list[str] = field(default_factory=list)

    def all_citations(self) -> list[Citation]:
        return self.chats + self.memory + self.knowledge + self.ledgers

    def is_empty(self) -> bool:
        return not self.all_citations()


@dataclass
class PlanArtifact:
    task: str
    activity: str
    provider_mode: str
    worker_count: int | None
    cwd: str
    repo_key: str
    citations: list[str]


def _clean(text: object, limit: int = EXCERPT_CHARS) -> str:
    cleaned = " ".join(str(text or "").replace(">>>", "").replace("<<<", "").split())
    return cleaned[:limit].rstrip()


def _chat_citations(rows: list[dict[str, Any]]) -> list[Citation]:
    out: list[Citation] = []
    for row in rows:
        sid = str(row.get("session_id") or "")
        if not sid:
            continue
        title = str(
            row.get("custom_title") or row.get("title") or row.get("agent") or ""
        ).strip()
        out.append(
            Citation(
                source="chat",
                key=sid[:8],
                title=title[:80],
                excerpt=_clean(row.get("snippet")),
            )
        )
    return out


def _memory_citations(hits: list[Any]) -> list[Citation]:
    out: list[Citation] = []
    for hit in hits:
        rid = getattr(hit, "legacy_id", None) or getattr(hit, "record_id", "?")
        rtype = getattr(hit, "legacy_type", None) or getattr(hit, "record_type", "?")
        out.append(
            Citation(
                source="mem",
                key=f"{rtype}:{rid}",
                excerpt=_clean(getattr(hit, "content", "")),
            )
        )
    return out


def _knowledge_citations(rows: list[dict[str, Any]]) -> list[Citation]:
    out: list[Citation] = []
    for row in rows:
        slug = str(row.get("topic_slug") or "")
        if not slug:
            continue
        filename = str(row.get("filename") or "")
        key = f"{slug}/{filename}" if filename else slug
        out.append(Citation(source="kb", key=key, excerpt=_clean(row.get("snippet"))))
    return out


def _ledger_citations(hits: list[Any]) -> list[Citation]:
    out: list[Citation] = []
    for hit in hits:
        content = str(getattr(hit, "content", "") or "")
        key = str(getattr(hit, "record_id", "?"))
        for line in content.splitlines():
            line = line.strip()
            if line.lower().startswith("ledger_key:") or line.lower().startswith("key:"):
                key = line.split(":", 1)[1].strip().split()[0]
                break
        out.append(Citation(source="ledger", key=key, excerpt=_clean(content)))
    return out


def gather_evidence(
    query: str,
    *,
    limit_per_source: int = FINDINGS_PER_SOURCE,
) -> Evidence:
    """Run the four read-only searches. Never raises on empty/missing indexes."""
    from core import indexer
    from memory.retrieval import retrieve_memory

    query = (query or "").strip()
    evidence = Evidence(query=query)
    if not query:
        return evidence

    try:
        rows = indexer.search_fts(query, limit=limit_per_source)
    except Exception:
        rows = []
    evidence.chats = _chat_citations(rows or [])

    try:
        result = retrieve_memory(query, limit=limit_per_source, surface="private")
    except Exception:
        result = None
    if result is not None:
        evidence.memory = _memory_citations(list(result.hits or []))
        receipt_id = (result.receipt or {}).get("receipt_id", "")
        if receipt_id:
            evidence.receipt_ids.append(str(receipt_id))

    try:
        rows = indexer.search_knowledge_fts(query, limit=limit_per_source)
    except Exception:
        rows = []
    if not rows:
        # Same recovery as `chats knowledge search`: build once when empty.
        try:
            indexer.update_knowledge_index()
            indexer.build_knowledge_fts()
            rows = indexer.search_knowledge_fts(query, limit=limit_per_source)
        except Exception:
            rows = []
    evidence.knowledge = _knowledge_citations(rows or [])

    try:
        ledgers = retrieve_memory(
            query, limit=limit_per_source, surface="private", record_type="ledger"
        )
    except Exception:
        ledgers = None
    if ledgers is not None:
        evidence.ledgers = _ledger_citations(list(ledgers.hits or []))
        receipt_id = (ledgers.receipt or {}).get("receipt_id", "")
        if receipt_id:
            evidence.receipt_ids.append(str(receipt_id))

    return evidence


def suggest_clarifiers(
    evidence: Evidence,
    *,
    repo_hint: str = "",
) -> list[str]:
    """Deterministic clarifier questions. Repo pin first when ambiguous."""
    questions: list[str] = []
    if not (repo_hint or "").strip():
        questions.append(
            "Which repo should this run against? (path under Projects; required — "
            "fleet cannot launch on an ambiguous checkout)"
        )
    if not evidence.memory and not evidence.ledgers:
        questions.append(
            "Is there prior context (a decision, person, or earlier attempt) I should "
            "know that memory doesn't hold?"
        )
    if not evidence.knowledge:
        questions.append(
            "Any docs, links, or reference material I should read before planning?"
        )
    if evidence.is_empty():
        questions.append(
            "What does 'done' look like — how will you verify the result?"
        )
    return questions[:4]


def resolve_repo_cwd(repo_hint: str) -> str:
    """Validate an explicit repo path to an absolute cwd. Raises ValueError."""
    from core.projects import project_root

    raw = (repo_hint or "").strip()
    if not raw:
        raise ValueError("a repo path is required (no guessing)")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"repo path must be absolute: {raw}")
    if not path.is_dir():
        raise ValueError(f"repo path is not a directory: {raw}")
    root = project_root(str(path))
    if not root:
        raise ValueError(f"repo path is not inside a known project: {raw}")
    return str(path.resolve())


def build_fleet_prompt(
    evidence: Evidence,
    answers: dict[str, str],
    *,
    activity: str = "auto",
    provider_mode: str = "auto",
    worker_count: int | None = None,
) -> PlanArtifact:
    """Compose the start_run-ready artifact. Pure function of its inputs."""
    from core.projects import project_key

    cwd = resolve_repo_cwd(answers.get("repo", ""))
    try:
        repo_key = project_key(None, cwd)
    except Exception:
        repo_key = cwd
    context = (answers.get("context") or "").strip()
    done = (answers.get("done") or "").strip()

    lines = [f"Task: {evidence.query}", "", f"Repo: {cwd}"]
    if context:
        lines += ["", f"Operator context: {context}"]
    if done:
        lines += ["", f"Done means: {done}"]
    citations = [c.line() for c in evidence.all_citations()]
    if citations:
        lines += ["", "Evidence (read-only findings; verify before trusting):"]
        lines += [f"- {line}" for line in citations]
    if evidence.receipt_ids:
        lines += ["", f"Retrieval receipts: {', '.join(evidence.receipt_ids)}"]

    return PlanArtifact(
        task="\n".join(lines),
        activity=activity,
        provider_mode=provider_mode,
        worker_count=worker_count,
        cwd=cwd,
        repo_key=repo_key or cwd,
        citations=citations,
    )
