"""The shared scripted timeline of SPEC section 13, and replay_to() (SPEC 17.4, 17.18).

One timeline, used by everything that needs the demo state at a given time: the Step 4
walkthrough, the Step 7 UI jump buttons, and the Level 3 live change (threshold 15 -> 30).
The clock only moves forward, so "go to time t with setting s" is always: reset, then replay.

Step 2 covers the steps that need no agent and no systems mock. The Wed 10:00 onwards
conversations are agent turns (Step 6); Step 4 adds the systems mocks and printing.
"""
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from dotenv import load_dotenv
from psycopg import Connection

from hub import lifecycle, store
from hub.acceptance import check_callback
from hub.admission import admit
from hub.clock import IST, SimClock
from hub.models import Candidate, Case, NewEvent
from hub.timeres import RelativeTime, resolve
from scripts.reset_db import reset

CUSTOMER = "CUST-001"
CARD_CASE = "CASE-CARD-001"
UPI_CASE = "PAY-2042"
START = datetime(2026, 11, 2, 18, 0, tzinfo=IST)  # before the first step


@dataclass(frozen=True)
class TimelineStep:
    at: datetime
    name: str
    run: Callable[[Connection, datetime], None]


def _admitted_claim(conn: Connection, case_id: str, fields: dict, now: datetime) -> None:
    decision = admit(Candidate(type="claim", fields=fields, source="customer"))
    assert decision.admitted, decision.reason  # scripted inputs are known-good; a failure is a bug
    lifecycle.create_claim(conn, case_id=case_id, customer_id=CUSTOMER, fields=fields, now=now,
                           actor_type="agent", actor_id="payments_agent")


def _told(conn: Connection, case_id: str, message: str, now: datetime) -> None:
    """Communication event: what the customer was told (SPEC 3 examples)."""
    store.append_event(conn, NewEvent(
        customer_id=CUSTOMER, case_id=case_id, event_type="customer_informed", actor_type="agent",
        actor_id="payments_agent", occurred_at=now, recorded_at=now, payload={"message": message},
    ))


def _bank_callback(conn: Connection) -> tuple[str, int]:
    (unit,) = [u for u in store.current_units(conn, case_id=CARD_CASE)
               if u.action == "callback" and u.owner == "bank"]
    return unit.unit_id, unit.version


def mon_1830(conn: Connection, now: datetime) -> None:
    store.create_case(conn, Case(case_id=CARD_CASE, customer_id=CUSTOMER, business_line="credit",
                                 case_type="card_dispute", opened_at=now))
    _admitted_claim(conn, CARD_CASE, {
        "text": "Charged twice for the same transaction", "amount_inr": 8500, "merchant": "UrbanCart",
        "transaction_ids": ["TXN-9001", "TXN-9002"], "transaction_date": "2026-10-31", "dispute_ref": "DSP-1001",
    }, now)
    _told(conn, CARD_CASE, "investigation has started", now)
    lifecycle.create_commitment(conn, case_id=CARD_CASE, customer_id=CUSTOMER, owner="customer",
                                action="upload_statement", due_at=datetime(2026, 11, 3, 18, 0, tzinfo=IST),
                                now=now, actor_type="agent", actor_id="payments_agent")
    # "Call me tomorrow at 9 PM": the model would give this structure; code resolves and decides.
    decision = check_callback(resolve(RelativeTime(day_offset=1, time="21:00"), now), now)
    assert decision.accepted, decision.reason
    lifecycle.create_commitment(conn, case_id=CARD_CASE, customer_id=CUSTOMER, owner="bank", action="callback",
                                due_at=decision.due_at, now=now, actor_type="agent", actor_id="payments_agent")


