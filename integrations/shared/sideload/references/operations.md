# Install, update, migrate, back up, and restore

Read this for any mutation. Start with one live status snapshot and read `local-policy.md` for Raghav's phone.

## Inspect the IPA

Run:

```bash
python scripts/inspect_ipa.py /absolute/path/to/app.ipa --json
```

Resolve every hard failure before installation. Review warnings about encryption, app extensions, executable bits, mixed signing state, minimum iOS, background modes, and bundle identity.

For an update, compare the IPA's canonical bundle ID with the installed app's `ALTBundleIdentifier` when available. Do not use the signing-team-suffixed installed identifier as the developer's canonical ID.

## Choose native or LiveContainer guest

Prefer a native SideStore installation when the app requires any of these:

- remote push notifications;
- widgets, share extensions, notification extensions, or other SpringBoard-registered extensions;
- dependable background Bluetooth, location, audio, or processing;
- associated domains, VPN/network extensions, HealthKit, NFC, CarPlay, or other special entitlements;
- independent launch behavior and integration outside LiveContainer.

A LiveContainer guest is suitable when the app is primarily foreground-driven and its required capabilities survive LiveContainer's documented limitations. Saving a free slot is not proof of compatibility.

## Native SideStore installation

1. Confirm Wi-Fi and LocalDevVPN on the phone.
2. Confirm SideStore pairing and Apple authentication are healthy.
3. Confirm a native slot and any required App IDs are available before attempting installation.
4. Prefer a trusted HTTPS AltSource for ongoing updates. Otherwise import the already-inspected IPA.
5. For an update, install over the existing app without deleting it and preserve the canonical bundle ID.
6. Wait for signing and installation to finish once. Do not repeatedly tap install.
7. Launch the exact native bundle, verify important data/login, and verify the current provisioning profile when relevant.
8. Save a sanitized post-install status snapshot.

## LiveContainer guest installation

1. Confirm the LiveContainer host itself launches and JIT-Less Diagnose passes when using JIT-less mode.
2. Confirm its SideStore certificate is imported.
3. Inspect the IPA before import and confirm it is decrypted.
4. Import through LiveContainer's app picker.
5. Allow LiveContainer to patch/sign the guest once.
6. Verify the guest's `Info.plist`, `LCAppInfo.plist`, application bundle, and at least one data container are discoverable.
7. Launch it and verify its actual required features, not just its splash screen.
8. Record its automatically discovered identity in the sanitized snapshot. Do not add a manual registry entry.

## In-place update

1. Capture live status and back up the target's data.
2. Inspect the new IPA or validate the source entry and downloaded IPA.
3. Require the same canonical bundle ID unless the user explicitly requested a separate installation.
4. Update through the manager that currently owns the app.
5. Verify version/build, launch, local data, authentication, notifications/background behavior when applicable, and profile validity.
6. Retain the backup until the next normal launch/refresh cycle succeeds.

## Combined LiveContainer + SideStore migration

Use the official combined IPA only.

1. Back up all guest bundles and guest data containers from the current LiveContainer.
2. Keep the existing working SideStore available as the recovery path.
3. Install the combined IPA over the existing LiveContainer using `Keep All Extensions (Use Main Profile)` when preserving the one-slot arrangement.
4. Open SideStore using the button in LiveContainer, sign in on-device, connect LocalDevVPN, and run `Refresh All`.
5. Return to LiveContainer and import the certificate from SideStore.
6. Run JIT-Less Diagnose and launch representative guests.
7. Confirm the built-in SideStore can refresh the host and other native apps.
8. Only then may a redundant standalone SideStore be removed, and only when the user explicitly requested that removal.

Never delete standalone SideStore first. Never select per-extension App ID registration without calculating the resulting App ID usage.

## Backups

Resolve the exact device and bundle before copying anything.

- Native app: archive or copy its accessible application data container using the least invasive supported mechanism.
- LiveContainer: preserve `Documents/Applications`, `Documents/Data`, guest metadata plists, and any shared data the requested guests use.
- SideStore: preserve operational preferences and sanitized logs only when needed. Do not put pairing files, `.p12` files, passwords, private keys, cookies, or Apple authentication material into ordinary backups.

Create a private directory with restrictive permissions. Record:

- collection timestamp;
- short device fingerprint, never full UDID;
- canonical bundle ID and observed version;
- included and intentionally excluded paths;
- file count, byte count, and checksums;
- source installation mode.

Verify the backup can be listed/read before mutation. Never use recursive deletion to clean a mounted iPhone path. Unmount first and leave the mountpoint intact if unmounting fails.

## Restore

1. Preserve the current broken state separately; it may contain newer user data.
2. Match device, canonical bundle ID, installation mode, and container identity.
3. Restore data without restoring stale certificates, pairing files, anisette state, or signing-team-specific identifiers.
4. Re-sign/re-provision the app through the current SideStore installation.
5. Verify launch and important data before removing either copy.

## Uninstall

Uninstall is destructive. It requires an explicit user request naming the app.

1. Resolve and display the exact app name, canonical bundle ID, installation mode, and affected containers.
2. Produce and verify a backup unless the user explicitly requested permanent data destruction.
3. For a LiveContainer guest, distinguish removing the app bundle from deleting its data container.
4. For a native host, enumerate every dependent guest before considering removal.
5. Remove only the exact resolved target.
6. Run live status and report whether recovery remains possible.

## Source feeds

- Require HTTPS unless the source is an explicitly local development endpoint.
- Validate source JSON, app identity, version/build, download URL, and downloaded IPA together.
- Do not treat source metadata as proof of IPA contents.
- Never change a source's bundle identity merely to make an update appear.
- After publishing, fetch the public source and IPA and compare them with the intended release before telling the user an update is available.
