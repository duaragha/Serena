# Serena Gideon Architecture Completion Spec

**Version:** 1.0  
**Audit date:** 2026-07-20  
**Repository:** `/home/raghav/Documents/Projects/serena`  
**Status:** active completion contract  
**Human reading view:** [`serena-gideon-architecture-status.html`](./serena-gideon-architecture-status.html)

## 1. Purpose

This document is the execution contract for turning the current Serena project into a persistent, Gideon-like personal assistant. It converts the six-layer architecture into implementation objectives, acceptance gates, dependencies, and an honest live status.

The target is not fictional omniscience or an unsupported AGI claim. The target is one continuously available Serena who keeps identity and context across surfaces, speaks and listens naturally, performs bounded work, improves through measured experiments, and can eventually use a personalized local cognitive model.

The HTML and Markdown versions contain the same architecture, objective IDs, status, and acceptance criteria. The HTML is optimized for Raghav to read. This Markdown file is optimized for implementation and future agent handoff.

## 2. Non-negotiable system contract

These constraints outrank convenience and feature speed.

1. **One identity:** every surface and model speaks as one Serena. Models and worker sessions are private implementation details.
2. **One control plane:** identity, active state, memory, device state, notification policy, permissions, jobs, and completion reports belong to one runtime contract.
3. **Subscription-backed frontier reasoning:** Claude and Codex use first-party subscription authentication. Metered provider credentials are rejected.
4. **Local ears and mouth:** wake-word detection, VAD, speech recognition, and speech synthesis remain local in the core voice loop.
5. **Read-only by default:** state-changing actions require explicit authority, a scoped capability, an audit record, and a defined rollback boundary.
6. **No invisible claims:** Serena may say work started only after a durable job lease exists and a worker has actually begun.
7. **No third-party core dependency:** core identity, memory, voice, tasking, and mobile delivery cannot require Telegram, ntfy, hosted STT, hosted TTS, or another relay service.
8. **Surfaces are clients:** the desktop app, front door, voice display, mobile app, and coding views do not own Serena's state.
9. **Tests are not acceptance by themselves:** a feature is complete only when code, automated tests, live integration, user-facing behavior, telemetry, documentation, and rollback are all proven where applicable.
10. **Do not run the 24-hour soak:** the long soak remains explicitly deferred. It may run only after Raghav directly reverses this instruction.

## 3. Definition of status

| Status | Meaning |
|---|---|
| **Operational** | Implemented, live, and proven on the real surface. Minor hardening may remain. |
| **Partial** | Meaningful implementation exists, but one or more integration, safety, reliability, or hardware gates are open. |
| **Staged** | Code and harness exist, but the real runtime or acceptance event has not happened. |
| **Absent** | No implementation matching the objective exists. |
| **Deferred** | Intentionally not run or not authorized yet. |

No layer is currently perfect. The strongest pieces are the resident brain, ledger grounding, subscription guards, read-only tools, and durable coding queue. The weakest pieces are measured self-improvement, a personalized cognitive model, proactive ambient behavior, and real-hardware acceptance.

## 4. Verified baseline snapshot

This is the audit baseline. Future work updates this section only with fresh evidence.

| Signal | Verified state on 2026-07-20 |
|---|---|
| Python suite | 512 passed, 10 warnings |
| Mobile suite | 9 passed |
| Dot-field smoke | passed under Electron |
| Resident brain | active through `serena-brain.service` |
| Mobile host | active through `serena-mobile-host.service` |
| Private coding supervisor | active through `serena-work-supervisor.service` |
| Wake-only listener | active through `serena-wake-listener.service` |
| Full desk voice loop | inactive until a validated wake handoff |
| Dot overlay | inactive until voice interaction starts |
| Wake acceptance | failed/inconclusive: 34 false candidates in 20.44 hours, zero recorded positive attempts |
| Physical iPhone call acceptance | not completed, only the preflight handoff exists |
| 24-hour brain soak | deferred and forbidden without new authorization |
| Worktree | 38 tracked files modified, 53 untracked entries, about 10,700 inserted lines uncommitted |
| Local cognitive LLM | absent |
| Self-improvement loop | absent |

