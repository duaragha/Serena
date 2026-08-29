# Tooling & Workflows

Operational reference for the `chats` CLI and Serena's cross-agent features.
Personality lives in Persona.md — this file is purely the how-to.

## Memory

Memories persist what you've learned about Raghav across sessions. They're injected at session start (you don't need to fetch them manually).

**Memory is not a todo list.** It holds what we did, how things work, and who he is. Anything *owed* (work in progress, a follow-up, something you're waiting on) is a **task**, never a memory. There is no "loop" type; it was removed deliberately after the open-loops rail grew to 69 half-tracked entries, most already shipped or plain wrong. If you catch yourself wanting to note "where we left off", that's a task.

- `chats memory add "..." --type task` — anything owed, whether he owns it or you do: his todo list, work you're mid-way through, a follow-up you're waiting on. Surfaced on every chat open + every turn. STEER him on the top one: tell him to do it or give a strict this-or-that, never open-ended. If he defers ("later"/"not now"), run `chats memory snooze <id>` so it goes quiet ~a week and a different task surfaces. Done = `chats memory remove <id>`. Write them so a cold reader could act: what's already done, what's left, the exact file/ID/command, and what would make it wrong.
- `chats memory add "what you learned" --type user` — who he is, how he works, preferences, style
- `chats memory add "what you learned" --type feedback` — what worked or didn't in YOUR approach
- `chats memory add "..." --type project` — ongoing work, decisions, constraints
- `chats memory add "..." --type reference` — tool/workflow/API pointers

### Auto-capture (do this without being asked)

Save immediately when you detect:
- **Corrections**: "no, not that", "I meant X", "don't do Y" → `--type feedback`
- **Preferences**: "I prefer X", "don't use Z" → `--type user` or `feedback`
- **Project decisions**: "we're going with X", "the plan is Z" → `--type project`
- **Personal facts**: job/relationship/goal/schedule changes → `--type user`
- **Tool/workflow choices**: "use this library", "deploy to X" → `--type reference`
- **Repeated friction**: same correction twice → that's a pattern → `--type feedback`
- **Anything owed**: starting something multi-session, waiting on him or an external thing, or "let's pick this up later" → `--type task`, NOT a memory. Remove it when it resolves.

Never save: things already in memory (check first), one-off debugging state, anything he says not to remember, and anything that is really a task. Outstanding work belongs in `--type task` where he can be nudged on it.

Don't announce it, don't ask permission — just run `chats memory add` alongside your response. If he objects, remove it. Default to capture, not miss.

Convert relative dates ("yesterday", "Thursday") to absolute dates based on the current date in the system prompt before saving.

## Recalling Past Chats

Full-text search across every Claude AND Codex conversation on this device (unified index — claude can find codex chats and vice versa).

- `chats recall "<topic or phrase>"` — top 10 matches with date, agent, sid, title, snippet
- `chats show <sid>` — full transcript of a specific chat

Run it when:
- Raghav says "remember when we...", "we already decided...", "I told you about X" — search before asking him to repeat
- A question smells like it came up before (project decisions, debugging history)
- You're about to advise on something a past chat likely covered

Skip it for trivial questions or things answerable from the current session. The rule: if you'd otherwise make him repeat himself, search first.

### Always-on Telegram recall

Locket keeps a sanitized cache of Claude/Codex chats and the knowledge base so phone Serena can search them while the laptop is off. The desktop queues a sync after completed turns and also refreshes every 30 minutes.

- `chats archive-sync` pushes only changed chats and knowledge files.
- `chats archive-sync --dry-run` shows counts without uploading.
- `chats archive-sync --force` repairs or rebuilds the remote cache.
- Add the `noindex` tag to a chat to remove it from the Locket cache on the next sync.

The cache contains user/assistant text only. Tool results, machine context, common secret formats, and Vault data are excluded. New memories carry the current chat session id and agent; old memories without provenance stay explicitly unknown.

## Talking to a Linked Sibling (claude ↔ codex)

Raghav's mental model: linked chats are a group text — he gets feedback from two people at once. Either agent can ping the other; he sees the conversation happen live on the other pane.

- **If you're claude** consulting codex → `chats ask-codex "<prompt>"`
- **If you're codex** consulting claude → `chats ask-claude "<prompt>"`

Both auto-detect your own sid, find the opposite-agent linked sibling via Serena's group metadata, type the prompt into that VTE, wait for the reply, return it.

### WRONG ways to consult the sibling (never use when linked)
- ❌ `mcp__codex__codex` / `mcp__codex__codex-reply` — spawns a fresh invisible MCP codex session, pollutes `~/.codex/sessions/`, breaks the linked-pair model
- ❌ The OpenAI codex plugin's background-task flow (`codex-companion.mjs task --background --resume`) — spawns an offline worker and tells you to watch it in another terminal; defeats the whole bridge
- ❌ Any `Task`/subagent that delegates to the other agent

### Decision flow
1. Run `chats ask-codex` (claude) / `chats ask-claude` (codex). It auto-detects whether you're linked.
2. If it says "no linked sibling" → fall back to a one-shot MCP consult, or tell Raghav to link one in Serena.
3. Never reach for a "spawn/background the other agent" tool without ruling out the bridge first.

## Serena Fleet

`/fleet <task>` in Claude Code, `$fleet <task>` in Codex. Both call the same
local `serena-fleet` MCP server and durable supervisor, which launches each
provider directly.

Fleet picks one to four durable worker chats from the task's independent
workstreams and runs each through four phases: Research, Code, Review, Fix for
coding, or Research, Analyze, Review, Refine for research. Every selected agent
runs every phase, so a run is 4, 8, 12 or 16 agent steps.

- Provider is a property of the PHASE, not the worker, and the model for each
  phase is a locked contract the server refuses to let a chat change.
- `no-claude:` / `no-codex:` pin a run to one provider when the other is out.
- No task lists recent runs. Also `status`, `wait`, `result`, `cancel`,
  `retry`, `handoff`, `steer`.
- The Fleet tab in Serena is the visual source of truth for phase progress and
  actual model identity.

**Do not recreate Fleet** with native subagents, `chats ask-*`, or Claude's
legacy `/workflows` relay.

Everything else about Fleet is enforced by the server, so it is documentation
rather than instruction and lives where it can be read on demand:

- Runtime, phase matrix, worker identity, read-only MCP access, writer
  isolation, completion and review contracts: `docs/fleet-runtime.md`
- Why each phase locks the model it does, with the benchmark numbers:
  `knowledge/openai-models/fleet-phase-model-picks.md`


## Image Generation — `chats gen-image`, NOT the linked codex

When Raghav asks for an image, run `chats gen-image "<his prompt>"`. Do NOT route it through `chats ask-codex` or any image MCP inside the linked session.

**Why:** codex stores each generated image as 2-4 MB of inline base64 in its rollout JSONL. A few `$imagegen` calls bloat the rollout past 100+ MB and break codex's websocket (`Broken pipe`). `chats gen-image` spawns a throwaway isolated `codex exec` session per call, so the linked codex stays clean.

- `chats gen-image "<prompt>"` — generate, save under `~/.codex/generated_images/`, print the path
- `chats gen-image -o <path> "<prompt>"` — save to a specific file/dir
- `chats gen-image --reasoning medium "<prompt>"` — default `low`; raise only if the prompt needs more thought

Wait for it (timeout 600s; usually 20-60s), report the saved path. Multiple images → multiple separate calls, never batch into one prompt.

## Personal project delivery

For implementation work Raghav has authorized in a personal-project repo,
finishing includes delivery by default. Branch from current `main`, use an
isolated worktree when needed to preserve his dirty checkout, stage only files
that belong to the task, run the relevant checks, commit, push, open a PR,
squash-merge it to `main`, and delete the branch. Do the full path without
asking permission at every hop, and use the reachable host and `gh` yourself.
Non-trivial changes need a clean typecheck/build before merging; never merge
code known to be broken.

This does not turn a read-only, review, or diagnostic request into an
implementation or delivery task. It does not apply to Frameworth work, to a
repo or task with an explicit no-commit/no-push/no-merge boundary, or to
unrelated changes already in the checkout. Never commit directly to `main` and
never sweep his pre-existing files into the task commit.

## Reaching the PC

The PC is `pc` on the tailnet, `pc.tail4d6220.ts.net`. It used to be
`raghavsgamingpc`; that name is dead as an ADDRESS, but the Windows hostname is
still `RaghavsGamingPC`, which is why `core/machine_context.py` still maps that
string and must not be "corrected".

Docker on the PC is **not** Docker Desktop any more. It lives in the VirtualBox
`Docker-Ubuntu` VM, so reach it through the VM and never through a docker
context:

```bash
ssh docker-pc "ssh -o BatchMode=yes docker-ubuntu-vm bash -s" < script.sh
ssh docker-vm docker logs <container>          # when already inside
```

Quoting through Windows cmd mangles braces and quotes, so pipe a script on
stdin rather than inlining the command. The VM sees this Projects tree at
`/mnt/projects` over Syncthing, so an edit here reaches the build context
without copying.

## Which machine am I on

A `SessionStart` hook runs `core/machine_context.py` and prints a short banner
before the first tool call: machine name (laptop or PC), OS, the projects root,
this repo's path, and the Python to use. Agents resumed on the other machine
used to assume Linux paths on Windows and fail on the first command; now the
answer arrives before any work starts.

The projects root is derived from where this repo actually sits, not guessed
from a folder name, because both `Documents/Projects` and `Projects` exist on
the PC and only one of them is real.

Install it on a machine by adding to `~/.claude/settings.json`:

```json
"SessionStart": [
  { "hooks": [ { "type": "command",
                 "command": "python <repo>/core/machine_context.py",
                 "timeout": 5 } ] }
]
```

## Reading a Codex chat

Codex records a turn differently between its own versions, and Serena reads both
through `core/codex_records.py`. Up to 0.146 a turn was an `event_msg` whose
payload type was `user_message` or `agent_message`. From 0.150 those events are
gone and the turn is a `response_item` message with a role. Reading only the old
shape made every new Codex chat index as zero messages, open as an empty
transcript, and hang `chats ask-codex` waiting for an event that never arrives.

Two traps if this changes again. Older rollouts carry BOTH shapes for the same
turn, so accepting both doubles every historical chat; the events win where they
exist. And the new shape exposes what Codex injects into the user slot
(AGENTS.md, `<environment_context>`, attached-file listings) which the old events
hid, so it is filtered: otherwise every chat is titled "AGENTS.md instructions".

The `codex exec --json` STDOUT stream is a DIFFERENT serialization from the rollout
file and still uses snake_case `agent_message`. Fleet and the voice supervisor
read that stream, not the rollout, and were never affected. Check a live run
before "fixing" them.

## Diagnosing her voice

Where the evidence actually is, because two of these look alive and are not:

- `~/.local/state/serena/voice-chats/serena-main.jsonl` is the gold mine. Both
  sides of every spoken turn verbatim, plus the model that answered it. It
  cracked three separate bugs open in one read: the wrong model serving voice,
  a reply whose second sentence never played, and whisper mangling his words.
- `~/.local/state/serena/desk_metrics.jsonl` is the live client-side telemetry.
- `~/.local/state/serena/call_metrics.jsonl` is DEAD, nothing has written to it
  since 2026-07-17. Do not draw conclusions from it.
- `journalctl --user -u serena-wake-listener` carries the wake events, which is
  where a 100% failure rate of the cold-start greeting was hiding.

Known-failing tests, so "did I break this" is a comparison and not a guess. As
of 2026-08-21 `voice/desk/tests voice/call/tests -p no:randomly` is 429 passed,
3 failed, and those three are pre-existing:
`test_awake_handoff_starts_one_session_without_scoring_another_wake`,
`test_import_does_not_load_heavy_model_modules`, `test_tts_backend_selection`.
Also `tests/test_plugin_runtime.py` passes 70/70 alone but fails under a full
sweep on file-descriptor exhaustion, which is the runner and not the code.

Measure before concluding, and measure more than once. On 2026-08-21 a single
sample produced two confident and wrong root causes in one night: a latency
metric declared broken that was accurate, and a remote TTS declared unreliable
that answered 15 of 15 calls in 0.10s. Both would have died against a five-run
baseline. State the sample size out loud when reporting a cause.

## Never restart the host you are running in

Chat panes are children of `serena-mobile-host`, so restarting it kills every
open terminal including the one issuing the command. The `systemctl restart`
then never returns and the pane hangs on a command that cannot finish. Worse,
each pane now lives in its own systemd scope, so a host restart no longer kills
the child, it strands it: the process keeps running with nobody holding the
other end of its PTY, unreachable and still holding memory. One survived 22
hours that way.

So an agent does not restart `serena-mobile-host` or `serena-desk` mid-work. A
`PreToolUse` hook (`~/.claude/hooks/block-host-restart.sh`) refuses it, the loop
form included, and `pkill`-ing the host process too. Reading status, logs or the
unit files is untouched.

The tray menu now carries **Restart Backend**, which runs that same helper and
reloads the window once the new server answers. It is there because the app
usually ATTACHES to the long-lived `serena-mobile-host` rather than owning a
server, so restarting the app reloads no Python at all: a fix can sit on disk
for hours while every request is served by the process that started before it.
The menu entry says how far behind the running server is, from
`/api/backend-freshness`, which compares the process start time against the
newest first-party `.py` mtime. Restarting still ends open panes and cycles the
voice pipeline that shares the unit, so it stays an explicit choice rather than
something tied to closing the window.

Restarts happen at the END of a stretch of work, and Raghav calls it. When he
has asked for one, use the detached helper rather than a bare systemctl:

```bash
SERENA_ALLOW_HOST_RESTART=1 scripts/serena-host-restart.sh serena-mobile-host.service
```

A bare restart is issued from a pane that lives inside the unit being
restarted, so the shell is killed partway through and the caller gets SIGKILL
instead of a result. It sometimes completes anyway and sometimes leaves the
restart half applied, and there is no way to tell which. The helper runs the
restart in its own transient systemd unit, outside the target's cgroup, so it
survives the pane dying and writes the outcome to
`~/.local/state/serena/host-restart.log`. Read that log after the new host is
up to confirm what happened. It honours SERENA_ALLOW_HOST_RESTART itself, so
it is not a way around the rule.

The bare form still works when he has asked for it:

```bash
SERENA_ALLOW_HOST_RESTART=1 systemctl --user restart serena-mobile-host
```

A starting host reaps pane scopes whose owner is gone, so a restart cleans up
what a previous one stranded. Panes owned by a host that is still alive are left
alone.

## Updating the desktop app

The app updates itself on both platforms through the same code path. The menu
bar carries **About → Check for Updates…** and **About Serena**, which report
the version and which platform build is running, as native dialogs.

Updates are deliberately manual and never silent. Serena IS the terminal, so an
update landing mid-turn is worse than a stale version: nothing downloads until
asked, and the swap happens on restart.

Publishing a release, from a clean checkout on either machine:

```bash
# bump desktop-electron/package.json, then
git tag v0.2.0 && git push origin v0.2.0
```

One tag builds and publishes both the AppImage and the NSIS installer to the
same GitHub Release, which is the feed electron-updater reads. The tag must
match the package version or the workflow refuses.

`duaragha/Serena` is **public** as of 2026-08-22, and the update channel is the
reason. A private repo's release assets need an Authorization header to
download, so the app would have to carry a GitHub token: baked into the artifact
where it can never be rotated, or placed in a file on every machine. Public
releases are read anonymously, so no credential exists anywhere in the update
path and the workflow publishes with its own `GITHUB_TOKEN`.

The history was scanned before flipping it. The only key-shaped strings are
obvious fixtures in `tests/test_fleet_context.py`. `Persona.md` was committed
twice early on, but that version predates anything personal and it has been
gitignored since. Persona, memory content, knowledge content and every `.env`
stay untracked; if that ever changes, the repo is public and the leak is
immediate.

Three files repeat the same owner/repo and a test keeps them identical:
`desktop-electron/package.json`, `windows/electron-builder.win.yml` (passing
`--config` makes electron-builder ignore the package.json block, so it cannot be
inherited), and `updates.js` for the runtime check.

The installed AppImage lives at `~/Applications/Serena.AppImage`, and the
missing version in that filename is load-bearing. electron-updater writes the
new build to the SAME path only when the current name has no version in it;
otherwise it drops `Serena-0.3.0-x86_64.AppImage` alongside the old one and the
`.desktop` entry keeps launching the version you already had. Do not rename it
to something versioned, and do not point the launcher into `dist/`, which is
build output that a rebuild overwrites.

Before installing a new build, archive the current one so a bad update is
recoverable, because a broken Serena is also the tool you would fix it with:

```bash
cd desktop-electron
npm run rollback:keep     # archive the current artifact
npm run rollback:list     # show what can be rolled back to
```

Windows signing is optional and free. `windows/make-signing-cert.ps1` creates a
self-signed certificate, which is what gives electron-updater its real
guarantee: an update must be signed with the same key as the install. A paid
certificate would only remove the SmartScreen prompt, which matters for
strangers downloading the app and not for two personal machines. Keep the .pfx
backed up and out of the repo; losing it means installs signed with it can no
longer be updated.

Two builds legitimately cannot update themselves and say so instead of failing
quietly: a development run, and a Linux AppImage that was extracted rather than
launched as a file, since electron-updater rewrites the AppImage in place.

When something goes wrong with the desktop app, read
`desktop-electron`'s backend log before guessing. A packaged Windows app has no
console, so the Python sidecar's stdout went nowhere: a backend that died left
no exit code, no traceback, and a window stranded on a dead port whose only
symptom was "Failed to fetch" in the renderer. Everything the sidecar prints and
every decision the shell makes about it (ready, exited, restarting) now lands in
`logs/backend.log` under the app's user data, reachable from **About → Open Log
Folder**. The directory is named after the package `name`, not the product name,
so it is `serena-desktop` and not `Serena`: `~/.config/serena-desktop/logs/` on
Linux, `%APPDATA%\serena-desktop\logs\` on Windows. The same directory holds
`announced-builds.json`, which is how to check whether the release notifier
actually fired.

The app announces a new release itself, as a native notification, one per
platform. `desktop-electron/releases.js` polls the releases API every fifteen
minutes and fires when a platform's installer AND its channel file are both on
the release. Both halves matter: the Linux job creates the release and uploads
the AppImage, the Windows job adds the installer minutes later, so announcing on
the tag alone fires once, too early, and points a machine at an artifact that is
not there yet. It reports both platforms rather than only the one it runs on,
because whoever cut the release is waiting on both from one screen. What has
already been announced lives in `announced-builds.json` under the app's user
data, so a restart does not replay it.
