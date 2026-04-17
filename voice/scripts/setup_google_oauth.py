"""Google Calendar OAuth2 setup script for Serena.

Run this script once to authorize Serena to read your Google Calendar.
It will open your browser for consent and save the credentials locally.

Usage:
    python -m serena.scripts.setup_google_oauth

Prerequisites:
    1. Go to https://console.cloud.google.com/
    2. Create a project (or select an existing one)
    3. Enable the Google Calendar API:
       - APIs & Services -> Library -> search "Google Calendar API" -> Enable
    4. Create OAuth 2.0 credentials:
       - APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
       - Application type: Desktop app
       - Name: Serena (or anything)
       - Download the JSON file
    5. Save the downloaded JSON as:
       ~/.config/serena/google_credentials.json
    6. Then run this script to complete the OAuth flow.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDENTIALS_PATH = Path.home() / ".config" / "serena" / "google_credentials.json"
TOKEN_PATH = Path.home() / ".config" / "serena" / "token.json"


def main() -> None:
    """Run the interactive OAuth2 setup flow."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "Error: Required packages not installed.\n"
            "Run: uv pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )
        sys.exit(1)

    print("=" * 60)
    print("  Serena — Google Calendar OAuth Setup")
    print("=" * 60)
    print()

    # Check for existing valid token
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds and creds.valid:
            print(f"Valid token already exists at {TOKEN_PATH}")
            print("Google Calendar is already configured.")
            return
        if creds and creds.expired and creds.refresh_token:
            print("Token expired, attempting refresh...")
            try:
                creds.refresh(Request())
                TOKEN_PATH.write_text(creds.to_json())
                print("Token refreshed successfully.")
                return
            except Exception as e:
                print(f"Refresh failed ({e}), re-authenticating...")

    # Check for client credentials file
    if not CREDENTIALS_PATH.exists():
        print("Client credentials file not found!")
        print()
        print("To set up Google Calendar access:")
        print()
        print("  1. Go to https://console.cloud.google.com/")
        print("  2. Create or select a project")
        print("  3. Enable the Google Calendar API:")
        print("     APIs & Services -> Library -> Google Calendar API -> Enable")
        print("  4. Create OAuth credentials:")
        print("     APIs & Services -> Credentials -> Create Credentials -> OAuth client ID")
        print("     - Application type: Desktop app")
        print("     - Name: Serena")
        print("  5. Download the JSON file and save it as:")
        print(f"     {CREDENTIALS_PATH}")
        print()
        print("Then run this script again.")
        sys.exit(1)

    # Ensure the config directory exists
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Run the OAuth flow
    print(f"Using credentials from: {CREDENTIALS_PATH}")
    print("Opening browser for Google authorization...")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)

    try:
        creds = flow.run_local_server(port=0)
    except Exception as e:
        print(f"Browser auth failed ({e}), trying console flow...")
        creds = flow.run_console()

    # Save the token
    TOKEN_PATH.write_text(creds.to_json())
    print()
    print(f"Token saved to: {TOKEN_PATH}")
    print()

    # Verify by listing upcoming events
    try:
        from googleapiclient.discovery import build

        service = build("calendar", "v3", credentials=creds)
        result = (
            service.events()
            .list(calendarId="primary", maxResults=3, singleEvents=True, orderBy="startTime")
            .execute()
        )
        events = result.get("items", [])
        if events:
            print("Verification successful! Your upcoming events:")
            for event in events:
                start = event["start"].get("dateTime", event["start"].get("date"))
                print(f"  - {event.get('summary', '(No title)')} ({start})")
        else:
            print("Verification successful! (No upcoming events found)")
    except Exception as e:
        print(f"Token saved but verification failed: {e}")
        print("This might still work — try running Serena.")

    print()
    print("Google Calendar setup complete.")


if __name__ == "__main__":
    main()