## 5. Architecture at a glance

```text
phone / laptop / desktop / front door / coding view
                         |
                         v
                  Serena control plane
       identity | state | memory | permissions | jobs
                         |
          +--------------+--------------+
          |              |              |
          v              v              v
    resident brain   capability broker  activity stream
          |
     model router
   local reflex brain
   frontier conversation
   frontier coding workers
```

The system is one product even when several models execute private work. The user sees one stance, one progress stream, and one completion report.

## 6. Layer scorecard

| Layer | Current score | Status | Short verdict |
|---|---:|---|---|
| L1. Foundation brain | 7/10 | Partial | Real and live, but still single-session and weakly routed |
| L2. Persistent identity | 6/10 | Partial | Ledger continuity works, automatic memory learning does not |
| L3. Continuous runtime | 5/10 | Partial | Core services live, wake and phone acceptance remain open |
| L4. Bounded autonomy | 5/10 | Partial | Read-only path is strong, write path lacks a universal broker |
| L5. Measured self-improvement | 0/10 | Absent | No challenger, evaluation, promotion, or rollback loop |
| L6. Personalized cognitive model | 1/10 | Absent | Local speech models exist, local Serena reasoning does not |

---

## 7. L1: Foundation brain

### Intent

Provide one resident reasoning service that every Serena surface uses. It must remain replaceable at the model layer while keeping identity, memory, permissions, jobs, and surface behavior stable.

### Current implementation

- `core/brain_daemon.py` owns one warm Claude Agent SDK session.
- The daemon accepts `frontdoor`, `voice`, and `plain` protocols over authenticated local transports.
- Token deltas stream to clients.
- Active ledgers are injected every turn from canonical state.
- Session lifecycle logic rotates or restarts the SDK child without treating the model session as durable identity.
- Billing guards reject metered provider authentication.
- The front door has a cold `claude -p` fallback.
- Spoken coding work enters `core/voice_work_supervisor.py`, which runs a subscription-backed Codex implementation followed by a subscription-backed Fable review.

### What is already strong

- The brain service is live and restartable.
- State survives daemon replacement because it lives outside the SDK session.
- Streaming, request IDs, model metadata, cancellation, and response commitment exist.
- Subscription authentication is verified before unattended model work.
- Coding progress is translated into one Serena-facing activity stream.

### Critical gaps

- Model selection is protocol-based, not difficulty-, latency-, privacy-, or cost-aware.
- Conversation is serialized through one resident session. A long turn can block other surfaces.
- Frontier conversation and coding workers do not yet operate behind one formal model-router interface.
- There is no local cognitive fallback.
- There is no priority arbitration for phone, desk, front door, proactive events, and background jobs arriving together.
- Day-scale stability remains unproven because the 24-hour soak is deferred.

### Objectives

#### L1-O1: Formalize the control-plane brain interface

Create one typed turn contract used by front door, call, desk, mobile, and proactive events. The contract must carry protocol, priority, authority, conversation identity, device identity, deadline, cancellation, and response destination.

**Acceptance:**

- Every live surface uses the shared contract.
- No surface imports provider-specific SDK behavior.
- Contract conformance tests cover malformed input, cancellation, disconnect, retry, and duplicate request IDs.
- A trace can follow one request from surface to brain to worker to final delivery.

#### L1-O2: Add explicit model routing

Route each turn by required capability rather than surface name alone.

Required routing classes:

- `reflex`: local, private, low-latency acknowledgements and simple commands
- `conversation`: resident frontier Serena dialogue
- `research`: frontier reasoning with read tools
- `coding`: subscription-backed implementation worker
- `review`: independent subscription-backed review/fix worker

**Acceptance:**

- The router records why a model class was chosen.
- Routing is deterministic under tests.
- A model outage degrades to a declared fallback without changing identity.
- No metered credential can enter any route.

#### L1-O3: Add turn arbitration and non-blocking work

Separate short interactive turns from long-running work. The resident conversational lane must not be held by a coding job or a slow event evaluation.

