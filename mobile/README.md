# Serena mobile (Capacitor + React + Vite)

Android client for the Serena/`chats` daemon. Talks to the daemon over a
WebSocket using the protocol in `src/types.ts`. Ships with a **mock daemon**
(`src/mock.ts`) so the whole app runs with **zero PC**.

## Run it now (no PC, no Android SDK)

```bash
cd mobile
npm install        # if node_modules isn't present
npm run dev        # open the printed URL in a browser (or phone on same wifi)
```

It boots in **mock mode** — real session list, threads, streaming replies, new
chats — all in-memory. Settings (⚙) → toggle "Use mock daemon" off and enter a
server URL + token to hit the real daemon instead. Settings persist in
localStorage.

## Architecture

- `src/types.ts` — the wire protocol (client↔daemon messages). Source of truth.
- `src/transport.ts` — `WebSocketTransport` (real) behind a `Transport` interface.
- `src/mock.ts` — `MockTransport`, an in-memory daemon implementing the protocol.
- `src/store.tsx` — React context: builds the transport from settings, maps
  incoming `ServerMsg` → UI state, exposes actions (open/send/new/refresh).
- `src/components/` — `SessionList`, `ChatThread`, `MessageBubble`, `Composer`,
  `SettingsScreen`, `ConnectionBadge`.

## Remaining steps (on the PC)

1. **Daemon protocol.** Make the Flask daemon (`ui/web.py`) serve `/ws/chat`
   speaking `src/types.ts`. (Can be pre-written without the PC running.)
2. **Headless + auth.** `chats serve` headless entrypoint + token check on every
   connection. Bind the tailnet/tunnel interface, not `127.0.0.1`.
3. **Reachability.** Tailscale on PC + phone (default), or Cloudflare Tunnel.
4. **Build the APK** (needs Android Studio / SDK on the build machine):
   ```bash
   npm run build
   npx cap add android
   npx cap sync
   npx cap open android   # build/sign APK in Android Studio, then sideload
   ```
5. In the app: Settings → server URL = `http://<tailnet-ip>:<port>/ws/chat`,
   token = the daemon secret, mock off.

> **Don't sync `node_modules`.** It's gitignored, but make sure Syncthing also
> ignores it (`.stignore`) — run `npm install` fresh on the PC instead.
