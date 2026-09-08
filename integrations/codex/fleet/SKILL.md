---
name: fleet
description: Start and manage Serena Fleet provider-routed workflows. Use when Raghav invokes $fleet, asks for a Fleet run, requests a multi-agent coding or research workflow, or wants to inspect, wait for, steer, retry, handoff, cancel, delete, or retrieve an existing Fleet run.
---

# Serena Fleet

Use only the tools from the `serena-fleet` MCP server. Fleet owns model routing, phases,
parallelism, retries, and truthful worker identity. Do not recreate the workflow with Codex
subagents, shell commands, or another orchestration system.

Read the command and task from the user prompt containing `$fleet`.

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
  `cwd`, and use `codex` as `origin_agent`. Omit `origin_session_id` unless the actual current
  thread id is available; the Fleet server detects it from the host environment. Set `dry_run`
  only when requested.

Provider and roster instructions are first-class. Pass `provider_mode: "codex"` for Codex-only,
only-Codex, no-Claude, or zero-Claude requests. Pass `provider_mode: "claude"` for the inverse,
`"balanced"` when both providers were explicitly requested, and `"auto"` otherwise. If the user
explicitly asks for one to four agents, pass that exact number as `worker_count`; otherwise omit
it so Fleet scales from the task. Never bury an explicit provider restriction only inside `task`.

Model routing is fixed server-side for every entrypoint. A provider handoff therefore selects the
target provider, not an arbitrary model. Coding runs use Luna max for Research, Astra medium for
both Code and Review, and Opus high for Fix. On confirmed Claude exhaustion, an unfinished
Fix phase moves to Astra high; Code and Review stay Astra medium. Claude-only runs retain their
explicit Opus stack. Pure research runs use Luna max for Research, Opus high for Analyze, Sol high for Review,
and Opus high for Refine. Do not pass, imply, or silently substitute another phase model.

For explicitly requested A/B tests, a coding Codex-only task may begin with the exact line
`Fleet comparison profile: sol` or `Fleet comparison profile: astra`. These fixed profiles
select Sol xhigh/Sol high or Astra medium/Astra medium for Code/Review respectively; both
keep Luna max Research and Astra high Fix. Use identical isolated fixtures and report actual
attempt identities, independent checks, and phase timings. Do not infer a universal ranking
from a single paired run.

For the opt-in research pilot, use `activity: research`, `provider_mode: balanced`,
and the exact first line `Fleet research comparison: luna` or
`Fleet research comparison: gemini`. Only Research changes (Luna max versus Gemini
3.8 Flash high via `agy`); later phases stay Opus high/Sol high/Opus high. Gemini is
limited to native read/search tools and has no peer or account MCP gateway yet.
Do not use it for account-connected tasks or as an automatic fallback. Report
actual timing, step counts, research quality, and these pilot limitations.

For coding Code/Fix integration gates with proven implementation failures, Fleet may
automatically queue one difficult retry on Astra xhigh when Codex has capacity.
This recorded exception changes only the failed leg, never Claude-only runs, and
does not apply to quota, infrastructure, or malformed-evidence failures. A second
failure stays failed. Do not manually promote arbitrary attempts to this model.

Every Research worker must use native online search extensively, including for local coding work.
Fleet requires observed searches plus current direct sources, authoritative evidence, best practices,
recent technology, and a stated impact on the recommendation; repository reading is not a substitute.

Do not wait for a newly started run unless Raghav explicitly asks to wait or babysit it. Auto and
balanced runs may cross providers after confirmed usage exhaustion or the bounded difficult-retry
gate above, and only when the target
has capacity. Confirmed exhaustion can leave a run in `waiting_for_capacity`; that is active durable
work, not a failed run. Fleet resumes only after a positive capacity signal. Explicit provider-only
runs stay on that provider unless Raghav uses `fleet_handoff`; never use another orchestration path.

## Response

Keep acknowledgements compact:

`fleet <run-id> | <status> | <activity> | <completed>/<total> agent steps | <chats> chats`

For a dry run, show the selected activity, provider routing and reason, four phases, and the chosen one to four durable agents with their assignments and models. The total is four agent steps per selected agent. For an error, state the exact backend error in one sentence. Do not claim the task completed while the run is active.
