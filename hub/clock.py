"""Simulated clock: the only source of "now" in the hub.

Why simulated: the demo must jump from Monday evening to Wednesday morning in one click, and
tests must be deterministic. Domain code never calls datetime.now().

Why a class and not a module-level global: every test, the walkthrough and the Streamlit
session each own an instance, so nothing leaks between them and nothing needs resetting.
Domain functions do not take the clock either; they take a plain `now: datetime` argument.
Only wiring code (hub/interface.py, the timeline, tests) reads the clock.
"""
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hub.errors import ClockError

IST = ZoneInfo("Asia/Kolkata")

Listener = Callable[[datetime, datetime], None]


def to_ist(value: datetime) -> datetime:
    """Return value in Asia/Kolkata. Naive datetimes are refused: their meaning is ambiguous."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ClockError(f"naive datetime {value!r}: every time must be timezone-aware")
    return value.astimezone(IST)


class SimClock:
    def __init__(self, start: datetime) -> None:
        self._now = to_ist(start)
        self._listeners: list[Listener] = []

    def now(self) -> datetime:
        return self._now

    def set(self, to: datetime) -> None:
        """Move the clock forward to `to`, then notify listeners (the lifecycle sweep).

        Backwards is refused: a sweep that marked a callback Missed cannot be undone by
        rewinding. Going back means reset and replay (scenarios/timeline.py, replay_to).
        """
        to = to_ist(to)
        if to < self._now:
            raise ClockError(f"clock only moves forward: now {self._now.isoformat()}, asked for {to.isoformat()}")
        old, self._now = self._now, to
        for listener in self._listeners:
            listener(old, to)

    def advance(self, minutes: int) -> None:
        self.set(self._now + timedelta(minutes=minutes))

    def add_listener(self, fn: Listener) -> None:
        """fn(old, new) runs after every change. Exceptions propagate: a failed sweep must be loud."""
        self._listeners.append(fn)
