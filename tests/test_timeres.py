from datetime import datetime

import pytest
from pydantic import ValidationError

from hub.clock import IST
from hub.errors import ClockError
from hub.timeres import RelativeTime, resolve
from tests.conftest import MON_1830, TUE_2015


def test_tomorrow_9pm_said_monday_1830_is_tuesday_2100_ist():
    due = resolve(RelativeTime(day_offset=1, time="21:00"), MON_1830)
    assert due == datetime(2026, 11, 3, 21, 0, tzinfo=IST)
    assert due.utcoffset().total_seconds() == 5.5 * 3600


def test_tomorrow_9am_said_tuesday_2015_is_wednesday_0900():
    assert resolve(RelativeTime(day_offset=1, time="09:00"), TUE_2015) == datetime(2026, 11, 4, 9, 0, tzinfo=IST)


def test_today_keeps_the_date():
    assert resolve(RelativeTime(day_offset=0, time="20:00"), MON_1830) == datetime(2026, 11, 2, 20, 0, tzinfo=IST)


def test_negative_offset_refused():
    with pytest.raises(ValidationError):
        RelativeTime(day_offset=-1, time="09:00")


def test_naive_now_refused():
    with pytest.raises(ClockError):
        resolve(RelativeTime(day_offset=1, time="09:00"), datetime(2026, 11, 2, 18, 30))
