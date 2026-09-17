# Serena's phone line

Serena has her own free SIP account (`its_serena@sip.linphone.org`). Raghav
runs the Linphone app on his phone as `its_raghav`, so she can ring him and he
can ring her. Both directions become an ordinary hands-free voice
conversation with her.

```text
his phone (Linphone app, push + CallKit)
  <-> sip.linphone.org (TLS signalling, ZRTP-encrypted media)
  <-> serena-phone container in the PC's Docker-Ubuntu VM   (voice/phone/bridge.py)
        PulseAudio null sinks phone_rx / phone_tx, parec / pacat
  <-> ws://10.0.2.2:8766/ws/desk  (the PC's loopback through VirtualBox NAT)
  <-> voice host on Windows: `chats phone voice-host`
        faster-whisper + Silero VAD, resident brain, Pocket TTS container
```

## Pieces

- `bridge.py` owns the Linphone core, answers only `RAGHAV_SIP` over the
  provider's authenticated TLS connection (no plain SIP listeners), places
  calls on request, and runs one desk session per call. Sustained speech from
  him while she talks interrupts her.
- The official liblinphone wheel has no sound card backend. The image keeps
  its Python wrapper and swaps in the Linphone desktop AppImage's libraries,
  which are built with PulseAudio.
- `core/phone_call.py` is the PC-side client (`chats phone call|line|hangup`).
  It rate-limits calls to one per ten minutes.
- `notify(..., channel="call")` rings through the notification authority. The
  task dispatcher (`serena.fleet.reconcile`) rings when a task finishes or
  gets stuck, outside quiet hours, on top of the text that always goes out.

## Deploy

Secrets, outside every synced tree:

| where | file | contents |
| --- | --- | --- |
| VM `~/serena-phone/` | `sip.env` | `SIP_USER`, `SIP_PASSWORD`, `SIP_DOMAIN`, `RAGHAV_SIP` |
| VM `~/serena-phone/` | `chat_token` | copy of the PC's `~/.config/serena/chat_token` |
| VM `~/serena-phone/` | `control_token` | same value as the PC's `~/.config/serena/phone-call-token` |

The PC forwards `127.0.0.1:8796` to the VM (`VBoxManage controlvm
Docker-Ubuntu natpf1 "phonectl,tcp,127.0.0.1,8796,,8796"`), and runs the
voice host as the `Serena Voice Host` scheduled task through
`run-service.ps1 -Name voice-host phone voice-host`.

### Control API

The phone bridge control API listens on `127.0.0.1:8796` on the PC,
forwarded from the Docker VM. It accepts `POST /call` (with `{"text": "..."}`,
the line she opens with), `POST /hangup`, and `GET /health`. All three require
an `Authorization: Bearer <token>` header, using the token from
`~/.config/serena/phone-call-token` on the PC.

## Debugging

Set these in the VM shell before `docker compose up -d`:

- `PHONE_LINPHONE_LOG=message` prints SIP and ICE traces.
- `PHONE_DEBUG_AUDIO=/data/debug` records what the voice host hears, per call.
- `PHONE_SIP_LISTEN=tcp` plus `PHONE_ALLOWED_CALLERS=its_serena` lets a test
  caller dial the bridge directly or through the provider. Never leave these
  on: they widen who can reach her.

Linphone's ZRTP code shows on his phone the first time; once he marks it
verified the app remembers her, because her keys live in the `/data` volume.
