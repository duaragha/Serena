#!/usr/bin/env bash
# Install the Serena ambient native-messaging host for Edge/Chromium.
# Usage: ./install.sh <extension-id>
# Load the unpacked extension first (edge://extensions, developer mode) and
# copy its id. The id pins allowed_origins; no other origin can connect.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <extension-id>" >&2
  exit 2
fi
EXTENSION_ID="$1"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="$DIR/serena_ambient_host.py"
chmod +x "$HOST"

render() {
  sed -e "s|HOST_PATH|$HOST|" -e "s|EXTENSION_ID|$EXTENSION_ID|" \
    "$DIR/native-host-manifest.json"
}

for target in \
  "$HOME/.config/microsoft-edge/NativeMessagingHosts" \
  "$HOME/.config/chromium/NativeMessagingHosts" \
  "$HOME/.config/google-chrome/NativeMessagingHosts"; do
  mkdir -p "$target"
  render > "$target/com.serena.ambient.json"
  chmod 600 "$target/com.serena.ambient.json"
  echo "installed $target/com.serena.ambient.json"
done

echo "Restart Edge, then check: chats ambient recent"
