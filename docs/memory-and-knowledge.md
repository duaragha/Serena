# Serena Memory & Knowledge Architecture

## Fleet operational playbooks

Fleet worker learning is a separate, project-scoped store in the Fleet database, not a writer to
personal memory. `fleet/learning.py` requires independent Review endorsement, a successfully
completed run with supervisor-observed passing integration gates, completed author/reviewer attempts,
and unchanged evidence-file fingerprints before promotion. Retrieval is bounded to three matching
lessons, the same canonical Git project, task-term relevance, matching evidence hashes and a 30-day lifetime.
`fleet/incidents.py` separately captures unverified failure observations from the event writer,
with a bounded restart backfill. Workers recall incidents and link evidence-backed proposals to them;
observed recovery alone never establishes a cause or promotes a remedy. Proactive findings and
recipient usefulness receipts remain unverified advice in the same local Fleet database.
Candidates are never injected as trusted future guidance. `fleet_revoke_lesson` provides explicit
rollback, and deletion of source runs cascades their provenance-dependent lessons.

`fleet_learning_report` reports measured run outcomes and available token receipts; it does not claim
continuous model training, guaranteed speedups or a measured escaped-defect rate. Fleet never silently
changes provider/model policies or removes review/test gates based on a self-written lesson. Full
mailbox/recovery/learning contracts live in `docs/fleet-runtime.md`.


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

## Computer conversation continuity

`core/computer_conversation.py` maintains a private SQLite record of the exact
launching Codex/Claude chat text and completed computer coaching. The caller
resolves identity before contacting the shared helper. Worker rotations replay
this history, and native `UserPromptSubmit` hooks return coaching to its parent
chat on the next question. This is conversation context, separate from curated
memories and tasks; screen text never becomes action authority. Rollouts remain
unchanged and screenshots are not persisted. See `docs/computer-use.md` for
hook installation, retention and context-size limits.

## Implemented knowledge lifecycle

`core/knowledge_maintenance.py` supersedes the historical prose maintenance plan
above, including its auto-fix/date-stamping instructions. The resident scheduler
installs `serena.knowledge.maintenance` once, runs it immediately and then weekly,
and respects operator pause/removal. No provider call or shell command is used.

Every topic Markdown file carries `trigger: <one-line retrieval description>` and
`last_verified: YYYY-MM-DD` frontmatter. `chats knowledge backfill` adds missing
metadata without rewriting bodies or unknown fields. Existing verification comments
are retained; unknown dates become `1970-01-01`, never file mtime. The scheduled
pass runs this idempotent metadata migration, then reports stale files (tech 60 days,
default 90), orphan topics, duplicate/strongly overlapping bodies, malformed triggers, and pending factual
feedback in `DATA_DIR/knowledge-maintenance.json`. It flags content for review and
does not rewrite research. `chats knowledge maintenance` runs the same pass manually.
Overlap uses exact normalized-body hashes or at least 80% three-word-shingle
similarity on the first 10,000 words (minimum 20 distinct shingles); it is a
review hint, never an automatic merge or semantic contradiction assertion.
Private knowledge is absent from isolated Git checkouts; its backfill runs against
the configured runtime knowledge directory after integration, or via the explicit CLI.

`core/knowledge_store.py` owns note/INDEX writes. Unknown INDEX lines and per-file
links are preserved. A process/thread lock, same-directory atomic replacements,
and a durable write-ahead journal make the pair recoverable. A failed INDEX replace
rolls back the note; a process interruption rolls forward before the next cooperating
read/write. Readers outside these APIs cannot observe a two-file atomic snapshot.
No filesystem offers a single rename transaction across two independent paths.

Knowledge read surfaces persist `knowledge_hits` in `DATA_DIR/knowledge.sqlite3`:
timestamp, slug, filename, SHA-256 query/content hashes, surface, caller, receipt ID.
Brain reads/search, daemon catalog reads, computer packs, CLI, and FTS use this store.
The newest 10,000 receipts are retained; raw queries and feedback speech are never
stored. Internal indexing/maintenance scans are not user retrieval receipts.
Trigger text joins titles in the brain search heading zone at three times body weight.

`record_knowledge_feedback` validates a genuine current/previous user turn and the
persisted file receipt. Relevance feedback and factual corrections are distinct
reviewable proposals. Factual corrections require a complete candidate; canonical
Markdown stays unchanged until `review_knowledge_proposal` explicitly approves it.
Approval checks the retrieved content hash to reject stale corrections. Consent must
be a complete, present-tense instruction whose review verb takes this proposal as its
object. Negation ("do not approve that proposal"), a verb aimed elsewhere ("approve
the deployment and leave the knowledge proposal unchanged"), a question ("should I
approve this proposal?"), deferred or conditional consent ("approve it only after I
confirm tomorrow"), quoted review language (`the document says "approve that
proposal"`), and any cited identifier that is not this proposal's are all refused,
never applied. Receipts also
require the canonical file to still exist, so a deleted note never produces a
retrieval record even when a cached index row still holds its content. Inspection
and review are also available through `chats knowledge proposals` and
`chats knowledge review <id> approve|reject`. Rejection never changes the note.

## Repository brief convention

Every code index refresh generates a missing brief at `repo-<key>/brief.md` with six
sections: layout, entry points, key modules, data stores, build commands, test commands.
Simple lowercase registry keys remain literal; path-like keys use a readable slug
plus a stable hash to prevent collisions and traversal. Ticket clones remain distinct.
Generation uses only guarded/redacted code-index chunks. Missing evidence is stated,
and inferred commands are labeled. No repository scripts run during generation.

`drift.json` stores the indexed HEAD, file hashes, brief hash, timestamp, and stale
state. A HEAD change or changed-file ratio greater than 20% marks it stale before
refresh. The stale state doubles as a durable retry queue; failed generation is
reported by index refresh and retried on the next refresh. Brief replacement is
atomic. `chats code brief <key>` shows it, `--generate` rebuilds from the existing
index, and `--refresh` updates the index first. Zero-hit `recall_code` returns
ranked brief excerpts without regenerating anything.
