# Spec: iMessage Conversation (text her like a person)

## Overview

**What**: Turn the iMessage line from a command parser into a real conversation with the resident brain, with instant delivery, typing indicators, images and voice notes.
**Why**: This is the cheapest high-value item in the whole roadmap — the pipe, the brain and the memory all exist and are already wired; one skip statement throws the value away. `core/phone_line.py` parses five commands (`task:`, `#id answer`, `retry #id`, `swapped`, `status`) and **drops everything else at `:382-392`**, after advancing the watermark. The brain is never called from this path. The competitor's equivalent (SOL) keeps 16 messages for 14 days and a 20-fact profile; Serena's brain has hybrid memory retrieval and 130 knowledge topics, so a conversational line is instantly better than theirs.
**Scope**: IN — routing free-form texts to the brain, webhook delivery, conversation shaping (debounce, bubbles, typing, read receipts, tapbacks), inbound images and voice notes, sender security. OUT — approvals (spec-approval-routes), proactive check-ins (spec-proactive-policy), Serena's own Apple ID (blocked by Apple, task 1038 — this runs on the hub self-thread until then).

## Requirements

- [x] WHEN a text is not a command THEN it goes to the resident brain with memory context and gets a real answer, instead of being dropped
- [x] WHEN a text is a command THEN it behaves exactly as today — commands are the fast path and must not regress
- [x] WHEN a message arrives THEN it is handled within ~3 s via webhook, with the 60 s poll kept only as a fallback
- [x] WHEN he sends three bubbles in a row THEN they are answered as one turn, not three
- [x] WHEN she is composing THEN the typing indicator is live and refreshed, and the reply arrives in at most 3 bubbles
- [x] WHEN he texts a photo or a voice note THEN the photo reaches the brain as an image and the voice note is transcribed
- [x] WHEN a message is not from his allowlisted handle over iMessage THEN it is ignored, and forwarded content is treated as data, never instructions

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/text_conversation.py` — the turn builder. Debounce window (2–4 s) to merge bursts, last ~15 messages as context, `protocol="imessage"`, and the brain call over `brain.sock` using the NDJSON client pattern in `core/frontdoor.py:591-615` (`{"type":"turn","request_id":…,"protocol":…,"text":…,"memory_query":…,"stream":True}`). Splits the reply into ≤3 bubbles on sentence boundaries.
- `MODIFIED core/phone_line.py` — at the non-command skip (`:382-392`), hand the message to `text_conversation` instead of dropping it. Command parsing stays first; the watermark and duplicate-fingerprint logic stay exactly as they are.
- `MODIFIED core/webhook_ingress.py` — register a `bluebubbles` route in `default_ingress()` (`:751-770`) with a validator, so BlueBubbles pushes messages instead of us polling. The HTTP surface already exists (`POST /webhooks/<name>`, `ui/webhook_web.py:68`) with HMAC verification and replay protection.
- `MODIFIED core/bluebubbles_line.py` — add `typing(chat_guid, on)` (private API `POST|DELETE /chat/:guid/typing`), `mark_read(chat_guid)`, `react(message_guid, reaction)`, and inbound handling for `kind != "text"` (today everything non-text is marked `other` at `:196`): images → brain images, audio → local whisper.
- `MODIFIED core/scheduler_actions.py` — `serena.phone.poll` stays as the fallback sweep; it must no-op cleanly when the webhook already handled a message.

**Key decisions**:
- **Commands first, conversation second.** The command grammar is how work gets queued from his phone; conversation must never swallow `task:` or an approval nonce.
- **Webhooks for latency, polling for truth.** A dropped webhook must never mean a lost message, so the 60 s sweep stays as the safety net, deduplicating by message GUID — BlueBubbles fires both `new-message` and `updated-message` for the same message, which doubles replies if you trust one.
- **Reply like a person: short, few bubbles, typing shown.** The typing indicator expires after a few seconds, so refresh it every 2–3 s while the brain is working. A read receipt on arrival is the cheapest "I've got it" there is.
- **The brain, not a separate agent.** This path must use the resident brain so that texting her has the same memory as talking to her. The mobile `/ws/chat` path (`core/chat_daemon.py:472-504`) deliberately spawns per-session CLI agents for coding; that is a different job and stays as it is.
- **Private API features degrade gracefully.** Typing, reactions and read receipts need the BlueBubbles Private API; if it is unavailable the conversation still works, just plainer.

## Tasks

> Execution: fleet, one run. This is the first thing built after the specs land — highest value per hour in the roadmap.

### Phase 1: Conversation core
- [x] `core/text_conversation.py` + the `phone_line` hand-off + bubble splitting
  - accept: a free-form text gets a brain answer with memory context; commands unchanged (existing `tests/test_phone_line.py` stays green); bursts merge into one turn
  - engine: fleet

### Phase 2: Instant delivery
- [x] BlueBubbles webhook route + GUID dedupe + poll fallback no-op
  - accept: message-to-reply under 3 s; a replayed webhook produces no second reply; with the webhook disabled the poll still answers
  - engine: fleet

### Phase 3: Feel
- [x] Typing keep-alive, read receipts, tapback on commands, inbound images and voice notes
  - accept: typing shows within 1 s and persists while composing; a texted photo is answered about its content; a voice note is transcribed and answered
  - engine: fleet

## Edge Cases / Gotchas

- Match on the **service field**, not the chat GUID prefix: modern macOS may store `any;-;` for every chat, so prefix-matching silently accepts SMS, which is spoofable.
- With Text Message Forwarding on, SMS arrives through the same Mac — another reason the service check matters.
- Group chats: ignore, or require an explicit mention from the allowlisted handle.
- The BlueBubbles password rides in the query string, so it lands in logs; keep the server tailnet-only and scrub URLs before logging (see spec-device-stack-hygiene).
- Long replies: the send path caps at 4,000 characters (`bluebubbles_line.py:35`). Bubble splitting must respect that, and a wall of text is the wrong answer to a text message anyway.
- Message edits are broken on macOS 26 (Tahoe) in BlueBubbles; don't build on edit.
- Her own Apple ID is still blocked by Apple (IDS 6001, task 1038), so this runs on his self-thread. The design must not assume a separate identity, but must not fight it later either.

## Testing

- [x] Command-regression suite (all five commands still parse and act)
- [x] Non-command routes to brain (with a faked brain socket) and returns ≤3 bubbles
- [x] Debounce test: three rapid messages produce one turn
- [x] Dedupe test: `new-message` + `updated-message` for one GUID replies once
- [x] Security tests: SMS service refused, non-allowlisted handle ignored, forwarded text with an imperative is not obeyed
- [x] Attachment tests: image reaches the brain as an image; audio path calls whisper

---

## Progress Log

**Status**: Complete (2026-09-18)
**Branch**: `serena/fleet/115b59e9-a603-481e-be11-e99652a32b18/agent-a`
**Current phase**: Done — all 3 phases
**Last completed task**: Phase 3 feel (typing keep-alive, read receipts, tapback, images, voice notes)
**Files modified**: `core/text_conversation.py` (added), `core/phone_line.py`, `core/bluebubbles_line.py`, `core/webhook_ingress.py`, `ui/webhook_web.py`, `tests/test_text_conversation.py` (added), `tests/test_bluebubbles_webhook.py` (added), `tests/test_phone_line.py`, `tests/test_webhook_tasks.py`
**Blockers**: none
**Review**: Fix leg 2026-09-18 closed 6 review findings on this spec: webhook debounce for chat bursts (commands stay fast), attachment-GUID dedupe fingerprints, poll-side sender gate matching the webhook, honest reply on unreadable attachments, ellipsis-marked bubble truncation. 178 focused tests green.
**Notes**: (1) BlueBubbles webhooks carry no HMAC, so the route authenticates with a URL token via a new per-route `auth` hook; the token must be configured at `SERENA_BLUEBUBBLES_WEBHOOK_TOKEN` and the server kept tailnet-only. (2) The webhook handler triggers `phone_line.poll()` rather than duplicating its logic; GUID claim prevents the `new-message` + `updated-message` double-fire. (3) `serena.phone.poll` needed no change — the watermark + fingerprint logic already no-ops after a webhook-handled message. (4) Voice notes need a staged faster-whisper model (`SERENA_VOICE_NOTE_MODEL`, else `SERENA_CALL_WHISPER_MODEL`); without one the turn falls back honestly.
