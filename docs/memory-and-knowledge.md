# Serena Memory & Knowledge Architecture


---

# Local memory retrieval and rollout

Serena generates memory candidates through three private local channels:

1. exact record, entity, person, project, and ledger identifiers;
2. SQLite FTS5 with BM25 over content and structured fields;
3. genuine dense embeddings from an explicitly staged local Sentence
   Transformers model.

`memory/hybrid.py` owns candidate generation and vector caching.
`memory/reranker.py` applies validity, sensitivity, state, temporal, confidence,
feedback, deduplication, and diversity policy. `memory/retrieval.py` remains the
one authority facade used by the CLI, resident brain, mobile, voice, and prompt
packing surfaces.

No memory text is sent to a cloud embedding service. The runtime accepts only a
local model directory. It sets Hugging Face offline and telemetry-disabled
controls, passes `local_files_only=True`, and refuses remote model code. If the
model is absent or temporarily fails, exact and FTS5 retrieval continue. The
receipt records `semantic_status`, the fallback reason, candidate counts, model
metadata, and cache activity. The old synonym map is not used.

## Explicit local model setup

Install the optional runtime deliberately:

```bash
.venv/bin/python -m pip install -e '.[semantic-memory]'
```

Acquire a semantic-search model into a private local directory in a separate,
explicit setup step. One suitable small model is
`sentence-transformers/multi-qa-MiniLM-L6-cos-v1`, which produces 384-dimension
vectors. Model acquisition does not read or transmit memories. Point Serena at
the completed local snapshot:

```bash
export SERENA_MEMORY_EMBEDDING_MODEL="$HOME/.config/serena/models/memory-embedding"
```

The directory is content-hashed. Cached vectors are used only when record text
hash, model id, model version, model hash, dimension, normalization, and cache
schema all match. A mismatch is re-embedded locally. The default legacy cache is
`~/.local/state/serena/memory-retrieval-cache.sqlite3`; set
`SERENA_MEMORY_RETRIEVAL_CACHE` to choose another private local path.

## Private regression corpus

The corpus is JSONL and belongs in ignored private state, never Git. The first
row is metadata; every later row is a positive or explicit no-answer case:

```json
{"kind":"corpus","schema_version":1,"corpus_id":"raghav-memory-v1","description":"private frozen retrieval cases"}
{"kind":"case","case_id":"atlas-channel","query":"which channel deploys Atlas?","expected_record_ids":["legacy:project:41"],"tags":["project","paraphrase"]}
{"kind":"case","case_id":"unknown-answer","query":"what is the zephyr code?","expected_record_ids":[],"expect_no_answer":true,"tags":["negative"]}
```

`memory/evaluation.py` validates this format and writes mode `0600` files. A
versioned report contains only query hashes, expected and returned record ids,
receipt ids, corpus hash, retrieval/ranking/model versions, Recall@K, MRR,
Precision@K, no-answer false-positive rate, context-budget pass rate, and
flooding rate.

## Shadow migration and evaluation

These commands require explicit candidate paths. They refuse the configured or
default live Memory v2 path and never call authority activation:

```bash
.venv/bin/python -m scripts.memory_retrieval shadow-migrate \
  ~/.config/serena/memory-candidates/retrieval-v1.sqlite3 \
  --model-path ~/.config/serena/models/memory-embedding

.venv/bin/python -m scripts.memory_retrieval evaluate \
  ~/.config/serena/memory-candidates/retrieval-v1.sqlite3 \
  ~/.config/serena/evaluation/memory-corpus.jsonl \
  --report ~/.config/serena/evaluation/retrieval-v1-report.json \
  --top-k 5 \
  --model-path ~/.config/serena/models/memory-embedding
```

Shadow migration is idempotent. It copies legacy records into the isolated
candidate, builds FTS and vector caches there, leaves the live Markdown and v2
stores untouched, and returns a content-hashed receipt.

## Canary and rollback

Create a default-safe shadow pointer first:

```bash
.venv/bin/python -m scripts.memory_retrieval canary \
  ~/.config/serena/memory-rollout.json \
  ~/.config/serena/memory-candidates/retrieval-v1.sqlite3 \
  --mode shadow
export SERENA_MEMORY_RETRIEVAL_ROLLOUT="$HOME/.config/serena/memory-rollout.json"
```

Shadow mode runs the candidate locally but always serves baseline results. A
canary percentage uses a salted stable hash, so the same request remains in the
same variant. Candidate failures fail back to baseline and are represented by
hashed diagnostics. Rollback changes only the pointer and never deletes or
rewrites candidate or canonical memory:

```bash
.venv/bin/python -m scripts.memory_retrieval rollback \
  ~/.config/serena/memory-rollout.json --reason "candidate regression"
```

Do not point canary state at the live v2 database. Promotion and authority
activation remain separate explicit operations.

