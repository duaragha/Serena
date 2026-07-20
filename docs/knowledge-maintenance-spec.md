# Knowledge Base Maintenance — Spec

## Problem

research gets added constantly but nothing cleans it up. topics overlap, content goes stale, INDEX.md gets out of sync, and files accumulate without anyone checking if they're still relevant. right now it's 43 topics and 258 files — manageable but growing fast.

## Solution

a weekly scheduled agent that reads the entire knowledge base, audits it, fixes what it can, and reports what it can't.

## What the Agent Does

### 1. Overlap Detection
- reads every topic's README.md and scans file names across all folders
- flags topics that cover the same domain (e.g., `typescript-2026/`, `typescript-clean-code/`, `typescript-tooling-2026/` — should these merge?)
- checks for files in different topics that cover the same subtopic
- outputs a list of suggested merges, doesn't force them — some overlap is intentional

### 2. Stale Content Detection
- checks INDEX.md research dates against current date
- anything older than 90 days gets flagged as potentially stale
- tech topics (frameworks, libraries, APIs) get a shorter threshold — 60 days
- personal topics (restaurants, workout) get longer — 180 days
- adds `last_verified: YYYY-MM-DD` to files it reviews so future runs skip recently checked content

### 3. INDEX.md Sync
- compares folders on disk vs entries in INDEX.md
- flags folders that exist but aren't in INDEX.md (orphans)
- flags INDEX.md entries that point to folders that don't exist (dead links)
- auto-fixes dead links by removing them
- lists orphans in the report for manual review

### 4. Empty/Tiny Files
- finds .md files under 100 bytes — probably stubs or abandoned
- finds topics with only a README.md and no other files — might be incomplete
- flags these in the report

### 5. Cross-Reference Check
- looks for topics that reference each other's content but aren't linked
- suggests cross-links between related topics

### 6. Formatting Consistency
- checks that every topic folder has a README.md
- checks that README.md has a `# Title` and file index
- checks for consistent heading structure across files
- fixes minor formatting issues (trailing whitespace, double blank lines, missing newline at EOF)

## Report Format

the agent writes a report to `~/Documents/Projects/knowledge/MAINTENANCE_REPORT.md`:

```
# Knowledge Base Maintenance Report
Run: 2026-04-15

## Overlap Detected
- typescript-2026/ and typescript-clean-code/ have significant overlap in patterns content
- google-ads/ and meta-ads/ both cover ad platform APIs — consider a shared "paid-ads/" topic

## Stale Content (>90 days)
- react-19/ — last research date 2025-12-15 (115 days ago)
- hydrogen-2026/ — no research date found

## INDEX.md Issues
- ORPHAN: ai-knowledge-systems/ exists on disk but not in INDEX.md (added)
- DEAD: removed entry for "deleted-topic/" (folder doesn't exist)

## Tiny/Incomplete
- phone-alerting/README.md is only 85 bytes
- voice-dictation/ has only README.md, no subtopic files

## Formatting Fixed
- Added missing newline at EOF in 3 files
- Fixed double blank lines in supabase/auth-patterns.md

## Stats
- 43 topics, 258 files, 2.2MB total
- 4 stale topics flagged
- 2 overlap groups detected
- 1 orphan added to INDEX.md
- 1 dead link removed
```

## Implementation

### Option A: `/schedule` (preferred)
- runs on anthropic's servers, laptop doesn't need to be on
- weekly cron: `0 9 * * 1` (monday 9am)
- the prompt file lives at `~/Documents/Projects/knowledge/.claude/maintenance-prompt.md`
- full access to filesystem so it can read/write knowledge files directly

### Option B: local cron + `claude -p`
- fallback if `/schedule` doesn't work or isn't available
- `0 9 * * 1 cd ~/Documents/Projects/knowledge && claude -p "$(cat .claude/maintenance-prompt.md)" --dangerously-skip-permissions --max-budget-usd 1`
- needs laptop on at that time

### The Prompt File

a markdown file that gives the agent clear instructions:
- what to check (all 6 items above)
- where the knowledge base lives
- how to write the report
- what it can auto-fix vs what it should only flag
- to read Persona.md so it writes the report in my voice

### Auto-Fix vs Flag Only

**auto-fix:**
- dead links in INDEX.md
- orphan folders (add to INDEX.md)
- formatting issues (whitespace, newlines)
- `last_verified` date stamps

**flag only (don't auto-fix):**
- topic merges (needs human judgment)
- stale content (might still be relevant)
- tiny files (might be intentionally brief)
- cross-reference suggestions

## Files to Create
1. `~/Documents/Projects/knowledge/.claude/maintenance-prompt.md` — the agent's instructions
2. update `~/.claude/settings.json` or use `/schedule` to register the weekly trigger
