#!/bin/sh
# Private PulseAudio with the two call sinks, then the bridge in the foreground.
set -eu
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/phone-runtime}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
pulseaudio --daemonize=yes --exit-idle-time=-1 --disallow-exit \
  --disable-shm=yes -n -F /app/voice/phone/phone.pa
for _ in 1 2 3 4 5 6 7 8 9 10; do
  pactl info >/dev/null 2>&1 && break
  sleep 0.5
done
exec python3 -m voice.phone.bridge "$@"
