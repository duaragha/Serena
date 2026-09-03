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
`/mnt/projects` as a VirtualBox shared folder (vboxsf) of the PC's Projects
directory. It is NOT Syncthing; Syncthing does not run in the VM at all. So an
edit here reaches the build context
without copying.

## Launching a GUI app on Windows, from a pane

Always detach it. `Start-Process app.exe`, or `start "" app.exe` from cmd. Never
just run the executable.

A Windows console is a shared object, not a file descriptor. A process started
from one attaches to that console and writes straight into its screen buffer for
the rest of its life, even after the shell that launched it exits. The agent TUI
drawing in that pane has no idea, so the app's log lines shred the display
mid-line: `Waiting for background terminalhu51changes':` is the TUI and someone
else's stderr interleaving character by character.

It looks like Claude or Codex is broken. It is neither, and it hits both equally
because it has nothing to do with the agent: it is whatever pane the app was
launched from. It happened twice, with OpenWhispr and with Unified Inbox, both
times because a chat restarted the app itself with a plain `.exe` invocation.

To confirm: find the app's root process and look at its parent. A parent that is
`gone` (an exited shell) with the GUI app still alive is the signature.

POSIX has no equivalent. A child there gets file descriptors, and each pane is
its own PTY, so an app cannot reach back into a terminal it was not given.

## Never edit the repo from the PC

Diagnose there, run tests there, read logs there. Do not write repo files there.

The PC's checkout holds stale copies of some files, and Syncthing propagates
whichever side was touched most recently. Editing anything in the repo from
Windows can therefore push an ancient version over the laptop's current one and
destroy uncommitted work. On 2026-08-30 it took out seven files, including
`cli.py` (2769 to 484 lines) and `core/indexer.py` (2065 to 744). The app stopped
importing entirely, because `ui/web.py` imports `get_usage_stats` and the stale
indexer no longer had it, and only kept serving because the running process held
older code in memory. Three functions another chat had in progress and never
committed were lost outright; there is no `.stversions` on either machine.

Editing on Windows also rewrites files with CRLF, which turns every later diff
into a whole-file rewrite.

To spot a clobber, compare top-level definitions against the committed file:

```bash
diff <(git show HEAD:core/indexer.py | grep -oE '^def [a-z_]+' | sort -u) \
     <(grep -oE '^def [a-z_]+' core/indexer.py | sort -u)
```

A file missing definitions while adding none has been overwritten by an older
copy, and `git checkout HEAD -- <file>` is safe. Real in-progress work always
adds something, so anything with additions is somebody's edit and must be left
alone.

Make edits on the laptop over ssh (`ssh laptop`), and commit from the laptop.
The PC's git HEAD is still `7b2d85e` and is not usable for committing.

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

## Syncing the Projects tree between machines

Syncthing carries the working tree. Git carries history, and it moves machine
to machine directly, never through Syncthing. `.stignore` is per-device and is
NOT itself synced, so every rule below must be applied on BOTH machines by
hand; an asymmetric `.stignore` is its own bug class.

**`.git` is never synced.** Syncing it was tried on 2026-09-03 and reverted the
same day, after one pass corrupted two repos. Git's object store has invariants
a file syncer cannot honour. A `.pack` and its `.idx` must arrive together, and
serena ended up with two `.idx` files whose `.pack` never came, which broke
`multi-pack-index` and made `git fsck` fail outright. `git gc` deletes loose
objects the instant it packs them, so transfers already in flight fail with
"no such file". `commit-graph` and `multi-pack-index` are caches that reference
THIS machine's packs, so a synced copy points at packs that do not exist here.
And locket picked up two empty, partially written loose objects in the same
run. Excluding only the obviously machine-local parts (`index`, `config`,
`worktrees`, `logs`) is NOT enough — the object store itself is the problem.

Each machine is instead a git remote of the other over the tailnet, so history
moves between them with no GitHub round trip:

```bash
# on the PC                      # on the laptop
git fetch laptop                 git fetch pc
git log --oneline laptop/main    git log --oneline pc/main
```

Prefer `fetch` over `push`: pushing to the branch the other machine currently
has checked out is refused, and fetching never touches its working tree.

The laptop's `pc` remote needs two extra settings, already configured, because
the PC's OpenSSH shell is `cmd.exe` with no git on its PATH. `remote.pc.uploadpack`
and `remote.pc.receivepack` point at `C:/Users/ragha/gitshim/*.cmd`, which are
shims that exist for two reasons: the real binaries live under `C:\Program
Files` and 8.3 short names are disabled on this machine, so a space-free path is
required; and git sends the repo path POSIX-quoted (`'C:/x'`) while cmd.exe does
not strip single quotes, so each shim strips them before calling the real
`git-upload-pack` / `git-receive-pack`. Without that the error is a confusing
`''C:/Users/ragha/Projects/serena'' does not appear to be a git repository`.

