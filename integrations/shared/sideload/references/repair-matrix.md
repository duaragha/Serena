# Evidence-led repair matrix

Use this for `diagnose`, `repair`, or any pasted SideStore/LiveContainer error. Run the status collector once first. Match the error to one layer and change only that layer. When the matching repair enters the phone UI, follow [device control](device-control.md) instead of defaulting to manual tap instructions.

## 1. Transport and host trust

Evidence:

- `idevice_id -l` cannot see a USB-connected phone;
- `idevicepair validate` fails;
- `ideviceinfo` reports a lock/trust error.

Response:

1. Confirm the exact cable/device is visible before changing software.
2. If `usbmuxd` is installed, check whether it is active. Restart it only when inactive or demonstrably wedged.
3. Have the user unlock the phone and accept the Trust prompt only if trust is actually missing.
4. Do not replace SideStore's pairing file until host trust works. These are separate records.

## 2. Wi-Fi and LocalDevVPN

Evidence:

- SideStore `1414`;
- minimuxer/AFC `27` accompanied by no VPN endpoint;
- recent SideStore log says Wi-Fi, StosVPN, SideVPN, or LocalDevVPN is unavailable.

Response:

1. Require Wi-Fi, not cellular data alone.
2. Connect LocalDevVPN.
3. Retry once.
4. Only move to pairing-file repair if the same error persists.

Do not infer current VPN state from an old successful log entry. Report its timestamp.

## 3. SideStore pairing file

Evidence:

- SideStore `1006`;
- minimuxer/AFC `27` after Wi-Fi and LocalDevVPN are verified;
- SideStore explicitly reports a missing, corrupt, expired, or invalid pairing file;
- the collector reports that the stored pairing file is malformed or belongs to another device.

Preferred official recovery:

1. Probe on-device control and open SideStore Settings; use `Reset Pairing File` when available.
2. In current iLoader, delete the stored pairing.
3. Connect by USB, unlock, and trust the computer.
4. Refresh iLoader's device list, open `Manage Pairing File`, and place the record into SideStore or all intended apps.
5. Fully quit and reopen SideStore, connect LocalDevVPN, then retry once through the screenshot-gated interaction loop.

Never print or copy the pairing plist into chat. Do not hand-edit or inject a lockdown plist unless the official flow is unavailable and the exact required schema has been independently verified. A host pairing validating successfully does not prove SideStore's embedded pairing file is valid.

On iOS 26.4 or newer, check the current SideStore error documentation before assuming stable and nightly have identical minimuxer support.

## 4. Apple authentication and anisette

Evidence and response:

- `1412`: the selected v3 anisette service is unreachable. Retry once or select another official v3 server.
- `3021`: anisette data is invalid. Verify date/time, server health, and authenticate again.
- `1100`: Apple session expired. Sign in again on the phone.
- `3002`, `-20101`, `3018`, or `3019`: resolve credentials or two-factor authentication on the phone. Never request those values in chat.
- `-1011`: sign into the Apple developer site and accept current terms.
- `1102`: verify developer terms and account eligibility.

`-45061` is not documented in SideStore's maintained error table. On Raghav's prior installation it correlated with stale on-device anisette state. Treat that as a lead, not a universal diagnosis: preserve the relevant SideStore state, prove the same stale-state signature in current logs, clear only the anisette artifact, then reauthenticate. Do not delete certificates, app data, or pairing records with it.

## 5. Free-account capacity

Evidence:

- iOS lists three developer-signed native apps and rejects a fourth;
- SideStore `1009` or Apple developer `3013`;
- SideStore reports no App IDs remaining.

Rules:

- The free-account native-app cap is three including SideStore.
- The separate rolling allowance is ten newly registered App IDs in seven days.
- App IDs cannot be manually freed early. Deleting their display entries does not release the Apple-side allowance.

Response:

- Native-app cap: preserve data, then deliberately choose an app to move into LiveContainer or remove. Do not assume a remaining-App-ID count means a native slot is free.
- App-ID limit: stop retries and wait for the recorded IDs to expire, or use a LiveContainer guest when its limitations are acceptable.
- Combined LiveContainer + SideStore migrations must use `Keep All Extensions (Use Main Profile)` when the intent is to retain the one-slot topology. Registering every extension can consume multiple App IDs.

## 6. Certificate state

Evidence:

- JIT-Less Diagnose says certificate revoked or missing;
- Apple developer error `3008`;
- every app signed by the same old certificate stops launching.

Response:

1. Enumerate every affected native app first.
2. Preserve their containers.
3. Use [device control](device-control.md) to refresh or re-sign each affected app through the SideStore instance that owns it. If only one app still carries the dead identity, refresh that app specifically rather than choosing `Refresh All`.
4. Re-read the installed profiles/signing state and verify each app launches afterward. A SideStore success banner by itself is not proof.

