# Serena Ambient Tabs

A small MV3 extension for Edge (Raghav's default) that reports the active
tab's URL and title to the local ambient sensor over native messaging. No
page contents, no remote-debugging port (Chrome 136+ ignores it on the
default profile, and an open CDP port is an unauthenticated handle on
every tab).

## Install

1. `edge://extensions` → developer mode → load this unpacked folder.
2. Copy the extension id, then `./install.sh <extension-id>`.
3. Restart Edge. Verify with `chats ambient recent`.

## Privacy

- Incognito tabs are never reported (checked in `background.js` *and* the
  native host). Keep the extension disallowed in incognito (the default).
- Denylisted URLs (banks, logins, `.onion`) are dropped by the host before
  the store sees them.
- Tab updates are debounced (2 s) on both ends so a noisy page cannot flood
  the store.
