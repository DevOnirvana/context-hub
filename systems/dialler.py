"""Mock outbound dialler (SPEC 11, 17.28). Emits call outcomes per profile:
none (main demo: so Wednesday 9 AM is Missed), attempted_no_answer, completed_on_time,
late_completed. A record has two times: when the call happened and when the dialler logged it."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from systems._fixtures import load, ts

Profile = Literal["none", "attempted_no_answer", "completed_on_time", "late_completed"]


class DiallerRecord(BaseModel):
    unit_id: str  # the hub commitment the call belongs to (the dialler's queue holds it)
    case_id: str
    outcome: Literal["completed", "attempted_no_answer"]
    occurred_at: datetime
    recorded_at: datetime


def records_between(customer_id: str, start: datetime, end: datetime, profile: Profile = "none") -> list[DiallerRecord]:
    """Records logged in (start, end]."""
    out = [DiallerRecord(unit_id=r["unit_id"], case_id=r["case_id"], outcome=r["outcome"],
                         occurred_at=ts(r["occurred_at"]), recorded_at=ts(r["recorded_at"]))
           for r in load("dialler").get(customer_id, {}).get(profile, [])]
    return [r for r in out if start < r.recorded_at <= end]
