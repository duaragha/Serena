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
still needs to consume the catalog notification; existing custom-title precedence
is unchanged.

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

Other documented CLI commands are not yet accounted for by this matrix. The
current command menu is not the complete Codex CLI catalog. Do not release or
declare full parity from these rows alone. Installation, account authentication,
permissions, background tasks and other non-command controls also retain their
provider-specific delivery gates in the main contract.
