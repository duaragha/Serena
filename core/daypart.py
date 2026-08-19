"""The real local clock, bucketed into the part of day Serena is speaking into.

Spoken surfaces get the wall clock as a signal, never as a script.  Callers
pass the bucket into whatever assembles a greeting or a reply and let the
wording vary; nothing here decides the words.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

MORNING = "morning"
AFTERNOON = "afternoon"
EVENING = "evening"
LATE_NIGHT = "late-night"

# Start hour of each bucket on a 24 hour clock, in order.  The last bucket
# wraps past midnight, so any hour before the first start belongs to it.
_BUCKET_STARTS: tuple[tuple[int, str], ...] = (
    (5, MORNING),
    (12, AFTERNOON),
    (17, EVENING),
    (23, LATE_NIGHT),
)
DAYPARTS: tuple[str, ...] = tuple(name for _start, name in _BUCKET_STARTS)


def now_local() -> datetime:
    """The current system time, as an aware local datetime."""
    return datetime.now().astimezone()


def daypart_for_hour(hour: int) -> str:
    """Return the day-part bucket for an hour of the 24 hour clock."""
    try:
        hour = int(hour) % 24
    except (TypeError, ValueError, OverflowError):
        return _BUCKET_STARTS[-1][1]
    bucket = _BUCKET_STARTS[-1][1]
    for start, name in _BUCKET_STARTS:
        if hour >= start:
            bucket = name
    return bucket


def current_daypart(clock: Callable[[], datetime] | None = None) -> str:
    """Return the day-part bucket for right now."""
    return daypart_for_hour((clock or now_local)().hour)


def spoken_clock(moment: datetime) -> str:
    """Render a moment the way a person says it, e.g. ``9:47 am``."""
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'am' if moment.hour < 12 else 'pm'}"


@dataclass(frozen=True, slots=True)
class TimeContext:
    """One reading of the system clock, ready to hand to a prompt."""

    local_time: str
    weekday: str
    clock: str
    daypart: str

    @classmethod
    def now(cls, clock: Callable[[], datetime] | None = None) -> TimeContext:
        moment = (clock or now_local)()
        return cls(
            local_time=moment.isoformat(timespec="minutes"),
            weekday=moment.strftime("%A"),
            clock=spoken_clock(moment),
            daypart=daypart_for_hour(moment.hour),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "local_time": self.local_time,
            "weekday": self.weekday,
            "clock": self.clock,
            "day_part": self.daypart,
        }

    def attributes(self) -> str:
        """XML-ish attributes for prompts that already carry a tag."""
        return (
            f'weekday="{self.weekday}" clock="{self.clock}" '
            f'local-time="{self.local_time}" day-part="{self.daypart}"'
        )