Focused verification:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_memory_hybrid.py tests/test_memory_evaluation_rollout.py -q
```

---

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

---

# Knowledge Base Maintenance — Spec

## Problem

research gets added constantly but nothing cleans it up. topics overlap, content goes stale, INDEX.md gets out of sync, and files accumulate without anyone checking if they're still relevant. right now it's 43 topics and 258 files — manageable but growing fast.

## Solution

a weekly scheduled agent that reads the entire knowledge base, audits it, fixes what it can, and reports what it can't.

## What the Agent Does

### 1. Overlap Detection
- reads every topic's README.md and scans file names across all folders
- flags topics that cover the same domain (e.g., `typescript-2026/`, `typescript-clean-code/`, `typescript-tooling-2026/` — should these merge?)
- checks for files in different topics that cover the same subtopic
- outputs a list of suggested merges, doesn't force them — some overlap is intentional

### 2. Stale Content Detection
- checks INDEX.md research dates against current date
- anything older than 90 days gets flagged as potentially stale
- tech topics (frameworks, libraries, APIs) get a shorter threshold — 60 days
- personal topics (restaurants, workout) get longer — 180 days
- adds `last_verified: YYYY-MM-DD` to files it reviews so future runs skip recently checked content

### 3. INDEX.md Sync
- compares folders on disk vs entries in INDEX.md
- flags folders that exist but aren't in INDEX.md (orphans)
- flags INDEX.md entries that point to folders that don't exist (dead links)
- auto-fixes dead links by removing them
- lists orphans in the report for manual review

### 4. Empty/Tiny Files
- finds .md files under 100 bytes — probably stubs or abandoned
- finds topics with only a README.md and no other files — might be incomplete
- flags these in the report

### 5. Cross-Reference Check
- looks for topics that reference each other's content but aren't linked
- suggests cross-links between related topics

### 6. Formatting Consistency
- checks that every topic folder has a README.md
- checks that README.md has a `# Title` and file index
- checks for consistent heading structure across files
- fixes minor formatting issues (trailing whitespace, double blank lines, missing newline at EOF)

## Report Format

the agent writes a report to `~/Documents/Projects/knowledge/MAINTENANCE_REPORT.md`:

```
# Knowledge Base Maintenance Report
Run: 2026-04-15

## Overlap Detected
- typescript-2026/ and typescript-clean-code/ have significant overlap in patterns content
- google-ads/ and meta-ads/ both cover ad platform APIs — consider a shared "paid-ads/" topic

## Stale Content (>90 days)
- react-19/ — last research date 2025-12-15 (115 days ago)
- hydrogen-2026/ — no research date found

## INDEX.md Issues
- ORPHAN: ai-knowledge-systems/ exists on disk but not in INDEX.md (added)
- DEAD: removed entry for "deleted-topic/" (folder doesn't exist)

## Tiny/Incomplete
- phone-alerting/README.md is only 85 bytes
- voice-dictation/ has only README.md, no subtopic files

## Formatting Fixed
- Added missing newline at EOF in 3 files
- Fixed double blank lines in supabase/auth-patterns.md

## Stats
- 43 topics, 258 files, 2.2MB total
- 4 stale topics flagged
- 2 overlap groups detected
- 1 orphan added to INDEX.md
- 1 dead link removed
```

## Implementation

### Option A: `/schedule` (preferred)
- runs on anthropic's servers, laptop doesn't need to be on
- weekly cron: `0 9 * * 1` (monday 9am)
- the prompt file lives at `~/Documents/Projects/knowledge/.claude/maintenance-prompt.md`
- full access to filesystem so it can read/write knowledge files directly

### Option B: local cron + `claude -p`
- fallback if `/schedule` doesn't work or isn't available
- `0 9 * * 1 cd ~/Documents/Projects/knowledge && claude -p "$(cat .claude/maintenance-prompt.md)" --dangerously-skip-permissions --max-budget-usd 1`
- needs laptop on at that time

### The Prompt File

a markdown file that gives the agent clear instructions:
- what to check (all 6 items above)
- where the knowledge base lives
- how to write the report
- what it can auto-fix vs what it should only flag
- to read Persona.md so it writes the report in my voice

### Auto-Fix vs Flag Only

**auto-fix:**
- dead links in INDEX.md
- orphan folders (add to INDEX.md)
- formatting issues (whitespace, newlines)
- `last_verified` date stamps

**flag only (don't auto-fix):**
- topic merges (needs human judgment)
- stale content (might still be relevant)
- tiny files (might be intentionally brief)
- cross-reference suggestions

## Files to Create
1. `~/Documents/Projects/knowledge/.claude/maintenance-prompt.md` — the agent's instructions
2. update `~/.claude/settings.json` or use `/schedule` to register the weekly trigger
