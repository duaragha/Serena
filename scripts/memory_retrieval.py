"""Explicit local tooling for Memory v2 shadow evaluation and rollout."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from memory.evaluation import load_regression_corpus
from memory.rollout import (
    RetrievalRolloutController,
    evaluate_shadow_database,
    shadow_migrate_legacy,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.memory_retrieval",
        description=(
            "Build and evaluate an isolated memory candidate. These commands never activate "
            "Memory v2 or modify the legacy Markdown authority."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    shadow = commands.add_parser(
        "shadow-migrate",
        help="copy legacy memories into an inactive candidate database",
    )
    shadow.add_argument("destination", help="new or existing inactive candidate SQLite path")
    shadow.add_argument(
        "--model-path",
        help="explicit local Sentence Transformers directory; no model is downloaded",
    )

    evaluate = commands.add_parser(
        "evaluate",
        help="evaluate an inactive candidate against a private JSONL corpus",
    )
    evaluate.add_argument("database", help="inactive candidate SQLite path")
    evaluate.add_argument("corpus", help="private regression corpus JSONL path")
    evaluate.add_argument("--report", required=True, help="private versioned JSON report path")
    evaluate.add_argument("--top-k", type=int, default=5)
    evaluate.add_argument(
        "--model-path",
        help="explicit local Sentence Transformers directory; no model is downloaded",
    )

    canary = commands.add_parser(
        "canary",
        help="write a default-safe shadow or deterministic canary pointer",
    )
    canary.add_argument("state", help="local rollout state JSON path")
    canary.add_argument("candidate", help="inactive candidate SQLite path")
    canary.add_argument("--mode", choices=("shadow", "canary"), default="shadow")
    canary.add_argument("--percent", type=float, default=0.0)
    canary.add_argument("--salt", default="")

    rollback = commands.add_parser(
        "rollback",
        help="switch a rollout pointer to baseline without deleting candidate data",
    )
    rollback.add_argument("state", help="local rollout state JSON path")
    rollback.add_argument("--reason", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "shadow-migrate":
        receipt = shadow_migrate_legacy(
            args.destination,
            embedding_model_path=args.model_path,
        )
        payload = receipt.to_dict()
    elif args.command == "evaluate":
        corpus = load_regression_corpus(args.corpus)
        report = evaluate_shadow_database(
            args.database,
            corpus,
            report_path=args.report,
            top_k=args.top_k,
            embedding_model_path=args.model_path,
        )
        payload = report.to_dict()
    elif args.command == "canary":
        state = RetrievalRolloutController(args.state).configure(
            args.candidate,
            mode=args.mode,
            canary_percent=args.percent,
            salt=args.salt,
        )
        payload = state.to_dict()
    else:
        state = RetrievalRolloutController(args.state).rollback(reason=args.reason)
        payload = state.to_dict()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
