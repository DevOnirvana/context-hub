"""Admission: the three tests that decide whether a candidate becomes memory (SPEC 3).

Pure function, no database, no LLM. It takes an already-structured Candidate (built by hand
in tests, by the extractor from Step 5 on) and returns a decision. Code, not the model,
decides admission and scope.

Privacy rule (SPEC 17.17): a decision's reason is always a fixed phrase, never text taken
from the candidate. A rejected health statement leaves only "rejected: test 3, category health".
"""
import re
from typing import Any, Literal

from pydantic import BaseModel

from hub.models import Candidate

Scope = Literal["session", "case", "customer"]


class AdmissionDecision(BaseModel):
    admitted: bool
    scope: Scope                     # where it would live; 'session' means never written to the hub
    failed_test: Literal[1, 2, 3] | None = None
    category: str | None = None      # sensitive category, when test 3 fails
    reason: str                      # fixed phrase, never contains candidate text


# Test 1: the only preference keys the hub stores. Anything else is not a defined type.
PREFERENCE_KEYS = frozenset({"preferred_explanation_style", "preferred_contact_channel", "preferred_language"})

# Test 2: facts that authoritative systems hold. A system-sourced candidate carrying one is a
# copy of a live fact and is refused; the agent fetches it at read time instead. A customer
# saying "I uploaded it" is different: that is a claim about what the customer said.
LIVE_FACT_KEYS = frozenset({
    "dispute_status", "balance", "statement", "total_due", "minimum_due", "upi_status", "upload_status",
})

# Test 3: category names the hub may record. Anything else from the extractor is logged as
# 'other' so a free-text category can never smuggle personal content into a reason.
KNOWN_SENSITIVE_CATEGORIES = frozenset({
    "health", "religion", "caste", "sexual_orientation", "political_opinion", "biometric", "genetic",
})

# Test 3 keyword backstop (SPEC 17.17). Fail-closed: a false positive rejects, by design.
# Known false positive: "Apollo Hospital Pharmacy" (a merchant name). The Step 5 extractor flags
# it as health too (recorded run, 8 Nov 2026); losing that claim is cheaper than storing health data.
SENSITIVE_KEYWORDS = {
    "health": re.compile(
        r"\b(hospital\w*|diagnos\w*|illness\w*|surgery|doctor\w*|medical|medication\w*|"
        r"cancer|pregnan\w*|disease\w*|icu|chemo\w*)",
        re.IGNORECASE,
    ),
}

ADMITTED_SCOPE: dict[str, Scope] = {
    "claim": "case",               # tied to one case
    "customer_context": "customer",  # relevant across cases
    "request": "session",          # pending; becomes a commitment only via accept_commitment()
}


def _texts(value: Any):
    """Every string inside the candidate's fields, however nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _texts(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _texts(v)


def _sensitive_category(c: Candidate) -> str | None:
    for category in c.sensitive_categories:
        return category if category in KNOWN_SENSITIVE_CATEGORIES else "other"
    for category, pattern in SENSITIVE_KEYWORDS.items():
        if any(pattern.search(text) for text in _texts(c.fields)):
            return category
    return None


def _reject(test: Literal[1, 2, 3], reason: str, category: str | None = None) -> AdmissionDecision:
    return AdmissionDecision(admitted=False, scope="session", failed_test=test, category=category,
                             reason=f"rejected: test {test}, {reason}")


def admit(candidate: Candidate) -> AdmissionDecision:
    c = candidate
    # Test 1, defined type: one of the memory types, or the request pathway.
    if c.type not in ADMITTED_SCOPE and c.type != "live_fact_query":
        return _reject(1, f"type {c.type} is not memory")
    if c.type == "customer_context" and c.fields.get("key") not in PREFERENCE_KEYS:
        return _reject(1, "unknown preference key")

    # Test 2, context value: not something an authoritative system already holds.
    if c.type == "live_fact_query":
        return _reject(2, "live fact, fetch from the system of record")
    if c.source == "system" and LIVE_FACT_KEYS & set(c.fields):
        return _reject(2, "copy of a system fact")

    # Test 3, purpose and permission: sensitive categories are rejected by default.
    category = _sensitive_category(c)
    if category is not None:
        return _reject(3, f"category {category}", category)

    scope = ADMITTED_SCOPE[c.type]
    note = "" if c.scope_suggestion in (None, scope) else f" (suggested {c.scope_suggestion}, overruled)"
    return AdmissionDecision(admitted=True, scope=scope, reason=f"admitted: {scope}{note}")
