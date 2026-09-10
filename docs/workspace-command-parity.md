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

## Installed Codex App Picker (2026-09-10)

The explicit Apps and connectors button and `/apps` now open an installed-app
picker. Selection is a removable, session-scoped persisted draft attachment.
Submit and steer pass selected IDs separately; the owner rechecks them against
the current native runtime and constructs exact `mention` inputs with
`app://<id>` and the native display name. It does not invent a text-only command,
enable apps, install anything, change permissions, or launch another owner.
Selected app attachments also block background-job admission through view context.
History renders app names as literal text with exact IDs in the tooltip.

[Official App Server documentation](https://learn.chatgpt.com/docs/app-server),
accessed 2026-09-10, documents `app/installed` for effective enabled/callable state,
`app/read` for metadata in batches of at most 100 IDs, and `mention` user input.
Implementation refreshes the installed snapshot for the exact loaded thread,
then reads metadata only for those IDs. Missing metadata makes selection unavailable.
The backend caps the installed inventory at 1,000 and selection at 20 distinct IDs;
it validates returned identities and states and excludes unrelated metadata.

Live discovery changed the implementation: initial full-directory `app/list`
proofs exited 1 on repeated IDs across pages, then on over 1,000 unique entries.
The final implementation does not fetch that directory. A non-refreshing
installed snapshot initially returned zero; explicit refresh returned eight.
The production picker therefore refreshes the runtime snapshot, not just an empty cache.

Commands below ran from the isolated worktree. Python is
`/home/raghav/Documents/Projects/serena/.venv/bin/python`.

- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_host.py::test_account_status_requires_explicit_owner_and_rejects_mutations tests/test_workspace_pane.py::test_app_picker_selects_exact_ids_preserves_failed_draft_and_never_auto_loads -q --tb=short`
  exited 0: 95 passed in 27.46s. Includes malformed metadata, missing metadata,
  100+1 batching, exact submit/steer routing, state revalidation, no auto-load,
  disabled selections, text-safe rendering and retained failed-send drafts.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_app_picker_selects_exact_ids_preserves_failed_draft_and_never_auto_loads -q --tb=short`
  exited 0: 2 passed in 3.81s after adding literal app-mention history rendering.
- `env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short`
  exited 0: 2 passed in 42.34s. Actual mounted Claude/Codex pages; selected app
  alone reports draft=true, removal reports false, no extra owner or submission.
- `node --test tests/workspace-connection.test.mjs` exited 0: 47 passed,
  0 failed, 152.559706ms. Includes exact-session app routes and no auto-launch.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --signed-apps`
  exited 0: signed-in native snapshot contained 8 installed apps; one exact
  selection revalidated through the real native metadata API. Same owner stayed
  ready, child reaped and disposable profile removed. Subscription auth was copied
  into an isolated profile with apps enabled there only. No browser, inference,
  tool execution, or user configuration writes. This proves discovery/selection,
  not a real model's downstream use of a connector; actual turn transport is
  covered by controlled submit/steer tests, not an inference claim.
- `env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_browser.py::test_navigation_projects_and_drafts_do_not_spawn -q --tb=short`
  exited 1 at setup: that separate legacy fixture ignores the executable override
  and its bundled Chromium is missing. No production assertion ran there. The
  mounted rich-page and pane tests above ran with installed Edge successfully.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py tests/test_workspace_app.py scripts/verify-workspace-account.py`
  exited 0: all checks passed. `node --check ui/static/workspace-pane.mjs`,
  `node --check ui/static/workspace-page.mjs`, and `git diff --check` each exited
  0 with no output.

Screenshots inspected at 390 and 1600 pixels:
`apps/desktop/build/workspace-proof/apps-{390,1600}.png` (controlled metadata,
real renderer). Dialogs fit without horizontal overflow; disabled state and
markup-as-text were visible. Remaining app parity: marketplace browsing/install,
configuration management, and a real inference using a selected connector.
The full rich-pane goal remains incomplete and unreleased.

## Codex Native Rename (2026-09-10)

The pane's Rename conversation button and `/rename [name]` open an explicit
confirmation dialog. The session owner calls `thread/name/set` for its exact
thread, then verifies `thread/read` returns the same identity and name. It keeps
the running turn intact and does not submit a prompt, start a thread, or attach
another owner. Invalid titles and unavailable ownership are refused. A failed
confirmation does not claim the requested name was persisted.

After native confirmation, the existing catalog registration path validates
the matching transcript's identity and project, writes the explicit custom
title, and indexes that exact session. This deliberately replaces an older
Serena custom title only for the user-requested Codex rename. Unrelated sessions
and linked siblings are not renamed. Catalog failure is reported separately as
"Native name saved, but Serena title synchronization failed"; a not-yet-written
transcript can still cause this condition. Passive Claude native rename behavior
and its existing-custom-title precedence remain unchanged.

The iframe asks the parent to refresh its sidebar only after the current rename
command succeeds. The parent checks origin, source window and SID, then fetches
current saved titles. It does not replay the requested title over newer cached
metadata, navigate away, or replace the owner. Ordinary composer drafts survive.

Source: [official App Server documentation](https://learn.chatgpt.com/docs/app-server),
accessed 2026-09-10; `thread/name/set`, `thread/name/updated` and persisted
`thread.name`. Installed generated schemas were also inspected.

Verification from the isolated worktree:

- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_catalog.py tests/test_workspace_host.py::test_rename_requires_owner_and_preserves_receipt_without_duplicate_native_write -q --tb=short`
  exited 0: 123 passed in 24.24s. Exact native routing/readback, ready/running
  state preservation, invalid names, malformed/foreign readback, exact metadata
  replacement, sibling preservation and duplicate-receipt protection.
- `env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_native_rename_requires_confirmation_and_preserves_failed_draft -q --tb=short`
  exited 0: 2 passed in 6.73s after fixing the initially unstyled input. Desktop
  and mobile confirmation, errors, draft preservation and successful clear.
- `env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch -q --tb=short`
  exited 0: 2 passed in 45.07s. Mounted Claude/Codex pages, explicit rename via
  real host route with a controlled provider/catalog, fresh sidebar reload,
  spoofed parent notification rejected, existing owner and draft preserved.
- `node --test tests/workspace-connection.test.mjs` exited 0: 48 passed,
  0 failed, 95.488869ms. Rename is an exact-session control, not prompt input.
- `env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-rename.py`
  exited 0: actual native rename and notification, real exact-session transcript
  registration, SQLite catalog/custom-title replacement, and same SID/name after
  process replacement. The old child was reaped before resume; all children and
  temporary profile removed. One print-only shell command materialized the
  disposable session; no inference, subscription auth, or user-profile writes.
- `/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py core/workspace_catalog.py scripts/verify-workspace-rename.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_catalog.py tests/test_workspace_pane.py tests/test_workspace_app.py`
  exited 0: all checks passed. `node --check ui/static/workspace-pane.mjs`,
  `node --check ui/static/workspace-page.mjs` and `git diff --check` each exited
  0 with no output.

Viewed `apps/desktop/build/workspace-proof/rename-390.png` and `rename-1600.png`:
black input, compact dialog, visible error and no horizontal overflow. Windows
packaged verification and release remain outstanding for the overall goal.

## Claude Explicit Custom-Title Replacement (2026-09-10)

This closes the custom-title replacement gap mentioned in the historical Claude
rename receipts below. A successful live `/rename <name>` completion now carries
both expected and confirmed native names into registration. The catalog requires
the exact session/project and matching persisted native title before replacing
that session's Serena custom title. Passive indexing has no replacement flag and
retains its existing precedence. Missing or stale native confirmation is refused;
unrelated and linked sibling metadata stays unchanged. Codex rename behavior is
unchanged.

The catalog confirmation flag is preserved through the host's result whitelist.
The frame then requests a fresh parent catalog read, rather than patching its
event's title over the cached custom name. Replayed confirmations fetch the
current saved name again; they do not replay the old requested title. The
existing origin/source/session checks remain, and the conversation and draft
are not replaced. This does not establish a new cross-machine metadata conflict
resolution protocol or a new native rename API; it completes synchronization of
the already-supported explicit Claude command.

Executed separately from the isolated worktree:

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_catalog.py tests/test_workspace_host.py::test_confirmed_claude_rename_indexes_exact_owner_without_attach tests/test_workspace_claude.py -q --tb=short
```

Exit 0: 86 passed in 2.99s. Exact-session replacement, passive precedence,
unconfirmed/stale rejection, sibling preservation, live completion and failures.

```sh
env SERENA_PROOF_BROWSER=/usr/bin/microsoft-edge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_app.py::test_app_route_bootstrap_and_real_browser_page_do_not_auto_launch tests/test_workspace_host.py::test_confirmed_claude_rename_indexes_exact_owner_without_attach tests/test_workspace_catalog.py -q --tb=short --show-capture=no
```

Exit 0: 30 passed in 34.48s after preserving the host confirmation flag.
Both mounted provider pages passed. The Claude test exercises confirmed and
stale replayed catalog events, fresh title reads and preserved drafts. An earlier
two-provider browser run exited 1 (1 passed, 1 failed in 15.32s) because the new
test passed Playwright's keyword-only `arg` positionally; corrected before rerun.

```sh
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-claude-commands.py
```

Final exit 0. Actual native Claude local rename persisted for the exact session;
the real parser/catalog initially preserved an existing Serena title under
passive indexing, then replaced it on the confirmed completion. The journal
contained the indexed title and confirmation flag. Nine local commands also
passed, with one native output/turn identity each. All disposable state and the
owned child were cleaned up, with no inference or user credentials. Initial live
exit 1 exposed the host dropping `native_rename`; that production defect was
fixed and regression-tested before the final run.

```sh
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_catalog.py core/workspace_host.py tests/test_workspace_catalog.py tests/test_workspace_host.py tests/test_workspace_app.py scripts/verify-workspace-claude-commands.py
node --check ui/static/workspace-page.mjs
```

Both commands exited 0; Ruff reported all checks passed, Node no output.
The earlier packaged proofs cover source `748573e`, not this subsequent rename
change. Full parity and release/default enablement remain incomplete.

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

Unhandled slash commands now fail explicitly in the pane and in the Codex owner
instead of becoming model prompts. The owner guard also covers steering and
non-pane callers. Native controls remain separate actions; paths containing
slashes, ordinary prose and `$skill` mentions remain ordinary input. This is an
honest failure boundary, not completion of the still-missing commands below.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_unrouted_commands_never_reach_submit_or_steer tests/test_workspace_codex.py::test_paths_and_normal_text_are_not_slash_commands tests/test_workspace_pane.py::test_unknown_codex_command_stays_in_draft_without_model_call -q --tb=short
# exit 0: 15 passed in 6.38s; unsupported, destructive, hyphenated and custom
# prompt command forms cannot silently become model input.
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py tests/test_workspace_pane.py::test_codex_local_commands_use_controls_not_model_prompts tests/test_workspace_pane.py::test_codex_inline_mention_replaces_command_only_after_file_selection -q --tb=short
# exit 0: 90 passed in 11.83s, full Codex owner and supported-control regressions.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --command-guard
# exit 0: four commands refused on the same real native owner; no turn started,
# inference=false, child reaped and temporary profile removed.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py tests/test_workspace_codex.py tests/test_workspace_pane.py scripts/verify-workspace-account.py
# exit 0: All checks passed!
node --check ui/static/workspace-pane.mjs
# exit 0.
```

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
| `hooks` | Native project-scoped `hooks/list` inspector | Read-only enabled/trust/source/handler state and diagnostics; trust/enable mutations remain unimplemented |
| `diff` | Bounded Git working-tree snapshot | Staged/unstaged/untracked regular files, explicit omitted-file notices; native Git and desktop/mobile rendering verified |
| `apps` | Installed connector picker | Exact native IDs/callability, retained drafts and native metadata proof; marketplace management and downstream inference remain |
| `rename` | Explicit native rename and Serena catalog sync | Same-session persistence through process replacement; sidebar refresh and draft preservation verified |

### Codex Documentation Inventory (2026-09-10)

Source: [official developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
accessed 2026-09-10. Documentation inventory is not proof of installed-version
support. These documented names are not yet fully covered by the rows above:

`ide`, `keymap`, `vim`, `setup-default-sandbox`, `sandbox-add-read-dir`, `agent`,
`subagents`, `apps`, `plugins`, `clear`, `archive`, `delete`,
`exit`, `experimental`, `approve`, `memories`, `import`, `feedback`, `init`,
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

### Native Hook Inspection and Plugin Constraint

### Project Diff Receipts (2026-09-10)

`/diff` uses Git, not a model request. An attached owner determines the directory;
caller-supplied paths are rejected. External diff/text conversion and fsmonitor
helpers are disabled. No Git writes or index refresh are requested. Output is
limited to 2 MiB, 100 untracked paths and a 15-second command deadline. Untracked
symlinks/non-regular files are named as omitted, not followed or hidden. Binary
changes use Git's binary notice rather than a fabricated text patch. Separate
commands are a working-tree snapshot, not an atomic snapshot during concurrent
edits. Windows-specific validation remains part of final packaged verification.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_project_diff_is_explicit_read_only_and_text_safe tests/test_workspace_diff.py -q --tb=short
# exit 0: 5 passed in 5.09s.
/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_host.py::test_project_diff_requires_existing_owner_and_ignores_caller_paths -q --tb=short
# exit 0: 1 passed in 0.75s.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python -c 'import hashlib,json; from pathlib import Path; from core.workspace_diff import read_project_diff; root=Path.cwd(); index=root/".git"; import subprocess; p=Path(subprocess.check_output(["git","rev-parse","--git-path","index"],text=True).strip()); before=hashlib.sha256(p.read_bytes()).hexdigest(); result=read_project_diff(root); assert hashlib.sha256(p.read_bytes()).hexdigest()==before; print(json.dumps({"ok":True,"indexUnchanged":True,"sectionBytes":{k:len(result[k]) for k in ("staged","unstaged","untracked")},"omittedCount":len(result["omitted"])}))'
# exit 0: index unchanged; staged 0, unstaged 9382, untracked 5082 characters,
# no omitted paths. Field sectionBytes measures decoded characters here.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_diff.py core/workspace_host.py tests/test_workspace_diff.py tests/test_workspace_host.py tests/test_workspace_pane.py
# exit 0: All checks passed!
node --check ui/static/workspace-pane.mjs
# exit 0.
```

Viewed `apps/desktop/build/workspace-proof/diff-390.png` and `diff-1600.png`:
all three sections and omitted-path notice fit; markup remains literal text.

### Hook API Evidence

[Official App Server documentation](https://learn.chatgpt.com/docs/app-server),
accessed 2026-09-10, lists `hooks/list` but explicitly warns against calling
`plugin/list`, `plugin/read`, `plugin/install` and `plugin/uninstall` from production
clients while those endpoints are under development. Full production plugin
management cannot be claimed using those endpoints without resolving that
constraint. No plugin installation, removal or configuration mutation was run.

`/hooks` now opens a read-only native inspector. Reads are bound to the exact
owner's project; malformed/cross-project results fail rather than guessing trust
or enabled status. Hook commands are displayed as text, never executed. Opening
the app does not fetch hooks; opening/refreshing the inspector does. Empty native
inventory is verified; nonempty state and malformed results use controlled tests.

```sh
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_codex.py::test_hook_catalog_is_project_scoped_read_only_and_validated tests/test_workspace_pane.py::test_hook_inspector_reads_only_and_preserves_draft tests/test_workspace_host.py::test_account_status_requires_explicit_owner_and_rejects_mutations -q --tb=short
# exit 0: 9 passed in 10.76s; initial exit 1 was a test assignment returning an
# async function to Playwright evaluate, causing immediate invocation.
env SERENA_PROOF_BROWSER_CHANNEL=msedge /home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_pane.py::test_hook_inspector_reads_only_and_preserves_draft -q --tb=short
# final exit 0: 2 passed in 4.26s, after screenshot-driven row layout repair.
env SERENA_EVIDENCE_KIND=live /home/raghav/Documents/Projects/serena/.venv/bin/python scripts/verify-workspace-account.py --hooks
# exit 0: nativeEmptyHookCatalogRead=true, same owner, no inference/login/browser,
# child reaped and temporary profile removed.
node --test tests/workspace-connection.test.mjs
# exit 0: 46 passed, 253.300931ms.
/home/raghav/Documents/Projects/serena/.venv/bin/ruff check core/workspace_codex.py core/workspace_host.py tests/test_workspace_codex.py tests/test_workspace_host.py tests/test_workspace_pane.py scripts/verify-workspace-account.py
# exit 0: All checks passed!
node --check ui/static/workspace-pane.mjs
# exit 0.
```

Inspected final `apps/desktop/build/workspace-proof/hooks-390.png` and
`hooks-1600.png`. Both use a real Codex pane with controlled hook data. Status is
readable on one line; the earlier shared task-row grid squeezed it into a narrow
column, now corrected and guarded by a browser height assertion.

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
