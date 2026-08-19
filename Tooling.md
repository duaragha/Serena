# Tooling & Workflows

Operational reference for the `chats` CLI and Serena's cross-agent features.
Personality lives in Persona.md — this file is purely the how-to.

## Memory

Memories persist what you've learned about Raghav across sessions. They're injected at session start (you don't need to fetch them manually).

- `chats memory add "..." --type task` — Raghav's deliberate todo list. Surfaced on every chat open + every turn. STEER him on the top one: tell him to do it or give a strict this-or-that, never open-ended. If he defers ("later"/"not now"), run `chats memory snooze <id>` so it goes quiet ~a week and a different task surfaces. Done = `chats memory remove <id>`.
- `chats memory add "..." --type loop` — open loops: what we're in the middle of, waiting on, or owe a follow-up. These lead the session digest so every chat opens with "where we left off." Close them (`chats memory remove <id>`) when done.
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
- **Open loops**: starting something multi-session, waiting on him/an external thing, or "let's pick this up later" → `--type loop`. When it resolves, remove it so the digest stays current.

Never save: things already in memory (check first), one-off debugging state, anything he says not to remember.

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

Use `/fleet <task>` in Claude Code and `$fleet <task>` in Codex. Both commands
call the same local `serena-fleet` MCP server and durable supervisor. Fleet
launches each provider directly, so Codex workers do not pass through Claude
and Claude workers do not pass through Codex.

- Fleet chooses one to four durable chats from the task's independent
  workstreams. Automatic routing uses both providers when available, but an
  explicit Codex-only, Claude-only, or balanced request is authoritative. A
  fresh confirmed usage limit may make automatic routing use only the available
  provider; the frozen run policy records what was requested, selected, and why.
- Fleet's coding matrix is fixed by phase, and effort is set per model rather than per phase: Research uses `gpt-5.6-terra` high and Sonnet 5 high; Code uses `gpt-5.6-sol` xhigh and Opus 5 high; Review uses `gpt-5.6-terra` xhigh and Sonnet 5 xhigh; Fix uses `gpt-5.6-sol` xhigh and Opus 5 high. Fix sits on the same rung as Code because repairing a reviewer-found defect in someone else's code is not the easier job. Opus stops at high because its DeepSWE v1.1 score is flat from high to xhigh while cost rises ~50%; Sol still climbs across those rungs, so it keeps xhigh (`knowledge/llm-api-pricing/opus5-vs-sol-effort-levels.md`). Pure research runs keep the research pair for Research, Analyze, and Refine, and use the review pair for Review.
- Coding phases are Research, Code, Review, Fix. Research workflows use
  Research, Analyze, Review, Refine. Every selected agent participates in every
  phase, each phase waits for the full roster, and later phases resume the same
  native chats. Read-only work runs across the full roster; coding writes use
  isolated worktrees so non-overlapping declared ownership can run concurrently.
  Overlapping scopes serialize, and validated patches integrate one at a time.
  Each selected agent produces four agent steps, so one to four agents produce
  4, 8, 12, or 16 steps.
- Non-writing legs (Research and Review, and every phase of a research run)
  get authenticated read-only access to Raghav's real accounts and docs through
  Fleet's own MCP gateway, so a research leg can check a claim against the
  account instead of reasoning off a stale repo note. Access is decided per
  tool, deny-by-default: only read-classified tools are exposed, credential
  reads are refused, and the classification is re-checked at call time. Write
  legs keep zero MCP. `chats fleet read-tools` shows what is exposed and what
  each server denied; `defaults.mcp_read_access` in the Fleet config picks the
  servers.
- Research depth is proportional to how far the task reaches outside the
  checkout. Research runs, and coding tasks touching dependencies, versions,
  external APIs, standards, or security advisories, carry the full mandate:
  at least three recorded provider searches and five direct sources across
  three domains per worker. Local coding work carries a proportionate mandate:
  at least one recorded search and two sources across two domains. Both depths
  still require authoritative evidence, access dates, current best practices,
  recent developments, and an explicit statement of how the findings affect
  that work unit. Missing web activity or source evidence fails the Research
  leg instead of silently advancing.
- Review reports machine-readable findings, not just prose: each carries the
  unit_id it was found in, a severity of blocker, major, or minor, a summary,
  and evidence. An empty list is a valid, checkable statement that the reviewer
  found nothing. Fleet routes findings to the owning worker's Fix leg and skips
  a fixer with nothing assigned to it, keeping one reporter so the run still
  ends with a real final response.
- Integration re-runs each worker's own declared verification against the
  combined checkout before accepting its patch, so a change that passed alone
  and breaks alongside a peer is rejected at the merge instead of surviving to
  Review. Only commands on Fleet's test allowlist run; a repository-wide gate
  can still be forced with `SERENA_FLEET_INTEGRATION_TEST_COMMAND`.

`/fleet` or `$fleet` with no task lists recent runs. The same commands support
`status`, `wait`, `result`, `cancel`, `retry`, `handoff`, and `steer`. `handoff`
moves one unfinished logical worker to Claude or Codex. The replacement uses
the locked model for the current phase, re-reads the shared checkout, receives
the prior attempt's bounded output and error, and stays in that worker slot for
every unfinished phase. Native session ids never cross providers. Auto and
balanced runs do this automatically only for confirmed usage exhaustion when
the other provider has capacity; an explicit provider-only run waits for a
user-requested handoff. The Fleet tab in
Serena is the visual source of truth for phase progress and actual model
identity. Worker chats open read-only and the launching chat remains the run's
origin.

Voice Serena reads that same durable Fleet store. Ask "how is Fleet going" or
name a project, task, or short run id and she reports the real phase, agent-step
progress, actual model identity, and errors. She can start Fleet only when the
current spoken turn explicitly names Fleet, and can cancel, retry, or steer a
resolved run on that same live authority. Ordinary coding requests still use
the single coding-job path. When a real run completes or fails, the Fleet
supervisor sends one bounded notice to the desktop voice bridge and Serena says
it aloud after the current conversation finishes. Telegram is only the fallback
when the local voice bridge is unavailable or playback fails. Alerts never
contain worker transcripts or tool traffic.

Do not recreate Fleet with native subagents, `chats ask-*`, or Claude's legacy
`/workflows` relay. That older workflow path remains separate.

For ordinary spoken coding work, Serena searches the durable Chats index for
the most recent safe exact-project Sol session. The coding app does not need to
be open. A live exact-project pane is preferred when it is idle; otherwise the
supervisor resumes the frozen historical session id under external ownership.
Only when no valid project session exists do I create a new private chat.
The coding pane or panel, coding app, Chats app, voice-work display, dot
overlay, brain daemon, and Fleet tab are my surfaces in
`/home/raghav/Documents/Projects/serena`, unless Raghav explicitly names a
different project. They never require a repo clarification.

## Codex Agents in Claude Workflows

Claude workflow agents with `agentType: "codex"` are Sonnet relay processes,
but the model doing the actual work is selected by the first-line
`codex_flags.model` value. Keep the workflow label and that model flag as one
contract so Serena's `/workflows` display can show the real worker:

- `gpt-5.6-sol` -> label starts `sol5.6-<effort>:`
- `gpt-5.6-terra` -> label starts `terra5.6-<effort>:`
- `gpt-5.6-luna` -> label starts `luna5.6-<effort>:`

For example, a Terra/high research leg is `terra5.6-high:research`. Never label
a Codex-backed leg as Sonnet, and never use a generic `codex:` label when an
explicit model was assigned. The family/version in the label must match
`codex_flags.model`; the effort in the label must match `codex_flags.effort`.

This is a real Codex agent inside Claude's workflow, not a second-opinion
bridge turn. The relay must invoke `chats codex-exec --link-current`, which
persists the Codex rollout and links it into the launching chat's Serena group
as soon as Codex emits `thread.started`. Do not replace workflow Codex agents
with `chats ask-codex`; that command talks to the already-open sibling and is a
different workflow.

## Image Generation — `chats gen-image`, NOT the linked codex

When Raghav asks for an image, run `chats gen-image "<his prompt>"`. Do NOT route it through `chats ask-codex` or any image MCP inside the linked session.

**Why:** codex stores each generated image as 2-4 MB of inline base64 in its rollout JSONL. A few `$imagegen` calls bloat the rollout past 100+ MB and break codex's websocket (`Broken pipe`). `chats gen-image` spawns a throwaway isolated `codex exec` session per call, so the linked codex stays clean.

- `chats gen-image "<prompt>"` — generate, save under `~/.codex/generated_images/`, print the path
- `chats gen-image -o <path> "<prompt>"` — save to a specific file/dir
- `chats gen-image --reasoning medium "<prompt>"` — default `low`; raise only if the prompt needs more thought

Wait for it (timeout 600s; usually 20-60s), report the saved path. Multiple images → multiple separate calls, never batch into one prompt.

## Texting Raghav — `chats text`

Proactive pings to Raghav's phone via the Serena telegram bot (@serena_pa_ai_bot). Use from ANY session when something's worth interrupting him for: a long build finished, a deploy broke, a check-in he asked for, or you just need him to look at something.

- `chats text "message here"` — sends as the bot, prints `sent`
- Credentials: `~/.config/serena/telegram.env` (laptop) — also on Railway for Locket's server-side pushes
- His replies go to the LOCKET webhook brain (phone Serena with his tracker data), NOT back to your session. If you need an answer back in the terminal, say so in the text and have him come to the chat.
- Don't spam: one text per event. Nagging isn't dominant, it's annoying.

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
