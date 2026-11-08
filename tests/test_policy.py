"""Policy resolution (SPEC 9, 17.3): pure functions, no database."""
import pytest

from hub.errors import PolicyError
from hub.policy import check_role_purpose, resolve

SEED = [  # the rows in sql/seed.sql
    {"agent_role": "payments_agent", "purpose": "payments_servicing", "case_type": "card_dispute", "sensitivity": "normal", "decision": "allow"},
    {"agent_role": "payments_agent", "purpose": "payments_servicing", "case_type": "upi_failed_debit", "sensitivity": "normal", "decision": "allow"},
    {"agent_role": "credit_agent", "purpose": "card_dispute_servicing", "case_type": "card_dispute", "sensitivity": "normal", "decision": "allow"},
    {"agent_role": "credit_agent", "purpose": "card_dispute_servicing", "case_type": "upi_failed_debit", "sensitivity": "normal", "decision": "existence_only"},
    {"agent_role": None, "purpose": None, "case_type": None, "sensitivity": "restricted", "decision": "deny"},
]


def rule(decision, agent_role=None, purpose=None, case_type=None, sensitivity=None):
    return {"agent_role": agent_role, "purpose": purpose, "case_type": case_type, "sensitivity": sensitivity,
            "decision": decision}


def credit(case_type, sensitivity="normal", rules=SEED):
    return resolve(rules, agent_role="credit_agent", purpose="card_dispute_servicing", case_type=case_type,
                   sensitivity=sensitivity)


def test_seed_credit_agent_sees_card_dispute_and_only_existence_of_upi():
    assert credit("card_dispute").decision == "allow"
    d = credit("upi_failed_debit")
    assert d.decision == "existence_only"
    assert d.reason == "rule(credit_agent, card_dispute_servicing, upi_failed_debit, normal) -> existence_only"


def test_restricted_is_denied_despite_role_rules():
    d = credit("card_dispute", "restricted")
    assert (d.decision, d.reason) == ("deny", "rule(*, *, *, restricted) -> deny")


def test_no_matching_rule_is_default_deny():
    d = resolve(SEED, agent_role="unknown_agent", purpose="x", case_type="card_dispute", sensitivity="normal")
    assert (d.decision, d.reason) == ("deny", "default deny: no matching rule")


def test_most_specific_rule_wins():
    rules = [rule("deny", agent_role="credit_agent"),
             rule("allow", agent_role="credit_agent", case_type="card_dispute")]
    assert credit("card_dispute", rules=rules).decision == "allow"
    assert credit("upi_failed_debit", rules=rules).decision == "deny"


def test_tie_goes_to_the_more_restrictive_decision():
    # Two different rules, both with 2 non-null columns, both matching.
    rules = [rule("allow", agent_role="credit_agent", case_type="card_dispute"),
             rule("existence_only", purpose="card_dispute_servicing", sensitivity="normal")]
    assert credit("card_dispute", rules=rules).decision == "existence_only"


def test_role_purpose_pairs():
    check_role_purpose("credit_agent", "card_dispute_servicing")
    with pytest.raises(PolicyError, match="purpose payments_servicing is not defined for role credit_agent"):
        check_role_purpose("credit_agent", "payments_servicing")
