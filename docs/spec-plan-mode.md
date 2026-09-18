# Spec: Plan Mode (CLI)

## Overview

**What**: Read-only exploration with cited answers → clarifier questions → generated Fleet prompt → dry-run preview → approved launch, starting as `chats plan`.
**Why**: Fleet starts hot today — no plan gate, no approval, wasted runs on misunderstood tasks. A plan stage with a real preview makes every launch deliberate.
**Scope**: IN — planner module composing existing read-only searches, normalized citations, CLI `chats plan` with clarifiers + dry-run preview + authority-gated launch. OUT — brain `"plan"` protocol, web approval card, mobile `plan` message type, voice approval, stateful plan store / cross-surface approval (stateless single-invocation v1), full non-interactive `--answer` flags.

## Requirements

- [ ] WHEN `chats plan "query"` runs THEN it prints cited findings from chats + memory + knowledge + ledger before asking anything
- [ ] WHEN findings are printed THEN every citation uses the normalized form (`chat:<sid8>`, `kb:<slug>`, `mem:<type>:<id>`, `ledger:<id>`) with an excerpt
- [ ] WHEN the repo is ambiguous THEN a clarifier pins it and no launch is possible until it is answered
- [ ] WHEN the user reaches the launch step THEN they see the exact `start_run(dry_run=True)` policy/scaling preview before any approve prompt
- [ ] WHEN the user approves THEN launch goes through an `ActionAuthority` confirmation (`fleet.start_run`, source `cli`); without a granted confirmation no worker wakes
- [ ] WHEN the user declines or aborts THEN nothing is launched and no run row is created (dry-run rows excepted)
- [ ] WHEN `--json` is passed THEN findings, clarifiers, prompt artifact, and dry-run preview emit as machine-readable JSON

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/plan_mode.py` — `gather_evidence(query, repo_hint)`, `build_fleet_prompt(evidence, answers)` → `{task, activity, provider_mode, worker_count, cwd, citations}`. Pure functions; imports read helpers from `core/brain_tools.py` directly (never via MCP); zero new retrieval code
- `MODIFIED cli.py` — `chats plan "query" [--repo PATH] [--approve] [--json]` next to `fleet_group`
- `MODIFIED core/action_authority.py` — register `fleet.start_run` capability if missing (effect `external`); plan mode is the first consumer of the confirmation flow for Fleet launches
- `MODIFIED docs/fleet-runtime.md` — short section documenting the plan → approve → launch contract

**Flow** (single invocation, stateless):
1. `gather_evidence` runs the four read-only searches, normalizes citations
2. Print findings → ask clarifiers (repo pin mandatory when ambiguous, plus up to 3 generated from gaps)
3. `build_fleet_prompt` → print artifact → `start_run(dry_run=True)` → print policy/scaling preview
4. Approve? → `ActionAuthority` confirmation → real `start_run` (origin auto-detect as in `chats fleet start`) : exit clean

**Approval authority**: `ActionAuthority.build_request(capability="fleet.start_run", effect="external", source="cli")` + `request_confirmation`. Typed `cli` source clears up to tier 3. This intentionally does NOT reuse the bespoke spoken-turn broker in `brain_fleet_tools.py` (voice/desk only) — plan mode is the forcing function that starts converging Fleet launches onto `ActionAuthority`.

**Key decisions**:
- Retrieval via structured layers (`indexer.search_fts`, `search_knowledge_fts`, `retrieve_memory` with `record_type` filter) instead of importing `brain_tools` string helpers — revision at build time: string parsing is brittle, structured rows carry the keys citations need. Same sources, better joints.
- CLI first (Raghav call 2026-09-16) — zero daemon/protocol/client changes; brain protocol + web card + mobile type are follow-up specs reusing `core/plan_mode.py`
- Stateless v1 — the approving invocation holds the JSON; no drafts table until cross-surface approval is needed
- `dry_run=True` preview is mandatory in the flow — it is the only honest preview of what approve does, and it costs zero model calls
- `--approve` skips the final confirm but never skips clarifiers; ambiguous repo + non-interactive → refuse with a message, never guess

## Tasks

> Execution: all direct — interactive CLI iteration, judgment-heavy wiring. No fleet.

### Phase 1: Planner module
- [ ] `core/plan_mode.py`: evidence gathering over existing helpers + citation normalization + prompt builder
  - accept: unit tests assert normalized citations for all four sources; golden test asserts prompt artifact shape from fixture evidence
  - engine: direct

### Phase 2: CLI
- [ ] `chats plan` with findings print, clarifiers, artifact print, dry-run preview, approve/decline flow, `--repo/--approve/--json`
  - accept: scripted TTY run produces preview then launches on approve; decline creates no run row; `--json` output parses and contains all four sections
  - engine: direct

### Phase 3: Authority wiring
- [ ] `fleet.start_run` capability + confirmation flow on the launch path; refusal paths (no confirmation, ambiguous repo)
  - accept: confirmation-denied test asserts `start_run` (real) is never called; ambiguous-repo non-interactive run refuses with repo clarifier
  - engine: direct

### Phase 4: Tests + docs
- [ ] Pytest suite + contract section in `docs/fleet-runtime.md`
  - accept: `pytest tests/test_plan_mode.py` green; docs describe flow, citation format, and follow-up surfaces
  - engine: direct

## Edge Cases / Gotchas

- Front door forbids tool calls by design — plan exploration cannot piggyback on `turn()`; that is why v1 is CLI, not front-door
- `resolve_repository_root` ambiguity is a launch-correctness issue, not just UX — clarifiers must pin cwd before the dry-run, since the preview depends on it
- Dry-run materializes a run row — `chats plan` declines must be distinguishable from real launches when listing runs (dry_run flag exists; make sure list surfaces it)
- Citation dialects differ per source today — normalization lives in `core/plan_mode.py`, not in each producer; do not refactor the producers
- Audit trail split is accepted for v1 (fleet store + action-authority records); convergence is a later project, not this spec

## Testing

- [ ] Citation normalization unit tests (all four sources, truncation bounds)
- [ ] Prompt-builder golden test (fixture evidence → exact artifact)
- [ ] Approve path: preview shown before launch, confirmation granted, run queued
- [ ] Decline path: no run row (dry-run excepted)
- [ ] Refusal paths: confirmation denied → no wake; ambiguous repo non-interactive → clarifier, no launch
- [ ] `--json` schema test: findings + clarifiers + artifact + preview all present and parseable

---

## Progress Log

**Status**: Implemented (phases 1-4), built direct by Serena 2026-09-17
**Branch**: working tree (no feature branch — direct build alongside fleet track 1)
**Current phase**: Done, pending review
**Last completed task**: Phase 4 — pytest suite + `docs/fleet-runtime.md` contract section
**Files modified**: `core/plan_mode.py` (new), `tests/test_plan_mode.py` (new), `cli.py` (`plan` command + helpers), `docs/fleet-runtime.md`
**Blockers**: —
**Review**: self-review vs requirements: all 7 MET (cited findings, normalized citations, repo-pin refusal, dry-run preview before launch, authority confirmation with consumed record, decline creates no run, --json schema). Live approve→stop probe verified the full chain. Ready for Raghav review.
