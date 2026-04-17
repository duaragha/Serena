#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_NAME="reminder-daemon"

echo "=== Reminder System Setup ==="

# Create virtual environment
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"

# Create .env from example if it doesn't exist
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo ""
    echo "Created .env file — edit it with your credentials:"
    echo "  $PROJECT_DIR/.env"
    echo ""
fi

# Create symlink for CLI
echo "Setting up 'remind' CLI command..."
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/remind" << EOF
#!/bin/bash
exec "$VENV_DIR/bin/python" "$PROJECT_DIR/cli.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/remind"

# Create systemd user service
echo "Creating systemd user service..."
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/$SERVICE_NAME.service" << EOF
[Unit]
Description=Reminder Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_DIR/bin/python $PROJECT_DIR/main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $PROJECT_DIR/.env with your credentials"
echo "  2. Set up Google Tasks API credentials (see below)"
echo "  3. Start the daemon: systemctl --user start $SERVICE_NAME"
echo "  4. Check status: systemctl --user status $SERVICE_NAME"
echo "  5. View logs: journalctl --user -u $SERVICE_NAME -f"
echo ""
echo "Google Tasks API setup:"
echo "  1. Go to https://console.cloud.google.com"
echo "  2. Create a project (or use existing)"
echo "  3. Enable 'Google Tasks API'"
echo "  4. Go to Credentials > Create Credentials > OAuth 2.0 Client ID"
echo "  5. Application type: Desktop app"
echo "  6. Download the JSON and save as: $PROJECT_DIR/credentials.json"
echo "  7. Run once to authenticate: $VENV_DIR/bin/python -c 'from inputs.google_tasks import poll_google_tasks; poll_google_tasks()'"
echo ""
echo "CLI usage:"
echo "  remind add 'get chili flakes' --when payment"
echo "  remind add 'take out trash' --at '7pm'"
echo "  remind add 'call mom' --in '20 minutes'"
echo "  remind list --pending"
echo "  remind cancel 3"
