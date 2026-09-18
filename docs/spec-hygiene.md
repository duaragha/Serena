# Spec: Engineering Hygiene Consolidation

## Overview

**What**: Pay down the structural debt blocking safe feature work — split the 14.5k-line Flask app, dedupe the fleet/core mirrors, finish the memory v2 cutover, and clear the uncommitted backlog into clean commits.
**Why**: Every feature spec touches the same mega-files (`ui/web.py` 14,580 lines, `fleet/supervisor.py` 4,247, `brain_daemon.py` 3,517); fleet-vs-core mirrors invite drift; memory v2 vs legacy split is mid-flight; and the tree carries a large uncommitted backlog. Features can land without this (specs-first per Raghav call), but each one gets riskier.
**Scope**: IN — web.py route split, mirror dedupe, v2 cutover, backlog triage into commits. OUT — behavior changes of any kind (pure moves + deletions; test suites are the proof); reformatting (drift predates, leave it).

## Requirements

- [ ] WHEN the split lands THEN no route module exceeds ~1,500 lines and every existing route + test passes unchanged
- [ ] WHEN mirrors are deduped THEN exactly one implementation remains per concept (shims where external imports exist) and the full suite passes
- [ ] WHEN v2 cutover completes THEN the legacy Markdown corpus path is removed (not just deprecated) and retrieval evals pass at parity-or-better
- [ ] WHEN the backlog triages THEN every hunk lands in a conventional commit or is explicitly discarded with a recorded reason
- [ ] WHEN any step lands THEN behavior is provably unchanged (targeted suites + smoke: web boot, fleet list, memory recall, knowledge search)

## Architecture / Design

**Changes** (blast radius — everything, but moves only):
- `ui/web.py` → route modules by area (sessions, memory/knowledge, frontdoor/bridges, voice, ops) behind the same Flask app object; only `fleet_web.py`/`operator_web.py`/`webhook_web.py` are extracted today — follow that pattern
- `fleet/` vs `core/fleet_*.py` — audit each pair (supervisor, store, policy, capacity, completion…): keep the real one, shim or delete the other; add an import-cycle guard test so they can't re-fork
- Memory v2 — cut the legacy path behind the `retrieval.py` facade, delete dead code, keep evals green (Recall@K/MRR at parity)
- Backlog — triage working tree hunk-by-hunk into `feat/fix/refactor/docs/test/chore` commits; dead paths (`mobile/`, `reminders-legacy`, `VoiceReminder.txt`, `archive/`) deleted or explicitly kept with a note

**Key decisions**:
- Specs-first, hygiene-later (Raghav call 2026-09-17) — features assume the current tree; this spec is scheduled after the first build wave, not before
- Moves only, zero behavior — each step is verifiable by existing suites alone; anything needing new tests is a feature, not hygiene, and gets its own spec
- No reformatting — ruff-format drift predates this work; mixing format churn into moves would make review impossible
- Import-cycle guard test — the mirrors re-forked silently once; the test makes it loud forever

## Tasks

> Execution: direct, sequenced hunk-by-hunk. No fleet — merge-conflict-prone mega-file surgery needs a single careful pair of hands.

### Phase 1: Backlog triage
- [ ] Hunk-by-hunk commits + dead-path decisions
  - accept: clean `git status` except intended untracked; every commit conventional + verified (tests/lint per AGENTS.md)
  - engine: direct

### Phase 2: Mirror dedupe + guard
- [ ] Audit pairs, collapse to one implementation each, add import-cycle test
  - accept: full suite green; cycle test fails on a planted circular import
  - engine: direct

### Phase 3: web.py split
- [ ] Route-area modules behind the same app object
  - accept: no module >1,500 lines; web + workspace suites green; boot smoke passes
  - engine: direct

### Phase 4: v2 cutover
- [ ] Legacy retrieval path removal + eval parity
  - accept: retrieval evals at parity-or-better; no `legacy` read path remains; memory suites green
  - engine: direct

## Edge Cases / Gotchas

- The tree is live (fleet service, brain daemon import from it) — land phases when no fleet run is active, restart `serena-fleet.service` after (resident code lesson, 2026-09-17)
- Syncthing + two machines + no `.git` sync — push each phase to origin before starting the next; never leave the tree mid-split overnight
- `mobile/` empty dir vs `apps/mobile/` real client — confirm dead before deleting (check for references, not just emptiness)
- Knowledge/INDEX drift noted in research is owned by the knowledge-triggers spec — don't fix content here, only code structure

## Testing

- [ ] Full suite green after every phase (not just at the end)
- [ ] Boot + smoke matrix per phase (web, fleet list, memory recall, knowledge search)
- [ ] Import-cycle guard test (planted cycle fails)
- [ ] Retrieval eval parity report for v2 cutover

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
