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
