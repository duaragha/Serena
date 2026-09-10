# Fleet runtime and recovery

## Read-only process liveness

Native process retry recognizes explicit Windows crash statuses (access
violation, illegal/privileged instruction, integer divide-by-zero, stack/heap
failure, control-C termination, fail-fast and fatal application exit), in both
unsigned DWORD and signed int32 form, alongside the existing POSIX signals.
The [Microsoft NTSTATUS reference](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-erref/596a1078-e883-4972-9bbc-49e60bebca55)
defines these codes. Ordinary exit1, unknown codes, launch failures and missing
DLL/configuration statuses are not treated as proven process crashes. A recorded
worker PID remains required. Cancellation and authority/evidence blockers still
win, and the same two process retries (30/60-second cooldowns) remain bounded.
Scheduled/exhausted receipts retain the actual exit code. The native Windows
writer test terminates only its own disposable process with an access-violation
status, then verifies the saved patch and completed Research survive retry;
it simulates the OS exit, not an actual illegal-memory-access fault.

Fleet, work jobs, external-session leases and shared process owners use
`core.process_probe.probe_process` for existence checks. On Windows it uses
psutil's read-only PID query; `os.kill(pid, 0)` sends `CTRL_C_EVENT` there and
is not a safe existence query. POSIX retains signal-zero semantics. Existing
birth-token fencing and each caller's permission-denial policy remain intact.
`tests/test_process_probe.py` checks both no-signal routing and a real private
child's continued survival, then confirms its absence after owned cleanup.

## Opt-in Gemini research pilot

For a matched research comparison, use `activity: research`, `provider_mode: balanced`,
and begin the task with `Fleet research comparison: luna` or
`Fleet research comparison: gemini`. Only Research changes: Luna max versus
`gemini-3.8-flash-high` through the subscription-authenticated `agy` CLI.
Analyze/Review/Refine retain Opus high/Sol high/Opus high. Ordinary defaults are unchanged.

Install the exact `fleet/gemini_research_agent.md` at
`~/.gemini/config/agents/serena-fleet-research/agent.md`. The adapter verifies the
definition before launch and rejects workspace overrides. It allows native file
reads and web search/fetch, not shell execution, writes, delegation, browser
actuation or account MCP tools. This initial pilot does **not** expose Fleet peer
MCP or account-read gateways to Gemini; use it for self-contained research only.
Gemini is not an automatic quota fallback and its exhausted attempts fail for
explicit same-provider retry, preserving comparison identity.

Antigravity 1.1.27's init `tools` is the global catalog, not the selected agent's
effective allowlist. The adapter checks the pinned agent and model identities and
rejects unexpected tool steps. A live negative write-capability probe and native
search smoke verified the restricted agent. Only `SUCCESS` with final text and
zero exit is accepted; WAITING, cancelled and missing results cannot pass. Metrics
deduplicate completed step indices and retain native usage, including thinking tokens.
CLI stdin uses the documented NDJSON user-message protocol, not a shell argument.

