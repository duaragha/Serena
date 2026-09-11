# Desktop Workspace Backend Selection

The 0.2.47 desktop could reuse any healthy resident backend. A resident
mobile host running an older checkout therefore served the old terminal UI
even after the desktop was updated. Restarting that host did not update its
checkout.

Starting in 0.2.48, shared backend discovery requires the health response's
`capabilities.structuredWorkspace` to equal protocol version `1`. Missing,
disabled, or unknown versions fall back to the desktop's bundled sidecar.
The health capability is advertised only when the workspace is mounted.
Explicit `SERENA_STRUCTURED_WORKSPACE=0` retains legacy backend sharing.

Discovery only performs an HTTP health request. It does not stop the resident
host, close its terminals, or launch a provider. Existing session ownership
checks still reject sessions held by a live CLI; they are not duplicated or
silently migrated while working.

Verification on 2026-09-11:

- `node --test apps/desktop/tests/shared-backend.test.js`: exit 0, 14 passed.
- `npm test` in `apps/desktop`: exit 0, 79 passed.
- `/home/raghav/Documents/Projects/serena/.venv/bin/python -m pytest tests/test_workspace_mount.py -q`: exit 0, 3 passed.
- Read-only runtime probe against the actual resident backend: legacy discovery
  accepted PID 4074982; workspace-required discovery returned null. Exit 0;
  no processes launched or sessions modified.

These checks prove selection and mounted capability behavior, not that an
already-open desktop window has switched backend. The corrected desktop must
be installed through the normal updater before it makes the new selection.
