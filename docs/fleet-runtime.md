# Fleet runtime and recovery

Fleet remains a Serena-owned orchestrator over the native `claude` and `codex` programs. It does
not use Hermes as a dependency, replace Serena's identity, or route through a generic model API.

## Run ownership and deletion

`serena-fleet.service` claims every queued run and supervises each in its own thread. There is no
numeric cap on simultaneous Fleet runs. Provider availability still controls whether a native turn
can start, and coding runs targeting the same repository retain the per-checkout lock so integration
cannot race in one working tree.

A terminal run can be deleted from the dashboard, `chats fleet delete`, or `fleet_delete`. Deletion
removes the Fleet database graph, its worker chats, event logs, and private worktrees. The chat that
launched the Fleet is origin context rather than Fleet-owned data and is never deleted with the run.

Before a real run is persisted, Fleet checks free space on the checkout and control-database
filesystems. Coding runs reserve 1 GiB of control-plane headroom plus 1 GiB per selected worker;
research reserves 1 GiB. This prevents isolated dependency installs from consuming the final bytes
and leaving a run that cannot record its own failure. `SERENA_FLEET_MIN_FREE_BYTES` is the explicit
operator override, including `0` to disable the check. A refusal happens before any worker is
dispatched and tells the operator to reclaim disposable cache or inactive-worktree dependencies.

Concurrent worker startup can briefly collide on SQLite's write lock. Fleet retries that narrow
`database is locked` / `database table is locked` class with bounded backoff around attempt and
lease setup. Other SQLite failures are never retried or hidden. A run already interrupted by ENOSPC
stays on its original run id and resumes only through `fleet_retry` after space is restored.

## Terminal ownership

The desktop defaults to `SERENA_TERMINAL_BACKEND=renderer`. One renderer owns terminal layout,
visibility, split geometry, focus, and resize. PTY processes remain local and native.

- xterm.js and its fit, links, canvas, and WebGL add-ons are pinned under `ui/renderer/` and
  vendored into `ui/static/vendor/xterm/`. Runtime startup does not depend on a CDN.
- hidden tabs never call `fit()` or send a zero-size resize to a provider CLI.
- returning to Chats waits for two animation frames and then restores the prior terminal or linked
  split at its real dimensions.
- closing the renderer kills the complete renderer PTY registry so provider processes are not
  orphaned.
- `SERENA_TERMINAL_BACKEND=vte` is the explicit recovery path for the former native overlay.

## Work units

Fleet scales from explicit task or objective boundaries, not a vague complexity score. Every
persisted workstream receives a durable work-unit contract containing:

- one stable logical owner and rotated reviewer keys;
- dependency ids and phase-barrier semantics;
- a bounded logical scope;
- `declare_before_edit` ownership for explicit coding paths, repository-serialized ownership when
  coding paths are unknown, or `read_only` ownership for research;
- acceptance criteria, required evidence, constraints, and stop conditions.

The parser accepts numbered, bulleted, or lettered entries under `tasks:`, `objectives:`, or
`workstreams:` headings, including prose headings such as `Use four independent workstreams:`.
When four explicit entries are present, all four slots bind to those entries and no synthetic
integration worker is padded into the plan.

`core/fleet_dag.py` materializes those contracts into durable work units, dependency edges, and one
executor record per phase. Research, Code, and Fix records follow the logical owner; Review records
follow the rotated review target. A review therefore cannot start before the target's Code phase,
and a target's Fix phase cannot start before its peer review. Before every provider wave Fleet
selects only units whose same-phase dependencies completed. Waiting units stay
`waiting_for_dependencies`; a failed prerequisite recursively moves its descendants to
`blocked_dependency_failed` without launching their provider CLIs. After a wave, selection runs
again so newly unblocked units advance in deterministic order. Provider handoffs keep the same
work-unit and worker keys.

