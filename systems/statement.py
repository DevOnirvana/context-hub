"""Mock card statement system (SPEC 11, 17.27). Read-only and clock-aware: adjustments such as
a provisional credit apply from their effective time."""
from datetime import date, datetime

from pydantic import BaseModel

from systems._fixtures import load, ts


class Statement(BaseModel):
    card_last4: str
    statement_date: date
    due_date: date
    total_due_inr: int
    minimum_due_inr: int
    disputed_amount_inr: int
    disputed_note: str
    provisional_credit_inr: int
    # Says in words what the numbers already mean (SPEC 17.27), so a reader of the tool result,
    # human or model, never has to guess whether the credit is in the total.
    total_due_note: str


def get_statement(card_last4: str, now: datetime) -> Statement:
    s = load("statements")[card_last4]
    total, minimum, credit = s["total_due_inr"], s["minimum_due_inr"], 0
    for adj in s["adjustments"]:
        if ts(adj["effective_at"]) <= now and adj["type"] == "provisional_credit":
            total, minimum, credit = adj["total_due_inr"], adj["minimum_due_inr"], adj["amount_inr"]
    return Statement(card_last4=card_last4, statement_date=s["statement_date"], due_date=s["due_date"],
                     total_due_inr=total, minimum_due_inr=minimum, disputed_amount_inr=s["disputed_amount_inr"],
                     disputed_note=s["disputed_note"], provisional_credit_inr=credit,
                     total_due_note=(f"total_due_inr already includes the provisional credit of Rs {credit:,}; "
                                     "it is the amount payable this month" if credit else
                                     "no provisional credit applied; total_due_inr includes the disputed amount"))