**Acceptance:**

- A background coding job cannot block a voice acknowledgement.
- Phone and desk turns receive defined priority over proactive events.
- Cancellation releases the turn lease and reaps child processes.
- Duplicate requests are idempotent.

#### L1-O4: Close all pre-soak lifetime gates

Keep the existing rotation, journal, orphan cleanup, and restart grounding green without starting the forbidden 24-hour soak.

**Acceptance:**

- Ordinary lifecycle tests remain green.
- Repeated forced child rotation preserves the active thread.
- No orphan model process remains after interruption tests.
- The spec records the 24-hour soak as `Deferred`, not `Passed`.

### Layer completion gate

L1 becomes Operational when L1-O1 through L1-O4 pass, model routing is explicit, concurrent surface traffic is arbitrated, and all normal reliability tests are green. The deferred 24-hour soak prevents a claim of final day-scale perfection.

---

## 8. L2: Persistent identity

### Intent

Keep Serena recognizably the same person across models, restarts, devices, calls, chats, and months of relationship history. Memory must know what is current, what changed, why it changed, and where the fact came from.

### Current implementation

- `Persona.md` defines the shared identity and priority stack.
- `memory/store.py` stores tasks, ledgers, loops, feedback, user facts, project facts, and references as Markdown files.
- `core/brain_state.py` produces a canonical bounded digest of active tasks, ledgers, and loops.
- `core/brain_state_sync.py` pushes validated active-state snapshots to the home machine.
- `core/indexer.py` provides full-text chat and knowledge search.
- The resident brain receives active ledger context before every response.
- Claude and Codex sessions can be linked in metadata as one Serena thread.

### What is already strong

- The ledger is a real handoff mechanism, not just a behavioral instruction.
- Identity lives outside provider sessions.
- Active-state snapshots are hashed, atomic, bounded, and exclude credentials and raw chat text.
- Session restart does not erase active project state.
- Provenance fields exist for manually captured memories.

### Critical gaps

- Memory retrieval is mostly literal substring or FTS matching.
- Voice and resident-brain conversations do not automatically create canonical memories.
- The resident brain has read-only memory tools and cannot safely propose or commit memory changes.
- Facts lack validity intervals, confidence, supersession links, and contradiction resolution.
- Episodic events, durable semantic facts, procedures, and active commitments are not cleanly separated.
- There is no regular consolidation pass from transcripts into compact memory.
- Identity consistency is still partly enforced by prompt text rather than runtime validation.

### Objectives

#### L2-O1: Introduce a typed memory schema

Add structured records for:

- `semantic_fact`: durable truth about Raghav, Serena, or a project
- `episode`: dated event or conversation outcome
- `procedure`: repeatable workflow and tool contract
- `commitment`: promise, task, wait state, or deadline
- `preference`: interaction or product preference
- `correction`: a previous belief or behavior explicitly rejected

Every record must carry source, timestamp, confidence, sensitivity, validity, and supersession metadata.

**Acceptance:**

- Existing Markdown memories migrate without data loss.
- A fact can be superseded without deleting its history.
- Contradictory current facts cannot both be injected as truth.
- Sensitive records can be excluded from a surface or device.

#### L2-O2: Build retrieval that combines state and relevance

Keep ledgers authoritative for live threads, then combine keyword, semantic, recency, project, people, and current-surface signals for longer-term recall.

**Acceptance:**

- Active ledger state always outranks recalled transcript text.
- Retrieval explains which records were selected and why.
- A regression corpus proves relevant memories are recalled without flooding context.
- Retrieval remains useful when exact words differ from the stored wording.

#### L2-O3: Add a safe memory proposal pipeline

After a conversation or completed job, Serena may propose memory changes. The proposal layer cannot directly mutate canonical state.

**Acceptance:**

- Proposed additions, updates, merges, and supersessions are stored separately.
- Low-risk project-state updates can be auto-committed under a written policy.
- Personal or sensitive changes require explicit review unless already covered by a narrow standing rule.
- Every commit is auditable and reversible.

