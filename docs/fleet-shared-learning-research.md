# Shared failure learning and collaboration

Research and implementation brief, 2026-09-22. Requested for unattended PC Fleet
work: every observed error should contribute to learning, and separate task
teams should help each other before repeating mistakes.

## Existing state and demonstrated gaps

Fleet already has durable run state, scoped worker capabilities, peer mailboxes,
read-only consultations, independent review, and evidence-backed lessons.
`fleet/learning.py` promotes lessons only after reviewed, tested successful work.
`fleet/collaboration.py` limits peers to one run. Preserve these guarantees.

Current project identity is a checkout path. Dispatcher task checkouts for the
same repository therefore become different knowledge namespaces. Retrieval also
requires an evidence filename literally present in the task brief. Natural
language tasks such as image dismissal and receipts rarely satisfy this rule.
Failure capture depends on workers choosing to propose lessons; tool failures
and failed runs do not reliably generate searchable incident records.

Observed examples on the PC:

- Unified task 94 passed 16 focused tests, then failed integration typecheck
  because its fresh base checkout lacked generated workspace package declarations.
  Building the packages resolves that prerequisite; dropping typecheck would
  conceal it. A failed integration environment must be recorded distinctly from
  a proven application defect.
- Tasks 95 and 96 reached Fix then failed on Claude HTTP 529. PR #211 implements
  durable Codex handoff and retries. A learned note must describe that observed
  recovery, never change provider restrictions or grant new authority.
- Tasks 94–97 share one Unified repository but separate task directories. They
  need repository-wide discovery and advice while retaining exclusive edit scope.

## External systems reviewed

Primary documentation accessed 2026-09-22. These are documented capabilities,
not measured performance comparisons. No vendor accounts or services were added.

