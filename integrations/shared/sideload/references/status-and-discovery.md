# Status and automatic discovery

Use this mode for `status`, as the first step of `diagnose`, and once before a repair or installation.

## Run

From the real skill directory:

```bash
python scripts/sideload_status.py --json
```

The collector uses standard `libimobiledevice` tools when present. It:

- discovers every USB-attached iPhone automatically, with network discovery only as a fallback or when requested;
- validates the host trust relationship;
- reads the device model and iOS version;
- enumerates developer-signed native apps without printing the Apple Account or signing-team identity;
- recognizes SideStore-managed identities through `ALTBundleIdentifier` when available;
- mounts each accessible SideStore or LiveContainer container read-only;
- identifies a combined LiveContainer + SideStore installation;
- enumerates every LiveContainer guest from `Documents/Applications/*.app`;
- maps guest data containers from `LCContainerInfo.plist` without exposing their UUIDs;
- validates only the structure and device match of the SideStore pairing file without exposing its contents;
- classifies recent SideStore log signals without reproducing raw logs, IP addresses, identifiers, or credentials;
- compares the sanitized live inventory with the previous sanitized snapshot and reports additions, removals, and version changes.

It never requires an app registry entry. `local-policy.json` only annotates matching apps with known preservation requirements.

## Truth labels

- `live`: observed from the connected device during this run.
- `cached`: last sanitized successful snapshot. Always state its timestamp and never use it to justify a mutation.
- `unknown`: the transport cannot prove the fact. Do not turn unknown into healthy or broken.

The installation proxy cannot prove the exact current provisioning-profile expiry for every app. If expiry or entitlement coverage matters, use LiveContainer's documented StikDebug `Tools -> App Expiry` verification. SideStore's success message alone is insufficient.

## Device unavailable

If the phone is missing or locked, show the cached snapshot if present and identify the one missing prerequisite. Do not repeatedly restart services or ask a list of generic questions. For a live repair, wait for only what is required: cable/trust, unlock, Wi-Fi, or LocalDevVPN.

## Multiple devices

`status` may scan all attached devices. Use `--device <UDID>` internally only when a mutation must target one exact phone. Never put that UDID in the response, skill files, cache, or logs; refer to the collector's short device fingerprint instead.
