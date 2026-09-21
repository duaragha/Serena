# Spec: Per-Repo Code Index

## Overview

**What**: Full-text index of configured repos' code in SQLite FTS, queryable from CLI and `unified_search`, so "how does X work in repo Y" answers with file:line citations.
**Why**: Serena indexes chats, memory, and knowledge — but not code. Code questions today are grep roulette. A code corpus closes the biggest retrieval gap and feeds plan mode's evidence gathering later.
**Scope**: IN — explicit repo registry, scanner + incremental indexer in a separate DB, chunked FTS with identifier-aware matching, `chats code` CLI group, `unified_search` 4th source, thin brain `recall_code` wrapper. OUT — watchdog auto-refresh (manual/periodic refresh v1), trigram tokenizer (only if recall proves bad), dense embeddings for code, `.gitignore` negation, files >200KB, cross-repo ranking tuning.

## Requirements

- [x] WHEN a repo is added to the registry and refreshed THEN its code files are searchable with `repo:rel_path:start-end` citations
- [x] WHEN refresh runs with no changes THEN it completes on stat-skip alone (no file reads, no FTS writes)
- [x] WHEN a file changes THEN only that file's FTS rows are deleted and re-inserted (never a full rebuild)
- [x] WHEN a file is deleted from the repo THEN its rows are pruned from the index (zombie prune)
- [x] WHEN a query uses a `snake_case` or `camelCase` identifier THEN hits match regardless of which form is stored
- [x] WHEN a path matches `.gitignore`, skip-directories, or protected-path guards THEN it is never read or indexed
- [x] WHEN file content contains secret patterns THEN the indexed text is redacted before insert
- [x] WHEN the same repo is checked out on two OSes THEN `rel_path + repo_key` (never absolute path) is the row identity
- [x] WHEN code search runs THEN it never blocks chat/knowledge index writers (separate DB file, separate lock)

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/code_index.py` — DB layer (`code-repos` registry load, `code_repos/code_files/code_fts` DDL, `update_code_index`, `search_code_fts`), mirrors `core/indexer.py` patterns (`_get_db`/`_migrate`, `skip_if_running` lock, stat-skip, zombie prune)
- `ADDED core/code_scanner.py` — file discovery: `.gitignore` subset (`*`, `**`, trailing-slash dirs; no negation v1), skip-directories, binary/minified/size guards, secret redaction before FTS insert
- `ADDED ~/.config/serena/code-repos.json` — explicit registry `{repo_key: root_path}`, roots validated via `projects.project_root()`, cross-OS translation via `canonical_cwd`/`resolve_session_cwd`
- `ADDED ~/.local/share/chats/code-index.db` — separate DB file (storage class 3/4: derived, rebuildable, never in repo tree)
- `MODIFIED core/indexer.py` — `unified_search` gains code as a 4th concatenated source (one call, no ranking merge — same as today)
- `MODIFIED cli.py` — `chats code add/remove/list/status/refresh/search` group mirroring `chats knowledge`
- `MODIFIED core/brain_tools.py` — thin `recall_code` wrapper returning `repo:rel_path:start-end` citation lines (bounded chars, same as `recall_chats`)

**Data model**:
```
code_repos {
  repo_key: TEXT PK — project_root(cwd), e.g. ~/Documents/Projects/personal_projects/locket
  display_name: TEXT, root_path: TEXT, remote_url: TEXT,
  head_sha: TEXT — informational (no git walking v1)
  branch: TEXT, file_count: INTEGER, total_size: INTEGER,
  modified: REAL, indexed_at: TEXT
}
code_files {
  id: INTEGER PK, repo_key: TEXT, rel_path: TEXT,
  file_path: TEXT — absolute at index time, translated at query time
  lang: TEXT — ext→lang map, NULL unknown
  file_size: INTEGER, file_mtime: REAL, content_hash: TEXT — sha256
  indexed_at: TEXT, UNIQUE(repo_key, rel_path)
}
code_fts VIRTUAL TABLE fts5(
  content, repo_key UNINDEXED, rel_path UNINDEXED,
  chunk_ordinal UNINDEXED, start_line UNINDEXED, end_line UNINDEXED,
  lang UNINDEXED) — unicode61 tokenizer v1 (see decisions)
