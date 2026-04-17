# Personal Assistant Features — Spec

## 1. Session Greeting with Context

**What it does:** When you start or resume a session, I greet you naturally with awareness of what you've been doing recently, time of day, and what's relevant right now.

**What gets injected via SessionStart hook:**
- Time of day → shapes tone ("Morning" vs "Evening" vs "Late night again?")
- Last 3 sessions: title, project, how long ago (parsed from most recent `.jsonl` files)
- Current git branch + uncommitted changes (if in a repo)
- Any memories with upcoming deadlines within 7 days

**What the greeting looks like:**
```
Evening, Raghav. You were on the hydrogen project about 2 hours ago
and the chats TUI before that. You've got uncommitted changes on main.
What are we doing?
```

**Not:**
```
Welcome back! Here's a summary of your recent activity:
- Session 1: ...
- Session 2: ...
How can I help you today?
```

**Implementation:** A Python script that runs on SessionStart, reads recent session files + git state + memories, outputs a structured context string. The Persona instructions tell me how to use it naturally — no bullet-point dumps.

**Known issue:** There's a bug (GitHub #10373) where SessionStart hook output silently fails on brand-new conversations. Works on resume, /clear, and /compact. Fallback: Persona.md instructions tell me to check recent sessions on first message on first message if no context was injected.

---

## 2. Daily Briefing (`/morning`)

**What it does:** One command gives you a full picture of your day. Not a wall of text — a concise, opinionated briefing.

**Data sources (in order):**
1. **Recent sessions** (last 24h) — what you worked on, what's unfinished
2. **Memories** — upcoming deadlines, active projects, personal dates (proposal planning, gym goals)
3. **Git activity** — across all project repos, uncommitted work, recent commits
4. **Google Calendar** (if MCP available) — today's schedule
5. **Knowledge base** — any recently modified topics (research you did yesterday)

**Format:**
```
Wednesday, April 8.

You worked on the chats TUI yesterday (token usage, mobile web UI).
The hydrogen project has uncommitted changes on the feature branch.
Gym goal: 185 lbs by Nov 21 — that's 227 days out.
No calendar events today.

Open threads:
- Syncthing isn't syncing .jsonl files to your phone
- Konpeki dashboard still has the outreach page type mismatches
```

**Implementation:** A Claude Code slash command (`~/.claude/commands/morning.md`) that instructs me to gather and present this info. Not a hook — it's on-demand because you won't always want it. The command file just says what to gather and how to present it; I do the actual work using available tools.

---

## 3. Session Wrap-up

**What it does:** Before a long chat ends, I proactively summarizes what was accomplished and what's still open. Not when you ask — I notice the session is wrapping up.

**Triggers (instructed via Persona.md):**
- You say something that sounds like you're leaving ("alright", "that's it for now", "gotta go", "thanks")
- The conversation has been going 20+ messages and hits a natural stopping point
- You explicitly ask for a summary

**What gets summarized:**
- Decisions made (not every detail — just what matters for next time)
- Files changed / features built
- Open items / things left unfinished
- Anything worth saving as a memory (auto-captured per the auto-capture rules)

**What it looks like:**
```
We built the knowledge integration today — 43 topics indexed, unified
search working across chats + knowledge + memories. Cross-linking is
in but you haven't tested it yet. Phone sync still broken — Syncthing
isn't copying .jsonl files over. Pick that up next time.
```

**Implementation:** Pure Persona.md instructions. No hook needed — I watch for wrap-up signals and proactively summarize. I also run `chats memory add` for any project-level decisions made during the session.

---

## 4. Proactive Date/Deadline Awareness

**What it does:** I surface important dates when they're relevant — not as a daily reminder dump, but naturally in context.

**Dates tracked (from memories):**
- Proposal to Kamakshi (ongoing planning)
- Gym goal: 185 lbs by Nov 21, 2026
- Konpeki launch timeline
- Any project deadlines saved via memories

**How it surfaces:**
- **SessionStart script** checks memories for dates within 14 days, injects them into context
- **During conversation** — if you're discussing a related project, I mention the deadline naturally ("That's due in 3 weeks, by the way")
- **Morning briefing** includes a deadlines section

**Storage:** Dates live in memories with type `project`. The SessionStart script parses them looking for date patterns (YYYY-MM-DD, "by November", etc.) and calculates proximity.

**Implementation:** Part of the SessionStart hook script (same one as #1). Parses memory content for date patterns, calculates days-until, includes anything within 14 days in the injected context. Persona.md tells me to mention these naturally, not as a checklist.

---

## 5. Cross-Session Continuity

**What it does:** Every new session starts with awareness of what happened in the last 1-3 sessions. Not a full transcript — a one-line summary per session so I can reference past work naturally.

**What gets injected:**
- Last 3 sessions: auto-generated title, project, time ago, duration estimate
- If resuming a specific session: what the last topic of conversation was
- Any unfinished items from the wrap-up (if #3 captured them)

**Example context I receive:**
```
Recent sessions:
- "Chats TUI — knowledge integration" (~/Documents/Projects/chats) — 3 hours ago
- "Hydrogen feedback form validation" (~/Documents/Projects/frameworth-hydrogen) — yesterday
- "Google Ads MCP server" (~/Documents/Projects/AdSorceryWebApp) — 2 days ago
```

**How I use it:**
I might say "Want to keep going on the knowledge stuff from earlier?" or just naturally reference it when relevant.

**Implementation:** Part of the same SessionStart hook script. Scans the 3 most recently modified `.jsonl` files, extracts the title (from the index DB or first message), project, and timestamp. Cheap operation — just a DB query, no file parsing needed at session start.

---

## Implementation Plan

All 5 share one SessionStart hook script (Python, not bash — needs DB access and date math). Plus:
- Persona.md updates for #3 (wrap-up behavior)
- One slash command file for #2 (`/morning`)

### Files to create/modify:
1. `chats/session_context.py` — SessionStart hook script (covers #1, #4, #5)
2. `~/.claude/commands/morning.md` — Daily briefing slash command (#2)
3. `Persona.md` — Add wrap-up behavior instructions (#3)
4. `~/.claude/settings.json` — Update SessionStart hook to use the new script
