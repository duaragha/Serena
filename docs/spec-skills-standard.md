# Spec: SKILL.md Standard for Repos

## Overview

**What**: A documented repo-local skill format (`SKILL.md` / `.agents/skills/<name>/SKILL.md`) that fleet workers auto-discover and consume — procedures travel with code instead of living in chat history.
**Why**: Fleet workers today get zero repo-local procedures (no AGENTS.md, no MUSE.md, no skills; Claude legs even block `Skill`). Every repo's "how we test/deploy/review here" is re-derived per run or lost. The sideload SKILL.md proves the format works.
**Scope**: IN — format doc, fleet discovery + prompt injection, determinism + receipts. OUT — changing Claude `--disallowedTools` flags (inject text instead); skill authoring tooling; Codex auto-load reliance.

## Requirements

- [ ] WHEN a run checkout contains `SKILL.md` or `.agents/skills/*/SKILL.md` THEN workers receive the skill catalog (`name: description`) in-prompt
- [ ] WHEN a skill matches the task THEN its full text is injected (bounded chars) with a context receipt
- [ ] WHEN a skill file is malformed THEN discovery skips it with a warning event, never failing the run
- [ ] WHEN the same skill name exists at multiple paths THEN precedence is documented and deterministic (root `SKILL.md` < `.agents/skills/<name>/`)
- [ ] WHEN a worker prompt is assembled THEN skill injection is provider-uniform (no per-provider flag changes)

## Architecture / Design

**Changes** (blast radius):
- `ADDED docs/skills-standard.md` — format: `name` + `description` (trigger) frontmatter, `## Route`-style dispatcher, `references/` lazy includes, root file < ~100 lines; template included
- `ADDED fleet/skills.py` (or `fleet/context.py` helper) — `discover_skills(checkout)` scan + `match_skills(skills, task)` + bounded excerpt builder
- `MODIFIED fleet/supervisor.py::_worker_prompt` — catalog injection always; matched-skill full text with `ContextReceipt` via `store.record_context_receipt` (same treatment as peer outputs)
- `MODIFIED docs/fleet-runtime.md` — discovery paths + precedence + receipt semantics

**Key decisions**:
- Prompt-injection over CLI skill systems — provider-uniform, deterministic, receipted; no `--disallowedTools` surgery, no dependence on Codex auto-load behavior under `--ignore-user-config`
- Sideload SKILL.md is the template — real, used, already has Claude/Codex entrypoints; standardize what works
- Catalog always, full text on match — workers see what's available without burning context on irrelevant procedures
- Malformed = skip + event — a broken skill must never strand a run (same philosophy as the delivery-evidence repair path)

## Tasks

> Execution: batch as ONE fleet coding run.

### Phase 1: Standard + discovery
- [ ] `docs/skills-standard.md` + template + `discover_skills` with precedence + malformed-skip
  - accept: fixture checkout with valid/invalid/duplicate skills yields exact expected catalog + skip event for the bad one
  - engine: fleet

### Phase 2: Injection + receipts
- [ ] Prompt injection (catalog + matched text) + context receipts
  - accept: worker prompt for fixture task contains catalog and matched skill within budget; receipts recorded with char counts
  - engine: fleet

## Edge Cases / Gotchas

- Skills are untrusted repo content — inject as task data with the same redaction/budget rules as peer outputs, never as authority
- Huge skill files: bound excerpts, note truncation in receipt (don't silently feed half a procedure — mark it)
- `.agents/skills/` may contain non-skill files — only `SKILL.md` (exact name) counts
- Serena's own repo skills vs target-checkout skills: discovery scans the RUN checkout, not the serena checkout (the Persona/Tooling injection stays as-is)

## Testing

- [ ] Discovery golden test (valid/invalid/duplicate fixture)
- [ ] Injection budget + receipt test
- [ ] Malformed-skip event test
- [ ] Precedence test (root vs nested)
- [ ] Prompt determinism test (same checkout + task = same injection)

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
