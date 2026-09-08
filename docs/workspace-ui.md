# Neon Workspace

The desktop workspace implements the approved Serena UI concept using black
surfaces, neon pink accents, provider identities, and compact desktop navigation.
It does not replace terminal output with HTML transcripts or introduce a second
composer in front of the provider CLI.

## Ownership

- `ui/static/workspace.css`: workspace chrome and responsive layout.
- `ui/static/workspace.js`: project picker, provider views, inspector, and status.
- `ui/web.py`: existing session controllers, real terminal mount, and routes.
- `ui/workspace_git.py`: bounded, read-only Git status with optional locks disabled.
- `ui/static/vendor/lucide.min.js`: locally bundled Lucide icons, no CDN dependency.

Project selection reuses the existing recursive tree and filter callbacks.
Session rows retain selection, context menus, linked identities, search excerpts,
starred items, archive sections, and active runtime close controls. Completed
chats have a persistent shortcut to their existing section. Keyboard shortcuts
remain available through the footer keyboard control.

Provider buttons reveal existing terminal instances. Split displays every live
linked member, including Gemini. Switching views does not spawn another CLI or
close the hidden runtime. Existing busy/idle policy still owns sleeping. Actual
terminal input, clipboard, image drop, output, and session resume remain on the
existing xterm/PTY path. Closing a provider-only pane restores surviving linked
panes instead of leaving a blank workspace.

The inspector uses actual file-tree and file-viewer APIs. Changes shows Git
status, including untracked and renamed paths, and refreshes every ten seconds
only while visible. There are no stage, commit, or revert actions. Missing
directories and failures are explicit; the route never falls back to HOME.
Persona and Tooling use the existing save endpoints and retain unsaved drafts
while navigating. There are no placeholder metrics or mocked session states.

## Verification

Focused browser tests use the full renderer and real xterm with isolated API
and socket fixtures. They do not launch providers. Tests cover routing, input,
single/split views, exit recovery, projects, completed chats, drafts, files,
Git status, narrow layouts, and preservation of lifecycle/clipboard contracts.

```sh
python -m pytest tests/test_workspace_browser.py tests/test_workspace_git.py -q
node --check ui/static/workspace.js
```

For a real-data, read-only browser check:

```sh
SERENA_EVIDENCE_KIND=live python scripts/verify-workspace.py --screenshots /path/to/screenshots
```

The proof starts an isolated loopback server, blocks mutations, reads the local
index and one transcript, exercises the Git inspector and Tooling view, captures
desktop/mobile screenshots, and closes its own server. It never starts or
attaches a coding runtime. Live provider-generated output and Windows native
ConPTY are not automated by this proof; the existing terminal implementation
and its tests are preserved. The installed app and any shared backend must load
the updated release before the new chrome appears. Running agents are not
restarted as part of verification or release.
