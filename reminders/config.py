import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

MY_PHONE_NUMBER = os.getenv("MY_PHONE_NUMBER", "")

NTFY_ALERT_TOPIC = os.getenv("NTFY_ALERT_TOPIC", "")
NTFY_INPUT_TOPIC = os.getenv("NTFY_INPUT_TOPIC", "")

GOOGLE_TASKS_LIST = os.getenv("GOOGLE_TASKS_LIST", "Claude")

DB_PATH = os.path.expanduser(os.getenv("DB_PATH", "~/Documents/Projects/serena/reminder_system/reminders.db"))

GOOGLE_CREDS_PATH = Path(__file__).parent / "credentials.json"
GOOGLE_TOKEN_PATH = Path(__file__).parent / "token.json"

POLL_INTERVAL = 30  # seconds
