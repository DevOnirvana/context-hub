"""Commitment and claim lifecycle (SPEC 7 and 17), including the sweep."""
import logging
from datetime import datetime, timedelta

import pytest

from hub import lifecycle, store
from hub.acceptance import check_callback
from hub.clock import IST
from hub.errors import IllegalTransitionError
from hub.models import Case
from hub.timeres import RelativeTime, resolve
from tests.conftest import MON_1830, THRESHOLD, TUE_2015, WED_0900

CASE = "CASE-CARD-001"


def wed(hour, minute=0, second=0):
    return datetime(2026, 11, 4, hour, minute, second, tzinfo=IST)


@pytest.fixture
def conn(app_conn):
    store.create_case(app_conn, Case(case_id=CASE, customer_id="CUST-001", business_line="credit",
                                     case_type="card_dispute", opened_at=MON_1830))
    return app_conn


def new_callback(conn, due_at, now=MON_1830):
    return lifecycle.create_commitment(conn, case_id=CASE, customer_id="CUST-001", owner="bank", action="callback",
                                       due_at=due_at, now=now, actor_type="agent", actor_id="payments_agent")


@pytest.fixture
def wed_callback(conn):
    """Callback v1 Tue 21:00, rescheduled at Tue 20:15 to v2 Wed 09:00 (scenario 1)."""
    v1 = new_callback(conn, datetime(2026, 11, 3, 21, 0, tzinfo=IST))
    return lifecycle.reschedule(conn, v1.unit_id, expected_version=1, new_due_at=WED_0900, now=TUE_2015,
                                actor_type="agent", actor_id="payments_agent")


def counts(conn):
    return conn.execute("SELECT (SELECT count(*) FROM case_memory.units), (SELECT count(*) FROM events.log)").fetchone()


def event_types(conn, unit_id):
    return [e.event_type for e in store.list_events(conn, unit_id=unit_id)]


# --- BUILD_PLAN Step 2 acceptance ------------------------------------------------------------

def test_reschedule_creates_v2_active_and_closes_v1_as_superseded(conn, wed_callback):
    v1, v2 = store.unit_history(conn, wed_callback.unit_id)
    assert (v1.valid_to, v2.status, v2.due_at, v2.change_reason) == (TUE_2015, "active", WED_0900, "reschedule")
    assert lifecycle.displayed_statuses([v1, v2]) == ["superseded", "active"]
    assert v1.status == "active"  # stored status untouched; "superseded" is derived, never stored
    (event,) = [e for e in store.list_events(conn, unit_id=v2.unit_id) if e.event_type == "commitment_rescheduled"]
    assert event.payload["to_due_at"] == WED_0900.isoformat()


def test_sweep_at_0916_marks_missed(conn, wed_callback):
    assert lifecycle.sweep(conn, wed(9, 16), THRESHOLD) == [wed_callback.unit_id]
    missed = store.current_unit(conn, wed_callback.unit_id)
    assert missed.status == "missed"
    (event,) = [e for e in store.list_events(conn, unit_id=missed.unit_id) if e.event_type == "commitment_missed"]
    assert (event.occurred_at, event.recorded_at) == (wed(9, 15), wed(9, 16))


def test_sweep_at_0916_with_30_minute_threshold_does_not(conn, wed_callback):
    assert lifecycle.sweep(conn, wed(9, 16), timedelta(minutes=30)) == []
    assert store.current_unit(conn, wed_callback.unit_id).status == "active"


def test_attempted_then_completed_gives_fulfilled(conn, wed_callback):
    lifecycle.apply_dialler_outcome(conn, wed_callback.unit_id, outcome="attempted_no_answer",
                                    occurred_at=wed(9, 1), now=wed(9, 1), threshold=THRESHOLD)
    assert store.current_unit(conn, wed_callback.unit_id).status == "attempted"
    done = lifecycle.apply_dialler_outcome(conn, wed_callback.unit_id, outcome="completed",
                                           occurred_at=wed(9, 10), now=wed(9, 10), threshold=THRESHOLD)
    assert done.status == "fulfilled"


def test_late_completed_flips_missed_to_fulfilled_with_status_corrected(conn, wed_callback):
    lifecycle.sweep(conn, wed(9, 16), THRESHOLD)
    done = lifecycle.apply_dialler_outcome(conn, wed_callback.unit_id, outcome="completed",
                                           occurred_at=wed(9, 4), now=wed(9, 40), threshold=THRESHOLD)
    assert done.status == "fulfilled"
    (event,) = [e for e in store.list_events(conn, unit_id=done.unit_id) if e.event_type == "status_corrected"]
    assert event.payload["on_time"] is True
    assert (event.occurred_at, event.recorded_at) == (wed(9, 4), wed(9, 40))  # happened vs learned


