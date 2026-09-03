# Tooling & Workflows

Operational reference for the `chats` CLI, cross-agent coordination, and system workflows.
Personality lives in `Persona.md` — this file is purely the operational how-to.

## Engineering & Architecture Contracts
Before modifying Serena's core runtimes, services, or data contracts, consult `docs/`:
- `docs/architecture.md`: Resident brain daemon protocol, Gideon 6-layer model, action authority, state graph, and continuity.
- `docs/fleet-runtime.md`: Serena Fleet orchestration, worker contracts, review gates, and capacity recovery.
- `docs/memory-and-knowledge.md`: Hybrid memory retrieval (dense + BM25), query planning, feedback, and knowledge curation.
- `docs/operations.md`: Repository storage classes, machine layout, backup/bootstrap doctor, Windows container setup, and nightly restic backups.

## Git & Commit Hygiene (Mandatory for All Agents)
All agents (Claude, Codex, Gemini) must maintain strict git discipline:
1. **Commit After Every Major Step**: Never leave uncommitted changes hanging across multiple logical steps. As soon as a feature milestone, bug fix, or refactor step passes verification, commit it immediately.
2. **Conventional Commits**: Format commit messages strictly per the conventional commits standard:
   - `feat(scope): ...` — new capabilities or user-facing features
   - `fix(scope): ...` — bug fixes
   - `refactor(scope): ...` — structural code changes with no behavior change
   - `docs(scope): ...` — documentation updates
   - `test(scope): ...` — adding or updating tests
   - `chore(scope): ...` — tooling, packaging, releases, dependencies
3. **Verify Before Committing**: Run tests and lint/typechecks (`pytest`, `npm test`, `tsc --noEmit`, etc.) before committing. Never commit broken code or syntax errors.
4. **Clean Staging Only**: Never stage or commit `.env*`, `node_modules/`, `__pycache__/`, transient debug scripts, or untracked test database files.
5. **Sync with Origin**: When a complete user request or work session is finished, push commits to `origin` (`git push origin <branch>`) so Syncthing and GitHub remain in sync across machines.

## Desktop & Mobile Releases
Never install an update by hand via SSH or manual copying. Both desktop apps self-update from GitHub Releases via `electron-updater`, and the phone self-updates from CodeMagic.

### 1. Serena Desktop (`apps/desktop/`)
- **Architecture**: Public repository `duaragha/Serena`. Releases are downloaded anonymously by `electron-updater`, requiring no token.
- **When**: Any changes affecting `apps/desktop/`, `ui/`, or core desktop services.
- **Workflow**:
  1. Test: `cd apps/desktop && npm test`
  2. Bump patch version in `apps/desktop/package.json` (e.g. `0.2.11` -> `0.2.12`).
  3. Commit and tag:
     ```bash
     git add apps/desktop/package.json
     git commit -m "chore(release): bump desktop version to vX.Y.Z"
     git tag vX.Y.Z
     git push origin master --tags
     ```
  4. GitHub Actions (`.github/workflows/desktop-release.yml`) builds Linux AppImage + Windows installer into the same release.