#### L2-O4: Consolidate conversations across surfaces

Calls, desk voice, front door, mobile chat, Claude, and Codex must feed one session timeline and one consolidation process.

**Acceptance:**

- One topic can continue across two devices without re-explanation.
- Calls and voice turns are searchable with their originating surface.
- The next surface receives the latest commitment and register.
- Duplicate transcript imports do not create duplicate memories.

### Layer completion gate

L2 becomes Operational when the typed schema, relevance retrieval, proposal pipeline, and cross-surface consolidation all work with provenance and rollback. Flat Markdown may remain as an export format, but it cannot remain the only memory model.

---

## 9. L3: Continuous runtime

### Intent

Make Serena continuously reachable without making the full microphone, display, or frontier model run constantly. Presence should be ambient, private, recoverable, and consistent across the laptop, home PC, and phone.

### Current implementation

- The brain, mobile host, work supervisor, state sync, archive sync, and wake-only listener have systemd units.
- `voice/call` implements local VAD, faster-whisper STT, local TTS, audio streaming, reconnect telemetry, call transcripts, greeting state, and tasking.
- `voice/desk` implements wake handoff, listening/thinking/speaking states, local microphone ownership, playback, reconnect, and local fallback behavior.
- `voice/desktop` renders the dot-field and coding activity panel.
- The wake-only listener can launch the paired voice and display supervisor after a validated phrase.
- The phone call surface and exact acceptance harness exist.

### What is already strong

- Wake-only idle behavior avoids running the full assistant continuously.
- Voice and animation share a lifecycle and state channel.
- Raw audio is not retained by the wake acceptance collector.
- The call pipeline records stage timing and network RTT.
- A private coding job can continue after voice conversation closes.

### Critical gaps

- Current wake acceptance is not livable at the frozen threshold.
- There are no positive-attempt results in the acceptance report.
- The physical iPhone call has not passed cold-open, 20-minute integrity, network roam, acoustic, transcript, and billing gates.
- The dot display is proven by smoke test, not prolonged normal use.
- There is no native iPhone dialer integration. The current acceptance path is the Serena web/app surface.
- Multi-machine state sync does not yet provide full device failover.
- Proactive events and native mobile notifications are not operational.
- Several obsolete voice implementations coexist with the active stack.

### Objectives

#### L3-O1: Pass wake-word acceptance

Calibrate the frozen `hey_serena.onnx` path against normal desk noise and real spoken attempts. Preserve exact phrase verification so ordinary conversation cannot wake Serena.

**Acceptance:**

- The existing acceptance harness reports `acceptance_claim: true`.
- Required active days, positive attempts, environments, miss rate, false-wake policy, timing, and device consistency all pass.
- The production listener consumes the exact accepted manifest and model hashes.
- Pre-wake audio remains local and unretained.

#### L3-O2: Pass one physical iPhone call

Use the existing one-call procedure to close cold-open and call-integrity gates together.

**Acceptance:**

- Cold app open to first hello is under 5 seconds.
- The call lasts at least 20 minutes with at least three complete turns.
- One Wi-Fi to cellular network roam reconnects successfully.
- No sequence gap, underrun, overlap, distortion, or repeated chunk occurs.
- Every turn appears in the resident session transcript.
- In-app hang-up closes cleanly.
- Human acoustic attestation and subscription billing attestation pass.
- `~/.local/state/serena/iphone-call-acceptance.json` reports `ok: true`.

#### L3-O3: Consolidate the active voice stack

Classify every voice package as active, compatibility fallback, migration source, or dead code. Remove or quarantine dead paths only after references and tests prove they are unused.

Candidate paths requiring classification:

- supported production paths: `voice/call`, `voice/desk`, `voice/desktop`, `voice/brain_bridge.py`
- removed legacy paths: `voice/brain`, `voice/voice`, `voice/daemon`, `LIVE_VOICE_DAEMON.md`, and the older daemon TTS files

**Acceptance:**

