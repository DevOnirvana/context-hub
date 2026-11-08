"""Memory extraction (SPEC 12, 17.34): the model proposes, code decides.

The extraction model reads one customer turn and returns typed items. This module validates them
with pydantic and maps them onto hub.models.Candidate. The schema has no scope, source or
admission field, and rejects extra fields, so the model cannot set any of those; code sets source
and scope_suggestion, and hub/admission.py decides what is admitted.
"""
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agents.llm import LLM
from hub.admission import KNOWN_SENSITIVE_CATEGORIES, PREFERENCE_KEYS
from hub.models import Candidate

PROMPT = (Path(__file__).resolve().parent / "prompts" / "extraction.md").read_text()

# Derived from the hub's own allowlists, so the schema can never drift from admission's rules.
PreferenceKey = Literal[tuple(sorted(PREFERENCE_KEYS))]
SensitiveCategory = Literal[tuple(sorted(KNOWN_SENSITIVE_CATEGORIES))]


class ExtractedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["claim", "customer_context", "request", "live_fact_query", "small_talk", "other"]
    text: str                       # the customer's words this item came from
    amount_inr: int | None
    merchant: str | None
    payee: str | None
    preference_key: PreferenceKey | None
    preference_value: str | None
    sensitive_categories: list[SensitiveCategory]


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ExtractedItem]


def extract(llm: LLM, customer_turn: str) -> ExtractionResult:
    """One call to the extraction model (Haiku). Thinking off and low effort: this is labelling,
    not reasoning, and it keeps the cost per turn tiny."""
    result = llm.call(
        purpose="extraction",
        model=llm.config.extraction_model_id,
        system=PROMPT,
        messages=[{"role": "user", "content": f"<customer_turn>\n{customer_turn}\n</customer_turn>"}],
        max_tokens=2000,
        thinking={"type": "disabled"},
        effort="low",
        output_format=ExtractionResult,
    )
    return result.parsed


def to_candidates(result: ExtractionResult) -> list[Candidate]:
    """Pure code. Source is 'customer' because extraction only runs on customer turns;
    scope_suggestion stays None because scope is the hub's decision, never the model's."""
    candidates = []
    for item in result.items:
        if item.type == "customer_context":
            fields = {"key": item.preference_key, "value": item.preference_value}
        elif item.type == "claim":
            fields = {"text": item.text}
            fields.update({k: v for k, v in (("amount_inr", item.amount_inr), ("merchant", item.merchant),
                                             ("payee", item.payee)) if v is not None})
        else:
            fields = {"text": item.text}
        candidates.append(Candidate(type=item.type, fields=fields, sensitive_categories=list(item.sensitive_categories),
                                    scope_suggestion=None, source="customer"))
    return candidates