- **Invariants**:
  - The installed AppImage on Linux must live at `~/Applications/Serena.AppImage`. The unversioned filename is load-bearing so `electron-updater` overwrites in-place instead of creating orphan files.
  - Rollback archive: `cd apps/desktop && npm run rollback:keep` (or `npm run rollback:list`).
  - Logs: `~/.config/serena-desktop/logs/backend.log` (Linux) or `%APPDATA%\serena-desktop\logs\` (Windows).
  - Release check: `apps/desktop/releases.js` polls every 15 minutes. It announces only when both the installer and its channel file are present.

### 2. Unified Inbox Desktop (`personal_projects/unified-inbox/apps/desktop/`)
- **Architecture**: Private source repo, public releases repo (`duaragha/unified-inbox-releases`).
- **When**: Any changes to `apps/desktop/` or its dependent core packages.
- **Workflow**:
  1. Verify: `pnpm --filter @unified-inbox/desktop check`
  2. Bump version in `apps/desktop/package.json` (e.g. `0.1.0-alpha.36` -> `0.1.0-alpha.37`).
  3. Commit, tag, and dispatch:
     ```bash
     git add apps/desktop/package.json
     git commit -m "chore(release): bump desktop version to vX.Y.Z"
     git tag vX.Y.Z
     git push origin master --tags
     gh workflow run desktop-release.yml -f release_tag=vX.Y.Z
     ```
  4. GitHub Actions verifies `UNIFIED_RELEASES_TOKEN` and publishes to `duaragha/unified-inbox-releases`.

### 3. iOS Apps (CodeMagic Feeds)
- Self-updates via live SideStore/LiveContainer feed endpoints (e.g. `/api/v1/sidestore/source`), which query CodeMagic API for the newest finished build.
- Shipped when a CodeMagic build for that app ID completes.

## Memory

Memories persist what you've learned about Raghav across sessions. They're injected at session start (you don't need to fetch them manually).

**Memory is not a todo list.** It holds what we did, how things work, and who he is. Anything *owed* (work in progress, a follow-up, something you're waiting on) is a **task**, never a memory. There is no "loop" type; it was removed deliberately. If you catch yourself wanting to note "where we left off", that's a task.

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

Never save: things already in memory (check first), one-off debugging state, anything he says not to remember, and anything that is really a task.

Don't announce it, don't ask permission — just run `chats memory add` alongside your response. If he objects, remove it. Default to capture, not miss. Convert relative dates ("yesterday", "Thursday") to absolute dates based on the current date in the system prompt before saving.

## Recalling Past Chats
Full-text search across every Claude AND Codex conversation on this device (unified index — claude can find codex chats and vice versa).
- `chats recall "<topic or phrase>"` — top 10 matches with date, agent, sid, title, snippet
- `chats show <sid>` — full transcript of a specific chat

Run it when Raghav says "remember when we...", "we already decided...", "I told you about X", or when a question smells like it came up before. If you'd otherwise make him repeat himself, search first.

### Always-on Telegram recall
Locket keeps a sanitized cache of Claude/Codex chats and the knowledge base so phone Serena can search them while the laptop is off.
- `chats archive-sync` pushes only changed chats and knowledge files.
- `chats archive-sync --dry-run` shows counts without uploading.
- `chats archive-sync --force` repairs or rebuilds the remote cache.
- Add the `noindex` tag to a chat to remove it from the Locket cache on the next sync.

## Talking to a Linked Sibling (claude ↔ codex)
Linked chats are a group text — Raghav gets feedback from two people at once. Either agent can ping the other live on the opposite pane.
- **If you're claude** consulting codex → `chats ask-codex "<prompt>"`
- **If you're codex** consulting claude → `chats ask-claude "<prompt>"`

Both auto-detect your sid, find the linked sibling, type the prompt into that VTE, wait for the reply, and return it.
- ❌ Never use `mcp__codex__codex` (pollutes sessions and breaks linked-pair model).
- ❌ Never use background task flows or subagent delegation to replace the bridge.

## Serena Fleet
`/fleet <task>` in Claude Code, `$fleet <task>` in Codex. Both call the local `serena-fleet` MCP server.
- Fleet runs 1 to 4 worker chats through 4 phases: Research, Code, Review, Fix (or Research, Analyze, Review, Refine).
- Provider is a property of the PHASE, locked by contract (`no-claude:` / `no-codex:` pin a run).
- Commands: `status`, `wait`, `result`, `cancel`, `retry`, `handoff`, `steer`.
- Do not recreate Fleet with native subagents, `chats ask-*`, or Claude's `/workflows` relay. Detailed runtime spec lives in `docs/fleet-runtime.md`.

## Image Generation — `chats gen-image`, NOT the linked codex
When Raghav asks for an image, run `chats gen-image "<prompt>"`. Do NOT route it through `chats ask-codex` or any image MCP inside the linked session.
- **Why**: Codex stores generated images as 2-4 MB inline base64 in its rollout JSONL. Multiple images bloat rollouts past 100+ MB and break the websocket (`Broken pipe`). `chats gen-image` spawns an isolated throwaway `codex exec` session, keeping rollouts clean.
- `chats gen-image "<prompt>"` — generate and save under `~/.codex/generated_images/`
- `chats gen-image -o <path> "<prompt>"` — save to specific file/dir
- `chats gen-image --reasoning medium "<prompt>"` — default `low`
- Wait for it (timeout 600s; usually 20-60s), report saved path. Never batch multiple images into one prompt.

## Personal project delivery
For authorized implementation work in personal projects, finishing includes autonomous delivery:
1. Branch from `main` or use an isolated worktree.
2. Stage only relevant task files.
3. Run checks (`pytest`, `npm test`, `tsc --noEmit`).
4. Commit, push, open a PR via `gh`, squash-merge to `main`, and delete branch.
5. Do the full path autonomously without asking permission at every hop. Never merge broken code. Never commit directly to `main`.

## Reaching the PC & VirtualBox VM
The PC is `pc` on the tailnet (`pc.tail4d6220.ts.net`).
Docker on the PC lives inside the VirtualBox `Docker-Ubuntu` VM. Reach it through nested SSH:
```bash
ssh docker-pc "ssh -o BatchMode=yes docker-ubuntu-vm bash -s" < script.sh
ssh docker-vm docker logs <container>          # when already inside
```
Pipe scripts via stdin (Windows cmd mangles quotes and braces). The VM sees this Projects tree at `/mnt/projects` as a VirtualBox shared folder (vboxsf).

## Launching a GUI app on Windows, from a pane
Always detach GUI applications: `Start-Process app.exe` in PowerShell, or `start "" app.exe` in cmd. Never run the bare executable.
- **Why**: Windows console is a shared object. A non-detached GUI process attaches to the pane's console buffer and spews logs directly into the agent's TUI display even after the launching shell exits.

## Never edit the repo from the PC
Diagnose on PC, run tests on PC, read logs on PC. **Do not write repo files on PC.**
- Syncthing propagates the newest mtime. The PC checkout can hold stale files, clobbering recent laptop edits.
- Windows tools rewrite line endings with CRLF, causing whole-file git diff churn.
- **Rule**: Make all repo edits on the laptop over SSH (`ssh laptop`) and commit from the laptop.

## Which machine am I on
Run:
```bash
python3 ~/Documents/Projects/serena/core/machine_context.py --text 2>/dev/null \
  || python ~/Projects/serena/core/machine_context.py --text