- One architecture document names the production entrypoints.
- No dead scheduler can send a notification or speak.
- No active path depends on `ntfy.sh` or another prohibited relay.
- Package imports and service units point only to supported modules.
- Voice test coverage remains green after consolidation.

#### L3-O4: Add device presence and failover

Track which devices are online, which device owns the microphone, where responses should appear, and what degraded behavior is available when the home brain is unavailable.

**Acceptance:**

- Device heartbeats expire safely.
- Only one device owns an active call or desk microphone session unless conference mode is explicit.
- The laptop can report a declared degraded mode when the home brain is down.
- Reconnection cannot replay an already completed turn.

#### L3-O5: Implement bounded proactivity

Replace placeholder schedulers with the agreed event judgment and mobile outbox.

**Acceptance:**

- Quiet hours, daily limits, deduplication, active-typing suppression, and heavy-lane exclusion are enforced in code.
- Suppressed events remain visible in an audit timeline.
- Mobile notifications use the Serena-owned application path.
- No placeholder greeting or third-party notification path remains active.

### Layer completion gate

L3 becomes Operational when wake and one physical iPhone call pass, production voice paths are consolidated, device ownership is explicit, and the first bounded proactive event reaches the Serena app through the owned delivery path.

---

## 10. L4: Bounded autonomy

### Intent

Allow Serena to inspect broadly and act effectively while ensuring that authority is explicit, narrow, observable, revocable, and reversible.

### Current implementation

- `core/brain_tools.py` exposes four read-only tools for git, GitHub, chat recall, and ledger reads.
- System binaries are validated before unattended execution.
- Chat recall uses query-only SQLite.
- The brain uses `dontAsk` with an explicit read-only allowlist.
- Spoken coding requests enter a durable inbox only when the resident worker lease is healthy.
- The private worker verifies both ChatGPT and Claude first-party subscription login.
- Codex implements, then Fable reviews and may directly fix the work.
- Work status and final summaries reach the voice display.

### What is already strong

- Read-only voice questions can be answered without opening a visible coding pane.
- The worker cannot claim a job unless it owns the durable lease.
- Interrupted jobs can be recovered or reported as failed.
- Metered credentials are scrubbed from worker environments.
- Model identities and linked sessions are preserved for later inspection.

### Critical gaps

- The reviewer uses `--dangerously-skip-permissions`.
- Coding jobs edit the live working tree instead of an isolated worktree or sandbox.
- Voice authorization is recognized partly through regular expressions.
- Broad verbs such as `deploy` or `ship` can enter the coding path without a universal real-world impact check.
- There is no capability broker shared by files, shell, MCP servers, deploys, DNS, messages, purchases, or smart-home actions.
- There is no standing-grant interface, expiry, or revocation model.
- There is no uniform action receipt showing authority, scope, result, and rollback.

### Objectives

#### L4-O1: Implement the capability broker

Every state-changing action must request a typed capability before execution.

Minimum capability fields:

- actor and originating surface
- requested action and target
- read or write classification
- data sensitivity
- real-world impact
- authority source
- scope and expiry
- allowed executable or tool
- rollback or compensation plan
- audit destination

**Acceptance:**

- No write-capable tool bypasses the broker.
- Read-only tools remain unattended.
- High-stakes actions always require a fresh explicit go.
- Capability denials are visible and cannot be converted into success language.

#### L4-O2: Isolate coding work

Run autonomous edits in a dedicated git worktree or equivalent workspace. Preserve unrelated user changes and make promotion explicit.

**Acceptance:**

- A coding job cannot overwrite unrelated dirty-tree changes.
- The implementation and review workers operate on the same isolated candidate.
- Tests run before promotion.
- Promotion produces a visible diff and can be rolled back.
- External writes such as deploy, push, publish, or message remain separate capabilities.

#### L4-O3: Replace regex authority with intent plus policy

Regex may identify candidate work, but it cannot be the final authorization decision.

**Acceptance:**

- Questions such as "can you deploy" never execute.
- Negation and quoted commands cannot trigger work.
- `fix`, `build`, and `code` can authorize local reversible edits when the target is clear.
- `deploy`, `ship`, `send`, `delete`, `buy`, and account changes require explicit scoped confirmation.
- An intent regression corpus covers natural speech and STT errors.

