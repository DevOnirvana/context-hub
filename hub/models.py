"""Data shapes shared across the hub. Column names follow SPEC section 8.

Every datetime field is converted to Asia/Kolkata on the way in, and naive datetimes are
refused (hub.clock.to_ist), so a timezone mistake fails at the boundary instead of silently.
"""
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field

from hub.clock import to_ist

ISTDateTime = Annotated[datetime, AfterValidator(to_ist)]

Source = Literal["customer", "agent", "system"]
ChangeReason = Literal["created", "reschedule", "correction", "replacement", "status_change"]
CommitmentStatus = Literal["active", "attempted", "fulfilled", "missed", "cancelled"]


class Case(BaseModel):
    case_id: str
    customer_id: str
    business_line: Literal["payments", "credit"]
    case_type: Literal["card_dispute", "upi_failed_debit"]
    opened_at: ISTDateTime
    closed_at: ISTDateTime | None = None

    def is_open(self, now: datetime) -> bool:
        return self.closed_at is None or self.closed_at > now


class UnitRef(BaseModel):
    """Points at the version a caller saw, for the expected-version check."""
    unit_id: str
    expected_version: int


class NewUnit(BaseModel):
    """A claim or commitment before the store gives it an ID and version 1."""
    case_id: str
    customer_id: str
    unit_type: Literal["claim", "commitment"]
    owner: Literal["bank", "customer"] | None = None
    action: str | None = None
    due_at: ISTDateTime | None = None
    status: str = "active"
    payload: dict[str, Any] = Field(default_factory=dict)
    source: Source
    sensitivity: Literal["normal", "restricted"] = "normal"
    created_by: str


class UnitVersion(NewUnit):
    """One stored version row of a unit."""
    unit_id: str
    version: int
    change_reason: ChangeReason
    valid_from: ISTDateTime
    valid_to: ISTDateTime | None = None


class ContextItem(BaseModel):
    item_id: str
    version: int
    customer_id: str
    key: str
    value: str
    source: Source
    change_reason: ChangeReason
    valid_from: ISTDateTime
    valid_to: ISTDateTime | None = None
    expires_at: ISTDateTime | None = None


class NewEvent(BaseModel):
    customer_id: str
    case_id: str | None = None
    unit_id: str | None = None
    event_type: str
    actor_type: Source
    actor_id: str
    occurred_at: ISTDateTime  # when it happened
    recorded_at: ISTDateTime  # when the bank learned it
    payload: dict[str, Any] = Field(default_factory=dict)


class Event(NewEvent):
    event_id: int


class Candidate(BaseModel):
    """A proposed memory, already structured. In Step 5 the extractor (LLM) produces these;
    until then tests build them by hand. Code, not the model, decides admission and scope."""
    type: Literal["claim", "customer_context", "request", "live_fact_query", "small_talk", "other"]
    # claim: {"text", "amount_inr", ...}; customer_context: {"key", "value"}; request: {"text"}
    fields: dict[str, Any] = Field(default_factory=dict)
    # Filled by the Step 5 extractor; the keyword backstop in hub/admission.py runs regardless.
    sensitive_categories: list[str] = Field(default_factory=list)
    scope_suggestion: Literal["session", "case", "customer"] | None = None
    source: Source
    # Set when the customer corrects an earlier claim ("actually it was Rs 5,800").
    corrects: UnitRef | None = None
