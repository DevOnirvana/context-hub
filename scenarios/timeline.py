"""The shared scripted timeline of SPEC section 13, and replay_to() (SPEC 17.4, 17.18, 17.25).

One timeline, used by everything that needs the demo state at a given time: the Step 4
walkthrough, the Step 7 UI jump buttons, and the Level 3 live change (threshold 15 -> 30).
The clock only moves forward, so "go to time t with setting s" is always: reset, then replay.

Every step goes through hub/interface.py, the same calls the agents make (SPEC 17.25). Where an
agent would use the model (structured candidates, relative times), the script supplies the
structured output directly. The Wed 10:00 onwards conversations are agent turns (Step 6).
"""
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from dotenv import load_dotenv

from hub.acceptance import check_callback
from hub.clock import IST, SimClock
from hub.interface import ContextHub, Session
from hub.models import Candidate, UnitRef
from hub.timeres import RelativeTime, resolve
from scripts.reset_db import reset
from systems import dialler, documents

CUSTOMER = "CUST-001"
CARD_CASE = "CASE-CARD-001"
UPI_CASE = "PAY-2042"
START = datetime(2026, 11, 2, 18, 0, tzinfo=IST)  # before the first step


@dataclass(frozen=True)
class TimelineStep:
    at: datetime
    name: str
    run: Callable[[ContextHub], None] | None  # None: only the clock moves (sweeps, system records)


def payments_session(hub: ContextHub) -> Session:
    return hub.open_session(agent_id="payments_agent", agent_role="payments_agent", purpose="payments_servicing",
                            customer_id=CUSTOMER, authenticated=True)


def system_session(hub: ContextHub, agent_id: str) -> Session:
    return hub.open_session(agent_id=agent_id, agent_role="system", purpose="system_event", customer_id=CUSTOMER,
                            authenticated=True)


def _claim(hub: ContextHub, s: Session, case_id: str, fields: dict) -> None:
    result = hub.propose_memory(s, Candidate(type="claim", fields=fields, source="customer"), case_id=case_id)
    if result.unit_id is None:  # scripted inputs are known-good; a rejection here is a bug
        raise RuntimeError(f"timeline claim rejected: {result.decision.reason}")


def _callback(hub: ContextHub, s: Session, case_id: str, rel: RelativeTime, reschedule_of: UnitRef | None = None):
    """What the request_callback tool will do in Step 6: resolve, check, then accept."""
    decision = check_callback(resolve(rel, hub.clock.now()), hub.clock.now())
    if not decision.accepted:
        raise RuntimeError(f"timeline callback refused: {decision.reason}")
    return hub.accept_commitment(s, case_id=case_id, owner="bank", action="callback", due_at=decision.due_at,
                                 reschedule_of=reschedule_of)


def mon_1830(hub: ContextHub) -> None:
    s = payments_session(hub)
    case = hub.open_case(s, case_type="card_dispute")  # CASE-CARD-001, DSP-1001, card 4417
    _claim(hub, s, case.case_id, {
        "text": "Charged twice for the same transaction", "amount_inr": 8500, "merchant": "UrbanCart",
        "transaction_ids": ["TXN-9001", "TXN-9002"], "transaction_date": "2026-10-31",
    })
    hub.record_event(s, case_id=case.case_id, event_type="customer_informed",
                     payload={"message": "investigation has started"})
    hub.record_customer_commitment(s, case_id=case.case_id, action="upload_statement",
                                   due_at=datetime(2026, 11, 3, 18, 0, tzinfo=IST))
    _callback(hub, s, case.case_id, RelativeTime(day_offset=1, time="21:00"))  # "tomorrow at 9 PM"


def tue_1940(hub: ContextHub) -> None:
    s = payments_session(hub)
    case = hub.open_case(s, case_type="upi_failed_debit")  # PAY-2042, UPI-7781
    _claim(hub, s, case.case_id, {
        "text": "UPI payment failed but money was debited", "amount_inr": 4200, "payee": "Ramesh K",
        "transaction_date": "2026-11-03",
    })
    hub.record_event(s, case_id=case.case_id, event_type="customer_informed",
                     payload={"message": "reversal expected by T+1"})


