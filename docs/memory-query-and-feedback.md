# Memory query understanding and retrieval feedback

Serena plans memory queries locally with `memory/query_understanding.py`. The
planner is deterministic and has no model, network, database, or download path.
It accepts the current query, at most four recent user turns, and optional
caller-approved people, entity, project, and alias catalogs. Recent context is
used only for short or deictic questions such as "what about that?".

The inspectable plan contains topic terms, matched people and entities, project,
time intent, likely record types, explicit aliases, and bounded typo candidates.
The original normalized query is always retained as the primary query. Aliases
and spelling candidates only add retrieval variants; they never silently replace
what the user wrote.

Raw query-plan values exist only in-process. Durable receipts use
`QueryPlan.to_dict()`, which stores versioned rule names, counts, time bounds,
and SHA-256 digests instead of raw queries, conversation text, names, aliases,
or projects. The resident brain supplies only its broker-bound immediately
previous genuine user turn, not the process-global conversation buffer.

## Feedback contract

Retrieval feedback is local Memory v2 state and is always bound to a persisted
retrieval receipt plus one record actually returned by that receipt.

- `record_memory_feedback` interprets explicit "irrelevant", "not relevant",
  "wrong result", and similar language as reversible relevance feedback.
- A bare "wrong" uses the safe non-mutating relevance interpretation.
- Explicit factual feedback requires complete corrected content. It creates a
  normal Memory v2 update proposal and leaves the canonical record unchanged
  until `review_memory_proposal` approves it.
- `list_memory_feedback` keeps relevance judgments and factual proposals
  visibly separate.
- `revoke_memory_feedback` deactivates a relevance example without deleting its
  audit row. Factual corrections are accepted, rejected, or rolled back through
  proposal review.

The broker classifies the current or immediately previous genuine voice/desk
turn, not model-supplied prose. Feedback reasons and queries are stored as
digests; source receipts retain provenance without raw speech.

Active negative relevance examples apply only to the same query digest and
surface. Their bounded penalty appears in ranking components and receipts, and
their IDs appear in score reasons. Local evaluation treats those records as
negative examples and reports their false-positive behavior. Revocation removes
the penalty on the next retrieval while preserving the audit history.

Focused verification:

```bash
.venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_memory_query_understanding.py tests/test_memory_feedback.py -q
```
