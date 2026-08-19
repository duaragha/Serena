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
target provider, not an arbitrary model. Coding runs use Terra/Sonnet high for
Research, Sol/Opus xhigh for Code, Terra/Sonnet xhigh for Review, and Sol/Opus high for Fix.
Pure research runs keep Terra/Sonnet high for Research, Analyze, and Refine, with Terra/Sonnet
xhigh for Review. Do not pass, imply, or silently substitute another phase model.

Do not wait for a newly started run unless Raghav explicitly asks to wait or babysit it. Auto and
balanced runs may cross providers only after confirmed usage exhaustion and only when the target
has capacity. Confirmed exhaustion can leave a run in `waiting_for_capacity`; that is active durable
work, not a failed run. Fleet resumes only after a positive capacity signal. Explicit provider-only
runs stay on that provider unless Raghav uses `fleet_handoff`; never use another orchestration path.

## Response

Keep acknowledgements compact:

`fleet <run-id> | <status> | <activity> | <completed>/<total> agent steps | <chats> chats`

For a dry run, show the selected activity, provider routing and reason, four phases, and the chosen one to four durable agents with their assignments and models. The total is four agent steps per selected agent. For an error, state the exact backend error in one sentence. Do not claim the task completed while the run is active.
