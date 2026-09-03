import logging
from pathlib import Path
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from config import GOOGLE_CREDS_PATH, GOOGLE_TOKEN_PATH, GOOGLE_TASKS_LIST
from parser import parse_reminder
from db import add_reminder

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/tasks"]

_list_id_cache: str | None = None


def _get_service():
    creds = None
    if GOOGLE_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GOOGLE_CREDS_PATH.exists():
                log.error(f"Google credentials not found at {GOOGLE_CREDS_PATH}")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(str(GOOGLE_CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        GOOGLE_TOKEN_PATH.write_text(creds.to_json())

    return build("tasks", "v1", credentials=creds)


def _get_list_id(service) -> str | None:
    global _list_id_cache
    if _list_id_cache:
        return _list_id_cache

    results = service.tasklists().list().execute()
    for tl in results.get("items", []):
        if tl["title"].lower() == GOOGLE_TASKS_LIST.lower():
            _list_id_cache = tl["id"]
            return _list_id_cache

    # Create the list if it doesn't exist
    new_list = service.tasklists().insert(body={"title": GOOGLE_TASKS_LIST}).execute()
    _list_id_cache = new_list["id"]
    log.info(f"Created Google Tasks list: {GOOGLE_TASKS_LIST}")
    return _list_id_cache


def poll_google_tasks():
    """Poll Google Tasks for new items, parse them as reminders, delete after processing."""
    try:
        service = _get_service()
        if not service:
            return

        list_id = _get_list_id(service)
        if not list_id:
            return

        results = service.tasks().list(
            tasklist=list_id,
            showCompleted=False,
            showHidden=False,
        ).execute()

        tasks = results.get("items", [])
        if not tasks:
            return

        for task in tasks:
            title = task.get("title", "").strip()
            if not title:
                continue

            log.info(f"New Google Task: {title}")
            parsed = parse_reminder(title)
            rid = add_reminder(
                message=parsed.message,
                trigger_type=parsed.trigger_type,
                trigger_at=parsed.trigger_at,
                source="google_tasks",
            )
            log.info(f"Reminder #{rid} created: {parsed.trigger_type} — {parsed.message}")

            # Delete the task so we don't process it again
            service.tasks().delete(tasklist=list_id, task=task["id"]).execute()

    except Exception as e:
        log.error(f"Google Tasks poll error: {e}")
