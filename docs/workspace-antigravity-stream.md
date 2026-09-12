# Gemini Native Coding View

The Gemini coding pane uses the installed Antigravity CLI's persistent
`--input-format stream-json --output-format stream-json` interface. It does not
copy CLI databases into ACP storage. The previous ACP integration remains a
separate adapter and is not used by the default Gemini factory.

The owner waits for native initialization and checks the exact conversation ID
and project before sending input. New conversations checkpoint the ID returned
by the CLI before publishing history. Existing PTYs and other writers still
block attachment; opening a view never terminates a terminal to take ownership.
The stream inherits existing subscription authentication and permission settings.

Saved history comes from Antigravity's readable transcript. Native SQLite files
remain the resume authority and are not decoded or migrated. Headless sessions
do not write the interactive CLI prompt index, so a native-confirmed workspace
is stored in Serena metadata and titles are recovered from transcript prompts.

The adapter renders incremental replies and tool inputs/outputs in the shared
pane, preserves native event details, and avoids duplicating the final result.
Commands and skills are discovered through read-only native commands. Control
queries such as `/help` run separately without an inference turn; skill prompts
go through the persistent stream. Model choices come from `agy models`.

Protocol limits are explicit: streamed input accepts text only. Attachments use
validated local-file references, not fabricated image protocol messages. The
headless interface does not provide interactive permission/question replies.
Stopping a turn closes its owned process tree, then a subsequent prompt resumes
the same saved conversation. A model/mode change is blocked after potentially
background-producing tools until the user explicitly stops that work. Closing
the view never closes the owner.

Verification on 2026-09-12 used installed Antigravity CLI 1.2.2: two native turns
shared one process, followed by a third turn after exact-session resume that
remembered the original marker. A second probe also verified headless catalog
metadata. Existing user chats were untouched. The replay, tool rendering and
composer were checked in Chromium at 390px and 1600px widths.

Repeat the live, isolated-conversation probe with:

```sh
PYTHONPATH=. .venv/bin/python scripts/verify-workspace-antigravity-stream.py \
  --live /absolute/path/to/a/new/proof-directory
```

Protocol reference: https://antigravity.google/docs/cli/headless/
