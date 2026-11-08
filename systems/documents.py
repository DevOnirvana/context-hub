"""Mock document system (SPEC 11). Emits upload records; the hub learns of them through
record_event (scenarios.timeline.advance_to). Upload status itself is never stored as memory."""
from datetime import datetime

from pydantic import BaseModel

from systems._fixtures import load, ts


class DocumentUpload(BaseModel):
    document: str
    case_id: str
    recorded_at: datetime


def uploads_between(customer_id: str, start: datetime, end: datetime) -> list[DocumentUpload]:
    """Uploads recorded in (start, end]."""
    out = [DocumentUpload(document=u["document"], case_id=u["case_id"], recorded_at=ts(u["recorded_at"]))
           for u in load("documents").get(customer_id, [])]
    return [u for u in out if start < u.recorded_at <= end]
