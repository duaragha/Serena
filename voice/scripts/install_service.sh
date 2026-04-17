#!/bin/bash
# Install Serena as a systemd user service
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_FILE="$PROJECT_DIR/systemd/serena.service"

if [ ! -f "$SERVICE_FILE" ]; then
    echo "Error: $SERVICE_FILE not found" >&2
    exit 1
fi

mkdir -p ~/.config/systemd/user
cp "$SERVICE_FILE" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable serena
echo "Serena service installed. Start with: systemctl --user start serena"
