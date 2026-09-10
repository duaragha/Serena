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
Rename succeeds natively; sidebar synchronization is a separate remaining gate.
All writes and command execution were confined to a disposable profile/project.

## Codex Pane Command Routes

| Names | Current route | Remaining gap |
| --- | --- | --- |
| `resume`, `fork`, `review`, `compact` | Existing native session controls | Preserve exact session and confirmation contracts; no claim for every CLI argument |
| `mcp`, `permissions`, `skills` | Native catalogs/control dialogs | Audit full CLI option coverage separately |
| `model`, `reasoning` | Focus native-catalog-backed selectors | Selection applies to next turn; not a claim of persistent TUI configuration |
| `status` | Read-only existing event snapshot | No account-rate-limit display or fresh provider configuration read yet |
| `plan` | Explicit native mode picker and `thread/settings/update` | Native Plan/Default confirmed with unchanged model/effort; workspace-confirmed mode restored across two real process replacements, no inference |

Other documented CLI commands are not yet accounted for by this matrix. The
current command menu is not the complete Codex CLI catalog. Do not release or
declare full parity from these rows alone. Installation, account authentication,
permissions, background tasks and other non-command controls also retain their
provider-specific delivery gates in the main contract.
