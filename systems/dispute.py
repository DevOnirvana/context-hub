"""Mock dispute system (SPEC 11). Read-only and clock-aware: the status is whatever the
fixture timeline says at `now`. The hub never stores it; agents fetch it live."""
from datetime import datetime

from pydantic import BaseModel

from systems._fixtures import load, ts


class DisputeStatus(BaseModel):
    dispute_ref: str
    status: str
    effective_at: datetime


def get_dispute_status(dispute_ref: str, now: datetime) -> DisputeStatus | None:
    entries = [e for e in load("disputes")[dispute_ref]["timeline"] if ts(e["effective_at"]) <= now]
    if not entries:
        return None  # the dispute did not exist yet at `now`
    last = entries[-1]
    return DisputeStatus(dispute_ref=dispute_ref, status=last["status"], effective_at=ts(last["effective_at"]))
