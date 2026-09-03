---
name: sideload
description: Inventory, diagnose, install, update, back up, operate, and repair every app managed by SideStore or LiveContainer, including screenshot-gated control of the connected iPhone. Use for IPA, iLoader, LocalDevVPN, pairing-file, signing, provisioning-profile, App ID, expiration, refresh, JIT-less, SideStore, or LiveContainer work. Auto-discovers native apps and LiveContainer guests; it never requires manual app registration.
---

# Sideload

Operate SideStore and LiveContainer from one self-contained workflow. Treat the connected iPhone and its current containers as truth. A cached snapshot or historical note is context, never proof of current state.

Codex invokes this skill as `$sideload`. Claude invokes the same skill as `/sideload`. Treat the remainder of the user's message as the command and arguments.

## Route the command

- No arguments or `status`: read [status and discovery](references/status-and-discovery.md), then run the live status collector.
- `diagnose`, `why`, `explain`, or another explicitly read-only question: collect status once, then read [repair matrix](references/repair-matrix.md). Report the exact failing layer without changing it.
- `fix`, `repair`, a pasted error, or a report that something is broken or not working, unless paired with the explicit analysis-only wording above: collect status, read [repair matrix](references/repair-matrix.md), diagnose the exact failing layer, then automatically apply and verify only the safe targeted repair matching positive evidence. Do not ask for a second “go ahead.”
- `install`, `update`, `migrate`, `source`, `backup`, `restore`, or `uninstall`: read [operations](references/operations.md). Inspect every IPA before signing or installation.
- A request whose result requires opening, tapping, swiping, or verifying an app on the phone: read [device control](references/device-control.md). Attempt live control before giving the user tap-by-tap instructions.
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
15. Do not stop at “do these taps” when the connected phone exposes CoreDevice screenshot and HID control. Probe once, inspect a fresh screenshot, perform one bounded action, and inspect the result before the next action.
16. The user must personally enter a device passcode, Apple Account password, two-factor code, or Trust confirmation. Never type, request, capture, or retain those secrets. Resume control after the protected prompt is complete.
17. Once the user has asked to fix the problem, or reported breakage without an analysis-only qualifier, continue from diagnosis through safe repair and live verification without another confirmation. This standing workflow does not authorize certificate revocation, app or data deletion, keychain cleaning, credential entry, publishing, or any other destructive or out-of-scope action.

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

Probe non-mutating on-device control:

```bash
python scripts/device_control.py probe --json
```

Capture a screen, launch an exact installed app, or perform screenshot-gated touch actions:

```bash
python scripts/device_control.py screenshot --output /private/path/before.png --json
python scripts/device_control.py launch LiveContainer --json
python scripts/device_control.py tap --screen /private/path/before.png --x 600 --y 2100 --json
```

Never copy coordinates from an example. Every touch must come from visual inspection of the fresh screenshot passed to that command. Delete transient screenshots after the operation is verified.

## Completion standard

Report:

- the exact failing layer and evidence;
- the single repair performed;
- which apps or data could have been affected;
- live post-repair evidence;
- anything still unverified because the phone was disconnected, CoreDevice control was proven unavailable, or the phone awaits a passcode, Apple credential, two-factor code, or Trust confirmation.

Do not bury the outcome in command transcripts.