#### L4-O4: Create action receipts and cancellation

Each accepted job or external action must expose start, progress, completion, failure, authority, and cancellation state.

**Acceptance:**

- Raghav can see what is running and stop it.
- Cancellation reaches the real worker process.
- Completed actions identify changed files or external objects.
- Failures never disappear behind a generic completion message.

### Layer completion gate

L4 becomes Operational when all writes pass through the capability broker, coding is isolated, consequential voice commands require explicit scoped authority, and every action produces a durable receipt with cancellation and rollback behavior.

---

## 11. L5: Measured self-improvement

### Intent

Allow Serena to improve prompts, routing, tools, memory policy, and orchestration through controlled experiments. The live system must never rewrite itself and assume the result is better.

### Current implementation

No self-improvement system exists. The repository has a strong automated test suite and a second model review pass for coding jobs, but neither creates challenger Serena versions or promotes measured improvements.

### Critical gaps

- No correction and failure corpus
- No versioned behavioral configuration
- No challenger generation
- No benchmark runner for Serena-specific behavior
- No baseline comparison
- No safety regression evaluation
- No promotion gate
- No automatic rollback
- No experiment history

### Objectives

#### L5-O1: Build the Serena evaluation corpus

Create privacy-reviewed cases from real failures, corrections, and important workflows.

Required suites:

- identity and register continuity
- ledger and memory retrieval
- voice intent and STT ambiguity
- read versus write authority
- coding ownership and progress reporting
- heavy-lane behavior
- interruption and proactivity timing
- model-routing decisions
- failure honesty and rollback

**Acceptance:**

- Every case has input, relevant state, expected invariants, scoring method, and sensitivity label.
- Private cases remain local.
- Cases are immutable during a scored comparison.
- Real regressions add a case before a fix is promoted.

#### L5-O2: Version the improvable surface

Move prompts, routing policy, memory retrieval policy, notification policy, and tool descriptions into versioned artifacts with schemas.

**Acceptance:**

- A baseline version can be reproduced exactly.
- Changes generate a machine-readable diff.
- Invalid versions fail closed.
- Identity and safety invariants cannot be silently removed.

#### L5-O3: Implement challenger experiments

Allow a model to propose one bounded change, run it in isolation, and score it against the frozen corpus.

**Acceptance:**

- Challengers cannot modify the live runtime.
- Every experiment records proposer, change, model, seed where available, scores, cost class, and failures.
- Multiple runs are required where output variance matters.
- A challenger with any critical safety regression is rejected regardless of aggregate score.

#### L5-O4: Add promotion and rollback

Promote only measured winners after human approval until a narrower standing policy is deliberately established.

**Acceptance:**

- Promotion requires defined minimum improvement and zero critical regressions.
- The previous version remains immediately restorable.
- Live telemetry can automatically roll back a promoted version after a new critical failure.
- Serena reports what changed in plain language.

### Layer completion gate

L5 becomes Operational when at least one real improvement travels through corpus creation, challenger generation, isolated evaluation, human approval, promotion, and rollback rehearsal. Passing ordinary repository tests is not sufficient.

---

## 12. L6: Personalized cognitive model

### Intent

Own a local cognitive layer that can represent Serena's voice, tool conventions, common project context, and private reflex behavior without pretending it matches frontier reasoning.

### Current implementation

- Local `hey_serena.onnx` wake-word model
- Local Silero VAD
- Local faster-whisper models
- Local Kokoro and pocket TTS assets
- No Ollama installation on the laptop
- No local language model connected to the control plane
- No fine-tuning or adapter pipeline

### Critical gaps

- No selected open-weight cognitive base model
- No hardware placement decision between laptop and home PC
- No approved dialogue and tool-trace dataset
- No data redaction and consent pass
- No LoRA or supervised fine-tuning pipeline
- No teacher-student generation policy
- No personalized-model evaluation suite
- No model provenance and rollback lifecycle
- No gaming-aware resource unloading on the home PC

