"""Time resolution: turn the model's structured relative time into an exact timestamp.

The model never computes dates. It says {"day_offset": 1, "time": "21:00"} and this code
resolves it against the simulated clock at the moment it was said (SPEC 10). That removes
a whole class of errors (wrong weekday, wrong timezone, "tomorrow" said near midnight).
"""
from datetime import datetime, time, timedelta

from pydantic import BaseModel, Field

from hub.clock import IST, to_ist


class RelativeTime(BaseModel):
    day_offset: int = Field(ge=0, le=14)   # 0 = today, 1 = tomorrow; past days make no sense for a request
    time: time                             # local IST wall time, e.g. "21:00"


def resolve(rel: RelativeTime, now: datetime) -> datetime:
    """Mon 2 Nov 18:30 with {day_offset: 1, time: 21:00} gives Tue 3 Nov 21:00 IST."""
    day = to_ist(now).date() + timedelta(days=rel.day_offset)
    return datetime.combine(day, rel.time.replace(tzinfo=None), tzinfo=IST)
