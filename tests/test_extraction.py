"""Step 5 acceptance: extraction in replay mode, no network.

The eight cases replay recorded responses from .llm_cache/ (recorded once with
LLM_MODE=record pytest tests/test_extraction.py). Model output is checked by meaning, not by
exact wording; then every candidate goes through the hub's own admission, as in production.
"""
import pytest
from pydantic import ValidationError

from agents.extraction import ExtractedItem, ExtractionResult, extract, to_candidates
from agents.llm import LLM, load_config
from hub.admission import admit


def item(**kw):
    base = dict(type="claim", text="x", amount_inr=None, merchant=None, payee=None, preference_key=None,
                preference_value=None, sensitive_categories=[])
    return ExtractedItem(**{**base, **kw})


# --- mapping onto Candidate (pure code) ------------------------------------------------------

def test_mapping_sets_source_and_scope_in_code():
    (claim, pref, req) = to_candidates(ExtractionResult(items=[
        item(text="charged twice, Rs 8,500 at UrbanCart", amount_inr=8500, merchant="UrbanCart"),
        item(type="customer_context", text="simple English please", preference_key="preferred_explanation_style",
             preference_value="simple English"),
        item(type="request", text="call me at 9"),
    ]))
    assert claim.fields == {"text": "charged twice, Rs 8,500 at UrbanCart", "amount_inr": 8500, "merchant": "UrbanCart"}
    assert pref.fields == {"key": "preferred_explanation_style", "value": "simple English"}
    assert req.fields == {"text": "call me at 9"}
    assert {(c.source, c.scope_suggestion) for c in (claim, pref, req)} == {("customer", None)}


def test_model_cannot_set_scope_or_unknown_keys():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({"items": [{**item().model_dump(), "scope": "customer"}]})
    with pytest.raises(ValidationError):
        item(type="customer_context", preference_key="favourite_colour", preference_value="blue")
    with pytest.raises(ValidationError):
        item(sensitive_categories=["chronic back pain"])  # only the fixed category list


# --- acceptance: recorded extraction, replayed offline ------------------------------------------

@pytest.fixture(scope="module")
def llm():
    config = load_config()  # LLM_MODE from the shell or .env; replay by default
    if config.mode == "replay":
        def no_network(_):
            raise AssertionError("replay mode must not build an API client")
        return LLM(config, client_factory=no_network)
    return LLM(config)


def kinds(result):
    return [i.type for i in result.items]


def admitted(result):
    return [c for c in to_candidates(result) if admit(c).admitted]


def test_duplicate_charge_is_a_claim_with_amount(llm):
    r = extract(llm, "I have been charged twice for the same transaction. It was Rs 8,500 at UrbanCart.")
    # By meaning: the model may split this into several claims; one of them must carry the details.
    claims = [i for i in r.items if i.type == "claim"]
    assert any(c.amount_inr == 8500 and c.merchant == "UrbanCart" for c in claims)
    assert [admit(c).scope for c in admitted(r)] == ["case"] * len(admitted(r))


def test_uploaded_statement_is_a_claim_not_a_fact(llm):
    r = extract(llm, "I have already uploaded my bank statement.")
    assert kinds(r) == ["claim"]


def test_status_question_is_a_live_fact_query(llm):
    r = extract(llm, "What is the current status of my dispute?")
    assert kinds(r) == ["live_fact_query"] and admitted(r) == []


def test_callback_is_a_request(llm):
    r = extract(llm, "Please call me tomorrow at 9 PM.")
    assert kinds(r) == ["request"]


def test_reschedule_and_preference_without_the_reason(llm):
    r = extract(llm, "Can you make that callback 9 AM tomorrow instead? And please explain things in simple English, "
                     "I find banking terms hard.")
    assert "request" in kinds(r)
    (pref,) = [i for i in r.items if i.type == "customer_context"]
    assert pref.preference_key == "preferred_explanation_style" and "simple english" in pref.preference_value.lower()
    assert "hard" not in pref.preference_value.lower() and "terms" not in pref.preference_value.lower()


def test_small_talk_is_not_stored(llm):
    r = extract(llm, "Thanks, have a good evening.")
    assert kinds(r) == ["small_talk"] and admitted(r) == []


def test_hospital_is_flagged_health_and_rejected(llm):
    r = extract(llm, "I was in hospital, that's why I missed my payment.")
    assert any("health" in i.sensitive_categories for i in r.items)
    assert admitted(r) == []
    assert {admit(c).reason for c in to_candidates(r) if c.type in ("claim", "other")} <= {
        "rejected: test 3, category health"}


@pytest.mark.known_limitation
def test_apollo_hospital_pharmacy_extractor_clean_but_backstop_still_rejects(llm):
    """SPEC 17.17: a merchant name is not health data about the customer, and the prompt says so.
    Observed in the recorded run (8 Nov 2026): Haiku 5.5 still flags 'health'. Both layers reject,
    fail-closed, and the only trace is the fixed reason. Kept on purpose: losing this claim is
    cheaper than storing health data. A real fix is an allowlist of merchant names (LATER.md)."""
    r = extract(llm, "I was charged twice at Apollo Hospital Pharmacy, Rs 1,200.")
    claims = [i for i in r.items if i.type == "claim"]
    assert any(c.amount_inr == 1200 for c in claims)
    assert admitted(r) == []
    assert {admit(c).reason for c in to_candidates(r)} == {"rejected: test 3, category health"}
