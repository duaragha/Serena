# Muse Subscription Limits

The limits ribbon reads subscription quota, not conversation context occupancy
or accumulated token counts. Stable native terminals and Dev structured panes
use the same `core/muse_usage_reader.py` reader.

## Source

Verified against Muse 1.3.0-R3057.1 and the signed-in account on 2026-09-16:

- The installed MSP schema exposes `usage/read` and `usage/changed`. These are
  process-local observations. A fresh `muse serve` host returned `{}` even
  after `account/read` and `model/list`; starting one cannot read another
  terminal's quota.
- The native account exchange, `POST https://api.meta.ai/muse-code/key`,
  returns `subs_usage.window` and `subs_usage.weekly`. `used_percent` is used
  quota (not remaining), `resets_at` is epoch seconds, and
  `window_duration_mins` identifies the short window. The live verification
  returned 0% / 9% with a 300-minute short window.
- The previous reader unconditionally returned unavailable. Its claim that
  Muse had no quota surface was incorrect for this installed version.

The reader uses only the existing native OAuth access token, honors
`MUSE_AUTH_PATH` and `XDG_CONFIG_HOME`, and sends it only to the fixed official
account origin. Redirects are rejected. This account exchange can issue a
credential, but Serena discards it: no credentials are written, logged, or
returned to the renderer. No model generation, session start/resume, or
terminal attachment occurs. A missing/expired login remains unavailable with
a sign-in reason; Serena never starts a login flow or substitutes an API key.

## Refresh Behavior

The API returns the cached observation immediately while one daemon worker
refreshes it. Successful observations are cached for five minutes; failures
retry after thirty seconds. Requests have an eight-second timeout and bounded
response size. Failures retain the prior values and original timestamp with
`stale: true`; sign-in file changes invalidate the previous account's cache.
The renderer says "as of" or "last known", not live, and respects the actual
short-window duration. Missing or malformed numbers never become zero.

## Verification

`tests/test_workspace_muse_usage.py` covers the native payload, zero/over-quota
values, malformed responses, credential redaction, unchanged auth files,
timeouts/errors, single-flight nonblocking refresh, account invalidation,
and the live-usage API. The Muse limits cases in
`tests/test_workspace_browser.py` cover desktop/mobile popup layout, both
windows, dynamic duration, initial loading, and stale presentation.

Delivered through the bundled desktop sidecar in stable 0.3.3 and Dev
0.3.3-dev.1. Use the normal in-app updater; no source checkout or separate
backend restart is needed.