Explicit repository paths in a workstream become enforced declared ownership at planning time.
Before a write leg launches, the supervisor claims those paths and serializes proven overlap.
Workstreams without path declarations receive one repository-wide supervisor claim, so only one
unknown-scope writer can run at a time. Workers never write Fleet's claim database themselves.
Retries refresh from the latest combined checkout when its baseline changed, reapply a preserved
patch only when it applies cleanly, and otherwise hand the preserved patch back to the same durable
worker for an explicit conflict repair.

## Relationship to Claude workflows

Claude `/workflows` and Fleet share the useful orchestration pattern: the parent defines bounded
lanes, gives every worker an explicit role and output contract, and owns phase transitions. Fleet
also has to coordinate persistent native Claude and Codex sessions that write to one repository.
That adds durable claims, isolated worktrees, dependency state, integration, retries, and provider
handoffs that a read-only research workflow does not need.

Those control-plane duties stay with the Fleet supervisor. A provider worker is never responsible
for updating Fleet's database to make its own result admissible. The planner may prove disjoint
paths and run those writers concurrently. If it cannot prove ownership before launch, Fleet uses a
repository-wide claim and serial execution. This keeps provider sandbox differences from changing
whether identical work is accepted.

## The phase matrix and worker identity

Fleet runs one locked model per phase, and every agent in that phase runs it:
Research `gpt-5.6-luna` max, Code `claude-opus-5` medium, Review `gpt-5.6-sol`
high, Fix `claude-opus-5` high. `core/fleet_policy.py` holds it as a hard
contract, `validate_config` refuses a config that drifts from it, and
`policy_models_match_contract` re-checks every run before it starts.

For coding runs, confirmed Claude exhaustion maps unfinished Code to
`gpt-5.6-sol` xhigh and unfinished Fix to `gpt-5.6-sol` max. Review is already
`gpt-5.6-sol` high and does not change. These are per-worker handoffs; healthy
workers and completed phases keep their original routing.

Workers are Agent A through Agent D. `worker_key` is `agent:a`, not
`codex:a`: the provider is a property of the phase, not of the worker, so one
agent moves between Codex and Claude as the run advances and keeps its name,
assignment, workstream and evidence lineage the whole way. A phase is therefore
single-provider when it is built, and stops being so only when one worker is
handed to the other provider for capacity, which is legitimate.

Two consequences fall out of alternating providers, both deliberate:

- **Sessions cannot span a provider change.** Code opens a fresh Claude session
  because Research ran on Codex. Fix continues Code's session, since both are
  Claude and a fixer repairing code it wrote is a benefit. Review deliberately
  does *not* continue Research even though both land on Codex, because a
  reviewer that sat through the research cannot independently disagree with it.
  That suppression is one condition in `fleet_store.py`'s continuation lookup.
- **A dead provider blocks its phases.** `no-claude:` and `no-codex:` pin a run
  to one provider's stack, and a mid-run exhaustion hands the unfinished phases
  to the other provider's stack through the existing handoff path. Both are
  recorded in the frozen policy; `no_silent_fallback` still holds.

Because every phase starts cold on the other provider, Research must end with a
`Read map` naming the `path:start-end` regions it actually opened. Rediscovery
was the largest measured input-token cost on a real Research leg, and the map
turns it into a lookup.

## Read access for non-writing legs

`core/fleet_read_mcp.py` owns this. Research and Review are read-only legs, and that used to be
enforced by handing every worker an empty MCP config. That stopped writes, but it also stopped the
only thing those phases exist for: a Research leg could read our notes about the Google Ads account
and never read the account, so three lanes could reason off the same stale repository doc and repeat
the same wrong number.

A leg whose access mode is not `write` now gets exactly one MCP server, Fleet's own stdio gateway,
and still nothing from the user's MCP configuration. The gateway exposes only tools classified
`read`, per tool, drawn from the server list in `defaults.mcp_read_access`. Nothing else about the
leg changes: no repository writes, no `Edit`/`Write`/`NotebookEdit` for Claude, and Codex stays on
`--sandbox read-only`. Write legs are untouched and keep zero MCP; they already have a shell.

Classification is deny-by-default and decided per tool, never inherited from the leg's label:

