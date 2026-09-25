# Selective Stable Promotion

Serena Dev has **Releases > Promote to Main**. Main does not register this window
or its IPC. GitHub CLI must be installed and signed in with workflow permission
on `duaragha/Serena`; the current laptop already has this setup. Credentials stay
in the CLI/keyring, never the renderer. **Open GitHub** reaches the workflow without
a local CLI (the workflow form requires the same validated request fields).

Select features, explicitly mark additions as tested, then **Check selection** to
build both platforms without publishing. **Build and publish to main** asks for
native confirmation and publishes only after both platform gates pass. Checking
does not publish. Publishing does not download, install, stop or restart either
locally running edition. Main's existing updater remains the installation path,
at the user's chosen time.

## Composition Contract

`config/promotion-features.json` is a reviewed allowlist, not arbitrary commits
supplied by the UI. Entries pin full commits, released Dev tags, exact source/test
paths and dependencies. The current sidebar changes are cumulative: order,
performance, grouping, compact rows, then utility sections. Their CSS/test
dependencies are explicit; later choices cannot silently bring earlier changes
along. Untested prerequisites block publishing. Future independent changes may
have no prerequisites.

Candidates start from the latest published stable tag, not master. Only selected
additions apply. Already-shipped features are retained via
`config/stable-promotion.json`; revising/removing their catalog entries is rejected.
Stable versions increment the previous stable patch independently of Dev's
sequence. No force pushes or overwrites are allowed.

The initial supported baseline is `v0.3.4`. An out-of-band stable release without a
compatible receipt requires reviewed baseline migration, not guessing. A source
or stable change after review forces refresh. Conflicts fail closed, without
automated resolution or unselected master changes.

### Adopted Baselines

A stable release published some other way (for example `v0.3.10`, shipped on
2026-09-25 from the `v0.3.10-dev.13` tree) is migrated by an `adoptedStable`
entry in the catalog: its tag, the exact commit the tag points at, a reason, and
the registered features that tree holds. Promotion then continues from it. The
window and `prepare-promotion.cjs` refuse the entry unless the tag still resolves
to that commit, and preparation checks every listed feature commit is an ancestor
of it. The first promotion after an adoption writes a normal receipt, so later
releases never need the entry again. Without an entry, the window says main was
published outside Promote to Main rather than reporting a sign-in failure.
Promotion window failures are also written to the Dev desktop log.

## Build And Publication Gates

`selective-promotion.yml` runs only on master, using one non-cancelling concurrency
group. Inputs reach scripts as data, never interpolated shell code. Preparation
checks dependencies, acknowledgements, source/Dev tag ancestry and every valid
registered subset. An immutable Git bundle carries the candidate to both builders.

Linux runs desktop, real browser/sidebar and native terminal/lifecycle tests,
builds the frozen backend and AppImage, and boots/shuts down the packaged shell.
Windows runs desktop tests and existing native PTY/Fleet/frozen sidecar smoke gates,
then builds NSIS. Neither platform job has write permission or publishes. The
finish job requires both successes, checks version, filenames, sizes and SHA-512
against both updater manifests, and rechecks the stable baseline.

Publish mode tags the candidate commit and uploads into a draft. Only a complete
draft becomes public/latest. Failed/partial uploads stay hidden from the stable
updater. A failed publish can leave an unpublished draft/tag: inspect that run and
remove only its unpublished draft/tag before retrying; never overwrite a published
version. Verify mode cannot create tags, releases or feed entries. Actions
artifacts expire after 14 days.

The window saves the request ID before dispatch. Lost replies cannot cause an
automatic duplicate. Reopening recovers status. **Dismiss tracking** does not cancel
a remote build; its native warning requires checking/cancelling the run on GitHub
before resubmitting. Failed gates leave main unchanged.

## Registering Features

1. Ship/test in Dev first. Keep commits scoped and separable.
2. Register immutable SHA, Dev tag and exact runtime/test paths. Exclude version
   churn, release plumbing and unrelated changes.
3. Declare prerequisites. Never revise entries already in stable receipts.
4. Run `node --test tests/promotion-composition.test.cjs` with full git history.
   Expand candidate regression gates when a feature touches more than the sidebar.
5. Run a `verify` workflow for the intended selection. Never test via stable publish.

The catalog is intentionally reviewed code. Calling arbitrary commits independent
features would provide a false safety guarantee.

Features spanning a reviewed fix series may declare an immutable `base` SHA.
Only the declared paths between that base and the feature commit are applied;
the base must be an ancestor. Single-commit entries still use the commit's parent.
Receipts retain both endpoints, and neither may change after a feature ships.

## Local Verification

```sh
npm --prefix apps/desktop ci
npm --prefix apps/desktop test
node --test tests/promotion-composition.test.cjs tests/promotion-publication.test.cjs
.venv/bin/python -m pytest tests/test_promotion_browser.py -q
```

Coverage includes desktop/mobile layout, dependency/testing gates, pending status,
source changes, native cancellation, duplicate clicks, lost dispatch replies and
strict sender/frame checks. Live proof must also run GitHub verify mode and confirm
the currently running main PID stays healthy.