Never revoke a certificate merely to make a dialog disappear. Revocation can invalidate all apps sharing it. If Apple forces replacement, keep a working SideStore path alive until every affected app is refreshed.

## 7. Provisioning profile and expiry

Evidence:

- `App Is No Longer Available`;
- an app closes immediately after the seven-day window;
- StikDebug `App Expiry` shows an expired profile or missing entitlements.

Response:

1. Use [device control](device-control.md) to refresh the exact app in SideStore.
2. Verify the newly installed profile and required entitlements with StikDebug when the result matters.
3. If the refreshed profile omits required entitlements, reinstall the same IPA through SideStore so its entitlement record is rebuilt, then verify again.

Do not use the certificate expiry date as the profile expiry date. Free-account certificates and provisioning profiles have different lifetimes.

## 8. IPA format, FairPlay, and signing

Always run `scripts/inspect_ipa.py` before retrying an IPA.

- SideStore `1007` or `App returned from its main function with code 0`: require a standard IPA with one `Payload/*.app`, a readable `Info.plist`, and a real executable.
- `could not register fairplay decryption` or `mremap_encrypted() => -1`: the IPA is probably still App Store encrypted. Obtain a properly decrypted IPA; re-zipping does not decrypt it.
- `ldid.cpp(...)`: inspect structure, executable modes, nested bundles, and header space first. Use the current workaround from SideStore's official error page only after preserving the original IPA.
- Avoid `__MACOSX`, AppleDouble `._*` entries, path traversal, or an application nested below an extra top-level directory.
- Preserve executable mode bits in the ZIP.
- Do not blame `cp -R` versus `ditto` without evidence. The actual failure is usually IPA layout, encryption, entitlements, a nested signing target, or current certificate state.

## 9. LiveContainer host and certificate import

Evidence and response:

- `Certificate not found`: use [device control](device-control.md) to open built-in SideStore, sign in, connect LocalDevVPN, refresh every app actually affected by the missing certificate, then import its certificate from LiveContainer Settings. Use `Refresh All` only when the evidence covers all managed apps.
- If direct import fails, use LiveContainer's documented export-from-SideStore/import-into-LiveContainer fallback and keep the export private.
- `executable was signed with invalid entitlements`: update to the minimum current SideStore/LiveContainer versions required by the official guide before repairing individual guests.
- `signed with latest certificate but code signature is invalid`: run JIT-Less Diagnose. If it passes, force re-sign only the affected guest.

If a log names one nested framework or extension as invalid, repair inside-out: nested dylibs/frameworks, extensions, then the containing app. This is an expert recovery, not a blanket action. Preserve the exact installed bundle first and verify the final signature on-device before replacing it.

## 10. LiveContainer guest compatibility

Evidence and response:

- Guest import crashes: inspect the IPA structure and signer compatibility.
- Guest launch returns FairPlay/decryption errors: use a decrypted IPA.
- Guest still crashes: check the current compatibility issues, then install the same IPA natively through SideStore as a discriminator when a slot is available.
- File picker fails: enable the guest-specific `Fix File Picker` option.
- Local notification permission blocks the app: enable `Fix Local Notifications` while acknowledging this does not add remote push.
- Push, extensions, background modes, special entitlements, or URL-scheme integration are required: prefer a native install. LiveContainer cannot recreate capabilities SpringBoard never registered for the guest.

## 11. Missing or orphaned LiveContainer data

Use the collector's guest/data-container mapping. Do not run `Clean unused data folders`.

1. Back up `Documents/Applications` and `Documents/Data` from the exact LiveContainer host.
2. Match `LCAppInfo.plist` and `LCContainerInfo.plist` by application identifier.
3. Treat unreferenced folders as preserved unknown data until their ownership is proven.
4. Restore or rebind only the exact container requested.

`Clean Keychain`, `Delete Data`, and `Remove Container` are destructive and never diagnostic actions.

## 12. Update lost login or local state

1. Confirm the update used the same canonical bundle ID.
2. Confirm the old application container still exists before reinstalling.
3. Separate missing files from inaccessible Keychain items or changed access groups.
4. Preserve both the current and previous container state before attempting migration.
5. Do not delete/reinstall as a first response; that destroys the best recovery evidence.

## Stop conditions

Stop a repair rather than cascading when:

- the live device cannot be uniquely resolved;
- a required backup is incomplete or unreadable;
- the next step would revoke a certificate or delete data without explicit user direction;
- evidence points to an upstream iOS/SideStore/LiveContainer incompatibility rather than local corruption;
- the app depends on an entitlement unavailable to the chosen installation mode.
