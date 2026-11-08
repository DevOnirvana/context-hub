"""Shared fixture loading for the systems-of-record mocks. Synthetic data only."""
import json
from datetime import datetime
from functools import cache
from pathlib import Path

from hub.clock import to_ist

DIR = Path(__file__).resolve().parent / "fixtures"


@cache
def load(name: str) -> dict:
    # Cached and treated as read-only: the mocks never change their own fixtures.
    return json.loads((DIR / f"{name}.json").read_text())


def ts(value: str) -> datetime:
    return to_ist(datetime.fromisoformat(value))