def tue_1600(conn: Connection, now: datetime) -> None:
    evidence = store.append_event(conn, NewEvent(
        customer_id=CUSTOMER, case_id=CARD_CASE, event_type="statement_upload_recorded", actor_type="system",
        actor_id="documents", occurred_at=now, recorded_at=now, payload={"document": "bank_statement"},
    ))
    (promise,) = [u for u in store.current_units(conn, case_id=CARD_CASE) if u.action == "upload_statement"]
    lifecycle.fulfil_customer_commitment(conn, promise.unit_id, evidence_event_id=evidence, occurred_at=now,
                                         now=now, actor_id="documents")


def tue_1940(conn: Connection, now: datetime) -> None:
    store.create_case(conn, Case(case_id=UPI_CASE, customer_id=CUSTOMER, business_line="payments",
                                 case_type="upi_failed_debit", opened_at=now))
    _admitted_claim(conn, UPI_CASE, {
        "text": "UPI payment failed but money was debited", "amount_inr": 4200, "payee": "Ramesh K",
        "upi_ref": "UPI-7781", "transaction_date": "2026-11-03",
    }, now)
    _told(conn, UPI_CASE, "reversal expected by T+1", now)


def tue_2015(conn: Connection, now: datetime) -> None:
    # "Can you make that callback 9 AM tomorrow instead?"
    decision = check_callback(resolve(RelativeTime(day_offset=1, time="09:00"), now), now)
    assert decision.accepted, decision.reason
    unit_id, version = _bank_callback(conn)
    lifecycle.reschedule(conn, unit_id, expected_version=version, new_due_at=decision.due_at, now=now,
                         actor_type="agent", actor_id="payments_agent")
    # "...explain things in simple English, I find banking terms hard." Only the value is stored.
    pref = {"key": "preferred_explanation_style", "value": "simple English"}
    decision = admit(Candidate(type="customer_context", fields=pref, source="customer"))
    assert decision.admitted, decision.reason
    lifecycle.set_preference(conn, customer_id=CUSTOMER, now=now, actor_type="agent", actor_id="payments_agent", **pref)


def wed_0915(conn: Connection, now: datetime) -> None:
    """Nothing to do: moving the clock here runs the sweep (callback Missed with a 15-minute threshold)."""


TIMELINE: list[TimelineStep] = [
    TimelineStep(datetime(2026, 11, 2, 18, 30, tzinfo=IST), "Mon 18:30 payments: dispute, promise, callback 9 PM", mon_1830),
    TimelineStep(datetime(2026, 11, 3, 16, 0, tzinfo=IST), "Tue 16:00 system: statement upload recorded", tue_1600),
    TimelineStep(datetime(2026, 11, 3, 19, 40, tzinfo=IST), "Tue 19:40 payments: UPI failed debit", tue_1940),
    TimelineStep(datetime(2026, 11, 3, 20, 15, tzinfo=IST), "Tue 20:15 payments: reschedule to 9 AM, simple English", tue_2015),
    TimelineStep(datetime(2026, 11, 4, 9, 15, tzinfo=IST), "Wed 09:15 clock sweep", wed_0915),
]


def replay_to(t: datetime, threshold: timedelta, *, admin_url: str | None = None,
              app_url: str | None = None) -> SimClock:
    """Reset the demo data and run the timeline up to and including time t.

    Returns a fresh clock at t with no listeners: the caller (UI, walkthrough) owns its own
    connection and attaches the sweep itself. URLs default to .env; tests pass a test database.
    """
    load_dotenv()
    reset(admin_url or os.environ["DATABASE_URL_ADMIN"])
    clock = SimClock(START)
    with psycopg.connect(app_url or os.environ["DATABASE_URL_APP"], autocommit=True) as conn:
        clock.add_listener(lambda old, new: lifecycle.sweep(conn, new, threshold))
        for step in TIMELINE:
            if step.at > t:
                break
            clock.set(step.at)          # sweep runs first, as it would in real time
            step.run(conn, clock.now())
        clock.set(max(t, clock.now()))  # final sweep at t
    return SimClock(clock.now())