def test_3am_request_declined_with_8am_offered_and_event_appended(conn):
    due = resolve(RelativeTime(day_offset=1, time="03:00"), MON_1830)
    decision = check_callback(due, MON_1830)
    lifecycle.decline_request(conn, customer_id="CUST-001", case_id=CASE, action="callback", decision=decision,
                              now=MON_1830, actor_type="agent", actor_id="payments_agent")
    (event,) = store.list_events(conn, case_id=CASE)
    assert event.event_type == "request_declined"
    assert event.payload == {"action": "callback", "requested_due_at": "2026-11-03T03:00:00+05:30",
                             "reason": "outside_servicing_hours", "offered_slot": "2026-11-03T08:00:00+05:30"}
    assert store.current_units(conn, case_id=CASE) == []  # nothing promised


# --- extras ----------------------------------------------------------------------------------

def test_missed_boundary_is_inclusive(conn, wed_callback):
    assert lifecycle.sweep(conn, wed(9, 14, 59), THRESHOLD) == []
    assert lifecycle.sweep(conn, wed(9, 15), THRESHOLD) == [wed_callback.unit_id]


def test_sweep_is_idempotent(conn, wed_callback):
    lifecycle.sweep(conn, wed(9, 16), THRESHOLD)
    before = counts(conn)
    assert lifecycle.sweep(conn, wed(9, 16), THRESHOLD) == []
    assert lifecycle.sweep(conn, wed(10, 0), THRESHOLD) == []
    assert counts(conn) == before


def test_sweep_never_marks_attempted_as_missed(conn, wed_callback):
    lifecycle.apply_dialler_outcome(conn, wed_callback.unit_id, outcome="attempted_no_answer",
                                    occurred_at=wed(9, 1), now=wed(9, 1), threshold=THRESHOLD)
    assert lifecycle.sweep(conn, wed(10, 0), THRESHOLD) == []
    assert store.current_unit(conn, wed_callback.unit_id).status == "attempted"


def test_completed_after_threshold_is_fulfilled_but_not_on_time(conn, wed_callback):
    lifecycle.sweep(conn, wed(9, 16), THRESHOLD)
    lifecycle.apply_dialler_outcome(conn, wed_callback.unit_id, outcome="completed",
                                    occurred_at=wed(9, 30), now=wed(9, 40), threshold=THRESHOLD)
    (event,) = [e for e in store.list_events(conn, unit_id=wed_callback.unit_id) if e.event_type == "status_corrected"]
    assert event.payload["on_time"] is False


def test_sweep_logs_one_line_per_missed_unit(conn, wed_callback, caplog):
    with caplog.at_level(logging.INFO, logger="hub.lifecycle"):
        lifecycle.sweep(conn, wed(9, 16), THRESHOLD)
    assert caplog.messages == [
        f"sweep: marked missed unit_id={wed_callback.unit_id} due_at=2026-11-04T09:00:00+05:30 threshold=15m"
    ]


def test_clock_listener_runs_the_sweep(conn, wed_callback, clock):
    clock.set(TUE_2015)
    clock.add_listener(lambda old, new: lifecycle.sweep(conn, new, THRESHOLD))
    clock.set(wed(9, 16))
    assert store.current_unit(conn, wed_callback.unit_id).status == "missed"


def test_customer_commitment_is_overdue_not_missed(conn):
    promise = lifecycle.create_commitment(conn, case_id=CASE, customer_id="CUST-001", owner="customer",
                                          action="upload_statement", due_at=datetime(2026, 11, 3, 18, 0, tzinfo=IST),
                                          now=MON_1830, actor_type="agent", actor_id="payments_agent")
    later = datetime(2026, 11, 3, 18, 30, tzinfo=IST)
    before = counts(conn)
    assert lifecycle.sweep(conn, later, THRESHOLD) == []  # bank commitments only
    assert counts(conn) == before                           # overdue is never stored
    assert lifecycle.is_overdue(store.current_unit(conn, promise.unit_id), later) is True
    assert lifecycle.is_overdue(store.current_unit(conn, promise.unit_id), MON_1830) is False


def test_customer_commitment_fulfilled_by_system_event(conn):
    promise = lifecycle.create_commitment(conn, case_id=CASE, customer_id="CUST-001", owner="customer",
                                          action="upload_statement", due_at=datetime(2026, 11, 3, 18, 0, tzinfo=IST),
                                          now=MON_1830, actor_type="agent", actor_id="payments_agent")
    tue_1600 = datetime(2026, 11, 3, 16, 0, tzinfo=IST)
    done = lifecycle.fulfil_customer_commitment(conn, promise.unit_id, evidence_event_id=1, occurred_at=tue_1600,
                                                now=tue_1600, actor_id="documents")
    assert done.status == "fulfilled" and lifecycle.is_overdue(done, wed(10)) is False


