# Spec: Vision Turns (she sees what you're looking at while you talk)

## Overview

**What**: Attach the live screen — phone mirror or desktop watch session — to a spoken or typed turn, so "what's this?" works without describing anything.
**Why**: This is the half of screen share that makes it feel like a video call. The plumbing is already there on one side only: `core/brain_daemon.py` accepts images (1 per turn, 5 MB, `core/image_input.py:9-10`), but **no voice client has ever sent one** — `voice/call/brain.py` and `voice/call/orchestrator.py` are text-only, and `core/frontdoor.py:_resident_stream_turn` sends no images either. Without this, the mirror is a screen Serena can look at only when explicitly asked, not something she can see while you talk.
**Scope**: IN — resolving "what is she looking at" at turn time, freshness and size rules, provenance, latency budget and instrumentation, graceful degradation when a model can't take images. OUT — the frame sources themselves (spec-phone-mirror, existing computer watch), tapping (spec-phone-control), always-on ambient context (spec-proactive-policy).

## Requirements

- [ ] WHEN a live screen session exists and Raghav speaks THEN the newest changed frame is attached to that turn, with no extra prompt from him
- [ ] WHEN no session is live, the session is stale (>45 s), or the device is locked THEN the turn goes text-only and says so in the provenance line, never silently
- [ ] WHEN a frame is attached THEN its long edge is ≤2576 px before sending (his 16 Pro is 2622 px, so it is always downscaled)
- [ ] WHEN the answering model cannot accept images (the local fallback refuses them, `brain_daemon.py:1288`) THEN the turn degrades to text plus an OCR line rather than failing
- [ ] WHEN an image turn runs THEN its latency is logged separately from text turns so the cost is visible
- [ ] WHEN the acknowledgement plays THEN it is unchanged (p50 1.13 s today) — image prefill must never delay the "one sec"

## Architecture / Design

**Changes** (blast radius):
- `ADDED core/vision_turn.py` — the resolver. `current_view(*, max_age=45) -> ViewAttachment | None`: finds the active source in priority order (phone mirror session → computer watch session → none), checks freshness and lock state, downscales to the Claude limit, and returns `(image_bytes, media_type, provenance)` where provenance is one line like `phone screen, 1.2s old`.
- `MODIFIED voice/call/brain.py` + `voice/call/orchestrator.py` — include `images` in the turn payload when `current_view()` returns one; the message shape is already `{"type":"turn", "protocol":…, "text":…}` over `brain.sock`, so this is one extra key (see the reference client at `core/frontdoor.py:591-615`).
- `MODIFIED core/frontdoor.py` — same one-key change in `_resident_stream_turn`, so typed chat gets the same eyes.
- `MODIFIED core/brain_daemon.py` — per-protocol budgets live at `:477` (`budgets.get(protocol, (7_000, 1_800, 5))`); give image-bearing turns their own budget so a screenshot doesn't evict memory context.
- `MODIFIED voice/call/metrics` (wherever `call_metrics.jsonl` is written) — add `had_image` and `prefill_ms` so image turns are separable in the p50/p90 tables.

**Key decisions**:
- **One image per turn, newest wins.** The daemon's limit is 1 and that is the right limit anyway; a sequence of frames is what the watch loop is for, not what a conversational turn needs.
- **Attach on every turn while a session is live, don't wait to be asked.** The whole point is that he stops describing his screen. The provenance line is what keeps it honest.
- **Low-res by default, full-res on demand.** The attached frame is the downscaled video frame; when she needs to read something she calls the mirror's `still` (full-res PNG) as a tool, which costs a round trip but is crisp.
- **Fast lane keeps answering.** Voice turns route through the `fast` lane (opus-5 low, `config/serena-policy.json:122,143`); vision does not change routing. Astra stays for action planning only, because its visual turns are p50 4.7 s / p90 6.6 s.
- **Degrade, never fail.** Codex and Muse pass images through; the local model refuses. That path falls back to text + an OCR line rather than erroring the turn.

## Tasks

> Execution: fleet, one run, after spec-phone-mirror Phase 2 (needs a frame source to attach).

### Phase 1: Resolver
- [ ] `core/vision_turn.py` with source priority, freshness, lock gating, downscale, provenance
  - accept: unit tests cover live/stale/locked/no-session; a 2622 px frame comes back ≤2576 px
  - engine: fleet

### Phase 2: Wire the turn paths
- [ ] Voice (`brain.py`, `orchestrator.py`) and typed (`frontdoor.py`) both attach; image-turn budget; metrics fields
  - accept: a live mirror session plus a spoken question produces a turn whose payload carries exactly one image and the provenance line; `call_metrics.jsonl` distinguishes it
  - engine: fleet

### Phase 3: Latency gate
- [ ] Measure 20 image turns against the text baseline; tune (low-res first, defer stills) until the gate passes
  - accept: acknowledgement p50 unchanged; content first-audio p90 ≤7 s; numbers recorded in the Progress Log
  - engine: direct (needs live calls)

## Edge Cases / Gotchas

- Budgets: images eat prefill context. Without a separate budget, a screenshot silently evicts memory context and she gets *less* personal while gaining eyes — the exact wrong trade.
- The phone-call brain runs on the **PC voice host** (`chats phone voice-host`, :8766), not the laptop, and that host is still on the stale July snapshot (task 1043). Frames must reach whichever host answers the turn — fix 1043 first or the vision turn lands in a brain with no memory and no clock.
- A frame captured mid-transition (app switching) is worse than no frame: prefer the last *settled* frame the change detector emitted.
- Do not attach images to reflex-lane turns; they are supposed to be instant and text-shaped.
- Provenance is not optional. If she answers about a screen she cannot actually see, that is the single worst failure mode in this whole roadmap.

## Testing

- [ ] Resolver unit tests (live / stale / locked / none / oversized)
- [ ] Payload test: exactly one image, correct media type, budget applied
- [ ] Degradation test: model that refuses images yields text + OCR line, not an error
- [ ] Metrics test: `had_image` and `prefill_ms` present and separable
- [ ] Regression: text-only turns are byte-identical to today's payloads

---

## Progress Log

**Status**: Not started
**Branch**: — (created at execution)
**Current phase**: —
**Last completed task**: —
**Files modified**: —
**Blockers**: needs spec-phone-mirror Phase 2; task 1043 (PC brain) for call-path turns
**Review**: —