- a curated per-tool decision wins first, so a mutation surface with a read-shaped name
  (`frameworth-shopify.graphql`) is refused and a genuine read the heuristic cannot place
  (`Railway.http_requests`) is allowed;
- a read that hands back credentials (`Railway.list_variables`, anything naming secrets or tokens)
  is refused. "Technically a read" is not a reason to pull secret values into a worker's context;
- the server's own `readOnlyHint`/`destructiveHint` annotations come next;
- then a name heuristic over the shared read/write vocabulary, tokenised so camelCase and acronyms
  split (`DNS_getDNSRecordsV1` reads as a getter, `DNS_updateDNSRecordsV1` does not);
- anything still unclassified is denied.

The catalog of exposed tools is built out of band and read synchronously, so building a worker argv
never touches the network. The supervisor refreshes it once per run before any leg starts: the first
build blocks, bounded; a stale one refreshes behind the run. Both the Claude allowlist
(`--allowedTools mcp__serena_read__…`) and the Codex allowlist (`mcp_servers.serena_read.enabled_tools`)
are written per tool from that catalog, and the gateway re-checks the classification against the live
server at call time, so a stale catalog can never widen access. No catalog means no MCP flags at all:
the failure mode is a worker that behaves exactly as it did before, never one that silently gets more.

`chats fleet read-tools` shows what is currently exposed and what each server denied; `--refresh`
rebuilds it. Each run records a `read_mcp_catalog` event with the same counts.

## Writer isolation and integration

`core/fleet_isolation.py` owns this. Its state lives in `fleet-isolation.sqlite3`, colocated with
the Fleet database, and the base repository is never its own working copy.

A preflight decides whether isolation is provable. A dirty base does not block it: Fleet freezes its
exact tracked and non-ignored untracked contents into a private baseline commit without staging or changing the
real checkout, then creates the worker worktree from that commit under
`~/.local/state/serena/fleet-worktrees/`. What does block it is a repository that cannot carry
another worktree, a bare repository, an unborn HEAD, or an interrupted merge, rebase, cherry-pick, or
bisect. A write leg fails closed when isolation cannot be proven. The previous shared-checkout path
is available only through the explicit emergency override `SERENA_FLEET_ISOLATION=off`.

Only write-access coding legs are isolated. Review and verify legs read the combined result in the
real checkout, and research legs never write. One logical worker keeps one durable workspace identity
across phases. After each accepted integration the worktree is refreshed from the combined base, so
later write phases see peer integrations as well as their own earlier work. A provider handoff keeps
the same worker identity and claims rather than forking a competing workspace.

The registry requires paths to be claimed by the supervisor before provider execution. Claims are exclusive per run,
all-or-nothing per call, and
directory-aware, so `core` collides with `core/fleet_store.py`. Two readers may share a path; a
writer may not share with anyone. Git metadata, key material, and env files cannot be claimed at all.

Integration is where a dirty base can actually be lost, so it is gated three ways and every gate
fails closed:

- a worker may only deliver paths it claimed, and one unclaimed path blocks its entire patch;
- no incoming path may differ from the dirty baseline the worker received, which detects concurrent
  base edits without rejecting pre-existing dirty work merely because it existed;
- the configured test gate must pass, or the change is reverted. Without an explicit command Fleet
  still runs `git diff --check` against the delivered paths.

Merges run in stable leg-ordinal and worker-key order and apply as a plain atomic patch. Three-way apply is
deliberately not used: it can leave conflict markers in the checkout, which is a corrupted tree
rather than a refused merge. Every applied integration keeps a durable pre-integration snapshot ref
and its own patch file, so `rollback_integration` can reverse it after the fact.

A stacked pull request is an explicit second delivery mode. The worker leaves the reserved Fleet
branch only when it needs an unmerged dependency, commits its own work as a contiguous suffix, pushes
the new branch with an upstream, opens the stacked PR, and leaves the worktree clean. Fleet verifies
the exact remote HEAD and finds the shortest bounded first-parent suffix whose diff equals the
envelope's `changed_paths`. It records that patch as `published_branch` and never applies either the
dependency or the worker suffix to the shared checkout. Dirty, unpublished, detached, unbounded, or
mismatched switched branches fail closed. The delivered HEAD becomes the workspace baseline so a
later Fix phase can update the same PR without replaying the dependency.

