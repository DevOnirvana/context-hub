"""The step vocabulary both backends receive (Step 8, T2). Backend-neutral on purpose.

A scenario is a list of these steps. Each backend turns each step into its own writes:
the hub through hub/interface.py (admission, acceptance, lifecycle), the baseline into plain
key puts. Neither backend sees anything the other does not. No step involves an LLM: where an
agent would use the model, the step carries the structured output directly, as in the timeline.
"""
from dataclasses import dataclass, field
from datetime import datetime

from systems import dialler


@dataclass(frozen=True)
class Clock:
    """Move time forward. System records logged in between (document uploads, dialler outcomes
    from the given profile) are delivered to the backend in time order."""
    at: datetime
    dialler_profile: dialler.Profile = "none"


@dataclass(frozen=True)
class OpenCase:
    case_type: str                        # card_dispute | upi_failed_debit (IDs come from the casemgmt mock)


@dataclass(frozen=True)
class Claim:
    case_id: str
    fact: str                             # the baseline's key for this fact, e.g. "disputed_amount"
    fields: dict
    sensitive_categories: list[str] = field(default_factory=list)  # as the extractor reports them


@dataclass(frozen=True)
class CorrectClaim:
    case_id: str
    fact: str
    fields: dict


@dataclass(frozen=True)
class Told:
    """The agent told the customer something (a communication event)."""
    case_id: str
    message: str


@dataclass(frozen=True)
class CustomerPromise:
    case_id: str
    action: str
    due_at: datetime


@dataclass(frozen=True)
class CallbackRequest:
    """The customer asks for a callback; time as the model would give it, resolved in code."""
    case_id: str
    day_offset: int
    time: str                             # HH:MM IST
    reschedule: bool = False


@dataclass(frozen=True)
class Preference:
    key: str
    value: str


Step = Clock | OpenCase | Claim | CorrectClaim | Told | CustomerPromise | CallbackRequest | Preference
