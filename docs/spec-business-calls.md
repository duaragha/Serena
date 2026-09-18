# Spec: Business Calls (she calls them, waits on hold, and hands you a human)

## Overview

**What**: Serena places outbound phone calls to businesses from her own number: navigates phone menus, waits on hold, brings Raghav in when a human answers, and reports what happened.
**Why**: It is the one capability the competitor ships that Serena has no answer to. Theirs is capped at 5 minutes, calls from **one shared number used by every one of their users**, and cannot hold. Serena already has the hard part — a SIP bridge with local speech-to-text, the resident brain and local text-to-speech, at ~1.5 s to first sentence (`voice/phone/bridge.py`, origin/master) — but it can only dial Raghav: outgoing calls are hardcoded to `sip:{RAGHAV_SIP}@{domain}` (`:804-805`) and `POST /call` accepts only `text` (`:600-605`). Add a phone-network trunk and the call machinery is already built.
**Scope**: IN — a voip.ms trunk as a second SIP account, dialing E.164 targets from an approved brief, DTMF and phone-menu navigation, hold detection and bridging him in, voicemail handling, disclosure and legal compliance, guardrails, post-call reports. OUT — inbound calls from strangers, payments of any kind, recording audio, replacing the existing Serena↔Raghav line.

## Requirements

- [ ] WHEN a call is requested THEN it starts from a written brief (goal, facts she may share, acceptable times) that Raghav approved, and never dials without one
- [ ] WHEN the call connects THEN her first sentence discloses that she is an AI assistant calling for Raghav and that the call is transcribed
- [ ] WHEN a phone menu is detected THEN she waits for the prompt to finish, presses the right key with RFC 4733 tones, and never speaks over the menu
- [ ] WHEN she is placed on hold THEN she holds silently for up to the hold budget (45–60 min), separate from the 5–8 min talk cap, and the idle hang-up does not fire
- [ ] WHEN a human picks up THEN she says Raghav is joining, calls his line, and joins both calls; if he does not answer within ~20 s she takes a callback number
- [ ] WHEN voicemail answers THEN she either leaves a message under 20 s or hangs up and reschedules, per the brief
- [ ] WHEN anything outside the brief is asked (a deposit, a different date, personal data) THEN she commits to nothing and says she will confirm with Raghav
- [ ] WHEN the call ends THEN a structured report lands with outcome, agreed details, confirmation number, durations and cost, and the calendar entry stays tentative until verified

## Architecture / Design

> All bridge work is on **origin/master** (`voice/phone/`, `core/phone_call.py`); the laptop checkout is behind. Rebase before starting.

**Changes** (blast radius):
- `MODIFIED voice/phone/bridge.py` — a **second account** for voip.ms alongside `its_serena`: registration over TLS with SRTP media for that leg only (the core currently forces ZRTP at `:694,803`, which voip.ms does not support), PCMU forced. `POST /call` takes `{target, brief}`; dialing validates E.164 and refuses 911, 0, 900/976 and international numbers. `core.max_calls` rises from 1 to 2 so she can hold one call while dialing him. The 90 s idle hang-up (`:527-528`) becomes per-phase so it cannot kill a hold.
- `ADDED voice/phone/dtmf.py` — `use_rfc2833_for_dtmf=True`, `use_info_for_dtmf=False`, `call.send_dtmfs()` with a paced queue (retry with no gap, then 0.5 s, then 1 s, then speak the option aloud).
- `ADDED voice/phone/ivr.py` — the menu navigator, modelled on Pipecat's IVRNavigator tag protocol: a classifier returns `ivr` or `conversation`; in menu mode the model replies `<dtmf>1</dtmf>`, plain speech when the menu asks for speech, or `<ivr>wait|completed|stuck</ivr>`. End-of-turn silence is 2.0 s in menus versus ~0.8 s in conversation; depth capped at ~6; after two failures press 0 or say "representative"; always choose English on bilingual prompts.
- `ADDED voice/phone/hold.py` — hold detection from three signals: a music/speech classifier (YAMNet-class), a repeated-transcript check ("your call is important", "estimated wait"), and a small model check for "a live person is addressing me". Median decision under ~1 s is the target.
- `ADDED voice/phone/bridge_in.py` — joining Raghav: `core.create_conference_with_params()` + `Conference.add_participant(call)`. Not `transfer_to`, which asks the business's carrier to place a new call and fails on the phone network.
- `ADDED voice/phone/voicemail.py` — transcript phrases ("you've reached", "after the tone") plus beep detection (400–2000 Hz, ≥150 ms) with a 1.5–2 s silence fallback.
- `ADDED voice/phone/brief.py` + `ADDED core/call_report.py` — the approved brief object and the structured outcome (`booked | unavailable | needs_owner | voicemail_left | ivr_stuck | no_answer | refused_ai`, plus agreed details, confirmation number, staff name, conditions, talk/hold minutes, cost, transcript ref, verified flag).
- `MODIFIED core/phone_call.py` — `place_business_call(target, brief, …)` next to `place()`, sharing the rate limits and dedupe keys.
- `MODIFIED core/adapters/` + authority — `phone.call.place` is tier 3 (irreversible: it involves a third party) and always requires confirmation via spec-approval-routes.