Isolation is enabled by default for production write legs. The supervisor acquires claims before
launch and integrates an accepted write result before recording the leg complete. Claim, integration,
or test-gate failure leaves the leg failed and the base unchanged. Proven disjoint declared paths may
run concurrently. Unknown ownership is repository-wide and serial. Integration remains one-at-a-time.

## Context and inspection

`core/fleet_context.py` gives every worker prompt a deterministic context budget. Context that fits
is delivered in full. Oversized context is excerpted per source rather than tail-truncated, and a
durable receipt records source bytes, delivered bytes, omitted bytes, source count, redaction count,
strategy, budget, and a hash of the complete sanitized source. The native chat remains authoritative,
while Fleet attempts retain sanitized output; failed or bounded prompt composition rewrites neither.

Fleet persistence filters common authorization headers, token/password environment assignments,
private keys, and high-confidence GitHub, Slack, and AWS credential forms. The filter covers worker
event logs, provider output/error receipts, handoff context, steering, and durable Fleet events. It
is a last-line persistence boundary, not a replacement for provider sandboxing or protected-path
claims.

Operators can inspect the same durable projection through `chats fleet inspect`, the read-only
`fleet_inspect` MCP tool, and `/api/fleet/runs/<id>/inspect`. A focus may be a work-unit id or worker
key. The dashboard exposes per-phase execution state, dependency blocks, context receipts, isolation
mode, claims, worktrees, integration results, and focus controls without changing chat navigation.
Bounded inspection returns the newest events in chronological order; streaming callers that pass an
`after` cursor retain the original forward-only behavior.

## Capacity recovery

Confirmed provider exhaustion moves a worker to `waiting_for_capacity` instead of failing the run.
The wait records the failed provider, eligible providers, reason, next probe, and any known reset.

```text
running worker
      |
      v
waiting_for_capacity
      | positive native capacity signal
      +----------------------+----------------------+
      | same provider        | eligible peer provider
      v                      v
same session retry      provider handoff receipt
      |                      |
      +-----------> queued <-+
```

Unknown telemetry never wakes a parked worker. Auto and balanced runs may cross providers when the
other provider positively recovers. Provider-only runs resume on that provider, unless the user
explicitly chooses the dashboard's provider handoff action. Cancellation deletes the pending wait.

## Shared control plane

Fleet's SQLite database is still authoritative. Each committed Fleet event also writes a control
outbox row in the same transaction. The resident service publishes those rows idempotently into
`event_id, surface, event_type, session_id, turn_id, request_id, job_id, provider, authority,
lifecycle_state, delivery_state, payload, occurred_at`

A non-dry Fleet run creates one `final_result_delivery` obligation. The obligation remains open
through completion or failure and closes only when a Fleet notification is recorded as delivered.
Failed delivery increments durable attempt evidence; cancellation closes the obligation as
cancelled. A process crash can delay publication but cannot erase the committed source event.

That Fleet rule is no longer hardcoded. `OBLIGATION_RULES` in `core/control_plane.py` declares one
row per surface naming the event that opens an obligation and the events that fulfil, fail, or
cancel it. Fleet's `final_result_delivery` is one of those rows with unchanged semantics, alongside
spoken job results, memory proposals awaiting Raghav, queued notices, tool calls, and chat turns. A
failed delivery increments durable attempt evidence and leaves the obligation open; only a real
delivery event closes it, and obligations are keyed per surface so two surfaces sharing a job id
cannot resolve each other's promises.

`SurfaceOutbox` generalizes Fleet's inline outbox for any SQLite-backed surface. The surface stages
an envelope on its own connection inside its own transaction, so the envelope and the state change
commit together or not at all, and publishes idempotently afterwards. A failing control plane
preserves the staged row and stops, keeping order intact for a later retry.

