"""advance_to(): system records reach the hub in recorded_at order, before the sweep can
misjudge them (SPEC 17.28). Uses the dialler profiles behind SCENARIOS 2 to 4."""
from datetime import datetime

import psycopg

from hub import store
from hub.clock import IST
from scenarios.timeline import CARD_CASE, replay_to
from tests.conftest import ADMIN_URL, APP_URL, THRESHOLD

WED_1000 = datetime(2026, 11, 4, 10, 0, tzinfo=IST)


def callback_after(profile):
    """Replay to Wed 10:00 with a dialler profile; return the callback, its event types in the
    order the hub LEARNED of them (recorded_at), and any status_corrected events."""
    replay_to(WED_1000, THRESHOLD, admin_url=ADMIN_URL, app_url=APP_URL, dialler_profile=profile)
    with psycopg.connect(APP_URL, autocommit=True) as conn:
        (cb,) = [u for u in store.current_units(conn, case_id=CARD_CASE) if u.action == "callback"]
        learned = sorted(store.list_events(conn, unit_id=cb.unit_id), key=lambda e: (e.recorded_at, e.event_id))
        corrected = [e for e in learned if e.event_type == "status_corrected"]
        return cb, [e.event_type for e in learned], corrected


def test_no_dialler_record_gives_missed(test_database):
    cb, events, _ = callback_after("none")
    assert cb.status == "missed" and "commitment_missed" in events


def test_call_logged_before_threshold_prevents_a_false_miss(test_database):
    # Logged at 09:05; the timeline's Wed 09:15 step would sweep. Delivered first, so no miss.
    cb, events, _ = callback_after("completed_on_time")
    assert cb.status == "fulfilled" and "commitment_missed" not in events


def test_no_answer_is_attempted_never_missed(test_database):
    cb, events, _ = callback_after("attempted_no_answer")
    assert cb.status == "attempted" and "commitment_missed" not in events


def test_late_record_flips_missed_to_fulfilled_on_time(test_database):
    cb, events, corrected = callback_after("late_completed")
    assert cb.status == "fulfilled"
    # Learned in this order: missed at 09:15, then the 09:40 record. (By occurred_at the
    # correction sorts first, because the call itself happened at 09:04.)
    assert events.index("commitment_missed") < events.index("status_corrected")
    (event,) = corrected
    assert event.payload["on_time"] is True
    assert (event.occurred_at, event.recorded_at) == (datetime(2026, 11, 4, 9, 4, tzinfo=IST),
                                                      datetime(2026, 11, 4, 9, 40, tzinfo=IST))
