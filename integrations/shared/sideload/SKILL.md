---
name: sideload
description: Inventory, diagnose, install, update, back up, and repair every app managed by SideStore or LiveContainer. Use for IPA, iLoader, LocalDevVPN, pairing-file, signing, provisioning-profile, App ID, expiration, refresh, JIT-less, SideStore, or LiveContainer work. Auto-discovers native apps and LiveContainer guests; it never requires manual app registration.
---

# Sideload

Operate SideStore and LiveContainer from one self-contained workflow. Treat the connected iPhone and its current containers as truth. A cached snapshot or historical note is context, never proof of current state.

Codex invokes this skill as `$sideload`. Claude invokes the same skill as `/sideload`. Treat the remainder of the user's message as the command and arguments.

## Route the command

- No arguments or `status`: read [status and discovery](references/status-and-discovery.md), then run the live status collector.
- `diagnose`, an error message, or a question about a failure: collect status once, then read [repair matrix](references/repair-matrix.md). Diagnose the exact failing layer before changing anything.
- `repair`: collect status, read [repair matrix](references/repair-matrix.md), and apply only the repair matching positive evidence.
- `install`, `update`, `migrate`, `source`, `backup`, `restore`, or `uninstall`: read [operations](references/operations.md). Inspect every IPA before signing or installation.
- Any mutation involving Raghav's known apps: also read [local policy](references/local-policy.md).
- When current upstream behavior matters, consult only the maintained primary links in [official sources](references/official-sources.md).

Resolve this `SKILL.md` to its real directory before invoking scripts. Symlinked Claude and Codex entrypoints both point here.

## Non-negotiable operating rules

1. Auto-discover all connected devices, developer-signed native apps, SideStore instances, LiveContainer hosts, guest apps, and guest data containers. Never ask the user to maintain an inventory.
2. Start with one bounded read-only snapshot. Reuse it for the current operation instead of repeatedly rescanning unchanged state.
3. Separate these layers: host trust/USB, Wi-Fi and LocalDevVPN, SideStore pairing, Apple authentication/anisette, certificate, provisioning profile, IPA structure, native installation, LiveContainer host, and guest compatibility.
4. Make one causally relevant change, then verify the original symptom. Repeated blind installs can consume the rolling App ID allowance.
5. Preserve the existing bundle ID for updates. A visible app-name change does not justify changing its identifier.
6. Back up the exact affected container before an operation that could replace, delete, migrate, clean, revoke, or re-sign data. Verify the backup is readable before proceeding.
7. Never delete the old installation until its replacement launches, its important data is present, and its refresh/signing path works.
8. Never print, retain in logs, or cache Apple passwords, two-factor codes, pairing-record contents, private keys, certificates, bearer tokens, full device identifiers, Apple Account emails, or signing-team identifiers. The user enters Apple credentials directly on the phone.
9. Never revoke a certificate as a generic fix. First enumerate every app it signs and state that all of them will require refresh or re-signing. A generic `repair` request does not authorize certificate revocation, app deletion, container deletion, or keychain cleaning unless the user explicitly included that destructive action.
10. Never use LiveContainer's `Clean Keychain`, `Clean unused data folders`, `Delete Data`, or `Remove Container` as troubleshooting shortcuts. Those are destructive operations.
11. Prefer official stable SideStore and LiveContainer releases. Use a nightly only when the installed iOS version or a confirmed upstream issue requires it, and preserve an exit path back to stable.
12. Treat LiveContainer guests as less capable than native apps. Remote push, app extensions, original entitlements, background execution, and URL schemes may not work. Do not move an app into LiveContainer solely to save a slot when those capabilities matter.
13. Do not call a profile refreshed merely because SideStore reported success. When expiry or missing entitlements are in dispute, verify the current profile using the method in the official LiveContainer refresh guide.
14. Do not claim a repair from source inspection or an IPA build alone. Verify on the physical device when the requested outcome is device behavior.

## Deterministic helpers

Run the status collector with the Python from `machine_context.py` when available:

```bash
python scripts/sideload_status.py --json
```

For an IPA supplied by path:

```bash
python scripts/inspect_ipa.py /absolute/path/to/app.ipa --json
```

Both helpers are read-only. The status cache is sanitized and contains no full UDID, credential, key, certificate, or pairing-record content.

## Completion standard

Report:

- the exact failing layer and evidence;
- the single repair performed;
- which apps or data could have been affected;
- live post-repair evidence;
- anything still unverified because the phone was disconnected, locked, or awaiting an on-device action.

Do not bury the outcome in command transcripts.