`core/control_recovery.py` runs on Fleet service boot. It reads open obligations older than a
staleness window and hands each back to the surface that owns redelivery. It never marks anything
fulfilled itself, a handler only reports that it re-dispatched, and an obligation past its attempt
budget resolves to `ambiguous` rather than success, because Serena not knowing whether Raghav
received something is a real state.

Migration status: Fleet is fully migrated. Notification authority now also commits `notice.queued`,
`notice.delivered`, and `notice.failed` envelopes through a notification-local transactional outbox,
using the notification id as the stable obligation key even when a caller has no source job id.
Boot recovery has concrete Fleet and notification handlers and reports recovery only after an actual
dispatch. Voice, memory, tool, and chat stores do not yet stage events or expose native redelivery
handlers, so those surfaces remain partial and are skipped rather than falsely marked recovered.
Their native journals stay authoritative throughout.

## Voice access to Fleet

Voice Serena reads the same durable Fleet store. Ask "how is Fleet going", or
name a project, task, or short run id, and she reports the real phase, agent
step progress, actual model identity, and errors.

She may START a run only when the current spoken turn explicitly names Fleet,
and may cancel, retry or steer a resolved run on that same live authority.
Ordinary spoken coding requests still use the single coding-job path, not Fleet.

When a real run completes or fails, the supervisor sends one bounded notice to
the desktop voice bridge and she says it aloud after the current conversation
finishes. Telegram is only the fallback when the local bridge is unavailable or
playback fails. Alerts never contain worker transcripts or tool traffic.

For ordinary spoken coding work she searches the durable Chats index for the
most recent safe exact-project Sol session; the coding app does not need to be
open. A live exact-project pane is preferred when idle, otherwise the
supervisor resumes the frozen historical session id under external ownership. A
new private chat is created only when no valid project session exists.

The coding pane or panel, coding app, Chats app, voice-work display, dot
overlay, brain daemon and Fleet tab are her own surfaces in
`/home/raghav/Documents/Projects/serena` unless Raghav names a different
project, so they never require a repo clarification.

## Completion contracts

### Review findings and the integration gate

Review reports machine-readable findings rather than prose. Each carries the
`unit_id` it was found in, a severity of `blocker`, `major` or `minor`, a
summary, and its evidence. An empty list is a valid and checkable statement
that the reviewer found nothing, which prose cannot express. Fleet routes each
finding to the owning worker's Fix leg and skips a fixer with nothing assigned
to it, keeping one reporter so the run still ends with a real final response.

Rotated Review advances the reviewed unit, not the reviewer's owned unit. Its
DAG dependency therefore waits for that target's successful Code attempt. The
completion gate independently checks that the Code attempt completed before
Review started, because a later Code result cannot exist in Review's immutable
context receipt. Exact retry atomically remaps legacy Review rows, reopens any
stale completed Review, and reopens its completed Fix descendants. A stale
snapshot can no longer move the wrong unit forward or preserve a Fix built from
the wrong review.

Integration re-runs each worker's own declared verification against the
combined checkout before accepting its patch, so a change that passed alone and
breaks alongside a peer is rejected at the merge instead of surviving to
Review. Only commands on Fleet's test allowlist run. A repository-wide gate can
be forced with `SERENA_FLEET_INTEGRATION_TEST_COMMAND`.


A provider CLI exiting zero says the process ran, not that the work was done. `core/fleet_completion.py`
validates the final worker message against the persisted work-unit contract, and
`core/fleet_completion_gate.py` collects the real claims and changed paths to check it against.

Every leg that was handed a contract must end its final message with one `<serena-evidence>` JSON
envelope reporting each owned unit. The prompt spec and the validator are generated from the same
module, so Fleet never gates a worker on a rule it was not given. A leg with no work-unit contract is
reported as `enforced: false` rather than being quietly passed.

Completion is refused when the envelope is missing, unparseable, duplicated, or when the evidence
contradicts itself. The enforced rules are:

- a unit reported `completed` alongside a triggered stop condition is a contradiction;
- `blocked` or `stopped` must name the stop condition that actually triggered. The evidence is
  accepted as truthful, but the leg is recorded failed and no downstream phase is allowed to run;