```
Prints machine name, OS, projects root, repo path, and Python binary. Resolves Linux vs Windows paths dynamically.

## Reading a Codex chat
Codex rollouts use `response_item` (v0.150+) or legacy `event_msg` (v0.146-). Both are parsed by `core/codex_records.py`. Injected AGENTS.md, `<environment_context>`, and attachment listings are filtered from session titles. The `codex exec --json` stream used by Fleet and supervisors uses snake_case `agent_message`.

## Diagnosing her voice
Telemetry locations:
- `~/.local/state/serena/voice-chats/serena-main.jsonl` — Verbatim spoken turns and answering models.
- `~/.local/state/serena/desk_metrics.jsonl` — Client-side telemetry.
- `~/.local/state/serena/call_metrics.jsonl` — DEAD (inactive since July 2026). Ignore it.
- `journalctl --user -u serena-wake-listener` — Local wake events and greeting status.
- Measure over multiple samples before diagnosing latency or connection drops.

## Never restart the host you are running in
Chat panes run as children of `serena-mobile-host`. Restarting the host kills or strands running terminal sessions and PTYs.
- Never run a bare `systemctl restart serena-mobile-host` from inside a chat pane.
- Use the detached transient helper when a restart is explicitly needed:
  ```bash
  SERENA_ALLOW_HOST_RESTART=1 scripts/serena-host-restart.sh serena-mobile-host.service
  ```
  Survives pane termination and writes status to `~/.local/state/serena/host-restart.log`.
- In desktop UI: use tray menu **Restart Backend**.

## Syncing the Projects tree between machines (Syncthing & Git)
Syncthing synchronizes working files; Git synchronizes history directly over the tailnet.
- **`.git` is NEVER synced by Syncthing.** Syncing `.git` corrupts packfiles and loose objects.
- History moves machine-to-machine via direct git fetch:
  ```bash
  # On PC                          # On Laptop
  git fetch laptop                 git fetch pc
  git log --oneline laptop/main    git log --oneline pc/main
  ```
- All Syncthing ignore patterns for derived content (`node_modules`, `build`, `__pycache__`, `.venv`) must use the `(?d)` prefix so Syncthing allows directory deletion.
- Windows git configs: ensure `core.autocrlf false`, `core.filemode false`, and `core.symlinks false` globally on PC.

## Cross-machine artifact storage
Final user-facing files and deliverables must live inside the synced `Projects/` root so they exist on both machines.
- Use the relevant project repository or `<Projects root>/_artifacts/<project>/`.
- Never deliver final files from `/tmp`, `Downloads`, or home directory.
- Report relative synced paths and verify the file exists on disk before concluding.