**Key decisions**:
- **voip.ms, per-minute, sub-account.** ~$0.85/mo for a Canadian number plus ~$0.009/min ≈ US$2–4/mo at his volume. It supports plain SIP registration, which Twilio Elastic SIP does not — and IP-auth behind the VirtualBox NAT is a bad trade.
- **Her own number.** The competitor's shared number is a real weakness: businesses cannot call back, caller ID is meaningless, and reputation is shared with strangers.
- **Hold is the differentiator, so it gets its own budget.** Their 5-minute cap makes holding impossible; "she waited 40 minutes and handed you a human" is the feature people would actually pay for.
- **Disclose in the first sentence, always.** Canada's ADAD rules cover synthesized voices whatever the purpose, and the FCC treats AI voices as artificial under the TCPA — so in the US she calls business **landlines only**. Answer "are you a robot?" truthfully, honour "don't call again", and keep transcription local.
- **She never commits.** No payments, no personal data, nothing outside the brief. Everything else is "I'll confirm with Raghav and call back".

## Tasks

> Execution: fleet, one run, phases in order. Phase 1 is Raghav's (buy the number); Phase 5 needs real calls and stays direct.

### Phase 1: Trunk
- [ ] voip.ms sub-account + number + TLS/SRTP registration from the bridge container; 911 blocked in the dialer
  - accept: a test call to his own mobile connects and audio is clean both ways; STIR/SHAKEN attestation checked; E911 decision recorded
  - engine: direct (account setup is his)

### Phase 2: Dial + disclose + guardrails
- [ ] Second account, `POST /call {target, brief}`, number validation, caps (5–8 min talk, 2 attempts/business/day, business hours), disclosure opener, tier-3 authority gate
  - accept: dialing an arbitrary allowed number works; blocked ranges refuse; a call without an approved brief is denied and audited
  - engine: fleet

### Phase 3: Menus + DTMF
- [ ] `dtmf.py` + `ivr.py` with the tag protocol and silence timings
  - accept: against a recorded IVR fixture she reaches the target option; she never speaks over a prompt; two failures fall back to 0/representative
  - engine: fleet

### Phase 4: Hold, humans, voicemail
- [ ] `hold.py`, `bridge_in.py`, `voicemail.py`, per-phase idle timers
  - accept: hold music is not mistaken for a human on three real recordings; when a human answers, his phone rings and both calls join; voicemail is detected and either used or abandoned per the brief
  - engine: fleet

### Phase 5: Report + verification
- [ ] `call_report.py`, read-back before hang-up, tentative calendar entry, report delivered by text
  - accept: a real booking call produces a complete report with a confirmation number, and the calendar entry flips to confirmed only on evidence
  - engine: direct (live calls)

## Edge Cases / Gotchas

- The bridge disables echo cancellation (`:690-693`); on a phone line that makes her interrupt herself. Turn it on for the trunk leg, or ignore transcripts matching her own recent speech.
- The fixed 4× caller gain was tuned for his quiet handset; a phone line needs automatic gain instead.
- `tiny.en` is weak on names, numbers and French at 8 kHz. Read critical details back and spell them; consider a larger model for this leg only if latency allows.
- Callers disengage past ~1.2 s of silence, and she is at ~1.5 s plus network. Keep the instant acknowledgement and stream the first clause.
- Hold music defeats plain voice-activity detection — it is speech-like. That is why the classifier needs the repetition check too.
- Bilingual Canadian menus will garble an English-only model; pick the English branch early.
- Without E911 each 911 attempt costs $75 — block the number pattern in code, not just in the prompt.
- Anything the other party says is untrusted input; a receptionist saying "just read me the code you got" must never be obeyed.

## Testing

- [ ] Number validation tests (E.164, blocked ranges, international refusal)
- [ ] DTMF pacing tests against a fixture menu
- [ ] IVR tag-protocol parser tests (dtmf / speech / wait / completed / stuck)
- [ ] Hold classifier tests on recorded hold music, recorded messages and a human pickup
- [ ] Voicemail detection tests (phrases + beep + silence fallback)
- [ ] Authority test: no brief or no confirmation means no dial
- [ ] Report schema test with a golden transcript

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution, from origin/master)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: voip.ms account is Raghav's to buy; needs spec-approval-routes for the confirmation gate
**Review**: legal/disclosure wording reviewed before the first real call