def tue_2015(hub: ContextHub) -> None:
    s = payments_session(hub)
    # The agent finds the callback in its context packet, as it would in Step 6.
    packet = hub.get_context(s)
    (card,) = [c for c in packet.cases if c.case_id == CARD_CASE]
    (cb,) = [u for u in card.units if u.action == "callback" and u.owner == "bank"]
    # "Can you make that callback 9 AM tomorrow instead?"
    _callback(hub, s, CARD_CASE, RelativeTime(day_offset=1, time="09:00"),
              reschedule_of=UnitRef(unit_id=cb.unit_id, expected_version=cb.version))
    # "...explain things in simple English, I find banking terms hard." Only the value is stored.
    hub.propose_memory(s, Candidate(type="customer_context", source="customer",
                                    fields={"key": "preferred_explanation_style", "value": "simple English"}))


# The shared timeline ends at Wed 10:00, just before the credit conversation (SPEC 17.28): that
# conversation is the live agent moment. System records (the Tue 16:00 upload, dialler outcomes)
# are not scripted here; the mocks emit them and advance_to() delivers them.
TIMELINE: list[TimelineStep] = [
    TimelineStep(datetime(2026, 11, 2, 18, 30, tzinfo=IST), "Mon 18:30 payments: dispute, promise, callback 9 PM", mon_1830),
    TimelineStep(datetime(2026, 11, 3, 16, 0, tzinfo=IST), "Tue 16:00 system: statement upload recorded", None),
    TimelineStep(datetime(2026, 11, 3, 19, 40, tzinfo=IST), "Tue 19:40 payments: UPI failed debit", tue_1940),
    TimelineStep(datetime(2026, 11, 3, 20, 15, tzinfo=IST), "Tue 20:15 payments: reschedule to 9 AM, simple English", tue_2015),
    TimelineStep(datetime(2026, 11, 4, 9, 15, tzinfo=IST), "Wed 09:15 clock sweep", None),
    TimelineStep(datetime(2026, 11, 4, 10, 0, tzinfo=IST), "Wed 10:00 credit agent conversation begins", None),
]


def advance_to(hub: ContextHub, t: datetime, *, dialler_profile: dialler.Profile = "none",
               customer_id: str = CUSTOMER) -> None:
    """Move the clock to t, delivering every system record logged in (now, t] in time order.

    For each record: set the clock to its recorded_at (the sweep runs for that moment), then
    record_event through a system session. Delivering in order matters: a call logged at 09:05
    must reach the hub before a 09:16 sweep, or the hub would report a miss the bank never had.
    """
    start = hub.clock.now()
    pending = [(u.recorded_at, "documents", u.case_id, "statement_upload_recorded", {"document": u.document},
                u.recorded_at) for u in documents.uploads_between(customer_id, start, t)]
    pending += [(r.recorded_at, "dialler", r.case_id, "dialler_outcome", {"unit_id": r.unit_id, "outcome": r.outcome},
                 r.occurred_at) for r in dialler.records_between(customer_id, start, t, dialler_profile)]
    for recorded_at, source, case_id, event_type, payload, occurred_at in sorted(pending, key=lambda p: p[0]):
        hub.clock.set(recorded_at)
        hub.record_event(system_session(hub, source), case_id=case_id, event_type=event_type, payload=payload,
                         occurred_at=occurred_at)
    hub.clock.set(t)


def replay_to(t: datetime, threshold: timedelta, *, admin_url: str | None = None,
              app_url: str | None = None, dialler_profile: dialler.Profile = "none") -> SimClock:
    """Reset the demo data and run the timeline up to and including time t.

    Returns a fresh clock at t with no listeners: the caller (UI, walkthrough) owns its own
    connection and builds its own ContextHub. URLs default to .env; tests pass a test database.
    """
    load_dotenv()
    reset(admin_url or os.environ["DATABASE_URL_ADMIN"])
    clock = SimClock(START)
    with psycopg.connect(app_url or os.environ["DATABASE_URL_APP"], autocommit=True) as conn:
        hub = ContextHub(conn, clock, threshold=threshold)  # attaches the sweep to the clock
        for step in TIMELINE:
            if step.at > t:
                break
            advance_to(hub, step.at, dialler_profile=dialler_profile)  # system records and sweeps first
            if step.run is not None:
                step.run(hub)
        advance_to(hub, max(t, clock.now()), dialler_profile=dialler_profile)
    return SimClock(clock.now())
