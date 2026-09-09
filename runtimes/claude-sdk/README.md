# Claude Workspace SDK

This pinned dependency runs Serena's structured Claude workspace through the
installed Claude CLI. It does not supply or replace authentication, start a
session during installation, or replace the normal terminal UI.

Provision from this directory:

```sh
npm ci --ignore-scripts --omit=optional --no-audit --no-fund
```

The workspace host checks the installed package name/version against this
manifest. Missing or mismatched dependencies fail explicitly, without switching
to a reduced-capability client. No installation happens during chat attachment.
The SDK worker always uses the installed CLI path supplied by the session owner.

`SERENA_WORKSPACE_RUNTIME_ROOT` may point to a packaged copy of this directory;
`SERENA_WORKSPACE_NODE` may select a provisioned Node executable. Neither changes
session ownership or enables the workspace UI. Desktop build recipes provision
and include this directory and the worker modules. The desktop passes its own
Electron executable; only the SDK worker receives `ELECTRON_RUN_AS_NODE=1`, not
the desktop/backend or the native CLI it starts. Completed frozen installer
verification is still pending; source and Linux Electron Node-mode proofs pass.
