"""Callback acceptance policy (SPEC 6, 17.7, 17.14, 17.15)."""
from datetime import datetime, timedelta

from hub.acceptance import check_callback
from hub.clock import IST
from hub.timeres import RelativeTime, resolve
from tests.conftest import MON_1830


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def test_tuesday_9pm_accepted_on_monday():
    d = check_callback(at(3, 21), MON_1830)
    assert d.accepted and d.reason == "ok" and d.next_valid_slot is None


def test_servicing_hours_are_inclusive():
    assert check_callback(at(3, 8), MON_1830).accepted
    assert check_callback(at(3, 21), MON_1830).accepted
    late = check_callback(at(3, 21, 1), MON_1830)
    assert (late.accepted, late.reason, late.next_valid_slot) == (False, "outside_servicing_hours", at(4, 8))


def test_3am_declined_with_8am_offered():
    due = resolve(RelativeTime(day_offset=1, time="03:00"), MON_1830)
    d = check_callback(due, MON_1830)
    assert (d.accepted, d.reason, d.next_valid_slot) == (False, "outside_servicing_hours", at(3, 8))


def test_past_time_offers_first_quarter_hour_30_minutes_out():
    # "Today at 9" said at 09:59: a queue cannot promise 10:00, so 10:30 (09:59 + 30 = 10:29, rounded up).
    d = check_callback(at(4, 9), at(4, 9, 59))
    assert (d.accepted, d.reason, d.next_valid_slot) == (False, "in_the_past", at(4, 10, 30))


def test_past_time_late_evening_rolls_to_next_morning():
    d = check_callback(at(3, 20), at(3, 20, 50))  # 21:20 would be after hours
    assert d.next_valid_slot == at(4, 8)


def test_past_time_exactly_reaching_close_is_offered():
    assert check_callback(at(3, 20), at(3, 20, 30)).next_valid_slot == at(3, 21)


def test_less_than_30_minutes_notice_is_too_soon():
    # 10:00 asked at 09:59: declined, and the offer is what the queue can keep (10:30).
    d = check_callback(at(4, 10), at(4, 9, 59))
    assert (d.accepted, d.reason, d.next_valid_slot) == (False, "too_soon", at(4, 10, 30))


def test_exactly_30_minutes_notice_is_accepted():
    assert check_callback(at(4, 10, 29), at(4, 9, 59)).accepted


def test_acceptance_and_offers_never_disagree():
    """Every slot the code offers must itself pass check_callback (SPEC 17.20).
    Sweeps 'now' across two days in 7-minute steps and asks for awkward times at each."""
    start = at(2, 0)
    for step in range(0, 2 * 24 * 60, 7):
        now = start + timedelta(minutes=step)
        for asked in (now - timedelta(hours=1), now + timedelta(minutes=5), now + timedelta(minutes=29),
                      now + timedelta(hours=9), now + timedelta(days=8), now + timedelta(days=7, minutes=1)):
            d = check_callback(asked, now)
            if not d.accepted and d.next_valid_slot is not None:
                again = check_callback(d.next_valid_slot, now)
                assert again.accepted, (now, asked, d, again)


def test_seven_day_limit_is_inclusive():
    assert check_callback(at(9, 18, 30), MON_1830).accepted  # exactly now + 7 days


def test_beyond_7_days_offers_same_time_on_day_7():
    d = check_callback(at(12, 10), MON_1830)
    assert (d.accepted, d.reason, d.next_valid_slot) == (False, "beyond_7_days", at(9, 10))


def test_beyond_7_days_early_morning_gets_day_7_opening():
    assert check_callback(at(12, 6), MON_1830).next_valid_slot == at(9, 8)


def test_beyond_7_days_evening_is_capped_at_now_plus_7_days():
    # Day 7's window ends at 18:30 (now + 7 days), earlier than 21:00.
    assert check_callback(at(12, 20), MON_1830).next_valid_slot == at(9, 18, 30)
    assert check_callback(at(9, 18, 31), MON_1830).next_valid_slot == at(9, 18, 30)


def test_beyond_7_days_when_day_7_has_no_valid_time():
    # Now Mon 07:00: day 7 window would end at 07:00, before opening; offer day 6 at 21:00.
    assert check_callback(at(12, 10), at(2, 7)).next_valid_slot == at(8, 21)
