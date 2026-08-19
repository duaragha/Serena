# Action authority and device automation

How Serena decides whether she may do something, and how the thing actually
gets done. Two modules: `core/action_authority.py` decides, and
`core/device_actions.py` drives adapters after it says yes.

## Why it exists

Authority used to be re-implemented per surface. The MCP capability broker
proved fresh direct authority one way, the laptop broker another way with a
different regex table, and the work broker a third. Three copies of the same
idea drift apart, and a copy that has drifted is a hole. This centralizes the
vocabulary without taking anything away from the existing brokers: they keep
every check they already had, and gain a shared classification, a global stop,
and durable evidence.

## Tiers

Classified by what the action does, not how it was phrased.

| Tier | Name | Meaning | What authorizes it |
| --- | --- | --- | --- |
| 0 | observe | Reads state, changes nothing | Nothing. Allowed even while stopped |
| 1 | reversible | Local, undoable in one step | Verified live turn, grant, or confirmation |
| 2 | consequential | Leaves this machine or someone else sees it | Verified live turn, grant, or confirmation |
| 3 | irreversible | Cannot be undone by asking again | A fresh confirmation naming this exact action |

The tier is the worse of two things: the effect the caller declares
(`read`, `reversible`, `external`, `irreversible`) and the deterministic risk
rules in `core/serena_policy.py`. A caller can escalate its own action; a
caller can never talk one down. `normal` risk escalates nothing, because the
absence of a signal is not evidence that something is dangerous.

Tier 0 stays available during an emergency stop on purpose. Being unable to
ask "what is happening" during an emergency is its own failure.

## The action request

`build_request()` validates and returns a frozen `ActionRequest` carrying
identity, source surface, session and turn ids, intent, capability, target,
requested scope, authorization basis, declared effect, dry-run flag, and a
request id. Anything unrecognized is refused at construction. Every decision
and every outcome is written against that request id.

Sources split into two sets. `LOCAL_SOURCES` (voice, desk, chat, ui, cli) are
places Raghav can actually speak from. `UNATTENDED_SOURCES` (system,
scheduler, fleet, automation, device) exist but are not a person talking, so
they may observe freely and may act only on a grant or a confirmation someone
made earlier on purpose.

## Authorization bases

- `origin_turn_verified` means an upstream broker independently proved that a
  live local turn directly authorized this action. Saying so is not enough:
  the request must carry a **turn proof** issued by whoever did the verifying,
  via `issue_turn_proof()`. A proof is bound to identity, source, session,
  turn, and the exact `(capability, target)` pairs the turn asked for. Each
  pair is good for one action, the proof expires (2 minutes by default, 10
  maximum), only a local source can be handed one, and a dry run does not
  burn a use. It works for tiers 1 and 2, and never for tier 3.
- `grant` is short-lived, counted, and narrow. Grants name capabilities or one
  prefix ending in `.*`; a bare `*` is refused. They expire (5 minutes by
  default, 1 hour maximum), carry a use count, and can never cover tier 3. A
  dry run does not burn a use, because simulating is not acting. Minting one
  needs a person: either an approved confirmation from
  `request_grant_confirmation()` naming that exact capability set and tier, or
  an explicit `operator_confirmed=True`. Importing the module is not authority
  to hand yourself standing permission.
- `confirmation` is a pending record a local surface resolves. It is
  single-use, expiring, and matched against the exact capability, target, and
  tier. Target matching is exact in both directions: an empty stored target
  matches only an empty request target and is never a wildcard, and a tier 2
  or 3 confirmation must name its target. It is the only thing that unlocks
  tier 3.

## Global stop

`engage_lock()` refuses every action above tier 0 and revokes every
outstanding grant **and turn proof** in the same transaction. A stop that
leaves a live warrant sitting there ends the moment someone lifts it, which is
not a stop. Pending confirmations are cancelled too. Only
`release_lock(operator_confirmed=True)` lifts it; there is no automatic
release path and no timeout.

The stop also reaches rollback. Compensation is a fresh authority request
(`authorize_compensation()`), warranted by the specific recorded action it
reverses rather than by a grant that is usually spent by then, so a stop
engaged between a step and its undo blocks the undo and the step is reported
`uncompensated` instead of quietly touching hardware.

The stop reaches the existing brokers. `core/laptop_actions.py` and
`core/mcp/capability_broker.py` each consult it before acting, so one stop
covers every surface instead of needing to be issued per broker.

## Fail closed

Unknown source, unknown effect, expired grant, missing confirmation,
unreadable store: all deny. If the risk policy file cannot be read, actions
escalate to consequential rather than falling back to permissive. If the lock
table itself cannot be read, the answer is "stopped", not "go ahead".

## Evidence

Two records, on purpose.

- SQLite (`~/.local/state/serena/action-authority.sqlite3`, mode 0600) holds
  requests, decisions, outcomes, grants, and confirmations, and answers
  "what is still unfinished" for restart recovery via `unfinished()`.
- A hash-chained JSONL beside it holds the same events. Each line carries the
  digest of the line before it, so deleting something in the middle is
  visible without needing a second copy. `verify_audit_chain()` recomputes it
  and reports the first line that breaks.

