# Local policy

Read this before changing Raghav's current iPhone installation. Live discovery overrides stale version numbers and app lists, but these placement and preservation requirements remain in force until Raghav changes them.

## Placement

- Unified is a standalone native app. Do not install or migrate it as a LiveContainer guest.
- Locket is preferably standalone because its background Bluetooth and notification behavior may not survive as a guest.
- LiveContainer and its built-in SideStore occupy one native slot together. Do not create a redundant standalone SideStore unless it is a temporary, verified recovery bridge.
- Preserve the current placement of every newly discovered or otherwise unknown app. Discovery does not authorize migration.

Machine-readable annotations are in `local-policy.json`. They enrich automatically discovered inventory. They are not the inventory and must never hide an app that is absent from the file.

## Identity and continuity

- Unified's canonical bundle ID is `dev.unifiedinbox.mobile`. SideStore may append a signing-team suffix to the installed ID; compare its `ALTBundleIdentifier` when available.
- Unified's SideStore source is `https://raw.githubusercontent.com/duaragha/unified-inbox-releases/main/sidestore-source.json`.
- Preserve bundle IDs during upgrades. Do not rename an identifier merely to remove old branding.
- An in-place update must retain the application container. Secure credentials can still become inaccessible when a free-profile re-sign changes a Keychain access group, so validate login/pairing after an update.

## Known healthy shape

The intended topology is one combined LiveContainer + SideStore host, Locket standalone, and Unified standalone. Do not force the phone back to this shape if live discovery shows that Raghav deliberately changed it. Report the difference first.

## Backups

Store device backups under Raghav's existing iPhone backup area when present. Create a uniquely named directory, record a sanitized manifest and checksums, and never copy pairing records, `.p12` files, passwords, private keys, or Apple credentials into a general project directory.