```

**Chunking**: 50-line chunks, no overlap, `start_line/end_line` recorded. Snippet assembly merges adjacent chunks at query time. One-row-per-file was rejected: `snippet()`/citations break on large files and there are no per-file offsets in FTS5.

**Incremental protocol**: per-file `(size, mtime)` stat-skip; on mismatch read file → sha256 → compare stored hash → skip if same; else `DELETE code_fts WHERE repo_key+rel_path` + re-chunk + `INSERT`. Zombie prune by discovered-set diff, same as sessions.

**Identifier matching**: unicode61 + preprocessing — split `camelCase`/`snake_case`/kebab into tokens, index both original and split forms. Trigram rejected for v1 (3–5× DB bloat); revisit only if recall tests fail.

**Guards** (all before read): `.gitignore` subset, `_SKIP_DIRECTORIES` + `security_policy` protected paths, binary probe (null byte in first 8KB → skip), minified probe (avg line length >500 → skip as generated), size cap 200KB, `_SECRET_PATTERNS` redaction on indexed text.

**Key decisions**:
- Explicit registry (Raghav call 2026-09-16) over auto-discover — bounded, predictable, no surprise indexing of huge trees
- Separate `code-index.db` over same-DB tables — a big code refresh must never contend with the chat index lock
- Rescan + stat-skip over watchdog (v1) — matches every existing scanner; watchers miss events under Syncthing bursts and need a rescan backstop anyway
- Chunked rows from day one — avoids a schema migration the moment someone searches a large file
- `unified_search` concatenation, not ranking merge — consistent with how chat/knowledge/memory combine today; ranking upgrades are a separate project

## Tasks

> Execution: all fleet-tagged tasks ship as ONE fleet coding run with this spec attached.

### Phase 1: Registry + scanner
- [x] `code-repos.json` load/validate (`project_root`, cross-OS translation) + `core/code_scanner.py` discovery with all guards + redaction
  - accept: fixture tree scan yields exactly the expected file set (ignores honored, binaries/minified/oversize skipped, secrets redacted in captured text)
  - engine: fleet

### Phase 2: Index + incremental
- [x] `core/code_index.py`: DDL/migrate, `update_code_index` (stat-skip + hash + file-level upsert + zombie prune), `search_code_fts` with chunk-merge + snippet
  - accept: index fixture repo → search finds snake + camel identifiers with correct line ranges; second refresh does zero reads/writes; delete-a-file refresh prunes its rows
  - engine: fleet

### Phase 3: Surfaces
- [x] `chats code` CLI group + `unified_search` 4th source + `recall_code` brain wrapper
  - accept: CLI roundtrip (add → refresh → search → remove) on a real repo; `unified_search` returns code hits alongside existing sources; `recall_code` citations match the `repo:rel_path:start-end` contract
  - engine: fleet

### Phase 4: Tests + docs
- [x] Pytest suite (fixture repo, guards, incremental, identity) + storage-class note in `docs/operations.md` if it enumerates index DBs
  - accept: `pytest tests/test_code_index.py` green; cross-OS identity test asserts `rel_path + repo_key` keys (absolute paths differ, rows match)
  - engine: fleet

## Edge Cases / Gotchas

- FTS5 has no UPDATE-by-key — file refresh is always DELETE + re-INSERT scoped to `repo_key + rel_path`; never a table-wide rebuild
- Per-ticket clone layout (`liquid-fw43…`) means the same logical repo may appear under many roots — registry keys are explicit paths, no auto-folding v1 (document it)
- `drop_index` semantics: code index gets its own drop (`chats code drop` / function), never coupled to the chat index wipe
- Absolute `file_path` stored is stale the moment a checkout moves — always translate `root + rel_path` at query time for display; identity never depends on it
- Knowledge FTS refreshes at topic granularity (stale-file bug documented in research) — do NOT copy that pattern; file-level is mandatory here
- Minified/long-line probe will skip some legitimate files (lockfiles, generated) — that is the intent; log skip counts in `status`

## Testing

- [x] Fixture-tree scan: exact expected file set, all guards firing, redaction verified
- [x] Identifier recall: snake + camel queries hit both stored forms with correct line ranges
- [x] Incremental: no-change refresh performs zero reads/writes; content-change refresh upserts one file; delete prunes
- [x] Cross-OS identity: same rel files under different roots share keys
- [x] Guards: `.gitignore` subset (`*`, `**/`, `dir/`), skip-dirs, protected paths, binary, minified, 200KB cap
- [x] CLI roundtrip + `unified_search` code hits + `recall_code` citation contract

---

## Progress Log

**Status**: Implemented and review findings fixed; awaiting Fleet integration
**Branch**: `serena/fleet/e8bf5527-ef62-4960-951d-579deaeaed6d/agent-a`
**Current phase**: Fix handoff
**Last completed task**: All four phases, plus the three review findings — cross-OS `personal_projects` layout translations are accepted (`layout_forms`), split identifier tokens moved to their own FTS column (schema v2, rebuild on upgrade) so snippets are source-only and match-centred, and every redaction pass now preserves newline counts
**Files modified**: `core/code_index.py`, `core/code_scanner.py`, `core/indexer.py`, `core/brain_tools.py`, `cli.py`, `tests/test_code_index.py`, `tests/test_brain_tools_read_only.py`, `docs/operations.md`, this spec
**Blockers**: No feature blocker. Broader regression limits: memory retrieval's existing `record_id` KeyError; four project-grouping failures under Fleet runtime paths; UI collection cannot open its runtime database. A wider Fleet regression was interrupted after repeated unrelated failures/timeouts, not reported as passing.
**Review**: Fleet owns integration and final review; no live daemon restart or operator registry changes performed.
