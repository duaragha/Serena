---
name: fleet
description: Start and manage Serena Fleet provider-routed workflows. Use when Raghav invokes /fleet, asks for a Fleet run, requests a multi-agent coding or research workflow, or wants to inspect, wait for, steer, retry, handoff, cancel, delete, or retrieve an existing Fleet run.
---

# Serena Fleet

Use only the tools from the `serena-fleet` MCP server. Fleet owns model routing, phases,
parallelism, retries, and truthful worker identity. Do not recreate the workflow with Claude
subagents, shell commands, or Claude's built-in Workflow tool.

Treat the text after `/fleet` as the request. `$ARGUMENTS`

## Dispatch

- No arguments: call `fleet_list` with `limit: 10`.
- `status <run-id>`: call `fleet_status`.
- `wait <run-id>`: call `fleet_wait`.
- `result <run-id>`: call `fleet_result`.
- `cancel <run-id>`: call `fleet_cancel`.
- `delete <run-id>`: call `fleet_delete`. Only terminal runs can be deleted; this also
  deletes the Fleet-owned worker chats and private worktrees, never the origin chat.
- `retry <run-id>`: call `fleet_retry`.
- `handoff <run-id> <leg-id> <codex|claude>`: call `fleet_handoff`. This preserves the
  logical worker assignment and moves every unfinished phase to the target provider's locked model.
- `steer <run-id> <message>`: call `fleet_steer`. Steering applies to the next leg or phase;
  it does not interrupt a worker already taking a turn.
- Anything else: call `fleet_start` once. Pass the request as `task`, use `activity: "auto"`
  unless `coding` or `research` was explicitly selected, pass the current working directory as
  `cwd`, and `claude` as `origin_agent`. Omit `origin_session_id` unless the actual current
  session id is available; the Fleet server detects it from the host environment. Set `dry_run`
  only when requested.

Provider and roster instructions are first-class. Pass `provider_mode: "codex"` for Codex-only,
only-Codex, no-Claude, or zero-Claude requests. Pass `provider_mode: "claude"` for the inverse,
`"balanced"` when both providers were explicitly requested, and `"auto"` otherwise. If the user
explicitly asks for one to four agents, pass that exact number as `worker_count`; otherwise omit
it so Fleet scales from the task. Never bury an explicit provider restriction only inside `task`.

Model routing is fixed server-side for every entrypoint. A provider handoff selects the target
provider, not an arbitrary model. Mixed coding uses GPT-5.6 Luna max Research, GPT-6.1 Sol
xhigh Code, and Claude Opus 5.5 xhigh Review and Fix. Codex-only uses GPT-5.6 Luna max
Research and GPT-6.1 Sol xhigh for the other three phases. Claude-only uses Claude Sonnet
5.5 high Research and Claude Opus 5.5 xhigh for the other three phases. Confirmed Claude
exhaustion or terminal overload hands unfinished phases to GPT-6.1 Sol xhigh when Codex
has capacity; explicit provider-only restrictions remain pinned. Pure research uses Luna 5.6
max Research, Opus 5.5 xhigh Analyze, Sol 6.1 xhigh Review, and Opus 5.5 xhigh Refine.
Do not pass, imply, or silently substitute another phase model.

For explicitly requested A/B tests, a coding Codex-only task may begin with the exact line
`Fleet comparison profile: sol` or `Fleet comparison profile: astra`. These fixed profiles
select Sol 6.1 xhigh/Sol 6.1 xhigh or Astra 6 medium/Astra 6 medium for Code/Review;
both keep Luna 5.6 max Research and Sol 6.1 xhigh Fix. Use identical isolated fixtures and report actual
attempt identities, independent checks, and phase timings. Do not infer a universal ranking
from a single paired run.

For coding Code/Fix integration gates with proven implementation failures, Fleet may
automatically queue one difficult retry on Sol 6.1 xhigh when Codex has capacity.
Attempts already running Sol 6.1 xhigh do not qualify for another model retry.
This recorded exception changes only the failed leg, never Claude-only runs, and
does not apply to quota, infrastructure, or malformed-evidence failures. A second
failure stays failed. Do not manually promote arbitrary attempts to this model.

Every Research worker must use native online search extensively, including for local coding work.
Fleet requires observed searches plus current direct sources, authoritative evidence, best practices,
recent technology, and a stated impact on the recommendation; repository reading is not a substitute.

Do not wait for a newly started run unless Raghav explicitly asks to wait or babysit it. Auto and
balanced runs may cross providers after confirmed usage exhaustion or the bounded difficult-retry
gate above, and only when the target
has capacity. Explicit provider-only runs require `fleet_handoff`; never use another orchestration path.

## Response

Keep acknowledgements compact:

`fleet <run-id> | <status> | <activity> | <completed>/<total> agent steps | <chats> chats`

For a dry run, show the selected activity, provider routing and reason, four phases, and the chosen one to four durable agents with their assignments and models. The total is four agent steps per selected agent. For an error, state the exact backend error in one sentence. Do not claim the task completed while the run is active.
