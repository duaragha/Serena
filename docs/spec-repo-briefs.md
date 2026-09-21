# Spec: Auto Repo Briefs (mini-deepwiki)

## Overview

**What**: Every indexed repo gets an auto-generated architecture brief in knowledge (`repo-<key>/brief.md`), refreshed when the code drifts, injected into fleet workers and code answers.
**Why**: The KB is 100% hand-curated and already drifting; fleet workers start cold on every repo (brain guesses `likely_files`); `recall_code` has no fallback when FTS misses. A generated brief kills all three.
**Scope**: IN — brief format + generator + drift-triggered refresh + worker injection + recall fallback. OUT — hand-editing workflow for briefs (regen only v1); diagrams.

## Requirements

- [ ] WHEN a repo is indexed THEN a brief exists at `repo-<key>/brief.md` with layout, entry points, key modules, data stores, build/test commands
- [ ] WHEN code drifts past threshold (changed-file ratio or HEAD move) THEN the brief is marked `stale:true` and regen is queued
- [ ] WHEN a fleet worker starts on a briefed repo THEN its prompt includes the brief (bounded chars)
- [ ] WHEN `recall_code` has no FTS hits THEN it falls back to the brief
- [ ] WHEN a brief regenerates THEN the previous version is replaced atomically (no half-written briefs served)

## Architecture / Design

**Changes** (blast radius):
- `ADDED knowledge/repo-<key>/brief.md` convention + `drift.json` (`head_sha`, `indexed_at`, `file_hashes` subset)
- `ADDED core/repo_brief.py` — `generate(repo_key)` (reads `code_files` hashes + top-level files via `code_scanner` guards + `computer_knowledge` redaction), `is_stale(repo_key)`, `refresh_if_needed(repo_key)`
- `MODIFIED core/code_index.py` — `update_code_index` reports changed-file ratio / HEAD move to the staleness check
- `MODIFIED fleet/supervisor.py::_worker_prompt` — inject `brief.md` (bounded excerpt + receipt, same treatment as peer outputs) when `run.cwd` matches a briefed repo
- `MODIFIED core/brain_tools.py::recall_code` — brief fallback on zero FTS hits
- `MODIFIED knowledge/reader.py` — `repo-*` slugs readable through existing topic APIs; `chats code brief <repo>` CLI (generate/show/refresh)

**Data model**:
```
drift.json {
  repo_key, head_sha, indexed_at, brief_sha256,
  file_hashes: {rel_path: sha256} — subset for staleness,
  stale: bool, stale_reason: str
}
```

**Key decisions**:
- Briefs live in knowledge (not a new store) — they reuse reader, FTS mirror, and Locket sync for free
- Staleness from the code index's own hashes — no second watcher; refresh piggybacks on index updates
- Worker injection bounded + receipted like peer context — briefs are evidence, not gospel
- Regen-only v1 — hand edits would be overwritten; if curation is wanted later, add an `overrides.md` layer then

## Tasks

> Execution: batch as ONE fleet coding run.

### Phase 1: Format + generator
- [ ] `repo-<key>/` convention + `core/repo_brief.py` generate/stale/refresh + `chats code brief`
  - accept: fixture repo yields a brief with all six sections; content within redaction rules (no secrets)
  - engine: fleet

### Phase 2: Refresh + consumers
- [ ] Drift trigger in index updates + worker-prompt injection + recall fallback
  - accept: touching >threshold files marks stale and regens; worker prompt contains brief for briefed cwd only; recall falls back on zero hits
  - engine: fleet

## Edge Cases / Gotchas

- Brief generation reads code through the same guards as the indexer (gitignore, binary, secrets) — never raw tree walks
- Monorepos / ticket clones (`liquid-fw43…`) — one brief per registry key, no auto-folding (same rule as the index)
- `save_knowledge`-style writes must also update INDEX.md — fix the drift source while here (see knowledge-triggers spec; coordinate, don't duplicate)
- Brief staleness during a run: workers get the brief as-of dispatch; mid-run regen must not rewrite a brief a live leg cited (version by `brief_sha256` in receipts)

## Testing

- [ ] Generator golden test on fixture repo (sections present, secrets absent)
- [ ] Staleness trigger test (ratio + HEAD move)
- [ ] Worker injection test (briefed vs unbriefed cwd)
- [ ] Recall fallback test
- [ ] Atomic-replace test (no partial reads during regen)

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
