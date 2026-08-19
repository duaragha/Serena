# Gideon runtime, vision, and supportive-mode contracts

This slice is additive. It does not replace the resident brain, voice loop,
capability broker, control-plane obligation ledger, or coding supervisor.

## Resident brain integration

`core.gideon_api.GideonAPI` is the provider-neutral facade over continuity,
commitments and briefings, the personal state graph, the world cockpit, device
scenes, supportive mode, runtime evidence, and consent-gated vision.
`core.brain_gideon_tools` exposes that facade as the `serena-gideon` MCP server.
The same tools are registered with both the resident Claude SDK session and the
Codex app-server registry, so a provider switch does not change Serena's local
capabilities.

Mutating tools do not trust model arguments as authority. They match the action
and target against the actual serialized local turn, issue one exact single-use
turn proof, and execute through `ActionAuthority`. Reads remain local
observations. Support status never returns raw reflection text.

The resident provider path now routes Claude to Codex to the loopback local
conversation model. If all three are unavailable it returns an honest offline
reply and stores a bounded, image-free copy of the turn in `ContinuityStore` for
later resumption. Local results retain their real provider and model labels.

## Runtime readiness

`core.runtime_readiness` defines the common state vocabulary `ready`,
`degraded`, `offline`, and `recovering`. A coordinator requires probes for the
brain, voice, tool boundary, primary UI, and coding jobs. Probe and recovery
callbacks are injected; importing the module never starts, stops, or restarts a
service.

Recovery attempts and unfinished work use a user-private SQLite ledger. Retry
budgets and cooldowns survive process replacement. Schema version 2 adds an
atomic, expiring work lease so two supervisors cannot replay one row at the
same time, while a crashed worker's lease becomes recoverable. Unfinished work
only resumes through a named idempotent handler. Existing supervisors can
register their native resume operation without moving native state into this
database.

`run_short_soak` is the practical acceptance harness. It records state counts
and transitions, caps runs at one hour, and rejects a 24-hour duration. The
24-hour soak remains deferred and was not run.

## Visual context

`core.visual_context` has no watcher and no background capture entry point.
Every frame needs a fresh consent envelope containing actor, source, session,
request, `screen.capture` scope, an authority-issued receipt, and an expiry of
five minutes or less. The service asks an injected local authority consumer to
atomically consume that receipt, and each request can be used once. The active
app is checked against private-app rules before the capture indicator or
screenshot adapter runs.

Screenshot, OCR, accessibility-tree, desktop-context, and visible-indicator
adapters are injected. Raw frames remain in process memory and the service
registry drops them on expiry. Provider payloads contain bounded redacted OCR,
accessibility data, redacted active app/document/task context, and capture provenance.
The pixels remain a separate validated frame for an explicitly selected local
or provider image path.

Post-action verification never executes an action. It accepts a visual capture
that names the authority broker's action receipt and checks declared observable
postconditions. The ws-2/ws-6 or desktop integration must implement the
injected consent consumer and pass the real action receipt into this API.

Production desktop adapters remain a physical integration gap. Tests use only
deterministic fakes and perform no real screenshot, OCR, or accessibility read.

## Supportive mode

`core.supportive_mode` is disabled by default. Check-ins and locally computed
pattern insights require separate opt-ins. Disabling the mode immediately stops
provider-context injection and check-in eligibility without silently deleting
private writing.

Reflections live in a user-private SQLite database with per-entry expiry,
explicit single-entry deletion, confirmed delete-all, and content-free lifecycle
events. Lowering the configured retention immediately caps existing entries and
removes any that are newly expired. Corrupted tag metadata fails empty without
hiding otherwise readable reflections. Pattern insights are local counts over
user-supplied moods and tags.
Each insight lists source entry IDs and content hashes and explicitly says it is
not a diagnosis. Raw journal text is never inserted into provider context.

`context_for_provider` produces one provider-neutral block, so the resident
Claude and Codex fallback paths can receive the same approved relationship
context. `support_boundary` supplies fixed medical and crisis constraints. It
does not diagnose, prescribe, claim therapist status, send a notification, or
contact emergency services.

The resident brain now injects that block into Claude, Codex, and local turns
when supportive mode is enabled. Fixed medical and crisis boundaries are
injected when the current words require them even if optional reflection and
check-in features are disabled. Raw reflection bodies are never injected.

No external messages, service changes, model downloads, real device actions, or
24-hour soak are part of these tests.