| System | Relevant documented approach | Application to Fleet |
| --- | --- | --- |
| [LangGraph memory](https://docs.langchain.com/oss/python/concepts/memory) | Thread checkpoints and namespace-scoped long-term stores serve different purposes. Collections support focused recall but need update and retrieval discipline. | Keep run recovery separate from reusable incidents and verified remedies. Namespace by stable repository identity. |
| [LangMem](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) | Memory extraction can run synchronously or in the background. Episodic records preserve successful experience; storage can remain application-owned. | Persist observed failures deterministically first; perform analysis and verification later. Do not make error recording depend on another model call. |
| [CrewAI memory](https://docs.crewai.com/en/concepts/memory) | Shared memory supports scoped views, recall ranking, and automatic task-output extraction. Its default embedding configuration uses a cloud model. | Adopt scope and relevance; keep Fleet's private local storage. Do not send project memory to a new embedding service or add another orchestrator. |
| [CrewAI collaboration](https://docs.crewai.com/v1.15.22/en/concepts/collaboration) | Coworker questions and delegation provide explicit coordination. Documentation describes excessive questions and delegation loops as failure modes. | Route bounded questions to relevant experts, retain ownership, and track answer usefulness and deadlines. Avoid unbounded all-to-all chat. |
| [Letta shared memory](https://docs.letta.com/concepts/shared-memory/index.md) | Current shared repositories are Git-backed, attached to agents, and synchronized by commit/push/pull. They require cloud-hosted agents; older shared-block guides describe a legacy API. | Retain provenance/versioning and explicit shared scope. The documented cloud repository feature is not a drop-in local PC dependency. |
| [Letta memory and dreaming](https://docs.letta.com/configuration/memory/index.md) | Background reflection consolidates lessons; optional agent review revises proposed updates before applying. | Reuse Fleet's independent lesson review rather than trusting self-authored advice. |
| [AutoGen memory](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/memory.html) | A memory protocol separates adding, querying, and inserting results into model context. | Expose explicit bounded discovery/recall tools and record which findings were supplied and used. |

Decision: extend Fleet's SQLite store, worker capability tools, and supervisor
hooks. Replacing orchestration with any of these frameworks would duplicate
existing provider routing, Windows process recovery, integration, and delivery.
Their memory patterns fit without an extra service. This is an architectural
inference from the documented APIs and the inspected Fleet contracts.

## Required behavior

### 1. Automatic incident capture

Record every observed nonzero tool/command result, explicit provider error,
completion rejection, integration/test failure, and supervisor recovery failure.
Use stable event/attempt identifiers for idempotency and restart reconciliation.
Record expected probes as observations, not automatically as actionable defects.
Store redacted bounded summaries plus links to existing evidence. Track source
run/attempt/event, provider, phase, category, fingerprint, recurrence and outcome.
Do not recursively capture the learning system's own writes.

Separate an observed incident, a proposed explanation, a tested recovery, and a
verified reusable lesson. Successful retry alone does not prove a proposed cause.
Failures must remain observable even when the run never succeeds. Corrupt or
unavailable learning state must not silently break the primary worker lifecycle.

### 2. Shared discovery and prevention

Canonicalize repository identity using verified local Git repository metadata,
including task worktrees and HTTPS/SSH remote equivalents. Never execute text
from a remote URL; reject credential-bearing identity material and keep projects
without a trusted remote isolated by local Git common directory/path. Preserve
compatibility with existing stored lessons; no unreviewed global namespace.

Before work and when blocked, retrieve relevant incidents and verified remedies
using task terms, paths, fingerprints and declared work-unit context. A natural
language task must not require literal filenames to find useful advice. Bound
results and context size; keep evidence freshness and expiry checks. Incident
history can be shown as unverified history, never masquerading as a tested fix.
Report recurrence, supplied advice, verified resolutions, and unresolved errors.

### 3. Cross-run consultation

Let an authenticated active worker discover relevant workers in other active
runs of the same canonical repository, ask a focused question, and reply through
a durable mailbox. Identify both run and worker; worker keys alone collide.
Queries and answers must carry source/evidence and remain advice. Agents may
publish useful findings proactively. Requesters confirm whether advice helped.

An ended ordinary worker turn must not silently lose a request: support existing
read-only consultant behavior or a durable explicit expiry/escalation. Preserve
attempt fencing, cancellation, review independence, and project boundaries.
Limit messages, outstanding requests, consultation concurrency, retries, and hop
depth. Never give a helper permission to edit another task's checkout. Feedback
and useful outcomes should inform later retrieval, not silently modify policy.

### 4. Verified learning

Link proposed remedies to incidents and observed checks; route proposals through
the existing independent review and successful gate requirements. Feed verified
lessons to later teams and record uses/outcomes. Invalidated evidence, expiry,
revocation, project mismatch, cancellation, and reviewer conflicts must prevent
promotion or reuse. Keep an auditable path from incident through resolution.
This changes durable context and coordination; it does not train model weights
or guarantee that all future errors can be prevented.

## Acceptance evidence

- A real supervisor/tool-event path records a failure once, survives restart,
  and records its later recovery without losing the original evidence.
- Two isolated task directories for one repository share relevant knowledge;
  another repository and invalid/stale capability cannot read or message it.
- Natural-language task B sees applicable verified guidance from task A.
- A live worker in run B asks run A a question, receives an answer, confirms or
  rejects its usefulness, and leaves a durable receipt across restart.
- Concurrent replies, duplicate events, terminal/cancelled runs, expired tokens,
  missing evidence, model outages, malformed output, and a broken learning write
  cannot corrupt run progress or bypass review/integration gates.
- Existing worker, supervisor, peer, lesson, capacity, and recovery suites stay
  green. Include focused Windows checks and a bounded PC canary with local
  synthetic data. Never use production private transcripts as test fixtures.

Delivery includes reviewed PR, merged source, compatible runtime rollout after
active workers finish, and actual PC canary evidence. Root owns deployment;
workers own code, tests and research artifacts in their isolated checkouts.
