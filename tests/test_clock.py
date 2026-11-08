from datetime import datetime, timedelta, timezone

import pytest

from hub.clock import IST, SimClock
from hub.errors import ClockError
from tests.conftest import MON_1830


def test_clock_refuses_naive_time():
    with pytest.raises(ClockError, match="naive"):
        SimClock(datetime(2026, 11, 2, 18, 30))


def test_clock_stores_ist_whatever_zone_it_is_given():
    utc = datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc)
    assert SimClock(utc).now() == datetime(2026, 11, 2, 18, 30, tzinfo=IST)
    assert SimClock(utc).now().tzinfo == IST


def test_clock_only_moves_forward(clock):
    clock.advance(15)
    assert clock.now() == MON_1830 + timedelta(minutes=15)
    with pytest.raises(ClockError, match="only moves forward"):
        clock.set(MON_1830)


def test_listeners_get_old_and_new(clock):
    seen = []
    clock.add_listener(lambda old, new: seen.append((old, new)))
    clock.advance(30)
    assert seen == [(MON_1830, MON_1830 + timedelta(minutes=30))]


def test_two_clocks_are_independent():
    # No module-level clock: each owner gets its own, nothing leaks between tests.
    a, b = SimClock(MON_1830), SimClock(MON_1830)
    a.advance(60)
    assert b.now() == MON_1830
