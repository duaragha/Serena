# Muse Workspace Contract

The sidebar reads Muse's durable `runtime.user_intent.accepted` and
`runtime.session` envelopes, including microsecond timestamps and nested
workspace metadata. Only root `sessions/YYYY/MM/DD/<id>/session.jsonl` logs
are catalogued; startup-only logs and internal children are not conversations.
Previously indexed zero-message rows are reparsed on the next normal refresh.
Native logs and synced user metadata are not deleted by this repair.

Interactive panes use `muse serve` and the MSP schema exported by Muse 1.3.0,
not `muse exec --session-id`. New sessions use `session/start`; saved sessions
use the exact `session/resume` identity. Commands carry UUIDv7 IDs. The shared
workspace transport drains stderr, bounds large JSONL frames and owns process
cleanup; the session lease spans the host lifetime, not just a prompt.

Live MSP items map to the existing message, reasoning, tool and compaction
renderers. Authoritative revisions replace deltas. Stop uses `turn/interrupt`
without waiting on the foreground turn. Native approvals remain explicit.

Some CLI-created histories return `history.noneReason=projectionUnavailable`
and reject `view/subscribe`. This is a verified native response, not an empty
conversation. Serena paints durable messages, folds `view/page` history, then
polls forward from the last observed cursor. No input is resent for recovery.
Polling failures disable sending and preserve Stop for unconfirmed work.

## Native History Refusal

Muse 1.3.0-R3057.1 can refuse `session/resume` and `session/read` with
`classify turns: session fork rejected: MalformedJsonl` even when every
physical log line is valid JSON. An isolated copy of a reported conversation
reproduces this before the session is loaded; `turn/start` then correctly
returns `sessionNotLoaded`. `excludeItems=true` does not bypass the refusal.

Prefix isolation located the first refusal at the completion record of a
long turn containing compaction. Retained-frame and omission-marker variants
did not resolve it. This is evidence of a native semantic-history refusal,
not proof that the original JSONL is corrupt or that compaction is its root
cause. Do not strip records, rewrite checkpoints, or silently resume as new.

Serena publishes readable durable messages before attempting native resume.
The browser now consumes that journal even when attachment returns `ok:false`,
then preserves the attachment error and disabled composer. A failed replay
must not replace the original error, retry a prompt, or advance past an
unaccepted event. Recovery into a different conversation requires an explicit
user choice. This mitigation does not claim to fix Muse's native validator.

## Verification (2026-09-16)

- Read-only discovery of the installed store: 84 logs, 16 root sessions, nine
  conversations with user messages. All 16 installed catalog rows previously
  had zero parsed messages.
- Disposable copy of the original integration chat: 28 messages restored,
  followed by 1,250 native view items. The paged monitor observed a real failed
  turn and cleared running state without resubmission.
- Separate temporary native session: actual Meta model reply, terminal
  completion, clean close and exact-session reopen with four retained items.
- Browser screenshots at 390px and 1600px verified messages, tool details,
  animated activity, Stop and no horizontal overflow.

Regression coverage is in `tests/test_workspace_muse.py`,
`tests/test_muse_firstclass.py`, and the Muse cases in
`tests/test_workspace_pane.py`. These checks do not certify unrelated Fleet
or brain-provider behavior inherited from the earlier integration.
