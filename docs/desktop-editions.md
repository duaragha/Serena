# Desktop Editions

Serena stable uses native CLI terminals. Serena Dev uses the structured
conversation workspace. Both are installed desktop applications, not a browser
pointing at a source checkout.

## Release Contract

- Stable tags: `vX.Y.Z`; Dev tags: `vX.Y.Z-dev.N`.
- Structured-view fixes ship a Dev tag only. Publishing a Dev update does not
  publish a stable update. Increase `N` for subsequent Dev builds on the same base.
- `apps/desktop/package.json` holds the base stable version. The release workflow
  validates the tag and configures the edition before building either platform.
- Stable publishes `latest-linux.yml` / `latest.yml`; Dev publishes
  `dev-linux.yml` / `dev.yml` on a GitHub prerelease. No cross-channel promotion.
- Both installers include their backend and Claude runtime. An app update swaps
  the entire package. No source pull or manual backend restart is required.
- App IDs, executable names, shortcuts, updater caches and Electron profiles are
  different. `--dev` cannot turn a packaged stable app into Dev.

## Runtime Boundaries

Each desktop starts its own bundled sidecar on a free loopback port and verifies
the edition, version and view capability before opening its window. It never
attaches to, installs over, or restarts `serena-mobile-host.service`. The existing
phone host remains independently managed; this split does not update that service.

| State | Stable | Dev |
| --- | --- | --- |
| Electron profile | `serena-desktop-stable` | `serena-desktop-dev` |
| Index, uploads, workspace events | `~/.local/share/chats` | `~/.local/share/chats-dev` |
| UI state | `~/.config/serena` | `~/.config/serena-dev` |
| Coding preference | `~/.local/state/serena` | `~/.local/state/serena-dev` |

Native histories, authentication, projects, memory, knowledge and chat metadata
remain shared. Dev is not a filesystem sandbox: an agent explicitly editing a
project or changing a native provider setting changes that real shared data.
Runtime leases remain shared so the same saved conversation cannot be opened for
writing in both apps. Close its owning pane before moving it between editions.

On Dev's first launch, it takes a read-only SQLite backup of the existing index
when available. This avoids reparsing every historical chat just to open Dev.
After that initial snapshot, the two indexes are separate; no existing Dev index
is replaced, and native conversations are not copied or changed.

On the first upgrade, the new stable profile can run beside a pre-split window.
Existing live work is not terminated to migrate it. Finish that work and close
the old window normally; future launches use the installed stable edition.

## Development And Verification

`npm run dev` is the source-development variant of Serena Dev. Packaged Dev
updates do not depend on that command, a checkout, or a manually running server.

Run desktop tests, workspace tests, terminal lifecycle tests, then verify both
published installers. The coexistence proof must check separate backend PIDs,
stable terminal rendering, Dev structured rendering, shared writer exclusion,
and that quitting Dev leaves stable healthy. Never use an active user's chat as
the test prompt.