Some paths are excluded from Syncthing outright. unified-inbox was, while the
two machines sat on different active branches; both are on `main` as of
2026-09-03. `mcp_servers` is PC-only infrastructure, and a deletion recorded on
the laptop propagated and wiped the amazon_shopping and opentable source from
the PC, leaving only `__pycache__`/`build`/`node_modules` so Syncthing looped on
five pull errors it could not clear. A symlink git manages is excluded
per-path, because Windows cannot represent one (it needs Developer Mode or
admin), so the PC writes a plain file and Syncthing carries that over the
laptop's symlink: konpeki's `apps/admin/templates` is the live example.

Every ignore pattern for derived content carries Syncthing's `(?d)` prefix, and
it is load-bearing. Without it Syncthing refuses to delete a directory that
still holds ignored files and loops forever on `directory has been deleted on a
remote device but contains ignored files`. That is what made both stuck cases
unclearable: five errors on `mcp_servers` whose source had been wiped down to
`__pycache__`/`build`/`node_modules`, and 425 on `frameworth/.worktrees` once
`.git` went back to being ignored. `(?d)` says "you may delete these when the
parent goes", which is right for bytecode, venvs, `node_modules`, build output
and `.git`. It is deliberately NOT applied to the path exclusions that mean
"keep this here, just do not sync it" — `/mcp_servers`,
`/personal_projects/unified-inbox` and konpeki's symlink — because there the
whole point is that the files survive.

Trailing whitespace in `.stignore` is worth stripping too; several patterns had
accumulated long runs of spaces, which is a silent way to make a pattern not
match what you think it matches.

Two Windows artifacts fake a dirty tree, and both cost hours before. Every repo
cloned on the PC carried a LOCAL `core.filemode = true` overriding the global,
so files showed modified on an exec bit Windows cannot store: konpeki reported
181 dirty files whose real diff was zero. Set `core.autocrlf false`,
`core.filemode false` and `core.symlinks false` globally, then
`git config --local --unset` each of them per repo or the local value keeps
winning. Diagnose with `git diff --ignore-cr-at-eol --stat`, and compare content
md5 with CR stripped: identical content means pure line-ending noise. A repo
with `eol=lf` in `.gitattributes` (atrium) will also show a file as modified
after any Windows tool rewrites it with CRLF; `git checkout --` on that path is
the fix.

Syncthing drift looks identical to real edits and is not. The tell is that the
working tree holds `origin/main` content while HEAD sits on an older branch.
Distinguish them by md5-ing each dirty file against `origin/main`: if they match
upstream exactly it is drift, not work, and resetting is safe once the branch is
confirmed pushed. Confirm the push before resetting, every time.

## Shipping: never a manual install

No device gets an update installed by hand. An update the user did not choose
is worse than a stale version, so each app checks for its own and swaps on
restart. There are two mechanisms in play, and they are not the same.

**Desktop (Electron): GitHub Releases.** Serena and Unified Inbox self-update
from GitHub Releases via electron-updater, and each solves the same problem a
different way. electron-updater downloads anonymously, so the release feed must
be readable without a token: Serena's own repo is therefore PUBLIC, while
Unified Inbox keeps its code private and publishes to a separate public repo,
`duaragha/unified-inbox-releases` (`RELEASE_REPOSITORY` in
`desktop-release.yml`). Do not look for Unified's desktop releases in the code
repo — the only releases there are Telegram-bridge and hub-runtime container
images, which is misleading.

Unified's workflow is `workflow_dispatch` only and its preflight refuses to run
without `UNIFIED_RELEASES_TOKEN`, a scoped token with release write access to
the releases repo. The tag must equal `v` + the version in
`apps/desktop/package.json`, not the root manifest. A run of the older
push-triggered version failed six times in a row in late August on that token
check; those failures are stale history, and publishing has been current since
(alpha.36, 2026-09-01). See **Updating the desktop app** for the AppImage
filename trap and the rollback commands.

**iOS: a live CodeMagic source, NOT a GitHub Release.** This is the part that
is easy to get backwards. Atrium, Vantage, Locket and OpenWhispr each serve a
SideStore/LiveContainer source endpoint from their own web container, e.g.
`https://pc.tail4d6220.ts.net/vantage/api/v1/sidestore/source`. That endpoint
queries the CodeMagic API for the newest finished build of its app id and
points the phone at that IPA, so the phone self-updates without anyone
installing anything. Nothing sources an IPA *from* a GitHub Release today.

