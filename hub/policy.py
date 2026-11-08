"""Access policy (SPEC 9, 17.3, 17.23, 17.24). Plain code, never delegated to the model.

A prompt can be subverted; a permission cannot be talked out of. Everything here is a lookup
or a comparison, so the same request always gets the same answer and the same logged reason.
"""
from typing import Literal

from pydantic import BaseModel

from hub.errors import PolicyError

Decision = Literal["allow", "existence_only", "deny"]

# Which purposes each role may claim. A session asking for anything else is refused at the door.
ROLE_PURPOSES: dict[str, frozenset[str]] = {
    "payments_agent": frozenset({"payments_servicing"}),
    "credit_agent": frozenset({"card_dispute_servicing"}),
    "system": frozenset({"system_event"}),  # dialler, documents: record events, never read
}

# The agent's own business line; a case on another line is a cross-line read (SPEC 17.22).
ROLE_LINE = {"payments_agent": "payments", "credit_agent": "credit"}

# Customer context is allowed for any servicing purpose (SPEC 9), decided here, not in policy.rules.
SERVICING_PURPOSES = frozenset({"payments_servicing", "card_dispute_servicing"})

# Who may open which case types (SPEC 17.23).
OPEN_CASE_TYPES: dict[str, frozenset[str]] = {
    "payments_agent": frozenset({"card_dispute", "upi_failed_debit"}),
    "credit_agent": frozenset(),
}

# Lower is stricter. On a tie between equally specific rules the stricter decision wins.
STRICTNESS: dict[str, int] = {"deny": 0, "existence_only": 1, "allow": 2}

MATCH_COLUMNS = ("agent_role", "purpose", "case_type", "sensitivity")


class PolicyDecision(BaseModel):
    decision: Decision
    reason: str


def describe(rule: dict) -> str:
    cols = ", ".join(rule[c] if rule[c] is not None else "*" for c in MATCH_COLUMNS)
    return f"rule({cols}) -> {rule['decision']}"


def resolve(rules: list[dict], *, agent_role: str, purpose: str, case_type: str, sensitivity: str) -> PolicyDecision:
    """1. Keep the rules whose every non-null column equals the request (NULL = wildcard).
    2. None left: default deny.
    3. Keep the most specific (most non-null columns).
    4. Tie: the most restrictive decision wins."""
    request = {"agent_role": agent_role, "purpose": purpose, "case_type": case_type, "sensitivity": sensitivity}
    matching = [r for r in rules if all(r[c] is None or r[c] == request[c] for c in MATCH_COLUMNS)]
    if not matching:
        return PolicyDecision(decision="deny", reason="default deny: no matching rule")
    specificity = lambda r: sum(r[c] is not None for c in MATCH_COLUMNS)  # noqa: E731
    top = max(specificity(r) for r in matching)
    best = min((r for r in matching if specificity(r) == top), key=lambda r: STRICTNESS[r["decision"]])
    return PolicyDecision(decision=best["decision"], reason=describe(best))


def stricter(a: PolicyDecision, b: PolicyDecision) -> PolicyDecision:
    """The more restrictive of two decisions (a unit can never be more visible than its case)."""
    return a if STRICTNESS[a.decision] < STRICTNESS[b.decision] else b


def check_role_purpose(agent_role: str, purpose: str) -> None:
    if purpose not in ROLE_PURPOSES.get(agent_role, frozenset()):
        raise PolicyError(f"purpose {purpose} is not defined for role {agent_role}")


def can_open(agent_role: str, case_type: str) -> bool:
    return case_type in OPEN_CASE_TYPES.get(agent_role, frozenset())
