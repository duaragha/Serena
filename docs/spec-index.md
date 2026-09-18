# Spec Index

Two programs. **Devin-parity** (specs 1-17) is the engineering-quality track.
**Device + presence** (specs 18-28) came out of the ConscioussAI teardown on
2026-09-17 — see `knowledge/ai-desktop-assistant/conscioussai-gap-roadmap.md`
for the research and the reasoning behind each.

Specs are the design record; execution is batched by track per SDD best
practice (batch related changes, size ceremony to the work) — not 1 run per
spec.

## Status

| # | Spec | Track | Status |
|---|---|---|---|
| 1 | `spec-run-reports.md` | — (done) | BUILT, verified, in prod |
| 2 | `spec-plan-mode.md` | direct (Serena) | BUILT, verified live, pending Raghav review |
| 3 | `spec-code-index.md` | — (done) | BUILT, verified 19/19 |
| 4 | `spec-proof-artifacts.md` | 1 verify | BUILT, verified 49-suite |
| 5 | `spec-review-upgrade.md` | 1 verify | BUILT, verified 49-suite |
| 6 | `spec-repo-briefs.md` | 2 knowledge | BUILT, verified 107-suite |
| 7 | `spec-knowledge-triggers.md` | 2 knowledge | BUILT, verified 107-suite |
| 8 | `spec-skills-standard.md` | 2 knowledge | BUILT, verified 107-suite |
| 9 | `spec-browser-profiles.md` | 3 browser | BUILT direct, 10/10, pending review |
| 10 | `spec-scripted-browser.md` | 3 browser | spec'd |
| 11 | `spec-workflow-scripts.md` | 1 verify | BUILT (fleet, revised), verified |
| 12 | `spec-automation-templates.md` | 4 platform | spec'd |
| 13 | `spec-fast-lane-routing.md` | 4 platform | spec'd, engine direct |
| 14 | `spec-observability.md` | 4 platform | spec'd |
| 15 | `spec-behavioral-evals.md` | 4 platform | spec'd, engine direct |
| 16 | `spec-api-tokens.md` | 4 platform | spec'd, engine direct |
| 17 | `spec-hygiene.md` | last, alone | spec'd, engine direct |
| — | `spec-fleet-improvements.md` | parked | spec'd, left alone per order |
| 18 | `spec-phone-mirror.md` | 5 phone | spec'd |
| 19 | `spec-vision-turns.md` | 5 phone | spec'd |
| 20 | `spec-phone-control.md` | 5 phone | spec'd |
| 21 | `spec-approval-routes.md` | 6 channels | spec'd |
| 22 | `spec-imessage-conversation.md` | 6 channels | spec'd |
| 23 | `spec-business-calls.md` | 6 channels | spec'd |
| 24 | `spec-ambient-sensing.md` | 7 ambient | spec'd |
| 25 | `spec-proactive-policy.md` | 7 ambient | spec'd |
| 26 | `spec-voice-control.md` | 8 voice+auto | spec'd, mostly direct |
| 27 | `spec-user-automations.md` | 8 voice+auto | spec'd |
| 28 | `spec-device-stack-hygiene.md` | last, alone | spec'd, engine direct |

## Execution order

1. Track 1 verify (fleet, one run, specs 4+5; spec 11 direct alongside — deep DAG surgery)
2. Track 2 knowledge (fleet, one run, specs 6+7+8)
3. Track 3 browser (spec 10 fleet; spec 9 direct alongside — security-sensitive)
4. Track 4 platform (specs 12+14 fleet; specs 13+15+16 direct alongside)
5. Hygiene (spec 17, direct, alone, no active runs) + commit split

### Device + presence (specs 18-28)

Raghav's order, highest value per hour first:

6. Track 6 channels, part 1 — spec 22 (texting) alone, then spec 21 (approvals).
   22 is the cheapest win in the program; 21 unblocks everything consequential.
7. Track 5 phone — spec 18, then 19, then 20. **18 Phase 1 is a measurement
   gate on the real phone** (USB + `usbmuxd`); nothing else in the track starts
   until the fps and latency numbers are in its Progress Log.
8. Track 6 channels, part 2 — spec 23 (business calls), after Raghav buys the
   voip.ms number. Based on origin/master.
9. Track 7 ambient — spec 24, then 25 (25 needs 24's breakpoints).
10. Track 8 — specs 26 and 27 (26's wake-word phases are his room, his voice).
11. Device stack hygiene (spec 28, direct, alone, no active runs).

Prerequisites outside the specs: task 1043 (the PC brain daemon still runs the
July snapshot and answers every phone call), and rebasing `laptop-master` onto
`origin/master` before any call work.

Dropped, deliberately: an iOS app that captures its own screen. Serena is a
LiveContainer guest, all three free app slots are taken, and he moves to
Android in Nov-Dec 2026 — so the phone track is laptop-side via
pymobiledevice3, and the anywhere version gets built on Android later.

Rules: one fleet run at a time (shared tree); direct work runs alongside but
touches `cli.py` only when no integration is mid-flight; every track verified
(suite + spec acceptance) before the next fires; `serena-fleet.service`
restarted after any `fleet/*` change lands.
