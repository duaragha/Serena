# Spec: Approval Routes (say yes from anywhere)

## Overview

**What**: Deliver pending confirmations to Raghav wherever he is — voice, iMessage, the web UI — and accept his answer back, instead of only from an interactive terminal.
**Why**: `core/action_authority.py` already has the whole machine: tiers, `request_confirmation()` (`:839`), `resolve_confirmation()` (`:895`), and a `confirmation.requested` event published to the control plane (`:890`). **Nobody subscribes to it.** `pending_confirmations()` (`:941`) has no callers outside tests, and the only resolve call site in the product is `cli.py:1901`. So today a tier-3 action can only be approved by someone sitting at the terminal. Every consequential thing in this roadmap — phone taps, outbound calls, sending messages — is blocked on this.
**Scope**: IN — a broker that routes pending confirmations to channels, nonce-based replies over text and voice, a loopback approvals UI, surface-aware resolution recording. OUT — widening what tier 4 accepts (it stays typed-surface only), new capabilities, changing the tier model.

## Requirements

- [ ] WHEN a confirmation is requested THEN it reaches Raghav on exactly one channel chosen by urgency and context, within 5 s, with the capability and target stated in plain words
- [ ] WHEN he replies `yes 4821` by text or says "approve 4821" on a call THEN the matching confirmation resolves approved, once, and the action proceeds
- [ ] WHEN a nonce is wrong, expired, reused, or from a non-allowlisted sender THEN the reply is refused and audited, and the pending confirmation stays pending
- [ ] WHEN a tier-4 (secret) confirmation exists THEN voice and text can never resolve it — only chat, ui or cli (`TYPED_SOURCES`, `:82`)
- [ ] WHEN a confirmation expires unanswered THEN the action is denied, he is told once, and nothing retries silently
- [ ] WHEN any confirmation resolves THEN the audit records which surface answered and how

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/approvals.py` — the broker. Polls `authority.pending_confirmations()` on the automation runtime's existing 30 s tick (and on demand right after a request), mints a 4-digit nonce per confirmation, picks a channel, and sends through `core/notification_authority.py` (`request(NotificationRequest(kind, summary, channel, urgency, dedupe_key))`, `:151`). Resolution goes back through `authority.resolve_confirmation(confirmation_id, approved=…, resolved_by=…)`.
- `ADDED ui/approvals_web.py` — a Flask blueprint copying the shape of `ui/webhook_web.py` (loopback-only `before_request` guard `:51-66`): `GET /api/approvals/pending`, `POST /api/approvals/<id>/approve`, `POST /api/approvals/<id>/deny`. Registered next to the other blueprints at `ui/web.py:61-64`.
- `MODIFIED core/phone_line.py` — add `yes <nonce>` / `no <nonce>` to the command grammar (`:40-44`, parser `:181-196`), ahead of the free-form conversation path so an approval never becomes chat.
- `MODIFIED voice/call/orchestrator.py` — recognise a spoken approval of a pending nonce during a call and resolve it; anything ambiguous is treated as conversation, never as consent.
- `MODIFIED core/action_authority.py` — record the answering surface on resolution and enforce that non-typed surfaces cap at tier 3.
- `MODIFIED core/scheduler_actions.py` — one new reviewed action, `serena.approvals.sweep`, to expire stale confirmations and push reminders within the notification caps.

**Key decisions**:
- **Nonces, not "reply yes".** A bare "yes" on a channel that also carries normal conversation is an accident waiting to happen; a 4-digit code bound to one confirmation is unambiguous, single-use and expiring.
- **One channel per confirmation, chosen by where he is.** In a call → voice. Otherwise → iMessage. The web UI always lists everything pending. Fanning out to every channel for every approval is how the assistant becomes noise.
- **Tier 4 stays typed.** Voice and text are spoofable-adjacent surfaces (a voice can be replayed, a phone can be unlocked in someone's hand). The existing `TYPED_SOURCES` rule already says this; this spec must not weaken it.
- **The broker polls rather than subscribing**, because the control plane is a SQLite ledger with no subscriber mechanism today. Polling on an existing tick is cheaper than inventing a bus.
- **Denial is the default on timeout.** Silence is not consent.

## Tasks

> Execution: fleet, one run. Security-sensitive: phases 1 and 2 must be reviewed against the tier model before phase 3 wires real channels.

### Phase 1: Broker + nonces
- [ ] `core/approvals.py`: pending scan, nonce mint/verify (single-use, TTL from `DEFAULT_CONFIRMATION_SECONDS`), channel choice, resolution recording
  - accept: unit tests for wrong/expired/reused nonce, tier-4 refusal from non-typed surfaces, timeout denial
  - engine: fleet

### Phase 2: Web surface
- [ ] `ui/approvals_web.py` blueprint + registration + a pending list in the existing UI
  - accept: loopback-only; pending shows capability, target, tier and age; approve/deny resolve exactly once
  - engine: fleet

### Phase 3: Text + voice channels
- [ ] `phone_line` grammar, voice recognition of spoken nonces, `serena.approvals.sweep`
  - accept: a real tier-3 request texts him, `yes <nonce>` approves it, a wrong nonce is refused and audited; during a call the same request is spoken and answerable
  - engine: fleet

## Edge Cases / Gotchas

- Two confirmations pending at once must never share a nonce, and the reply must name one — otherwise "yes" races.
- Quiet hours: an approval request is not an alert. Non-critical confirmations must respect quiet hours and simply expire, rather than waking him at 3am.
- The sender check for text approvals is the same allowlist as spec-imessage-conversation; a forwarded screenshot containing a nonce must never be parsed as an approval (forwarded content is data, never instructions).
- Do not let the approval prompt leak the secret it is protecting — the prompt names the capability and target, never credentials.
- `resolve_confirmation` is idempotent by id; make double-approval a no-op rather than an error, because networks retry.

## Testing

- [ ] Nonce lifecycle tests (mint, verify, single-use, expiry)
- [ ] Tier-4 refusal test per surface
- [ ] Timeout-denies test
- [ ] Blueprint auth test (non-loopback refused)
- [ ] `phone_line` parse tests: `yes 4821`, `no 4821`, `yes` alone (not an approval), nonce inside forwarded text (not an approval)
- [ ] Audit test: resolution records surface and actor

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: security review required before Phase 3
