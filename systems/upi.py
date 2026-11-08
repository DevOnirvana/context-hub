"""Mock UPI system (SPEC 11). Read-only and clock-aware. Only the payments agent has a tool
for it; the credit agent cannot reach UPI data by any path."""
from datetime import date, datetime

from pydantic import BaseModel

from systems._fixtures import load, ts


class UpiStatus(BaseModel):
    upi_ref: str
    amount_inr: int
    payee: str
    transaction_date: date
    status: str
    effective_at: datetime


def get_upi_status(upi_ref: str, now: datetime) -> UpiStatus | None:
    t = load("upi")[upi_ref]
    entries = [e for e in t["timeline"] if ts(e["effective_at"]) <= now]
    if not entries:
        return None
    last = entries[-1]
    return UpiStatus(upi_ref=upi_ref, amount_inr=t["amount_inr"], payee=t["payee"],
                     transaction_date=t["transaction_date"], status=last["status"], effective_at=ts(last["effective_at"]))
