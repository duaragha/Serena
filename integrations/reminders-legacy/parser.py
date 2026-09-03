import re
from datetime import datetime, timezone
from dataclasses import dataclass
import dateparser


@dataclass
class ParsedReminder:
    message: str
    trigger_type: str  # 'time' | 'payment' | 'immediate'
    trigger_at: datetime | None = None


# Patterns for event-based triggers
PAYMENT_PATTERNS = [
    r"when\s+(?:i|I)\s+pay",
    r"after\s+(?:i|I)\s+pay",
    r"after\s+(?:paying|payment)",
    r"when\s+(?:i|I)\s+(?:make\s+)?(?:a\s+)?payment",
    r"after\s+(?:i|I)\s+(?:make\s+)?(?:a\s+)?payment",
    r"once\s+(?:i|I)\s+pay",
    r"after\s+(?:i|I)\s+(?:check\s*out|checkout)",
]

# Patterns for extracting the reminder message
REMIND_PREFIX = re.compile(
    r"^(?:remind\s+me\s+(?:to\s+)?|remember\s+(?:to\s+)?|don'?t\s+forget\s+(?:to\s+)?)",
    re.IGNORECASE,
)

# Time trigger patterns — match and remove these from the message
TIME_PATTERNS = [
    re.compile(r"\b(?:at|by)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?)\b", re.IGNORECASE),
    re.compile(r"\bin\s+(\d+\s*(?:min(?:ute)?s?|hours?|hrs?))\b", re.IGNORECASE),
    re.compile(r"\b(tomorrow(?:\s+(?:at|by)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)\b", re.IGNORECASE),
    re.compile(r"\b(tonight(?:\s+(?:at|by)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)\b", re.IGNORECASE),
]


def parse_reminder(text: str) -> ParsedReminder:
    text = text.strip()

    # Strip "remind me to" prefix
    text = REMIND_PREFIX.sub("", text).strip()

    # Check for payment trigger
    for pattern in PAYMENT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            message = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
            message = _clean_message(message)
            return ParsedReminder(message=message, trigger_type="payment")

    # Check for time-based trigger
    time_str = None
    clean_text = text

    for pattern in TIME_PATTERNS:
        match = pattern.search(text)
        if match:
            time_str = match.group(0)
            clean_text = pattern.sub("", text).strip()
            break

    # Also try to find time at the beginning: "at 7pm get chili flakes"
    if not time_str:
        leading_time = re.match(r"^(at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+", text, re.IGNORECASE)
        if leading_time:
            time_str = leading_time.group(1)
            clean_text = text[leading_time.end():].strip()

    if time_str:
        parsed_time = dateparser.parse(
            time_str,
            settings={
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "TIMEZONE": "America/Toronto",
                "TO_TIMEZONE": "UTC",
            },
        )
        if parsed_time:
            message = _clean_message(clean_text)
            return ParsedReminder(message=message, trigger_type="time", trigger_at=parsed_time)

    # No trigger found — try parsing the whole text as containing a time reference
    # dateparser can handle "get milk tomorrow" etc.
    parsed_time = dateparser.parse(
        text,
        settings={
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": "America/Toronto",
            "TO_TIMEZONE": "UTC",
        },
    )
    # Only use if dateparser found something that looks intentional (not just "now")
    if parsed_time and abs((parsed_time - datetime.now(timezone.utc)).total_seconds()) > 60:
        message = _clean_message(text)
        return ParsedReminder(message=message, trigger_type="time", trigger_at=parsed_time)

    # Default: immediate reminder (no trigger specified)
    return ParsedReminder(message=_clean_message(text), trigger_type="immediate")


def _clean_message(text: str) -> str:
    # Remove dangling prepositions and whitespace
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:to\s+)", "", text, flags=re.IGNORECASE)
    text = text.strip(" ,.-")
    return text if text else "No message"
