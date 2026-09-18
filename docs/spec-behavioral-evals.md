# Spec: Behavioral Evals (L5)

## Overview

**What**: A golden behavior corpus + judge + regression gate for persona voice, register fit, authority refusals, and interrupt handling — so behavior changes ship on scores, not vibes.
**Why**: Memory retrieval has Recall@K/MRR evals; Serena's actual behavior (identity, register, authority, interrupts) has zero. Every persona/authority edit today is unprotected. The architecture doc self-scores L5 0/10 — this spec is the climb plan.
**Scope**: IN — behavior corpus format + seed cases, headless driver + interrupt injection, judge/rubric + report, CI gate on refusal accuracy. OUT — model-based judge tuning (rules first); covering every persona edge (seed 40, grow by failures).

## Requirements

- [ ] WHEN the corpus evaluates THEN each case yields (comply/refuse/ask, register fit, interrupt cleanliness) against its expectation
- [ ] WHEN authority is probed THEN prompt-injection-as-authority cases refuse (user text never grants authority)
- [ ] WHEN an interrupt lands mid-stream THEN the turn stops cleanly with no partial side effects
- [ ] WHEN refusal accuracy drops vs baseline THEN CI fails (gate, not advisory)
- [ ] WHEN the corpus changes THEN its sha pin updates and the report records corpus + code versions

## Architecture / Design

**Changes** (blast radius):
- `ADDED memory/behavior_evaluation.py` — mirrors `evaluation.py`: `BehaviorCase(case_id, prompt, persona, register, expect:{refuse|comply|ask}, must_contain/must_not_contain, interrupt_at?)`, `BehaviorCorpus` JSONL with sha pin, `evaluate_behavior_corpus(corpus, driver)`, versioned `BehaviorReport`
- `ADDED behavior corpus JSONL` (private state, 0600, like retrieval corpus) — seed ~40: persona voice (5), register shifts (10), authority refusals incl. injection (15), interrupts (10)
- Driver runs headless turns (reuse `frontdoor` one-shot or `LocalBrain.turn` shape) + interrupt injection; judge is rules-first (`must_contain`/`must_not_contain` + outcome match), model-judge later
- Metrics: `compliance_accuracy/refusal_accuracy/register_fit/interrupt_cleanliness`; gate on `refusal_accuracy` drop
- `MODIFIED` CI config — eval step with baseline comparison

**Key decisions**:
- Mirror `evaluation.py` exactly (corpus format, sha pins, 0600, versioned reports) — the pattern is proven in-repo; behavior evals inherit its rigor
- Rules-first judge — deterministic, debuggable, no judge-model bills; graduate to model-judge only for cases rules can't express
- Gate on refusals, advisory on the rest — authority failures are safety; voice drift is taste. CI blocks the former, reports the latter.
- Seed 40, grow by failures — every future behavior bug adds a case; the corpus compounds like the memory one does

## Tasks

> Execution: direct — persona/authority judgment calls throughout. No fleet.

### Phase 1: Harness + seed
- [ ] `behavior_evaluation.py` + corpus format + 40 seed cases + headless driver + interrupt injection
  - accept: harness runs the seed corpus end to end and produces a versioned report with all four metrics
  - engine: direct

### Phase 2: Gate
- [ ] CI step with baseline comparison + refusal gate + report archiving
  - accept: planted refusal regression fails CI; planted voice drift warns only; report pins corpus + code versions
  - engine: direct

## Edge Cases / Gotchas

- Headless driver ≠ live brain — document the fidelity gap (no real-time interrupts, no multi-surface state); cases that need live fidelity are marked, not faked
- Flaky model outputs — cases assert outcomes + key phrases, never exact strings; rerun policy (e.g. 2/3) documented for borderline voice cases
- Corpus is private state (prompts may contain personal patterns) — 0600, never in git, same as the retrieval corpus
- Baseline updates need a ceremony — deliberate `update-baseline` step with diff review, never auto-accept (or the gate rots)

## Testing

- [ ] Harness end-to-end test (seed corpus → report with metrics)
- [ ] Refusal-gate trip test (planted regression fails)
- [ ] Voice-advisory test (drift warns, doesn't fail)
- [ ] Interrupt-cleanliness test (no partial side effects)
- [ ] Baseline ceremony test (pin update requires explicit step)

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
