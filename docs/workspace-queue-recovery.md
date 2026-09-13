# Workspace Queue And Recovery

The composer queues follow-up messages while Claude, Codex, or Gemini is busy.
Queued messages appear above the composer and can be edited or cancelled until
dispatch. Codex's separate Send now action still steers the active turn; Claude's
Send now uses its existing native input path. Gemini waits for the current turn.

The queue reuses the host's durable FIFO and ownership locks. Upload references
and per-turn settings stay with each message. Only confirmed unsent queue entries
are restored on explicit attachment after a restart. Dispatch removes the entry
from the durable pending snapshot before calling the provider, so uncertain
in-flight delivery is never automatically replayed. Lost enqueue acknowledgements
use the original command receipt, including when the current turn has ended.

Compaction uses an indeterminate progress bar because native events do not
provide a percentage. Native completion fills it; failure or interruption stops
it. Reduced-motion preferences disable animation.

Codex retry warnings clear only on progress from the affected turn; unrelated
events and fatal errors do not clear them. Gemini stream closure preserves the
preceding native API error and reaps the owned process tree before releasing its
lease. Unconfirmed cleanup still blocks a second writer. Interrupted prompts are
not resent automatically.

Verification covers provider queue order, edit/cancel, receipt deduplication,
attachment retries, restart restoration, terminal compaction states, and real
Chromium views at 390px and 1600px. Provider network outages themselves are not
prevented by these changes.
