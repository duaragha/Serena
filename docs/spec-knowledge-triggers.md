# Spec: Knowledge Triggers + Usage Tracking

## Overview

**What**: Every KB file gets a `trigger:` line for retrieval routing; every KB read leaves a receipt; staleness and contradictions surface instead of rotting silently.
**Why**: 130+ topics, hand-maintained INDEX already format-drifted, zero read logging, zero trigger convention, weekly maintenance exists only as prose last run 2026-04-08. The KB is flying blind — retrieval can't learn, staleness can't be found.
**Scope**: IN — trigger frontmatter + backfill, `knowledge_hits` receipts, stale/contradiction detection, maintenance wiring, INDEX write fix. OUT — auto-rewriting stale content (flag only); KB embeddings.

## Requirements

- [ ] WHEN a KB file is retrieved THEN a receipt records (timestamp, slug, file, query hash, surface, caller) with no raw query text
- [ ] WHEN `_search_knowledge` ranks THEN trigger text weights alongside titles (×3 zone with headings)
- [ ] WHEN `save_knowledge` creates a file THEN INDEX.md is updated in the same operation (drift source closed)
- [ ] WHEN a topic passes its `last_verified` threshold THEN it appears on the stale report (tech 60d, default 90d)
- [ ] WHEN knowledge feedback is recorded THEN it follows the memory pattern: reviewable proposal, canonical unchanged until approved
- [ ] WHEN the maintenance pass runs THEN it is scheduled code (not prose), covering overlap/stale/orphan + trigger validation

## Architecture / Design

**Changes** (blast radius):
- `MODIFIED knowledge/*` convention — `trigger:` one-liner + `last_verified:` frontmatter (sideload-`description` style); one research leg backfills existing files
- `MODIFIED core/brain_tools.py::_search_knowledge` — heading-weight zone extends to trigger text; `_read_knowledge` callers write receipts
- `ADDED knowledge_hits` store — tiny SQLite table or JSONL (`ts, slug, file, query_sha256, surface, caller`), written by brain tools, chat daemon, computer pack, CLI
- `ADDED record_knowledge_feedback(slug, file, kind)` brokered tool — mirrors `record_memory_feedback` (grounded in genuine user turn, digest-only storage, proposal pipeline)
- `MODIFIED core/brain_memory_tools.py::save_knowledge` — INDEX.md upsert in the same write (atomic with file creation)
- `ADDED core/knowledge_maintenance.py` — scheduled pass (overlap/stale/orphan/trigger validation) replacing `maintenance-prompt.md` prose; wired to scheduler

**Key decisions**:
- Copy the memory feedback contract verbatim (receipts → digests → proposals → review) — proven, privacy-safe, no new concepts
- Triggers are retrieval routing, not display — `_knowledge_index` may swap titles for triggers later; v1 keeps titles
- Flag-only for stale content — rewriting knowledge is a judgment call for the maintenance agent with proposals, never silent mutation
- INDEX fix rides this spec (not repo-briefs) — single owner for the drift source

## Tasks

> Execution: batch as ONE fleet coding run (backfill leg can run parallel).

### Phase 1: Triggers + INDEX fix
- [ ] Frontmatter convention + backfill leg + `save_knowledge` INDEX upsert + search weighting
  - accept: all existing files carry triggers; new file appears in INDEX same-operation; trigger terms rank above body-only matches
  - engine: fleet

### Phase 2: Receipts + feedback
- [ ] `knowledge_hits` writes on all read paths + `record_knowledge_feedback` tool + proposal flow
  - accept: reads from brain/CLI/daemon leave receipts with no raw text; feedback creates reviewable proposal, canonical untouched
  - engine: fleet

### Phase 3: Maintenance wiring
- [ ] Scheduled `knowledge_maintenance` pass (overlap/stale/orphan/triggers) + stale report surface
  - accept: planted stale/orphan/overlap fixtures all surface in one pass run
  - engine: fleet

## Edge Cases / Gotchas

- INDEX.md is format-drifted (per-file links, bare lines) — the updater must preserve unknown lines, not normalize them away
- Receipt volume: cap table/JSONL growth (rotation or aggregation; decide at build, document it)
- `last_verified` backfill: unknown = epoch (forces review) vs file mtime (lenient) — pick epoch; leniency hides rot
- Contradiction detection v1 is feedback-driven (user/agent flags), not pairwise NLI over 800 files — full semantic dedupe is a later project

## Testing

- [ ] Trigger ranking test (trigger term beats body-only)
- [ ] INDEX upsert atomicity test (file + index or neither)
- [ ] Receipt presence + no-raw-text test on all read paths
- [ ] Feedback proposal flow test (canonical unchanged until approval)
- [ ] Maintenance fixture test (stale/orphan/overlap all surface)

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