- `completed` requires the exact contract acceptance criteria, with no substitutions or duplicates,
  answered `met: true` with concrete evidence, and `constraints_respected: true`;
- `completed` is refused while a declared dependency is incomplete or its state is unavailable;
- a read-only leg reporting changed files is refused;
- a write leg may only change files covered by an active claim, must declare every path the working
  tree actually shows as changed, and must record a direct allowlisted test command. Fleet reruns that
  command without a shell in the worker workspace and rejects disagreement with the reported exit;
- when a worker changes a Node manifest or lockfile, integration performs the lockfile-native install
  with lifecycle scripts disabled before merged-tree checks. A failed check rolls the patch back and
  resynchronises the old dependency graph, so neither stale nor half-updated `node_modules` can decide
  a later worker's result;
- every worker must stop only an exact PID or process group it started. The prompt forbids `pkill`,
  `killall`, and pattern-based termination, and the completion gate rejects either broad command from
  the provider event stream so unrelated dev servers, user processes, and sibling workers are not
  silently killed;
- a Research worker's read-only sandbox is phase separation, not a work-unit stop condition. It must
  finish the research and implementation handoff for Code even when it cannot edit, commit, push, or
  update GitHub itself. A genuinely unavailable source is recorded as a limitation, not confused
  with missing write authority;
- a worker never hides inside one sleep or wait longer than 60 seconds. Background-server checks use
  bounded polling, resolve the actual listener or surviving child process group rather than trusting
  a package-manager wrapper's `$!`, stop only that exact target, and prove no process or listener it
  started remains;
- an envelope with no readable prose answer fails the final-response obligation.

A refused leg is recorded `failed` with the concrete reasons, emits a durable
`leg.completion_evidence_rejected` event carrying every failure, and stays retryable through the
existing retry path. It is never counted as success. Because a rejected contract is not provider
exhaustion, it deliberately does not trigger automatic capacity handoff. If the gate itself raises,
the leg fails closed and a retryable `leg.completion_gate_failed` event distinguishes evidence
infrastructure failure from contradictory worker evidence.

An honest stop emits `leg.completion_evidence_stopped` with `accepted: true` and
`completion_allowed: false`; it is not auto-retried as though the worker merely formatted its
receipt incorrectly. Retrying a terminal run is refused while any recorded worker process is still
alive. A retry reopens unfinished DAG phases and also repairs legacy runs whose older supervisor
mistakenly counted a `blocked` or `stopped` receipt as complete, or persisted Review against the
reviewer's own unit. Healthy completed descendants are preserved; descendants of an unfinished or
stale Review are reopened because their input was not valid. The scheduler rechecks the run state
before every dispatch and drains existing workers without launching another leg if the run became
terminal concurrently.

Adversarial coverage lives in `tests/test_fleet_completion.py`, and live supervisor coverage, where
the gate is not stubbed, lives in `tests/test_fleet_completion_gate.py`.

## Bounded extensibility and automation

Four small modules, none of which can extend themselves at runtime.

`core/serena_plugins.py` holds the typed manifest and lifecycle. A manifest declares tools, UI
contributions, hooks, permission scopes, filesystem and network reach, secret references, and a
health check. Unknown fields, unknown scopes, unknown hook events, unknown UI surfaces, wildcard
hosts, absolute or escaping paths, and protected targets are all refused. Secrets are declared by
reference (`env:NAME` or `file:relative/path`); a manifest carrying an actual value is rejected, so a
plugin file cannot become a place credentials live. The lifecycle is
`staged -> installed -> enabled <-> disabled -> removed`, every transition needs a named actor, and
staging never installs. Sensitive scopes require an approved staged manifest matching the stored one,
so editing a manifest after approval does not grant new reach. There is no autonomous installation
and no dynamic import path.

