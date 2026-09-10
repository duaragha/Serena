# Workspace Command Parity Audit

Status: incomplete. Catalog presence and generic input forwarding are not proof
that a command's full behavior works. Gemini is deferred.

## Evidence Sources

- Installed Claude CLI 2.1.267 / pinned SDK: `scripts/verify-workspace-claude-commands.py`,
  executed 2026-09-10, exit 0. Disposable unsigned profile, 45 catalog names.
  Catalogs can differ with credentials, plugins, project and native version.
- [Claude commands](https://code.claude.com/docs/en/commands), accessed 2026-09-10:
  official descriptions distinguish skills, workflows and local controls.
- [Codex commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
  accessed 2026-09-10: official reference, not proof of installed capability.
- Source: `core/workspace_claude.py`, `core/workspace_codex.py`,
  `ui/static/workspace-pane.mjs`; focused tests and exact historical runtime
  receipts are in `interactive-workspace.md`.

## Claude Native Catalog

Every name in the observed 45-entry catalog is included below. Forwarding means
unchanged input through the owned SDK session, not a substitute prompt or CLI
process. Commands can still require subscription access or native configuration.

| Names | Current route | Evidence or remaining gate |
| --- | --- | --- |
| `clear` (`reset`, `new`) | Explicit pane session transition | Existing exact-session transition tests/proofs; CLI naming argument parity remains |
| `color` | Pane-local prompt color | Browser persistence tests; not native remote color synchronization |
| `reload-plugins`, `reload-skills` | Native SDK controls | Existing control tests/proofs; complete flag/argument parity remains |
| `doctor` (`checkup`) | Native skill input | Browser routing plus actual unsigned native acceptance; authenticated repair not run |
| `deep-research`, `design`, `design-sync`, `dataviz`, `update-config`, `verify`, `debug`, `code-review`, `simplify`, `batch`, `fewer-permission-prompts`, `loop`, `claude-api`, `workflow-authoring`, `run`, `run-skill-generator` | Native skill/workflow input | Advertised and forwarded; no blanket end-to-end claim for these workflows |
| `agents`, `auto-mode-setup`, `autocompact`, `compact`, `config`, `context`, `effort`, `fast`, `heapdump`, `init`, `mcp`, `model` | Native input; some also have pane controls | Individual controls have earlier receipts; full argument, output and error matrix remains |
| `__remote-workflow`, `workflow-launch-exec` | Native input | Server-session prerequisites; not exercised or launched by audit |
| `rename`, `security-review`, `usage`, `insights`, `recap`, `goal`, `design-consent`, `design-revoke`, `list-agents`, `team-onboarding` | Native input | Advertised; side effects and integration with pane metadata still need verification |

The terminal-command and skill sets can overlap. A skill entry must not be
silently replaced with a similarly named local command. Diagnostics remains a
separate installation tool, not the doctor skill.

### Verified Local Invocation Forms

On 2026-09-10 the expanded native proof exited 0 for `/effort low`, `/context`,
`/usage`, `/agents`, `/list-agents`, `/model`, `/config --help`,
`/rename command-proof`, and `/autocompact auto`. It verifies zero inference,
one output row through the production event adapter, exact turn identity and
same native process/session. `/agents` correctly reports that the native wizard
was removed, not a fabricated management UI. `/model` here is inspection only.
`/usage` is the unsigned session report, not proof of subscription-limit fetching.
Rename persists a `custom-title` JSONL record containing `customTitle` and the
exact `sessionId`; the native proof now asserts both against the disposable
session file rather than trusting the success message. `parse_metadata` now
retains the latest valid exact-session title and `_upsert_session` uses it for
Claude's catalog title. Synced custom metadata still takes precedence. Native
rename through real persistence, parsing, SQLite indexing and catalog search is
proved. Immediate refresh and intentionally replacing an existing Serena custom
title remain separate work; blindly replaying old transcript titles into synced
metadata would be unsafe.

The live Claude owner now tracks explicit text-only `/rename <name>` by input
turn ID. Only a successful matching completion emits a catalog signal; failed
turns clear it without updating the title. The host indexes that same owner and
project without attachment or a new session, verifies the expected title against
native persistence, and bounds flush retries to five attempts. Indexing errors
are journaled separately without interrupting the owner. The open parent sidebar
now consumes successful indexed catalog notifications from its exact same-origin
iframe, patches only the named chat and current heading, and renders titles as
text. It does not reload/navigate the conversation or touch its draft. Newer
cached custom names retain precedence over replayed native names. Intentionally
replacing an existing Serena custom name from a native rename remains open.

Open-sidebar refresh receipts (2026-09-10):

```sh
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short --show-capture=no
# final exit 0: 2 passed in 26.98s, both provider panes, actual polled catalog event,
# same owner/draft, escaped heading, wrong-source rejection, newer custom name.
# Earlier exits 1: event envelope was not unwrapped, then fixture lacked convTitle.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_workspace_host.py::test_confirmed_claude_rename_indexes_exact_owner_without_attach -q --tb=short
# exit 0: 23 passed in 5.20s.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-commands.py
# exit 0: native rename indexed; catalog notification contains the actual display
# title; owned transport and disposable profile removed, no inference.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_catalog.py core/workspace_host.py tests/test_workspace_app.py
# exit 0: All checks passed!
node --check ui/static/workspace-page.mjs
# exit 0.
```

```sh
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_claude.py tests/test_workspace_host.py tests/test_workspace_catalog.py -q --tb=short
# exit 0: 205 passed in 24.24s before extending the host test with pending-flush coverage.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_confirmed_claude_rename_indexes_exact_owner_without_attach -q --tb=short
# exit 0: 2 passed in 1.23s, including bounded pending-flush retries.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-commands.py
# exit 0: real native rename persistence -> host publication -> production catalog
# registration -> exact-session saved title. No inference or extra owner launch.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_claude.py core/workspace_host.py core/workspace_catalog.py tests/test_workspace_claude.py tests/test_workspace_host.py scripts/verify-workspace-claude-commands.py --fix
# exit 0: one test-local import ordering issue fixed.
```
All writes and command execution were confined to a disposable profile/project.

```sh
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-commands.py
# exit 0: 45 native commands, 9 local forms, nativeRenamePersistedForExactSession=true;
# zero inference, expected unsigned doctor refusal, child and profile cleaned up.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-claude-commands.py
# exit 0: All checks passed!
```

Persisted-title integration receipts (2026-09-10):

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py -q --tb=short
# exit 0: 21 passed in 0.37s; includes native name, newer rename, foreign SID,
# malformed/empty/control-character/oversized names and explicit custom precedence.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-commands.py
# exit 0: nativeRenameIndexedInCatalog=true. Initial exit 1: temporary project's
# -tmp-serena-* name correctly triggered internal-project hiding; changed fixture
# prefix, not production visibility rules.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_catalog.py scripts/verify-workspace-claude-commands.py
# exit 0: All checks passed!
```

Shared parser/indexer lint remains at 15 pre-existing findings (exit 1),
confirmed against `git show HEAD:core/parser.py` and `git show HEAD:core/indexer.py`
piped separately to Ruff `check --stdin-filename <path> --output-format concise -`
(exit 1 each: 5 and 10 findings). Unrelated cleanup was not included.

## Codex Pane Command Routes

| Names | Current route | Remaining gap |
| --- | --- | --- |
| `resume`, `fork`, `review`, `compact` | Existing native session controls | Preserve exact session and confirmation contracts; no claim for every CLI argument |
| `mcp`, `permissions`, `skills` | Native catalogs/control dialogs | Audit full CLI option coverage separately |
| `model`, `reasoning` | Focus native-catalog-backed selectors | Selection applies to next turn; not a claim of persistent TUI configuration |
| `status` | Existing event snapshot plus explicit native account-limit refresh | Native unsigned refusal, signed-in successful retrieval and controlled positive rendering verified; explicit snapshot, not a continuous feed |
| `plan` | Explicit native mode picker and `thread/settings/update` | Native Plan/Default confirmed with unchanged model/effort; workspace-confirmed mode restored across two real process replacements, no inference |
| `copy` | Copy button, slash action and Ctrl+O | Completed response/plan only; drafts retained, native browser clipboard verified. Rollback-specific suppression still needs coverage. |
| `ps` | Existing native background-task dialog | Explicit refresh and task controls; slash routing tested without submitting a prompt |
| `mention` | Existing project file picker, including inline search | Selection replaces the slash command with a quoted file mention; cancellation preserves draft; desktop/mobile verified |

### Codex Documentation Inventory (2026-09-10)

Source: [official developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
accessed 2026-09-10. Documentation inventory is not proof of installed-version
support. These documented names are not yet fully covered by the rows above:

`ide`, `keymap`, `vim`, `setup-default-sandbox`, `sandbox-add-read-dir`, `agent`,
`subagents`, `apps`, `plugins`, `hooks`, `clear`, `rename`, `archive`, `delete`,
`diff`, `exit`, `experimental`, `approve`, `memories`, `import`, `feedback`, `init`,
`logout`, `fast`, `goal`, `personality`, `stop`, `app`, `side`,
`btw`, `raw`, `new`, `quit`, `usage`, `debug-config`, `statusline`, `title`, `theme`,
`pets`, `pet`.

Several have existing non-slash controls; each still needs an explicit mapping
and provider-specific verification. Do not equate absent aliases with absent
backend capabilities, or existing buttons with complete CLI argument parity.
Destructive/configuration/account commands require their own explicit authority
and confirmation flows; auditing them does not authorize executing them here.
No full-parity release claim follows from this inventory. Installation, account authentication,
permissions, background tasks and other non-command controls also retain their
provider-specific delivery gates in the main contract.

Copy verification:

Read-only command routing receipts (2026-09-10):

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_codex_inline_mention_replaces_command_only_after_file_selection tests/test_workspace_pane.py::test_project_file_picker_preserves_draft_and_never_sends tests/test_workspace_pane.py::test_codex_local_commands_use_controls_not_model_prompts tests/test_workspace_pane.py::test_codex_unavailable_or_argument_commands_do_not_submit -q --tb=short
# exit 0: 22 passed in 10.78s.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-copy.py
# exit 0: real browser clipboard plus controlled inline file search/selection
# during a running turn. No provider launched; does not prove native file search.
# Initial exit 1 exposed the send-disabled guard blocking read-only commands;
# ps/mention now bypass only that guard, not control availability checks.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check tests/test_workspace_pane.py scripts/verify-workspace-copy.py
# exit 0: All checks passed!
node --check ui/static/workspace-pane.mjs
# exit 0.
```

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_copy_completed_output_ignores_running_turn_and_preserves_draft tests/test_workspace_pane.py::test_codex_local_commands_use_controls_not_model_prompts tests/test_workspace_pane.py::test_codex_picker_lists_local_actions_and_preserves_draft -q --tb=short
# exit 0: 10 passed in 5.85s; includes empty history and clipboard denial.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-copy.py
# exit 0: real Chromium clipboard round-trip, partial response excluded, draft
# retained, no provider process and disposable browser closed.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check scripts/verify-workspace-copy.py tests/test_workspace_pane.py
# exit 0: All checks passed!
node --check ui/static/workspace-pane.mjs
# exit 0.
```