References: [headless protocol](https://antigravity.google/docs/cli/headless/),
[agent tool definitions](https://antigravity.google/docs/subagents).

Fleet remains a Serena-owned orchestrator over the native `claude` and `codex` programs. It does
not use Hermes as a dependency, replace Serena's identity, or route through a generic model API.

## Run ownership and deletion

Fleet, isolation, worker-lease, control-plane and outbox operation scopes close
their SQLite connection immediately after commit or rollback, including commit
failure. Resource probes and guarded activation use the same owned-connection
contract. This prevents finished reads from retaining descriptors until cyclic
garbage collection happens. Raw callers can still explicitly manage transactions
and close; a connection must not be nested or reused after its owning scope exits.
Connection-configuration failure also closes the new handle. Tests cover real
commit, body-error rollback, deferred-constraint commit failure, explicit manual
ownership, all five store factories and descriptor counts with GC disabled.

The Linux service starts through `scripts/serena-fleet-service.sh`. When NVM is
installed, it selects the operator's already-installed `default` alias rather
than pinning a versioned Node directory in the unit. It does not source login
profiles or install runtimes. An unavailable configured default refuses startup
with an explicit error instead of silently using another Node. Without NVM,
the inherited service PATH is preserved. `--check` reports runtime resolution
without starting Fleet. This honours the operator default; per-project engine
compatibility still requires separate validation. Unit changes require a
systemd daemon reload and a safe Fleet-only restart, never an active-worker kill.

`serena-fleet.service` claims every queued run and supervises each in its own thread. There is no
numeric cap on simultaneous Fleet runs. Provider availability still controls whether a native turn
can start, and coding runs targeting the same repository retain the per-checkout lock so integration
cannot race in one working tree.

A terminal run can be deleted from the dashboard, `chats fleet delete`, or `fleet_delete`. Deletion
removes the Fleet database graph, its worker chats, event logs, and private worktrees. The chat that
launched the Fleet is origin context rather than Fleet-owned data and is never deleted with the run.

Before a real run is persisted, Fleet checks free space on the checkout and control-database
filesystems. Coding runs require 1 GiB of control-plane headroom plus 1 GiB per selected worker;
research requires 1 GiB. This is a point-in-time admission check, not a reservation: later writes
can still exhaust storage. `SERENA_FLEET_MIN_FREE_BYTES` is the explicit
operator override, including `0` to disable the check. A refusal happens before any worker is
dispatched and tells the operator to reclaim disposable cache or inactive-worktree dependencies.

Concurrent worker startup can briefly collide on SQLite's write lock. Fleet retries that narrow
`database is locked` / `database table is locked` class with bounded backoff around attempt and
lease setup. Other SQLite failures are never retried or hidden. A run already interrupted by ENOSPC
stays on its original run id. Proven orphaned owners are recovered boundedly after storage returns;
an already terminal failed run still requires explicit `fleet_retry`.

Store initialization serializes check-then-ALTER schema migrations with an immediate SQLite
transaction. This prevents simultaneous worker startup from adding the same migration column twice.
Concurrent WAL-mode switches can return SQLITE_BUSY/SQLITE_LOCKED before that transaction;
Fleet and isolation stores retry only those codes up to five times with bounded backoff.
Exhaustion and non-lock errors remain visible; disk-full and corruption are not hidden.

## Durable resource recovery and actionable stops

Declared integration test sequences have one narrow generated-type preparation
pass: when `npm run typecheck` exits 1 or 2 with TS2307 naming a `.generated` or
`/generated` module, and the combined checkout declares a `codegen` script,
Fleet invokes that script with npm lifecycle hooks disabled and rechecks the
same typecheck once. The gate retains the original failure, preparation result
and recheck result. Ordinary missing packages and unrelated TypeScript errors
do not trigger this path; preparation or recheck failure still rejects the
integration. This repairs checkout-local generated state without another model
turn or copying unverified generated files from a peer. It does not repair
unsupported runtime versions.

The resident recovery poll can queue one supervisor-only integration replay per
leg for saved failures in this exact class. Admission requires the current failed
zero-exit writer attempt, previously accepted completion evidence, a rejected
local integration receipt, and its saved patch. Cancellation and queueing share
a transaction; live attempts prevent admission. Other input blockers remain
untouched. A dedicated Python helper owns the replay's process group and normal
worker lease; the resident service must never become the worker PID for recovery
or termination. The parent uses Fleet's bounded process/output transport and
tracks cancellation. The helper takes the write claim and revalidates
completion evidence, and requires an exact saved-patch SHA-256 match inside the
integration lock before applying. It neither refreshes the worker checkout nor
spends a native model turn. Its new attempt has no observed model identity; the
original failed attempt retains provider provenance. A refused replay parks for
input rather than repeatedly spending attempts.

Local patch integration now commits an immutable intent in the isolation database
before Git changes the combined checkout. The intent binds the canonical checkout,
target branch, worker workspace incarnation/base, and exact patch SHA-256 to full
pre/post file images and the original rollback reference. A restarted integration
under the repository lock may recheck an exact postimage without applying it twice;
a mixture of whole-file pre/postimages is restored to the original preimage before
applying again. Every affected path is checked before restoration, and each write
boundary is checked again. Binary bytes, symlinks, executable modes and original
rollback permissions are preserved. The journal never bypasses current claims,
patch fingerprints, completion evidence, or integration tests. Test receipts expose
the intent ID and whether a postimage or mixed-image recovery occurred.
Saved patch files use unique exclusive names and are flushed, along with their
containing directory on POSIX, before any receipt can reference them. Two attempts
in the same second cannot overwrite each other's recovery evidence.

Foreign bytes, within-file partial writes, redirected parent directories, malformed
or corrupt intents, and changed patch identities are not guessed away. They refuse
integration and preserve surviving work. Preview does not perform pending recovery.
Older applied patches without an intent remain unproven; this is not retroactive
reconstruction. Run deletion removes that run's private journal images. This covers
same-patch local integration reconciliation, not published-branch deliveries,
arbitrary external effects of gate commands, or unrecordable database/filesystem loss.

Replay attempt creation and its verification-only dispatch marker commit in one
transaction. If the helper dies by a supported POSIX signal before recording its
outcome, the parent retains the real signal exit status and uses the existing
two-retry process budget and 30/60-second delays. The next attempt remains a
verification helper tied to the original saved result, not a native model turn.
Cancellation remains cancelled; exhausted budgets park for input. Newer unrelated
attempts cannot be mistaken for a replay. Real SIGKILL tests cover death after
application, after the successful integration receipt but before attempt completion,
and during rollback; exact-file journal recovery retains normal verification gates.
Verification helpers also clean up their POSIX process group after direct-process
exit, even when a gate uses private pipes and does not keep the helper's output
open. A captured process birth identity protects against signalling a reused
leader PID. Tests cover normal exit and SIGKILL with a SIGTERM-ignoring gate.
Descendants that escape the owned POSIX process group remain a coverage gap.
The separate Windows helper ownership contract is described below.

The Windows sidecar dispatches `--fleet-integration-replay` before GUI startup
and restores inherited standard pipes for the windowed executable. Repository
integration uses a portable process lock: POSIX flock or Windows byte-range
locking at offset zero. The Windows lock retries contention explicitly rather
than relying on the CRT's ten-attempt blocking mode; see the
[Python locking contract](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking).
Canonical Windows lock identities are case-normalized. Tests prove cross-process
exclusion and release on owner death on actual Windows, not just mocked imports.

Git patch application and explicit rollback use byte-mode stdin and lossless
patch-file reads. No platform text-mode conversion may rewrite LF, CRLF or
binary patch payloads. The Windows installer build runs native lock/patch tests
and a saved-integration replay against its actual frozen executable, with real
completion validation and Git gates. Native Windows source replay has been
verified; the build smoke is required evidence for each packaged candidate.
The dedicated Windows replay helper is assigned to an unnamed, non-inheritable
Job Object before receiving its stdin request. Assignment failure refuses to
release that request. Closing the owner handle kills associated descendants,
including gates with private pipes, on normal helper return or owner death.
This uses the [Windows Job Object contract](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects);
it does not enable breakaway. Native tests cover helper return, cancellation,
owner death and assignment refusal. The Windows build requires those tests.
This is helper-specific: ordinary provider programs are not stdin-gated and
cannot safely use this post-launch assignment without a separate launch design.
If a launched replay helper exits while its current attempt is still running,
the parent records an unrecorded-helper outcome and uses the existing process
retry budget. Admission requires that exact attempt's durable helper dispatch
marker and observed process, inside the attempt-finalization transaction.
Zero, ordinary nonzero and Windows NTSTATUS exits preserve their actual codes;
none are fabricated into POSIX signals. Native model failures cannot opt into
this class without helper provenance. Cancellation, superseded attempts and
already-recorded verifier rejections do not schedule this recovery. These tests
exercise real helper subprocess exits and ordinary completion/Git gates on retry;
they do not claim that Windows model-worker crash classification is solved.

The frozen replay build smoke also runs an external process-kill probe after
Git application. It checks the attempt PID, lease owner and process birth token
before killing that disposable helper, verifies its gate process is gone, then
requires journal postimage recovery and real completion/Git checks on replay.
The same probe runs against source. Retry cooldown is advanced in the temporary
test database; this does not prove the resident service timer or a live business
run recovered unattended.

When an ENOSPC outcome can be committed, the failed attempt and its resource-wait receipt are
recorded atomically. The logical leg becomes `waiting_for_resources`, preserving the failed
attempt as evidence. Independent ready work continues; a run with only parked work releases its
owner and waits durably. Every 30 seconds the resident service checks the source and database
filesystems plus recorded integration, worker checkout and event-log locations. A disk wait resumes
only when every checked location has the required free bytes (normally at least 2 GiB), positive
unprivileged inode availability where fixed inode accounting exists, and no read-only filesystem flag.
Dynamic-inode filesystems and platforms without statvfs retain byte checks without inventing inode
measurements. Unreadable locations remain parked. The wait reason exposes the failed check and
the resume event retains observed filesystem checks. These read-only, point-in-time measurements
do not reserve capacity or prove quota/write permission; a database too full to commit the initial
receipt is still a separate failure class.

Mixed failures do not delete a sibling's recovery receipt. A remaining capacity wait takes
run-state precedence over a resource wait, but resource probes support both states so disk recovery
need not wait for provider recovery. Either recovered lane can queue the run while the other wait
and any unchanged authority blocker remain intact. Each capacity-probe pass wakes at most one lane
per run, avoiding a second resume against the now-queued run's stale parked snapshot.

Narrow transient transport failures and recorded POSIX worker deaths by signals 6, 9, 11, 13 or 15
have separate per-leg budgets of two same-provider, same-model retries with 30/60-second backoff.
Transport classification excludes authority, authentication, identity, quota and acceptance errors.
Cooldown expiry is a diagnostic retry, not evidence that a connection or process has recovered.
Retry counts survive restart. Cancellation prevents wakeup, and terminal or superseded attempt
callbacks are fenced before they can overwrite the current leg.

An accepted honest stop or exhausted transport/process budget immediately parks its leg in
`waiting_for_input`, even while healthy siblings run. The scheduler parks the whole run only when
no independent work can advance. The failed attempt remains failed; the unfinished phase/run has no
completion timestamp and does not automatically redispatch an unchanged blocker. The durable event and UI expose
the reason and next action. Steering and explicit whole-run or targeted-leg retry preserve valid
completed work. A targeted input-blocker retry can also wake a resource-parked run without waking
or removing the sibling's resource wait. Other unclassified failures still fail closed; these mechanisms are not a universal
recovery guarantee. Autonomy shows scheduled retries, resource wakeups, ignored late callbacks and
actionable stops separately from successful completion.

A worker whose Code is durably waiting for input may still perform a rotated Review
of a disjoint, ready peer unit. Likewise, a ready Fix is not held behind that worker's
unrelated Review waiting on dependencies or input. Research must still complete;
active or queued turns are never bypassed, unknown/overlapping assignments fail closed,
and the target's DAG dependencies and one-live-turn-per-worker rule remain mandatory.
Neither exception marks the parked assignment complete or retries its unchanged blocker.

The resident service also checks durable input waits on its recovery poll. If an older
scheduler parked a run despite a now-ready independent peer Review, Fleet atomically
reconciles the DAG and requeues the run with `run.ready_work_resumed`. The blocked Code
attempt is not retried, reset or marked complete. A run with no eligible review stays
unchanged; cancellation wins under the same writer transaction. One malformed run cannot
prevent another run's readiness probe. This restores eligible peer work after a service
upgrade without requiring the chat orchestrator to babysit or retry authority blockers.

## Activating repairs without cancelling parked runs

The ordinary acceptance gate still refuses active runs. For durable input/resource/capacity
waits, `python -m fleet.activation --repo <repo> --receipt <receipt> --fleet-db <database>`
is an explicit Fleet-only restart operation, not a read-only diagnostic. It requires
current passing source acceptance, takes SQLite's immediate writer lock, then refuses
queued/running/stopping/unknown run states and any live or unverified owner, worker,
helper or lesson-review execution. It holds that lock across the fixed systemd restart,
so another dispatcher cannot claim work between inspection and restart. Missing schema
or unverifiable process/service identity fails closed. It requires the service's exact
repository, active state and Type=simple, which can start without waiting for DB access.
No run, attempt, wait or saved work is deleted or marked complete; no chat host is restarted.
After return the lock is released. A restart command failure is reported as unconfirmed,
not proof the old process survived; inspect service health before any further action.

## Explicit run baseline and local task branches

`Fleet baseline: <local-ref>` or the task directive `MANDATORY start point: branch ... at commit
<40-character SHA>` selects a frozen, locally resolvable commit before dispatch. Missing or conflicting
explicit baselines refuse dispatch. Arbitrary commit citations do not change the starting point.
Before Research, Fleet creates a detached run-owned integration checkout; every phase uses it instead
of the unrelated source checkout. Status retains the original `source_cwd`, effective `cwd`, and the
checkout receipt. Projects-based checkout artifacts live in the synced
`Projects/_artifacts/fleet-checkouts/<run-id>/` tree. The source branch, index and dirty files remain
untouched. Deleting a run refuses to discard modified or committed delivered work in that checkout.

Legacy runs can adopt an explicit baseline only when no writer is running and no write result or
integration has been accepted. Completed Research is retained. Clean stale worker checkouts refresh
when ancestry or the base tree changed; modified ones retain the preserved-patch/reapply path.

An explicit `own task branch named ...0N` directive provisions ordinal branches `...01` through
`...04`; one worker may name a literal branch. Fleet validates the name and refuses pre-existing
branches it does not own. The recorded branch survives retries and Fix. Local task-branch integration
requires neither a push nor a PR; switching away from that reserved branch still uses the separate
published stacked-PR delivery gate below.

## Continuous orphan recovery and progress budgets

After a native process exits, the pipe-drain grace period bounds inherited output
handles, not metadata callback latency. On expiry Fleet terminates the owned process
group and parses the finite event backlog already received before returning. A slow
session/event callback therefore cannot discard queued model identity or the final answer;
new output from a descendant cannot extend that captured backlog indefinitely.

The resident service reconciles dead process owners every 30 seconds, not only on boot. Its own
thread registry also identifies a per-run supervisor thread that exited while the service PID stayed
alive. Quiet output, age, or a missing heartbeat alone never proves that thread dead. Before requeue,
Fleet stops only birth-token-verified worker/helper/reviewer processes, fences active leases and peer
capabilities, and preserves completed legs and attempts. Unverifiable ownership blocks recovery;
cancellation stays cancelled. Two automatic orphan recoveries per run are allowed, then the run
fails explicitly instead of looping. Existing worktree refresh/integration gates remain mandatory.

Useful progress is separate from the monitor's five-second heartbeat: completed native tools/file
changes and produced answer text count; startup/settings, reasoning and token-stream chatter do not.
Default no-progress budget is 20 minutes, with warning at one-third and suspect at two-thirds.
Actual progress clears the warning with a `worker.progress.resumed` receipt. At the limit Fleet
stops the worker and uses the existing bounded stall retry (two retries by default). A separate
90-minute turn budget cannot be reset by output. `SERENA_FLEET_STALL_SECONDS`,
`SERENA_FLEET_TURN_SECONDS`, and `SERENA_FLEET_MAX_STALL_RETRIES` configure newly acquired leases.
These defaults are operational bounds, not claims about optimal model thinking time.

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

Explicit positive ownership may name a new file before it exists. Read-only,
preserve, and negated clauses are not ownership. When retrying a failed writer,
Fleet repairs older missing-file declarations from the original frozen workstream:
it adds the missing paths without dropping existing claims, changing completed
attempts, or widening to a repository claim. The policy and materialized contract
are updated together with a `run.retry_ownership_refreshed` receipt.

## The phase matrix and worker identity

Fleet runs one locked model per phase, and every agent in that phase runs it:
Research `gpt-5.6-luna` max, Code `gpt-6-astra` medium, Review `gpt-6-astra`
medium, Fix `claude-opus-5` high. `fleet/policy.py` holds it as a hard
contract, `validate_config` refuses a config that drifts from it, and
`policy_models_match_contract` re-checks every run before it starts.

For coding runs, confirmed Claude exhaustion maps unfinished Fix to
`gpt-6-astra` high. Code and Review are already `gpt-6-astra` medium and do not
change. Explicit Claude-only runs retain their Opus stack. These are per-worker handoffs; healthy
workers and completed phases keep their original routing.

Explicit coding Codex-only experiments may start their task with
`Fleet comparison profile: sol` or `Fleet comparison profile: astra`.
The two fixed Code/Review pairs are Sol xhigh/Sol high and Astra medium/Astra medium.
Both keep Luna max Research and Astra high Fix. Each run persists the exact phase models;
no mutable global experiment switch is used. Ordinary tasks retain the default stack.

### Bounded difficult retries

A failed Code or Fix integration test may queue one extra attempt on `gpt-6-astra`
`xhigh`. This is separate from quota recovery: the supervisor must observe a real
nonzero test gate with concrete assertion, syntax, or type-check failure output,
and Codex must have a positive capacity signal. Infrastructure failures, missing
dependencies, malformed evidence, honest stops, Research, Review, and Claude-only
runs do not qualify. Ambiguous failures stay failed.

The failed patch is rolled back before retry. Only the failed leg changes model;
its worker identity, prior attempt identity, and the other phases are preserved.
The frozen policy records a `difficult_retries` receipt and the event log records
`leg.difficult_retry_queued`. A second implementation failure stays failed for
operator review. This initial implementation covers integration-gate failures,
not every error a worker may report in prose or encounter inside its workspace.

Workers are Agent A through Agent D. `worker_key` is `agent:a`, not
`codex:a`: the provider is a property of the phase, not of the worker, so one
agent moves between Codex and Claude as the run advances and keeps its name,
assignment, workstream and evidence lineage the whole way. A phase is therefore
single-provider when it is built, and stops being so only when one worker is
handed to the other provider for capacity or a recorded difficult retry.

Two consequences fall out of alternating providers, both deliberate:

- **Sessions cannot span a provider change.** Default Code can continue the
  Codex Research session; default Fix opens a Claude session. Review deliberately
  does *not* continue Research or Code even though they land on Codex, because a
  reviewer that sat through the research cannot independently disagree with it.
  That suppression is one condition in `fleet_store.py`'s continuation lookup.
- **A dead provider blocks its phases.** `no-claude:` and `no-codex:` pin a run
  to one provider's stack, and a mid-run exhaustion hands the unfinished phases
  to the other provider's stack through the existing handoff path. Both are
  recorded in the frozen policy; `no_silent_fallback` still holds.

Because some phases start cold or cross providers, Research must end with a
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
`--sandbox read-only`. Write legs receive no account gateway. They may receive the separate
attempt-scoped `serena_peer` server described below, which has no account or filesystem write tools.

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
server at call time, so a stale catalog can never widen access. No catalog means no account MCP flags:
the failure mode is a worker that behaves exactly as it did before, never one that silently gets more.

`chats fleet read-tools` shows what is currently exposed and what each server denied; `--refresh`
rebuilds it. Each run records a `read_mcp_catalog` event with the same counts.

## Unattended peer collaboration

`fleet/collaboration.py`, `peer_mcp.py`, and `peer_runtime.py` add a durable advice plane.
Workers do not need the originating chat to watch, steer, or manually retry them. The resident Fleet
service remains the execution authority: messages cannot acquire claims, edit peer files, change
models/providers, approve a result, remove tests, bypass an honest stop, or extend the task.

Every ordinary attempt gets an expiring capability through its process environment, never argv or
prompt text. Only its hash is stored. The gateway derives sender/run/attempt identity from that
capability and checks current attempt generation and run cancellation on every call. Native writers
still have their existing filesystem sandbox; this capability is API authorization, not a replacement
for OS isolation against a deliberately malicious process running under the same user account.

| Worker tool | Contract |
| --- | --- |
| `read_messages(acknowledge)` | Exact roster keys, unacknowledged inbox, own help status, lesson candidates. Delivery and acknowledgement are separate. |
| `send_message(recipient, body, dedupe, reply_to)` | Targeted same-run advice; stable dedupe keys and parent-linked replies. No broadcasts or cross-run addressing. |
| `request_help(recipient, body, dedupe)` | Durable bounded diagnostic request, serviced even after the peer's normal turn ends. |
| `resolve_request(message_id, resolved, reason)` | Original requesting logical worker confirms the observed solution or explicitly escalates. |
| `propose_lesson(summary, evidence_paths)` | Candidate project fact backed by files in the integrated checkout. No immediate reuse. |
| `review_lesson(lesson_id, approve, reason)` | Independent Review worker must inspect unchanged evidence and explain endorsement/rejection. |

Messages arrive through tools at safe checkpoints, not instant interrupt injection. Workers are
instructed to read at turn start, before completion, and when blocked; they can do independent work
while waiting. A message is not proof merely because another model wrote it. Reviews retain their
independent evidence/completion gate.

Actionable requests persist their owner (recipient), deadline and outcome separately from delivery,
acknowledgement and consultation state: `pending -> answered -> resolved` or `escalated`. A reply
does not resolve a blocker. Unconfirmed requests escalate on deadline, helper failure, or run end.
Only the requester can explicitly resolve; automatic failure-help may also resolve after its
same-owner retry passes supervisor gates. Later proven resolution can close an escalation, but a
late reply alone cannot. Informational mail has no fake resolution obligation and retains honest
delivered/acknowledged state. Escalation is a durable visible status, not a new permission or an
unbounded extra worker loop.

The service reserves **one additional read-only consultation slot per run** beyond the main worker
limit. This avoids deadlock when every normal slot is occupied by a worker waiting for advice.
Consultations use the addressed peer's frozen phase provider/model/effort, positive provider capacity,
a fresh native session (never a concurrent resume of the peer's main chat), and a 300-second deadline
from request creation. Helpers may only answer the assigned request and cannot recursively ask for
help. A normal peer reply can satisfy a queued request without launching a consultation.

Limits: 96 messages/run, 3,000 characters/message, four reply hops, eight help jobs/run, one outstanding
request per attempt, one consultation at a time, and at most one restart of an interrupted helper
within its original deadline. Replies and jobs survive service restart. Replaced capabilities are
fenced, cancellation/deadline stops only the recorded process birth-token/group, and deletion cascades
mailboxes/lessons and includes helper-owned sessions. `SERENA_FLEET_PEERS=off` disables grants and
new automatic failure-help requests; it does not discard existing durable mail.

Before the existing difficult-retry escalation, an eligible supervisor-observed Code/Fix integration
test failure can synthesize a help request. A successful consultation queues **one same-owner,
same-model retry per leg**, atomically with its receipt. Failed patches remain rolled back, healthy
siblings continue, and the retry reads its incoming diagnosis. If the repair still fails, the existing
single Astra xhigh difficult retry may apply. Quota/infra failures, permission denials, malformed
evidence, honest stops, and Claude-only automatic escalation remain outside this classifier. Explicit
worker help messages work in either provider; failed/expired consultations stop boundedly rather
than claiming repair. Main workers should consume help before finishing, not exit expecting advice
to override a stop condition.

`peer.message.sent`, `peer.help.queued/answered/failed`, and `peer.retry.queued` events plus the
`collaboration` status projection expose authored messages, ack state, native helper sessions,
requested recovery, and retry receipts. The dashboard has a Peer collaboration panel.

Desktop builds explicitly depend on the MCP SDK and smoke-test `--fleet-peer-mcp` on the frozen
executable before packaging. The Windows windowed executable reconstructs its inherited standard
pipes for this mode only, before importing the MCP server; ordinary GUI startup is unchanged.
This follows [PyInstaller's windowed-stdio contract](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)
and [Win32 standard-handle semantics](https://learn.microsoft.com/en-us/windows/console/getstdhandle).

## Verified Fleet playbooks and measurements

`fleet/learning.py` stores operational lessons in the Fleet database, separate from personal memory.
An ordinary worker may propose up to eight candidates/run, with a 1,200-character summary and
1–8 existing, project-relative evidence files (each at most 1 MB). Files are SHA-256 fingerprinted;
path traversal and symlink escapes are rejected. Proposal identity is run/attempt-bound. Review
cannot endorse its own worker's lesson, a different run's lesson, or changed evidence.

Promotion happens only on a successful terminal run, with supervisor-accepted integration test
receipts, a completed author attempt, a completed independent reviewer attempt, and unchanged
evidence. Candidate or merely endorsed prose is never reused. A provider's zero exit alone does not
promote anything. Research-only runs without integration evidence cannot promote operational code
lessons under this first conservative gate.

After all Code/Review/Fix legs finish, coding runs with remaining candidates get one independent,
fresh-session read-only lesson review batch using the frozen Review model/effort (including
single-worker runs). The checkout lock is retained until that batch ends. The optional batch has
a 300-second deadline, at most one crash restart within that original deadline, and its own durable
identity/session/receipt. It must return an exact per-candidate decision envelope; changed evidence,
wrong model identity, malformed answers, cancellation or unavailable capacity cannot approve a lesson.
Review failure leaves candidates unverified and does not fail otherwise accepted code. Successful
independent endorsement STILL needs successful terminal run, completed author, real integration
test receipts and matching fingerprints before promotion. Model weights and routing remain unchanged.

Later turns receive at most three verified lessons from the same canonical project, only when the
task names an evidence file and every fingerprint still matches. Lessons expire after 30 days;
changed files make them inapplicable immediately. `fleet_revoke_lesson(lesson_id, reason)` rolls back
a bad lesson without mutating model/permission/test policy. Source-run deletion removes its lessons
and uses, rather than leaving unverifiable knowledge behind.

Terminal receipts record elapsed time, attempts/retries, passing integration gates and lesson use.
`fleet_learning_report(cwd)` adds available provider-reported token totals, including consultations;
missing usage is null, not a guessed number. These are observed cohorts, not causal speedup claims:
task difficulty, model choice, cache state and tool latency confound comparisons. No escaped-defect
rate is inferred from passing tests. No model weights are trained, and no model matrix or safety gate
is automatically optimized. Use matched held-out tasks before promoting broader workflow changes.

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
integration checkout (the source checkout when no explicit baseline was selected), and research legs
never write. One logical worker keeps one durable workspace identity
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
  accepted as truthful, but the attempt is recorded failed and no downstream phase is allowed to run;
  phase failure resolution parks the affected work in `waiting_for_input`;
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

The first enforced rejection receives one same-model corrective turn. Its failed attempt,
queued leg/DAG state and `leg.completion_repair_requested` receipt commit in one transaction;
there is no post-failure callback window in which an interruption can lose the repair.
The budget survives restart and duplicate callbacks. Repeated rejection records
`leg.completion_repair_exhausted` and immediately parks its leg in `waiting_for_input`,
retaining the failed attempt and rejection reasons. An unchanged blocker does not spin.
Disk exhaustion takes precedence and waits for storage readiness without spending this budget.
Cancellation never schedules correction, and an accepted honest stop is not a format repair.

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

POSIX terminal reads use `poll()` rather than descriptor-limited `select()`.
Acceptance includes a real PTY duplicated to descriptor1024: output remains
readable, detached reserved work stays alive, and releasing the reservation lets
cleanup finish. Invalid/closed descriptors return the terminal-gone result;
the Windows ConPTY read path is unchanged. Timeout conversion follows the
[Python poll contract](https://docs.python.org/3/library/select.html#polling-objects).

### Visual resilience lab

From the repository root, run:

```bash
.venv/bin/python scripts/fleet-resilience-lab.py --output /absolute/path/to/a/new/report-directory
```

The report directory must be new: previous evidence is never overwritten. `index.html` is an
offline, expandable test report; `results.json`, `results.xml`, `test-output.txt` and `traces/`
retain test outcomes and committed SQLite events. Tests cover dead local supervisor threads,
recovery bounds, stale leases, unchanged completed work, cancelled runs, progress warnings and hard
budgets, requester-only resolution, unavailable peers, late replies, post-Fix review/promotion,
quota recovery and difficult retries. Provider outputs, clocks and capacity are scripted; the real
Fleet control code, temporary SQLite stores, and selected real subprocess/git/test gates are used.
The lab does NOT spend subscription usage, fill the disk, stop services or inject faults into live runs.

In the Fleet tab, expand **Autonomy** for the latest 80 relevant durable events, **Supervision** for
heartbeat versus progress stage and turn budget, and **Peer collaboration** for request outcomes,
review jobs, lesson verdicts and reuse. All use real status projections; no synthetic success badges.

For production acceptance, use a disposable repository and a dedicated test service/database (never
the work Fleet service): run two workers, require A to ask B for help and confirm the outcome, and
require a Fix-time lesson on an unchanged evidence file. Close the originating chat and verify final
tests, request resolution, post-Fix independent review and promotion from durable records. Then use
an equivalent held-out task naming that file to verify reuse. Separately stop only the test service
or its recorded test-worker process, restart it and verify completed attempt IDs/hashes are unchanged.
Use the deterministic lab for quota and hard timeout injection rather than exhausting real accounts.
For matched trials set `SERENA_FLEET_LESSONS=off` only on the dedicated control-arm service:
it disables retrieval with a receipt, without disabling peer messaging or candidate review.
Repeat matched lessons-on/off trials before claiming faster or better results; green recovery tests
alone cannot establish learning effectiveness or real-model reliability.

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
