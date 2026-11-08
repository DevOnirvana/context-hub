"""Admission tests 1 to 3 on structured candidates (no LLM in Step 2)."""
import pytest

from hub.admission import admit
from hub.models import Candidate


def cand(type_, fields=None, source="customer", **kw):
    return Candidate(type=type_, fields=fields or {}, source=source, **kw)


def test_duplicate_charge_claim_admitted_to_case():
    d = admit(cand("claim", {"text": "I have been charged twice for the same transaction", "amount_inr": 8500}))
    assert (d.admitted, d.scope) == (True, "case")


def test_customer_says_uploaded_is_a_claim_not_a_live_fact():
    d = admit(cand("claim", {"text": "I have already uploaded my bank statement"}))
    assert (d.admitted, d.scope) == (True, "case")


def test_preference_admitted_to_customer_scope():
    d = admit(cand("customer_context", {"key": "preferred_explanation_style", "value": "simple English"}))
    assert (d.admitted, d.scope) == (True, "customer")


def test_unknown_preference_key_fails_test_1():
    d = admit(cand("customer_context", {"key": "favourite_colour", "value": "blue"}))
    assert (d.admitted, d.failed_test) == (False, 1)


def test_small_talk_fails_test_1():
    d = admit(cand("small_talk", {"text": "Thanks, have a good evening."}))
    assert (d.admitted, d.failed_test, d.scope) == (False, 1, "session")


def test_status_question_fails_test_2():
    d = admit(cand("live_fact_query", {"text": "What is the current status of my dispute?"}))
    assert (d.admitted, d.failed_test) == (False, 2)


def test_system_copy_of_live_fact_fails_test_2():
    d = admit(cand("claim", {"dispute_status": "provisional_credit_issued"}, source="system"))
    assert (d.admitted, d.failed_test) == (False, 2)


def test_request_stays_in_session_until_accepted():
    d = admit(cand("request", {"text": "Please call me tomorrow at 9 AM"}))
    assert (d.admitted, d.scope) == (True, "session")


def test_scope_suggestion_is_overruled_by_code():
    d = admit(cand("claim", {"text": "charged twice"}, scope_suggestion="customer"))
    assert d.scope == "case" and "overruled" in d.reason


def test_hospital_statement_rejected_and_text_never_kept():
    text = "I was in hospital, that's why I missed my payment."
    d = admit(cand("claim", {"text": text}))
    assert (d.admitted, d.failed_test, d.category) == (False, 3, "health")
    # SPEC 17.17: the only record is the fixed phrase; nothing from the input survives.
    assert d.reason == "rejected: test 3, category health"
    assert "hospital" not in d.model_dump_json().lower()


def test_extractor_category_rejected_and_unknown_category_not_echoed():
    assert admit(cand("claim", {"text": "x"}, sensitive_categories=["health"])).category == "health"
    d = admit(cand("claim", {"text": "x"}, sensitive_categories=["chronic back pain"]))
    assert d.reason == "rejected: test 3, category other"


def test_keyword_found_in_nested_fields():
    d = admit(cand("claim", {"text": "late payment", "notes": ["was diagnosed last week"]}))
    assert d.failed_test == 3


@pytest.mark.known_limitation
def test_apollo_hospital_pharmacy_rejected_known_false_positive():
    """KNOWN LIMITATION (SPEC 17.17), kept on purpose: the keyword backstop is fail-closed, so a
    duplicate charge at a merchant named "Apollo Hospital Pharmacy" is rejected. Losing this claim
    is cheaper than storing health data. The Step 5 extractor's categories will refine it."""
    d = admit(cand("claim", {"text": "Charged twice at Apollo Hospital Pharmacy", "amount_inr": 1200}))
    assert (d.admitted, d.failed_test, d.reason) == (False, 3, "rejected: test 3, category health")