def test_reschedule_after_no_answer_is_allowed(conn, wed_callback):
    lifecycle.apply_dialler_outcome(conn, wed_callback.unit_id, outcome="attempted_no_answer",
                                    occurred_at=wed(9, 1), now=wed(9, 1), threshold=THRESHOLD)
    v4 = lifecycle.reschedule(conn, wed_callback.unit_id, expected_version=3, new_due_at=wed(17), now=wed(10),
                              actor_type="agent", actor_id="credit_agent")
    assert (v4.status, v4.due_at) == ("active", wed(17))


def test_cancel_from_attempted_is_allowed(conn, wed_callback):
    lifecycle.apply_dialler_outcome(conn, wed_callback.unit_id, outcome="attempted_no_answer",
                                    occurred_at=wed(9, 1), now=wed(9, 1), threshold=THRESHOLD)
    done = lifecycle.cancel(conn, wed_callback.unit_id, expected_version=3, now=wed(10), actor_type="agent",
                            actor_id="credit_agent")
    assert done.status == "cancelled"


def test_missed_cannot_become_attempted_or_be_rescheduled(conn, wed_callback):
    lifecycle.sweep(conn, wed(9, 16), THRESHOLD)
    with pytest.raises(IllegalTransitionError, match="missed -> attempted"):
        lifecycle.apply_dialler_outcome(conn, wed_callback.unit_id, outcome="attempted_no_answer",
                                        occurred_at=wed(9, 20), now=wed(9, 20), threshold=THRESHOLD)
    # A new callback after a miss is a new unit (SPEC 17.8), not a reschedule of the missed one.
    with pytest.raises(IllegalTransitionError):
        lifecycle.reschedule(conn, wed_callback.unit_id, expected_version=3, new_due_at=wed(17), now=wed(10),
                             actor_type="agent", actor_id="credit_agent")


def test_claim_correction_keeps_v1_as_corrected(conn):
    claim = lifecycle.create_claim(conn, case_id=CASE, customer_id="CUST-001", fields={"amount_inr": 8500},
                                   now=MON_1830, actor_type="agent", actor_id="payments_agent")
    lifecycle.correct_claim(conn, claim.unit_id, expected_version=1, new_fields={"amount_inr": 5800},
                            now=TUE_2015, actor_type="agent", actor_id="payments_agent")
    history = store.unit_history(conn, claim.unit_id)
    assert lifecycle.displayed_statuses(history) == ["corrected", "active"]
    assert [v.payload["amount_inr"] for v in history] == [8500, 5800]
    assert event_types(conn, claim.unit_id) == ["claim_recorded", "claim_corrected"]


# --- preferences: created, replaced, withdrawn (SPEC 17.21) ----------------------------------

def _pref(conn, value, now):
    return lifecycle.set_preference(conn, customer_id="CUST-001", key="preferred_explanation_style", value=value,
                                    now=now, actor_type="agent", actor_id="payments_agent")


def _pref_events(conn):
    return [e.event_type for e in store.list_events(conn, customer_id="CUST-001") if e.event_type.startswith("preference_")]


def test_preference_change_is_a_replacement(conn):
    v1 = _pref(conn, "simple English", TUE_2015)
    v2 = _pref(conn, "Hindi", wed(10))
    assert (v2.item_id, v2.version, v2.change_reason) == (v1.item_id, 2, "replacement")
    old, new = store.item_history(conn, v1.item_id)
    assert (old.valid_to, new.valid_to, new.value) == (wed(10), None, "Hindi")
    assert _pref_events(conn) == ["preference_set", "preference_replaced"]


def test_same_preference_value_writes_nothing(conn):
    _pref(conn, "simple English", TUE_2015)
    before = counts(conn), len(store.item_history(conn, "CUST-001-C1"))
    _pref(conn, "simple English", wed(10))
    assert (counts(conn), len(store.item_history(conn, "CUST-001-C1"))) == before


def test_withdrawal_closes_without_successor(conn):
    item = _pref(conn, "simple English", TUE_2015)
    lifecycle.withdraw_preference(conn, customer_id="CUST-001", key="preferred_explanation_style", now=wed(10),
                                  actor_type="agent", actor_id="credit_agent")
    assert store.current_items(conn, "CUST-001") == []
    (only,) = store.item_history(conn, item.item_id)
    assert only.valid_to == wed(10)
    assert _pref_events(conn) == ["preference_set", "preference_withdrawn"]


def test_replacement_is_refused_for_units(conn, wed_callback):
    with pytest.raises(ValueError, match="customer-context items"):
        store.insert_next_version(conn, wed_callback.unit_id, expected_version=2, changes={}, change_reason="replacement",
                                  at=wed(10), created_by="test")