Consequences worth knowing before touching it. The endpoint needs
`CODEMAGIC_API_TOKEN` in the web container, supplied by the env file at
`CODEMAGIC_ENV_FILE` (default `~/.config/serena/codemagic.env`); without it the
endpoint returns `source unavailable`. It also depends on the tailnet and on
the PC's container being up, since there is no public origin any more — Railway
was retired 2026-08-17. And CodeMagic prunes build artifacts, so old versions
disappear in a way a GitHub Release would not. Moving iOS IPA hosting onto
GitHub Releases would fix the retention and the PC dependency, but it is a
migration across four apps rather than a config edit, and it is not done.

The GitHub Actions side only *triggers* CodeMagic. atrium and locket each have
a `codemagic-ios.yml` that POSTs to `api.codemagic.io/builds` with a hardcoded
app id and `secrets.CODEMAGIC_API_TOKEN`. vantage has no such workflow: its
CodeMagic app id is `6a5504d651238a3fa7259752` (hardcoded in
`apps/web/src/app/api/v1/sidestore/source/route.ts`) and the repo holds no
secrets, so its builds are started by hand. Note vantage is a PUBLIC repo,
which is why adding the token there is a decision and not a chore.

So a change is not shipped when the code is merged, nor when the container is
redeployed. Desktop is shipped when the release exists; iOS is shipped when a
CodeMagic build for that app id finishes. Never SSH into a device to install a
build, and never hand-copy an artifact onto a phone or laptop.

## The nightly backups, and the share that vanishes

Two restic repos, both on E: which is a DIFFERENT physical disk from the data,
written by systemd timers inside the Docker VM:

- `atrium-backup.timer` at 03:30 -> `E:\Backups\atrium-restic`, from
  `/srv/atrium-data` (the originals live inside the VM now, not on the vboxsf
  share). 14 snapshots as of 2026-09-03, 4455 files, 36 GiB, `restic check`
  clean.
- `unified-backup.timer` at 02:30 -> `E:\Backups\unified-restic`: a pg_dump of
  every database individually, the state and secrets volumes, and the synapse
  media store.

The password for both is `/etc/atrium-restic-pass`. `sudo` strips the
environment, so use `sudo -n env RESTIC_PASSWORD_FILE=/etc/atrium-restic-pass
restic ...` — a plain `sudo restic` reads an empty password and dies with
"an empty password is not a password", which looks like a corrupt repo and is
not.

**The shares are TRANSIENT VirtualBox mappings, and transient mappings do not
survive a VM restart.** Both scripts begin with `mountpoint -q "$REPO"` and exit
1 when it is missing, so after any reboot the backups fail — honestly, but they
fail. They cannot be converted while the VM runs: `VBoxManage sharedfolder add`
returns `VBOX_E_INVALID_OBJECT_STATE` against a locked machine. So
`~/start-docker-vm.ps1` starts the VM and re-adds the shares if absent
(idempotent, logs to `~/start-docker-vm.log`), and a Startup entry
`ensure-docker-vm-shares.cmd` runs it at logon. Making them permanent needs the
VM powered off, once:

```powershell
VBoxManage sharedfolder add "Docker-Ubuntu" --name atriumresticE --hostpath "E:\Backups\atrium-restic"  --automount
VBoxManage sharedfolder add "Docker-Ubuntu" --name unifiedrestic  --hostpath "E:\Backups\unified-restic" --automount
```

The same VM start task is still `Interactive only` and editing it needs an
elevated shell, which is why the Startup entry exists as the non-elevated path.

A backup finishing in five seconds is normal, not broken: restic dedups, so a
run with nothing new to store does almost no work. Verify with
`restic snapshots` and `restic stats latest`, never by wall-clock time.

The old 41 GB repo at `D:\Atrium\restic` was deleted on 2026-09-03, after
confirming E: held every one of its snapshot IDs plus two newer ones and passed
a full `restic check`. Its transient share was detached first. Note that the
`atrium-backup.service` unit description still reads `-> D:\Atrium\restic`; that
string is stale, `REPO` inside the script is correct.

## Cross-machine artifact storage

Final files and user-facing outputs must live inside the active machine's
synced `Projects` root so they are available on both the Linux laptop and the
Windows PC. Use the relevant project repository when one exists. When there is
no natural project, use `<Projects root>/_artifacts/<project>/`.

Do not hand off a final artifact from `Documents`, `Downloads`, `/tmp`, a home
directory, or an app-specific cache. Temporary files may be created there while
working, but copy the finished output into `Projects` before reporting it. Use
`machine_context.py` at session start to resolve the current root rather than
hardcoding the Linux or Windows path. Report the synced relative path and
verify that the final file exists before opening or delivering it.
