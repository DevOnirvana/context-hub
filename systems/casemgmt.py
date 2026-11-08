"""Mock case-management system (SPEC 17.23). Synthetic fixture data.

The bank's case-management and dispute systems issue case IDs and references; the model never
invents them. This mock returns the fixture entry for the customer and case type. A real system
would allocate new IDs; the demo needs the same IDs on every replay.
"""
import json
from pathlib import Path

from pydantic import BaseModel

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cases.json"


class CaseOpening(BaseModel):
    case_id: str
    case_type: str
    business_line: str
    refs: dict[str, str]  # references into systems of record, e.g. {"dispute_ref": "DSP-1001"}


def open_case(customer_id: str, case_type: str) -> CaseOpening:
    entry = json.loads(FIXTURE.read_text())[customer_id][case_type]
    return CaseOpening(case_type=case_type, **entry)
