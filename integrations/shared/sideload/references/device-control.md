# On-device control

Use this whenever completing or verifying the requested outcome crosses into the iPhone UI. The default is to operate the connected phone, not to turn known taps into homework for the user.

The helper uses pymobiledevice3's iOS 17+ CoreDevice userspace tunnel. It captures the real display and drives the virtual HID touchscreen without installing WebDriverAgent or consuming a native app slot.

## Establish control

Run this once from the real skill directory:

```bash
python scripts/device_control.py probe --json
```

`ok: true` proves, live in the current run, that the phone is uniquely resolved, its active display is readable, a screenshot can be captured, and the touchscreen surface is available. A cached status snapshot does not prove control.

If the probe reports a missing developer disk image and the user's request requires UI operation, mount the matching image once with the current official pymobiledevice3 release, then re-run the probe:

```bash
uvx --no-progress pymobiledevice3 mounter auto-mount --userspace
```

Do not install WebDriverAgent as a fallback. It consumes a native slot and is unnecessary for this path.

## Safe interaction loop

For every screen transition:

1. Launch the exact native host when useful: `python scripts/device_control.py launch LiveContainer --json`.
2. Capture a new screen to a private path with `screenshot`.
3. Inspect that PNG visually. Resolve controls from what is visible now, never from remembered coordinates or a prior device.
4. Perform exactly one `tap` or `swipe`, passing that same screenshot with pixel coordinates.
5. Inspect the automatically captured `after.screenshot` before acting again.
6. Stop if the expected transition did not occur. Diagnose the current screen instead of repeating the action.
7. Delete transient screenshots as soon as the result is verified. They can contain private app content.

The helper rejects screenshots older than three minutes, coordinates outside the image, dimension changes, ambiguous devices, ambiguous app names, and overwrite attempts. It emits only a short device fingerprint, never the full device identifier.

## Secret and destructive boundaries

Pause only for a device passcode, Apple Account password, two-factor code, or Trust confirmation. Tell Raghav the single protected action needed, let him perform it on the phone, then continue from a fresh screenshot.

Never type or capture those secrets. Never approve certificate revocation, app deletion, container deletion, `Clean Keychain`, `Clean unused data folders`, `Delete Data`, or `Remove Container` merely because a button is visible. Destructive actions still require the explicit scope required by this skill.

## Refresh one affected app

When evidence identifies one stale or revoked signing identity, refresh only the affected app unless every app signed by that identity needs replacement:

1. Collect the sanitized live inventory and note every native app sharing the affected signing state.
2. Preserve the affected app's data before any operation that may replace its container.
3. Probe control, launch LiveContainer, and inspect the resulting screenshot.
4. Open the built-in SideStore from LiveContainer.
5. Go to `My Apps` and locate the exact target by its displayed and canonical identity.
6. Tap that app's own refresh control. Do not use `Refresh All` merely for convenience.
7. Wait for a terminal success or exact error, checking screenshots between transitions. Never repeatedly tap refresh.
8. Re-run live inventory, verify the target's current profile/signing state using the official refresh-verification flow when relevant, and launch the exact app.
9. Confirm the original launch or refresh failure is gone before reporting success.

This is the workflow that should handle a case such as Locket retaining an old revoked certificate while LiveContainer and Unified already use the replacement certificate. The correct action is the exact Locket refresh inside the built-in SideStore, followed by profile and launch verification, not a broad reinstall or a manual handoff.

## When control really is unavailable

Manual instructions are the last fallback. Use them only after the live probe proves one of these blockers:

- the phone is disconnected or cannot be uniquely resolved;
- host trust is invalid and requires an on-device Trust confirmation;
- the phone is locked and requires its passcode;
- CoreDevice screenshot or touchscreen service remains unavailable after the bounded official recovery;
- the next screen requires an Apple credential or two-factor code.

State the exact blocker and the one user action that clears it. Once cleared, resume device control rather than handing over the rest of the workflow.
