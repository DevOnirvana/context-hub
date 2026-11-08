"""Callback acceptance policy (SPEC 6, 17.7, 17.14, 17.15). Pure, deterministic code.

The model may ask for any time; this code decides. A promise the bank cannot keep is never made,
and when a request is declined, the code (never the model) picks the slot to offer instead.
"""
from datetime import datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel

from hub.clock import IST, to_ist

SERVICE_OPEN = time(8, 0)     # servicing hours, inclusive at both ends (SPEC 17.7)
SERVICE_CLOSE = time(21, 0)
MAX_AHEAD = timedelta(days=7)  # inclusive: due_at <= now + 7 days
SLOT = timedelta(minutes=15)   # offered slots fall on quarter-hours
MIN_LEAD = timedelta(minutes=30)  # an offered slot is at least this far from now: a callback
                                  # queue cannot promise 10:00 at 09:59 (SPEC 17.14)

Reason = Literal["ok", "in_the_past", "too_soon", "beyond_7_days", "outside_servicing_hours"]


class AcceptanceDecision(BaseModel):
    accepted: bool
    reason: Reason
    due_at: datetime                       # what was asked for
    next_valid_slot: datetime | None = None  # offered instead, when declined


def _in_hours(dt: datetime) -> bool:
    return SERVICE_OPEN <= dt.timetz().replace(tzinfo=None) <= SERVICE_CLOSE


def _at(dt: datetime, t: time) -> datetime:
    """Same IST date as dt, at wall time t."""
    return datetime.combine(dt.date(), t, tzinfo=IST)


def _floor_slot(dt: datetime) -> datetime:
    return dt.replace(minute=dt.minute - dt.minute % 15, second=0, microsecond=0)


def _ceil_slot(dt: datetime) -> datetime:
    floored = _floor_slot(dt)
    return floored if floored == dt else floored + SLOT


def check_callback(due_at: datetime, now: datetime) -> AcceptanceDecision:
    """Accept iff now + 30 min <= due_at <= now + 7 days and 08:00 <= local time <= 21:00.

    The 30-minute notice is the same MIN_LEAD the offers use (SPEC 17.20), so acceptance and
    offers never disagree: every slot this module offers would itself be accepted.
    """
    due_at, now = to_ist(due_at), to_ist(now)
    if due_at <= now:
        return AcceptanceDecision(accepted=False, reason="in_the_past", due_at=due_at,
                                  next_valid_slot=next_valid_slot(now, now))
    if due_at < now + MIN_LEAD:
        return AcceptanceDecision(accepted=False, reason="too_soon", due_at=due_at,
                                  next_valid_slot=next_valid_slot(due_at, now))
    if due_at > now + MAX_AHEAD:
        return AcceptanceDecision(accepted=False, reason="beyond_7_days", due_at=due_at,
                                  next_valid_slot=day7_slot(due_at, now))
    if not _in_hours(due_at):
        return AcceptanceDecision(accepted=False, reason="outside_servicing_hours", due_at=due_at,
                                  next_valid_slot=next_valid_slot(due_at, now))
    return AcceptanceDecision(accepted=True, reason="ok", due_at=due_at)


def next_valid_slot(after: datetime, now: datetime) -> datetime | None:
    """First quarter-hour slot at or after `after`, at least MIN_LEAD from now, inside
    servicing hours and within 7 days. Example: 3 AM Tuesday requested on Monday gives Tue 08:00."""
    after, now = to_ist(after), to_ist(now)
    slot = _ceil_slot(max(after, now + MIN_LEAD))
    if slot.timetz().replace(tzinfo=None) < SERVICE_OPEN:
        slot = _at(slot, SERVICE_OPEN)
    elif slot.timetz().replace(tzinfo=None) > SERVICE_CLOSE:
        slot = _at(slot + timedelta(days=1), SERVICE_OPEN)
    if slot <= now + MAX_AHEAD:
        return slot
    return _latest_slot(now)


def day7_slot(due_at: datetime, now: datetime) -> datetime | None:
    """For a request beyond 7 days: the same time of day on day 7 if valid, else the nearest
    valid slot on day 7 (SPEC 17.15). Day 7's window ends at the earlier of 21:00 and now + 7 days,
    because the limit is inclusive of now + 7 days."""
    due_at, now = to_ist(due_at), to_ist(now)
    day7 = now + MAX_AHEAD
    start = _at(day7, SERVICE_OPEN)
    end = min(_at(day7, SERVICE_CLOSE), _floor_slot(day7))
    if end < start:  # now is before 08:00, so day 7 has no valid time; use the last valid slot
        return _latest_slot(now)
    wanted = _at(day7, due_at.timetz().replace(tzinfo=None))
    return min(max(wanted, start), end)


def _latest_slot(now: datetime) -> datetime | None:
    """The last quarter-hour slot inside servicing hours at or before now + 7 days."""
    slot = _floor_slot(now + MAX_AHEAD)
    if slot.timetz().replace(tzinfo=None) > SERVICE_CLOSE:
        slot = _at(slot, SERVICE_CLOSE)
    elif slot.timetz().replace(tzinfo=None) < SERVICE_OPEN:
        slot = _at(slot - timedelta(days=1), SERVICE_CLOSE)
    return slot if slot >= now + MIN_LEAD else None
