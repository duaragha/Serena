"""The clock signal spoken surfaces read before they greet him."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.daypart import (
    AFTERNOON,
    DAYPARTS,
    EVENING,
    LATE_NIGHT,
    MORNING,
    TimeContext,
    current_daypart,
    daypart_for_hour,
    now_local,
    spoken_clock,
)


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, LATE_NIGHT),
        (3, LATE_NIGHT),
        (4, LATE_NIGHT),
        (5, MORNING),
        (9, MORNING),
        (11, MORNING),
        (12, AFTERNOON),
        (16, AFTERNOON),
        (17, EVENING),
        (22, EVENING),
        (23, LATE_NIGHT),
    ],
)
def test_every_hour_buckets_into_one_named_part_of_day(hour: int, expected: str) -> None:
    assert daypart_for_hour(hour) == expected


def test_all_twenty_four_hours_land_in_a_known_bucket() -> None:
    buckets = {daypart_for_hour(hour) for hour in range(24)}

    assert buckets == set(DAYPARTS)
    assert daypart_for_hour(24) == daypart_for_hour(0)
    assert daypart_for_hour("not-an-hour") == LATE_NIGHT  # type: ignore[arg-type]


def test_context_reads_the_injected_clock_not_a_hardcoded_phrase() -> None:
    moment = datetime(2026, 8, 5, 23, 5, tzinfo=timezone(timedelta(hours=-4)))

    context = TimeContext.now(lambda: moment)

    assert context.daypart == LATE_NIGHT
    assert context.weekday == "Wednesday"
    assert context.clock == "11:05 pm"
    assert context.local_time == "2026-08-05T23:05-04:00"
    assert context.as_dict()["day_part"] == LATE_NIGHT
    attributes = context.attributes()
    assert 'day-part="late-night"' in attributes
    assert 'clock="11:05 pm"' in attributes
    assert "good evening" not in attributes.lower()


def test_spoken_clock_reads_like_a_person_says_it() -> None:
    assert spoken_clock(datetime(2026, 8, 5, 0, 7)) == "12:07 am"
    assert spoken_clock(datetime(2026, 8, 5, 12, 0)) == "12:00 pm"
    assert spoken_clock(datetime(2026, 8, 5, 9, 47)) == "9:47 am"


def test_current_daypart_follows_the_real_system_clock() -> None:
    moment = now_local()

    assert moment.tzinfo is not None
    assert current_daypart() == daypart_for_hour(moment.hour)