`core/serena_scheduler.py` runs only actions registered in code. It cannot execute a shell command or
a callable supplied by a manifest, so a plugin can ask for a schedule but cannot become one. Bounds
are a 60-second interval floor, a 25-action per-tick cap, approval required before a schedule first
runs, active-state enforcement for manual runs, an atomic execution lease that prevents concurrent
ticks from running one schedule twice, and a consecutive-failure breaker that disables a schedule
after five failures instead of retrying forever.

`core/notification_authority.py` is the intended gate for anything Serena sends unprompted. It enforces
deduplication windows, per-channel hourly limits, quiet hours (which `critical` may bypass), explicit
approval for configured kinds, bounded exponential-backoff retries, and durable delivery history, and
it commits queue and delivery lifecycle envelopes transactionally for idempotent publication into the
shared control plane. The channel list is fixed in code.

`core/webhook_signing.py` is HMAC-SHA256 over an explicit signing string with the timestamp inside
the signature and a replay window, using only the standard library. The case-insensitive receiver
helper also consumes accepted signatures through a durable SQLite replay cache, so a valid request
cannot be delivered twice inside the freshness window. It signs and verifies; it never sends.

Operator commands: `chats plugin list|stage|pending|approve|reject|set-state`,
`chats schedule list|approve|pause|history`, and `chats notify history|pending|approve|flush`.

Honest boundary: no plugin ships with Serena. An enabled, explicitly approved plugin runs in an
isolated child with live revocation, bounded hooks, scoped secrets, and Serena's shared URL policy.
The five hook sources are mounted in Fleet, chat turns, memory proposals, notification delivery, and
scheduler ticks. The resident automation service registers only the reviewed actions in
`core/scheduler_actions.py`. Fleet terminal alerts now pass through the notification authority, so
quiet hours, limits, deduplication, retry, and the voice-to-Telegram fallback share one decision.

## Deployment and checks

After updating a running installation, restart the Fleet user service and reopen the Serena desktop
so the resident Python process and renderer both load the new code.

The focused acceptance suite covers policy, store, supervisor, MCP/CLI, dashboard, linked-session
navigation, PTY lifecycle, desktop split restoration, control outbox, and obligations.

## Implementation matrix

| Area | Status | Honest boundary |
| --- | --- | --- |
| Durable Fleet DAG | implemented | Provider launches are selected from persisted dependency and phase state; completion evidence enforcement is a separate gate. |
| Context budgets and Fleet secret filtering | implemented | Full sanitized source is hashed and preserved in authoritative history; oversized prompt delivery uses inspectable excerpts. |
| Operator DAG/context/isolation inspection | implemented | CLI, MCP, API, and dashboard read the durable projection; no live service restart was used for acceptance. |
| Worktree isolation and integration | implemented | Write legs are isolated and integrated before completion by default, with dirty-base preservation, claims, deterministic single-writer ordering, drift checks, rollback evidence, and a test gate. No live provider run was used for acceptance, and an explicit emergency shared-checkout override remains. |
| Shared cross-surface control | partial | Fleet and notification authority are transactional and have concrete boot recovery handlers. Voice, memory, tool, and chat native journals remain authoritative until each surface stages its own outbox rows and implements redelivery. |
| Completion contracts | implemented | Enforced in the real supervisor before a leg is recorded completed; refusals are durable and retryable. Evidence is validated against the contract, active claims, and the worktree diff, but Fleet cannot see changes to gitignored paths. |
| Plugin manifest and lifecycle | implemented | Approved plugins execute in isolated children with scope checks, live revocation, bounded hook fan-out, fail-closed secrets, and shared URL validation. No plugin ships with Serena. |
| Scheduler and notification authority | implemented | The resident bounded loop registers only reviewed actions. Quiet hours, dedup, limits, approvals, retries, delivery history, Fleet alerts, voice, and Telegram fallback are enforced through one authority. |
| Signed webhooks | implemented | Signed ingress, replay rejection, held-request approval with exact-body replay, loopback-only management routes, and the public HTTP mount are tested. |
| Worker supervision, memory v2 | implemented | Worker leases, stalled-run recovery, reviewed memory proposals, typed records, retrieval receipts, retention, contradiction, supersession, and normal-surface routing are enforced and tested. |
