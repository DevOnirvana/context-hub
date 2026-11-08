"""The context packet returned by get_context(): what the next agent sees (SPEC 9, 17).

What is NOT here matters as much as what is: no field can hold a live fact (dispute status,
balance, statement, UPI status). The packet carries references (LiveFactRef) so the agent knows
what to fetch through its own tools. extra="forbid" stops a field being added by accident.
"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from hub.models import ChangeReason, Source


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnitView(Strict):
    """A unit's version as of now."""
    unit_id: str
    case_id: str
    unit_type: Literal["claim", "commitment"]
    version: int
    owner: str | None
    action: str | None
    due_at: datetime | None
    status: str
    payload: dict[str, Any]
    source: Source
    valid_from: datetime
    overdue: bool  # derived at read time (SPEC 17.16), never stored


class VersionView(Strict):
    version: int
    shown_status: str  # superseded / corrected / replaced, or the stored status (SPEC 17.2)
    due_at: datetime | None
    change_reason: ChangeReason
    valid_from: datetime
    valid_to: datetime | None


class CommitmentHistory(Strict):
    unit_id: str
    owner: str
    action: str
    versions: list[VersionView]


class EventView(Strict):
    event_type: str
    occurred_at: datetime
    recorded_at: datetime
    actor_type: Source
    case_id: str | None
    unit_id: str | None
    payload: dict[str, Any]


class LiveFactRef(Strict):
    """What to fetch, never the fact itself."""
    kind: Literal["dispute", "statement", "upi"]
    ref: str
    fetch_with: Literal["get_dispute_status", "get_statement", "get_upi_status"]


class CaseView(Strict):
    case_id: str
    business_line: str
    case_type: str
    opened_at: datetime
    closed_at: datetime | None
    units: list[UnitView]
    commitment_history: list[CommitmentHistory]
    events: list[EventView]       # the last 50 by occurred_at (SPEC 17.26)
    events_truncated: int         # older events left out
    live_fact_refs: list[LiveFactRef]


class PreferenceView(Strict):
    key: str
    value: str
    valid_from: datetime


class OtherCase(Strict):
    """Existence only: no case ID, no type, no contents."""
    business_line: Literal["payments", "credit"]


class ContextPacket(Strict):
    customer_id: str
    as_of: datetime
    agent_role: str
    purpose: str
    cases: list[CaseView]                 # cases with decision 'allow'
    customer_context: list[PreferenceView]
    customer_events: list[EventView]      # customer-scope events (case_id NULL)
    other_cases: list[OtherCase]          # cases with decision 'existence_only'
    other_cases_summary: str | None       # "1 other open case: Payments"