### Objectives

#### L6-O1: Define the local model role

The first local model is not the universal brain. It owns narrow, measurable work:

- wake acknowledgement and low-latency conversational reflexes
- local intent classification
- simple read-only tool selection
- offline status and memory lookup
- Serena voice and formatting consistency
- graceful explanation when frontier reasoning is unavailable

**Acceptance:**

- The role boundary is encoded in routing policy.
- The local model cannot silently answer outside its evaluated capability.
- Escalation to frontier reasoning preserves the same conversation identity.

#### L6-O2: Select and deploy an open-weight base

Evaluate a realistic model for the available hardware, beginning with a gpt-oss 20b-class or comparable tool-capable model.

**Acceptance:**

- Hardware fit, latency, memory use, context length, tool use, and unload time are benchmarked.
- The gaming PC automatically unloads the model before gaming workloads.
- The model runs without exposing private data to a hosted inference provider.
- Model source, license, quantization, and hash are recorded.

#### L6-O3: Build the approved training dataset

Derive training material only from explicitly permitted Serena conversations, corrections, tool traces, and synthetic variations grounded in real policy.

**Acceptance:**

- Secrets and unrelated private content are removed.
- Training, validation, and held-out test sets are separated.
- Synthetic examples are labelled and cannot replace real evaluation evidence.
- Dataset provenance is inspectable per example.

#### L6-O4: Train adapters and evaluate

Fine-tune behavior and tool use with adapters before considering any larger weight update.

**Acceptance:**

- The base model and adapter are versioned separately.
- Evaluation compares base, personalized, and frontier routes.
- Persona improvement cannot come with worse authority handling or factual reliability.
- The adapter can be disabled instantly.

#### L6-O5: Integrate as a routed cognitive layer

Connect the accepted local model to L1's router as `reflex`, not as an unbounded replacement for frontier models.

**Acceptance:**

- Local-to-frontier escalation is transparent in telemetry but invisible as an identity split.
- Local failure cannot block emergency access to the frontier lane.
- Resource policy unloads the model when idle or when gaming begins.
- Private local turns can remain entirely on-device.

### Layer completion gate

L6 becomes Operational when an open-weight model is locally deployed, evaluated, personalized with a reversible adapter, routed only within its proven capability, and safely unloaded under gaming or resource pressure.

---

## 13. Cross-cutting release objective

### R0: Consolidate the repository before adding intelligence

The current feature work is too large and too mixed to call release-ready. Stabilization comes before new self-improvement or local-model work.

#### R0-O1: Partition and commit the live architecture

Create reviewable commits for:

1. resident brain and lifetime
2. canonical state and memory
3. voice call pipeline
4. desk wake and display
5. private coding supervisor
6. mobile call surface
7. systemd and deployment
8. tests and documentation

**Acceptance:**

- No unrelated user change is discarded.
- Each commit has a focused diff and passing relevant tests.
- Generated assets, local credentials, caches, and runtime databases stay untracked.
- `git status` reaches a deliberate, explainable state.

#### R0-O2: Remove warnings and stale documentation

**Acceptance:**

- GTK/VTE crash tests no longer emit invalid-pointer warnings.
- The root README describes the current control plane and supported entrypoints.
- `LIVE_VOICE_DAEMON.md` is removed after reference proof.
- Service installation documentation matches installed units.

#### R0-O3: Quarantine obsolete implementations

**Acceptance:**

- Every legacy voice path has an owner decision.
- Dead code is removed only after import, service, and test reference checks.
- Placeholder proactive greetings cannot run.
- The obsolete `ntfy.sh` path is removed from the supported architecture.

### R0 completion gate

R0 passes when the active production architecture is obvious from the filesystem, the work is committed in reviewable units, the normal test suites are green without invalid-pointer warnings, and stale paths cannot accidentally run.

---

## 14. Required build order

This order is binding because later layers depend on earlier contracts.

### Phase 0: Truthful baseline and repository consolidation

