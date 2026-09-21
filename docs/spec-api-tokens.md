# Spec: Versioned Token API (v1)

## Overview

**What**: A versioned `/api/v1/*` surface with per-token scoped Bearer [REDACTED] replacing today's single shared secret + unauthenticated local endpoints.
**Why**: Today's auth is one shared chat token, an ephemeral brain token, per-artifact tokens, HMAC webhooks, and some local-only endpoints with nothing — no versioning, no scopes, no expiry, no revocation. Anything scriptable (shortcuts, phone automations, external tools) currently shares the crown-jewel token or nothing at all.
**Scope**: IN — token store + scoped Bearer [REDACTED] CLI issuance/revocation, `/api/v1` version prefix on covered routes. OUT — migrating existing WS paths (untouched); OAuth; per-user identities (single-user system).

## Requirements

- [ ] WHEN a token is issued THEN it carries (`id`, scopes, created_at) with only `sha256(secret)` stored
- [ ] WHEN `/api/v1/*` is called THEN `Authorization: Bearer <id>.<secret>` is verified with constant-time compare and scope-checked per route
- [ ] WHEN a token is revoked THEN its calls fail immediately (no grace cache beyond the request)
- [ ] WHEN the chat token is used THEN v1 routes reject it (separate credential domains — compromise of one is not compromise of the other)
- [ ] WHEN an unscoped caller hits a scoped route THEN it gets 403 with the required scope named (no silent behavior differences)

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/api_tokens.py` — store (`~/.config/serena/api-tokens.sqlite3`, 0600): `id, secret_sha256, scopes, created_at, last_used, revoked`; `issue/revoke/verify` with `hmac.compare_digest`; issuance via CLI (extend `install_setup_token.py` flow or `chats token ...`)
- `MODIFIED ui/web.py` — `/api/v1/*` prefix routes with token auth decorator; existing WS + JSON paths untouched
- Scopes v1: `chat/call/read/ops` (route map documented in the decorator table)
- `MODIFIED docs/operations.md` — credential inventory gains the token store (storage class: private identity, backed up, never git)

**Key decisions**:
- Separate domain from the chat token — the phone's token and automation tokens must not be interchangeable; blast-radius isolation
- `id.secret` format — id routes the lookup, secret verifies; revocation by id without touching the secret
- Version prefix, no migration — v1 grows alongside current paths; migrating WS auth is risk with no payoff
- CLI issuance only — no web UI for minting credentials (a token-minting page is the highest-value XSS target in the system)

## Tasks

> Execution: direct — credential system, judgment-heavy. No fleet.

### Phase 1: Store + auth
- [ ] `core/api_tokens.py` + CLI issue/revoke/list + v1 decorator + scope map on initial routes
  - accept: issue → call → revoke → 403 lifecycle test; chat-token-on-v1 rejection test; timing-safe compare test
  - engine: direct

## Edge Cases / Gotchas

- `last_used` updates on every call — write contention under automation bursts; batch or debounce the update, never block auth on it
- Token listing shows ids + metadata only — secrets exist solely at issuance output (shown once, like webhook secrets)
- Scope creep guard: new v1 routes must declare scopes in the decorator table; a route without scopes fails closed (deny), not open
- Backup story: token store joins the private-identity backup tier; document restore (revoke-all-and-reissue vs restore-file trade-off)

## Testing

- [ ] Full lifecycle test (issue/call/revoke/403)
- [ ] Scope enforcement matrix test
- [ ] Credential-domain separation test (chat token rejected on v1)
- [ ] Constant-time compare test
- [ ] Load test on `last_used` path (no auth blocking)

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: —
**Review**: —