Both mirror into `core/control_plane.py` under the `action` surface when it is
available. That mirror is deliberately best-effort: a missing mirror is a
smaller failure than refusing to act. An `action` obligation opens on
`action.authorized` and closes on `action.completed`, so an authorized action
that never reported back stays visible instead of being assumed done.

## Device automation

`core/device_actions.py` runs one action or a named scene. Every step goes
through the authority first, whatever the adapter underneath is.

- **Dry run is the default.** A simulated step reports `simulated`, never
  `completed`, so a plan can be read aloud without anyone believing it
  happened. A dry run also reports whether the device is even reachable.
- **Scenes** are declarative JSON (see `config/serena-scenes.example.json`,
  copy to `~/.config/serena/scenes.json`). Listing a step in a scene grants
  nothing on its own; each one still passes the authority.
- **Idempotency**: an identical action with the same key inside 90 seconds
  returns the recorded result instead of pressing the button twice.
- **Timeouts** are per step, capped at 120 seconds, and enforced by the runner
  rather than trusted to the adapter: the call runs on a bounded worker and a
  step that does not answer in time is reported `timeout`. Python cannot
  safely kill the thread, so the wording is `abandoned`, not `cancelled` — the
  call may still be in flight, which is why an adapter is still required to
  bound its own work.
- **Rollback** runs compensation for already-completed steps in reverse order
  when a later step fails with `on_failure: rollback`. This is not a
  transaction and does not pretend to be: an adapter that has no honest undo
  returns `None` and the step is named in `uncompensated` rather than hidden.
- **Postconditions**: after a step succeeds, the adapter is asked whether the
  world actually changed. If the adapter says it worked and the device
  disagrees, the device wins and the step is reported failed.
- **Partial failure** is reported explicitly. `ActionReport.partial` is true
  when some steps worked and some did not, which is the dangerous shape.

## Adapters

All four are registered whether or not their hardware exists. Registering is
not a claim that the device is present; each reports its own availability, so
a phone and a house show up as known but unreachable rather than silently
missing. None of them ever returns success for doing nothing.

| Adapter | Needs | When absent |
| --- | --- | --- |
| `laptop` | A graphical session | Reports no desktop session |
| `android` | `adb`, and a connected authorized phone | Distinguishes "no adb", "no device", and "device not authorized" |
| `home` | `SERENA_HOME_ASSISTANT_URL` and `SERENA_HOME_ASSISTANT_TOKEN` | Names the missing variable |
| `mqtt` | `SERENA_MQTT_HOST` and `paho-mqtt` | Says which one is missing |

The laptop adapter delegates to `core/laptop_actions.py` and does not
reimplement or bypass any of its checks; without the originating local turn it
returns `unauthorized` rather than trying.

The Android adapter is a closed allowlist. There is no generic `adb shell`,
no `uninstall`, no `rm`, no wipe. App targets must be valid package names, so
a target string cannot become shell arguments.

Matter devices go through Home Assistant like anything else. Its Matter
integration exposes them as ordinary entities, so `light.kitchen` is the same
call whether the bulb speaks Zigbee, Matter, or Wi-Fi. There is no separate
Matter path on purpose.

The Home Assistant adapter opts into private-network access explicitly and
narrowly through `core/security_policy.py`'s `URLPolicy`: a house controller
lives on the LAN, so it allows http and the configured host only. It never
follows a redirect, and that is now enforced rather than asserted: the opener
installs a handler that raises on any 3xx, because `urlopen` follows redirects
by default and would otherwise carry the bearer token to whatever the
controller pointed at.

The MQTT adapter refuses a topic it cannot use — empty, or containing `..`,
`#`, or `+` — instead of rewriting it to a fallback topic and reporting
success. Publishing somewhere nobody asked for is worse than not publishing.

## Configuration

| Variable | Meaning |
| --- | --- |
| `SERENA_ACTION_DB_PATH` | Authority store location |
| `SERENA_ACTION_AUDIT_PATH` | Hash-chained audit log location |
| `SERENA_HOME_ASSISTANT_URL` | Home Assistant base URL, LAN address expected |
| `SERENA_HOME_ASSISTANT_TOKEN` | Long-lived access token |
| `SERENA_MQTT_HOST` / `SERENA_MQTT_PORT` | MQTT broker |

## The boundary a turn proof does not cross

A turn proof stops a caller widening its own authority and stops a warrant
being replayed. It does not stop code that is already running inside this
process, which can call `issue_turn_proof()` for itself the same way it can
call `authorize()`. Closing that needs the authority to live in a separate
privileged process with the callers talking to it over a socket, which is a
larger change than this slice and is not claimed here.

What it does buy today is real: a compromised or confused component has to
name the exact action it wants, for the exact turn it claims, once — and every
one of those attempts is in the audit chain with the binding it presented.

## What is not proven

No real device action has ever been executed through this code. The Android
adapter is verified against a fake `adb` runner, Home Assistant against a fake
HTTP transport, and MQTT only in its unavailable state. `adb` and `scrcpy` are
installed on this machine, but no phone was attached and no Home Assistant or
MQTT broker exists yet, so live hardware proof remains outstanding and cannot
be claimed from these tests.