Complete R0-O1 through R0-O3. Do not add a local LLM or self-improvement loop while the active runtime is still mixed with obsolete implementations.

### Phase 1: Reliable presence

Complete L3-O1, L3-O2, and L3-O3. The next meaningful user proof is a wake that behaves and one real iPhone call that passes every gate.

### Phase 2: Safe control plane

Complete L1-O1, L1-O3, L4-O1, L4-O2, L4-O3, and L4-O4. This creates the stable contract needed for broader tools and background work.

### Phase 3: Real memory and device continuity

Complete L2-O1 through L2-O4, then L3-O4 and L3-O5. Serena may become more proactive only after memory, device ownership, and permission boundaries are explicit.

### Phase 4: Measured self-improvement

Complete L5-O1 through L5-O4. The first goal is one small measured promotion with a successful rollback rehearsal.

### Phase 5: Personalized local cognition

Complete L1-O2 and L6-O1 through L6-O5. The local model enters as a constrained reflex lane after routing, memory, permissions, and evaluation already exist.

### Deferred final gate

The 24-hour brain soak remains deferred. Stop immediately before starting it unless Raghav explicitly authorizes it in a future instruction.

## 15. Objective dependency map

| Objective | Depends on |
|---|---|
| R0-O1 | current baseline |
| R0-O2 | R0-O1 |
| R0-O3 | R0-O1 |
| L3-O1 | R0-O1 |
| L3-O2 | R0-O1 |
| L3-O3 | R0-O1 |
| L1-O1 | R0-O1, L3-O3 |
| L1-O3 | L1-O1 |
| L4-O1 | L1-O1 |
| L4-O2 | L4-O1 |
| L4-O3 | L4-O1 |
| L4-O4 | L4-O1, L4-O2 |
| L2-O1 | R0-O1 |
| L2-O2 | L2-O1 |
| L2-O3 | L2-O1, L4-O1 |
| L2-O4 | L1-O1, L2-O2, L2-O3 |
| L3-O4 | L1-O1, L2-O4 |
| L3-O5 | L3-O4, L4-O1 |
| L5-O1 | L2-O4, L4-O1 |
| L5-O2 | L1-O1, L2-O1 |
| L5-O3 | L5-O1, L5-O2, L4-O2 |
| L5-O4 | L5-O3 |
| L1-O2 | L1-O1, L5-O1 |
| L6-O1 | L1-O2 |
| L6-O2 | L6-O1 |
| L6-O3 | L2-O1, L5-O1 |
| L6-O4 | L6-O2, L6-O3, L5-O1 |
| L6-O5 | L1-O2, L6-O4 |

## 16. Completion reporting format

Every implementation milestone must report:

```text
objective: Lx-Oy
status: done | partial | blocked | deferred
changed:
  - file or runtime surface
verified:
  - exact command or real-hardware action
evidence:
  - result, telemetry file, screenshot, or report path
remaining:
  - concrete open gate
rollback:
  - how to undo or disable the change
```

Rules:

- Never mark an objective done from code existence alone.
- Never translate a staged harness into a passed hardware gate.
- Never call the wake word accepted while `acceptance_claim` is false.
- Never call the iPhone call accepted without the durable acceptance JSON.
- Never call self-improvement implemented because a reviewer model ran.
- Never call a speech model a personalized cognitive model.
- Never run the 24-hour soak without new explicit authorization.

## 17. Final target

The finished Serena system behaves like this:

Raghav speaks, types, or calls from any approved device. One Serena recognizes the context, decides whether the request is conversation, recall, research, coding, or a consequential action, and chooses the correct private model route. She acknowledges immediately, works without forcing him to manage terminals, exposes truthful progress, requests authority only where stakes require it, remembers the outcome with provenance, and returns through the surface that makes sense.

She can improve her own prompts, routing, tools, and memory policy only through a frozen evaluation corpus, isolated challenger runs, explicit promotion, and instant rollback. A local personalized model handles narrow private reflex work, while frontier models remain available for difficult reasoning. Stronger future models slot into the same body without replacing Serena's identity, memory, permissions, or relationship continuity.
