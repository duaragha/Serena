"""Private regression corpora and versioned retrieval evaluation reports."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CORPUS_SCHEMA_VERSION = 1
EVALUATION_REPORT_VERSION = "memory-retrieval-evaluation-v1"
DEFAULT_TOP_K = 5
DEFAULT_MAX_CONTEXT_CHARACTERS = 7_000
DEFAULT_MAX_CONTEXT_TOKENS = 1_800
DEFAULT_MAX_CONTEXT_RECORDS = 5

_TOKEN = re.compile(r"[\w]+|[^\w\s]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class RegressionCase:
    case_id: str
    query: str
    expected_record_ids: tuple[str, ...] = ()
    expect_no_answer: bool = False
    surface: str = "private"
    tags: tuple[str, ...] = ()
    max_context_characters: int = DEFAULT_MAX_CONTEXT_CHARACTERS
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    max_context_records: int = DEFAULT_MAX_CONTEXT_RECORDS

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("regression case id is required")
        if not self.query.strip():
            raise ValueError("regression case query is required")
        if self.expect_no_answer and self.expected_record_ids:
            raise ValueError("no-answer cases cannot declare expected records")
        if not self.expect_no_answer and not self.expected_record_ids:
            raise ValueError("positive cases require expected records")
        if min(
            self.max_context_characters,
            self.max_context_tokens,
            self.max_context_records,
        ) <= 0:
            raise ValueError("context budgets must be positive")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["expected_record_ids"] = list(self.expected_record_ids)
        value["tags"] = list(self.tags)
        return value


@dataclass(frozen=True, slots=True)
class RegressionCorpus:
    corpus_id: str
    cases: tuple[RegressionCase, ...]
    description: str = ""
    schema_version: int = CORPUS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CORPUS_SCHEMA_VERSION:
            raise ValueError("unsupported memory regression corpus schema")
        if not self.corpus_id.strip():
            raise ValueError("regression corpus id is required")
        if not self.cases:
            raise ValueError("regression corpus must contain at least one case")
        ids = [case.case_id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("regression corpus case ids must be unique")

    @property
    def sha256(self) -> str:
        return _sha256(_json(self.to_dict()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "corpus_id": self.corpus_id,
            "description": self.description,
            "cases": [case.to_dict() for case in self.cases],
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    report_id: str
    corpus_id: str
    corpus_sha256: str
    retrieval_version: str
    ranking_version: str
    model_metadata: dict[str, Any]
    top_k: int
    case_count: int
    positive_case_count: int
    negative_case_count: int
    recall_at_k: float
    mean_reciprocal_rank: float
    precision_at_k: float
    false_positive_rate: float
    no_answer_accuracy: float
    context_budget_pass_rate: float
    flooding_rate: float
    cases: tuple[dict[str, Any], ...]
    created_at: float
    report_version: str = EVALUATION_REPORT_VERSION

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["cases"] = [dict(case) for case in self.cases]
        value["model_metadata"] = dict(self.model_metadata)
        return value


def load_regression_corpus(path: str | Path) -> RegressionCorpus:
    """Load the private JSONL format without retaining an open file handle."""

    source = Path(path).expanduser()
    rows = []
    try:
        for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid regression corpus JSON on line {line_number}") from exc
            if not isinstance(row, Mapping):
                raise ValueError(f"regression corpus line {line_number} must be an object")
            rows.append(dict(row))
    except OSError as exc:
        raise ValueError(f"unable to read regression corpus: {source}") from exc
    if not rows or rows[0].get("kind") != "corpus":
        raise ValueError("regression corpus must start with a corpus metadata row")
    header = rows[0]
    schema_version = int(header.get("schema_version") or 0)
    cases = []
    for row in rows[1:]:
        if row.get("kind") != "case":
            raise ValueError("regression corpus contains an unsupported row kind")
        cases.append(
            RegressionCase(
                case_id=str(row.get("case_id") or "")[:256],
                query=str(row.get("query") or "")[:2_000],
                expected_record_ids=_strings(row.get("expected_record_ids"), limit=64),
                expect_no_answer=bool(row.get("expect_no_answer", False)),
                surface=str(row.get("surface") or "private")[:64],
                tags=_strings(row.get("tags"), limit=32),
                max_context_characters=int(
                    row.get("max_context_characters") or DEFAULT_MAX_CONTEXT_CHARACTERS
                ),
                max_context_tokens=int(
                    row.get("max_context_tokens") or DEFAULT_MAX_CONTEXT_TOKENS
                ),
                max_context_records=int(
                    row.get("max_context_records") or DEFAULT_MAX_CONTEXT_RECORDS
                ),
            )
        )
    return RegressionCorpus(
        corpus_id=str(header.get("corpus_id") or "")[:256],
        description=str(header.get("description") or "")[:2_000],
        schema_version=schema_version,
        cases=tuple(cases),
    )


def write_regression_corpus(path: str | Path, corpus: RegressionCorpus) -> Path:
    destination = Path(path).expanduser()
    rows = [
        _json(
            {
                "kind": "corpus",
                "schema_version": corpus.schema_version,
                "corpus_id": corpus.corpus_id,
                "description": corpus.description,
            }
        )
    ]
    rows.extend(_json({"kind": "case", **case.to_dict()}) for case in corpus.cases)
    _write_private(destination, "\n".join(rows) + "\n")
    return destination


def evaluate_regression_corpus(
    corpus: RegressionCorpus,
    retriever: Callable[[RegressionCase, int], Any],
    *,
    top_k: int = DEFAULT_TOP_K,
    retrieval_version: str,
    ranking_version: str,
    model_metadata: Mapping[str, Any] | None = None,
    packer: Callable[[RegressionCase, Any], Any] | None = None,
    now: float | None = None,
) -> EvaluationReport:
    """Measure positive recall and negative abstention separately."""

    clean_k = max(1, min(50, int(top_k)))
    positive_count = 0
    negative_count = 0
    recall_total = 0.0
    reciprocal_total = 0.0
    relevant_total = 0
    positive_returned_total = 0
    negative_false_positives = 0
    context_passes = 0
    flooding_cases = 0
    rows = []
    for case in corpus.cases:
        response = retriever(case, clean_k)
        hits = _extract_hits(response)[:clean_k]
        returned_ids = [record_id for record_id, _content in hits]
        expected = set(case.expected_record_ids)
        found = expected.intersection(returned_ids)
        ranks = [returned_ids.index(record_id) + 1 for record_id in found]
        negative = case.expect_no_answer
        if negative:
            negative_count += 1
            false_positive = bool(returned_ids)
            negative_false_positives += int(false_positive)
            recall = 0.0
            reciprocal = 0.0
            precision = 1.0 if not returned_ids else 0.0
        else:
            positive_count += 1
            recall = len(found) / len(expected)
            reciprocal = 1.0 / min(ranks) if ranks else 0.0
            precision = len(found) / len(returned_ids) if returned_ids else 0.0
            recall_total += recall
            reciprocal_total += reciprocal
            relevant_total += len(found)
            positive_returned_total += len(returned_ids)

        packed = packer(case, response) if packer is not None else None
        pack_metrics = _pack_metrics(case, hits, packed)
        context_passes += int(pack_metrics["context_budget_passed"])
        flooding_cases += int(pack_metrics["flooded"])
        receipt_id = _receipt_id(response)
        rows.append(
            {
                "case_id": case.case_id,
                "query_sha256": _sha256(" ".join(case.query.split())),
                "expect_no_answer": negative,
                "expected_record_ids": sorted(expected),
                "returned_record_ids": returned_ids,
                "recall": round(recall, 6),
                "reciprocal_rank": round(reciprocal, 6),
                "precision": round(precision, 6),
                "false_positive": bool(negative and returned_ids),
                "receipt_id": receipt_id,
                **pack_metrics,
            }
        )

    case_count = len(corpus.cases)
    false_positive_rate = (
        negative_false_positives / negative_count if negative_count else 0.0
    )
    return EvaluationReport(
        report_id=str(uuid.uuid4()),
        corpus_id=corpus.corpus_id,
        corpus_sha256=corpus.sha256,
        retrieval_version=str(retrieval_version)[:256],
        ranking_version=str(ranking_version)[:256],
        model_metadata=dict(model_metadata or {}),
        top_k=clean_k,
        case_count=case_count,
        positive_case_count=positive_count,
        negative_case_count=negative_count,
        recall_at_k=round(recall_total / positive_count if positive_count else 0.0, 6),
        mean_reciprocal_rank=round(
            reciprocal_total / positive_count if positive_count else 0.0, 6
        ),
        precision_at_k=round(
            relevant_total / positive_returned_total if positive_returned_total else 0.0,
            6,
        ),
        false_positive_rate=round(false_positive_rate, 6),
        no_answer_accuracy=round(1.0 - false_positive_rate if negative_count else 0.0, 6),
        context_budget_pass_rate=round(context_passes / case_count if case_count else 0.0, 6),
        flooding_rate=round(flooding_cases / case_count if case_count else 0.0, 6),
        cases=tuple(rows),
        created_at=time.time() if now is None else float(now),
    )


def write_evaluation_report(path: str | Path, report: EvaluationReport) -> Path:
    destination = Path(path).expanduser()
    _write_private(destination, json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    return destination


def _extract_hits(response: Any) -> list[tuple[str, str]]:
    raw_hits: Sequence[Any]
    if isinstance(response, Mapping):
        value = response.get("hits") or ()
        raw_hits = value if isinstance(value, Sequence) else ()
    else:
        value = getattr(response, "hits", ())
        raw_hits = value if isinstance(value, Sequence) else ()
    hits = []
    for item in raw_hits:
        if isinstance(item, Mapping):
            record = item.get("record") if isinstance(item.get("record"), Mapping) else item
            record_id = str(record.get("record_id") or item.get("record_id") or "")
            content = str(record.get("content") or item.get("content") or "")
        else:
            record = getattr(item, "record", item)
            record_id = str(getattr(record, "record_id", ""))
            content = str(getattr(record, "content", ""))
        if record_id:
            hits.append((record_id, content))
    return hits


def _pack_metrics(
    case: RegressionCase,
    hits: Sequence[tuple[str, str]],
    packed: Any,
) -> dict[str, Any]:
    if packed is None:
        selected_count = len(hits)
        text = "\n".join(content for _record_id, content in hits)
        character_count = len(text)
        token_count = _estimate_tokens(text)
    elif isinstance(packed, Mapping):
        selected = packed.get("selected_record_ids") or ()
        selected_count = len(selected) if isinstance(selected, Sequence) else int(
            packed.get("record_count") or 0
        )
        character_count = int(packed.get("character_count") or 0)
        token_count = int(packed.get("token_count") or 0)
    else:
        selected_count = len(getattr(packed, "selected_record_ids", ()) or ())
        character_count = int(getattr(packed, "character_count", 0) or 0)
        token_count = int(getattr(packed, "token_count", 0) or 0)
    passed = (
        selected_count <= case.max_context_records
        and character_count <= case.max_context_characters
        and token_count <= case.max_context_tokens
    )
    return {
        "context_record_count": selected_count,
        "context_character_count": character_count,
        "context_token_count": token_count,
        "context_budget_passed": passed,
        "flooded": not passed,
    }


def _receipt_id(response: Any) -> str:
    if isinstance(response, Mapping):
        receipt = response.get("receipt")
        return str(receipt.get("receipt_id") or "") if isinstance(receipt, Mapping) else ""
    receipt = getattr(response, "receipt", {})
    return str(receipt.get("receipt_id") or "") if isinstance(receipt, Mapping) else ""


def _estimate_tokens(text: str) -> int:
    lexical = len(_TOKEN.findall(text))
    character_bound = math.ceil(len(text.encode("utf-8")) / 4)
    return max(lexical, character_bound)


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _strings(value: object, *, limit: int) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, Sequence):
        return ()
    return tuple(
        dict.fromkeys(
            clean
            for item in value[:limit]
            if (clean := " ".join(str(item or "").split())[:256])
        )
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


__all__ = [
    "CORPUS_SCHEMA_VERSION",
    "EVALUATION_REPORT_VERSION",
    "EvaluationReport",
    "RegressionCase",
    "RegressionCorpus",
    "evaluate_regression_corpus",
    "load_regression_corpus",
    "write_evaluation_report",
    "write_regression_corpus",
]
