"""T2 scenarios 1 to 12 (docs/SCENARIOS.md) as step streams, with deterministic probes.

Each probe answers the scenario's question from the backend's context only, and passes only if
that context answers it fully and correctly (the SCENARIOS.md pass rule). Each check returns
(passed, what the context says), so every verdict can be read and challenged.
"""
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from backends.lww import LwwContext, iso
from backends.steps import (CallbackRequest, Claim, Clock, CorrectClaim, CustomerPromise, OpenCase, Preference,
                            Step, Told)
from backends.structured import HubContext
from hub.clock import IST

CARD, PAY = "CASE-CARD-001", "PAY-2042"
MON_1830 = datetime(2026, 11, 2, 18, 30, tzinfo=IST)
TUE_0300 = datetime(2026, 11, 3, 3, 0, tzinfo=IST)
TUE_0900 = datetime(2026, 11, 3, 9, 0, tzinfo=IST)
TUE_1601 = datetime(2026, 11, 3, 16, 1, tzinfo=IST)
TUE_1800 = datetime(2026, 11, 3, 18, 0, tzinfo=IST)
TUE_1940 = datetime(2026, 11, 3, 19, 40, tzinfo=IST)
TUE_2015 = datetime(2026, 11, 3, 20, 15, tzinfo=IST)
TUE_2100 = datetime(2026, 11, 3, 21, 0, tzinfo=IST)
WED_0900 = datetime(2026, 11, 4, 9, 0, tzinfo=IST)
WED_0916 = datetime(2026, 11, 4, 9, 16, tzinfo=IST)
WED_0941 = datetime(2026, 11, 4, 9, 41, tzinfo=IST)
WED_1000 = datetime(2026, 11, 4, 10, 0, tzinfo=IST)
DEC_04 = datetime(2026, 12, 4, 10, 0, tzinfo=IST)
UPI_DETAILS = ["PAY-2042", "UPI-7781", "Ramesh", "4200"]

# --- shared story pieces (SPEC 13) --------------------------------------------------------------

def opening(callback: list[Step] | None = None) -> list[Step]:
    """Mon 18:30: claim (U1), customer promise (U2), callback (U3: the dialler fixtures expect U3)."""
    return [Clock(MON_1830), OpenCase("card_dispute"),
            Claim(CARD, "disputed_amount", {"text": "Charged twice for the same transaction", "amount_inr": 8500,
                                            "merchant": "UrbanCart"}),
            Told(CARD, "investigation has started"),
            CustomerPromise(CARD, "upload_statement", TUE_1800),
            *(callback if callback is not None else [CallbackRequest(CARD, 1, "21:00")])]


RESCHEDULE = [Clock(TUE_2015), CallbackRequest(CARD, 1, "09:00", reschedule=True),
              Preference("preferred_explanation_style", "simple English")]
UPI_CASE = [Clock(TUE_1940), OpenCase("upi_failed_debit"),
            Claim(PAY, "upi_failed_debit", {"text": "UPI payment failed but money was debited", "amount_inr": 4200,
                                            "payee": "Ramesh K"}),
            Told(PAY, "reversal expected by T+1")]


# --- reading the two kinds of context --------------------------------------------------------------

def hub_callback(ctx: HubContext):
    (cb,) = [u for c in ctx.packet.cases for u in c.units if u.action == "callback" and u.owner == "bank"]
    return cb


def hub_events(ctx: HubContext) -> list:
    return [e for c in ctx.packet.cases for e in c.events]


def hub_text(ctx: HubContext) -> str:
    return ctx.packet.model_dump_json() + json.dumps({str(k): v.model_dump(mode="json") for k, v in ctx.live.items()})


def lww_text(ctx: LwwContext) -> str:
    return json.dumps(ctx.memory) + json.dumps({str(k): v.model_dump(mode="json") for k, v in ctx.live.items()})


def when(dt: datetime | str) -> str:
    dt = datetime.fromisoformat(dt) if isinstance(dt, str) else dt
    return dt.astimezone(IST).strftime("%a %H:%M")


def live_dispute(ctx) -> str | None:
    return next((v.status for (kind, _), v in ctx.live.items() if kind == "dispute" and v is not None), None)


# --- probes: (passed, what the context says) -----------------------------------------------------------

def s1_hub(c):
    cb = hub_callback(c)
    return cb.due_at == WED_0900 and cb.status == "active", f"due {when(cb.due_at)}, {cb.status}"


def s1_lww(c):
    due, status = c.memory.get("callback_due"), c.memory.get("callback_status")
    return due == iso(WED_0900) and status == "active", f"due {when(due)}, {status}"


def s2_hub(c):
    cb = hub_callback(c)
    return cb.status == "missed", cb.status


def s2_lww(c):
    status = c.memory.get("callback_status")
    return status == "missed", f"{status} (no rule marks a miss)"


def s3_hub(c):
    cb = hub_callback(c)
    return cb.status == "attempted", f"{cb.status}: the bank called, no answer"


def s3_lww(c):
    status = c.memory.get("callback_status")
    return status == "attempted_no_answer", f"{status} (dialler record put)"


def s4_hub(c):
    corrected = [e for e in hub_events(c) if e.event_type == "status_corrected"]
    ok = hub_callback(c).status == "fulfilled" and len(corrected) == 1
    return ok, f"{hub_callback(c).status}; status_corrected on_time={corrected[0].payload['on_time'] if corrected else None}"


def s4_lww(c):
    status = c.memory.get("callback_status")
    return status == "completed", f"{status} (no record that it was reported missed first)"


def s5_hub(c):
    (claim,) = [u for case in c.packet.cases for u in case.units if u.unit_type == "claim"]
    return claim.payload.get("amount_inr") == 5800, f"Rs {claim.payload.get('amount_inr')}, version {claim.version}"


def s5_lww(c):
    amount = (c.memory.get("disputed_amount") or {}).get("amount_inr")
    return amount == 5800, f"Rs {amount} (Rs 8,500 overwritten, no history)"


def s6_hub(c):
    (hist,) = [h for case in c.packet.cases for h in case.commitment_history if h.action == "callback"]
    first, second = hist.versions[0], hist.versions[1]
    ok = first.due_at == TUE_2100 and first.shown_status == "superseded" and second.change_reason == "reschedule"
    return ok, f"v1 {when(first.due_at)} {first.shown_status}, v2 {second.change_reason}"


def s6_lww(c):
    found = iso(TUE_2100) in lww_text(c)
    return found, "Tue 21:00 present" if found else "only the current value: Tue 21:00 is gone"


def s7_hub(c):
    declined = [e for e in hub_events(c) if e.event_type == "request_declined"
                and e.payload["requested_due_at"] == iso(TUE_0300)]
    cb = hub_callback(c)
    ok = len(declined) == 1 and cb.due_at == TUE_0900 and cb.status == "active"
    return ok, f"03:00 declined ({declined[0].payload['reason'] if declined else 'none'}); {when(cb.due_at)} {cb.status}"


def s7_lww(c):
    request = c.memory.get("callback_request") or {}
    refused = "declined" in lww_text(c)
    return refused, f"last request {when(request.get('requested'))} {request.get('outcome')}; the refusal is overwritten"


def s8_hub(c):
    promises = [u for case in c.packet.cases for u in case.units if u.unit_type == "commitment" and u.owner == "customer"]
    ok = promises and all(u.status != "active" for u in promises)
    return ok, ", ".join(f"{u.action} {u.status}" for u in promises)


def s8_lww(c):
    promise = c.memory.get("customer_promise") or {}
    return promise.get("status") == "fulfilled", f"{promise.get('action')} {promise.get('status')}"


def s9_hub(c):
    leaked = [s for s in UPI_DETAILS if s in hub_text(c)]
    ok = [x.case_id for x in c.packet.cases] == [CARD] and c.packet.other_cases_summary == \
        "1 other open case: Payments" and not leaked
    return ok, f"cases {[x.case_id for x in c.packet.cases]}; '{c.packet.other_cases_summary}'; leaked {leaked}"


def s9_lww(c):
    leaked = [s for s in UPI_DETAILS if s in lww_text(c)]
    return not leaked, f"no policy filter: leaked {leaked}"


def s10_hub(c):
    told = [e.payload["message"] for e in hub_events(c) if e.event_type == "customer_informed"]
    status = live_dispute(c)
    return status == "provisional_credit_issued" and told == ["investigation has started"], \
        f"live: {status}; last told: {told[-1] if told else None}"


def s10_lww(c):
    told = (c.memory.get("last_told") or {}).get("message")
    status = live_dispute(c)
    return status == "provisional_credit_issued" and told == "investigation has started", \
        f"live via stored dispute_ref: {status}; last told: {told}"


def s11_hub(c):
    prefs = {p.key: p.value for p in c.packet.customer_context}
    return prefs.get("preferred_explanation_style") == "simple English", f"{prefs}"


def s11_lww(c):
    value = c.memory.get("preference:preferred_explanation_style")
    return value == "simple English", f"{value}"


def s12_hub(c):
    stored = "hospital" in hub_text(c).lower()
    return not stored, "stored" if stored else "not stored (admission test 3: health)"


def s12_lww(c):
    stored = "hospital" in lww_text(c).lower()
    return not stored, "stored under missed_payment_reason" if stored else "not stored"


@dataclass(frozen=True)
class Scenario:
    number: int
    steps: list[Step]
    role: str                                   # whose context answers the probe
    check_hub: Callable[[HubContext], tuple[bool, str]]
    check_lww: Callable[[LwwContext], tuple[bool, str]]


SCENARIOS = [
    Scenario(1, opening() + RESCHEDULE, "payments_agent", s1_hub, s1_lww),
    Scenario(2, opening() + RESCHEDULE + [Clock(WED_0916)], "payments_agent", s2_hub, s2_lww),
    Scenario(3, opening() + RESCHEDULE + [Clock(WED_0916, "attempted_no_answer")], "payments_agent", s3_hub, s3_lww),
    Scenario(4, opening() + RESCHEDULE + [Clock(WED_0916), Clock(WED_0941, "late_completed")], "payments_agent",
             s4_hub, s4_lww),
    Scenario(5, [Clock(MON_1830), OpenCase("card_dispute"),
                 Claim(CARD, "disputed_amount", {"text": "Charged twice", "amount_inr": 8500, "merchant": "UrbanCart"}),
                 CorrectClaim(CARD, "disputed_amount", {"text": "Charged twice", "amount_inr": 5800,
                                                        "merchant": "UrbanCart"})],
             "payments_agent", s5_hub, s5_lww),
    Scenario(6, opening() + RESCHEDULE, "payments_agent", s6_hub, s6_lww),
    Scenario(7, opening(callback=[CallbackRequest(CARD, 1, "03:00"), CallbackRequest(CARD, 1, "09:00")]),
             "payments_agent", s7_hub, s7_lww),
    Scenario(8, opening() + [Clock(TUE_1601)], "payments_agent", s8_hub, s8_lww),
    Scenario(9, opening() + UPI_CASE, "credit_agent", s9_hub, s9_lww),
    Scenario(10, opening() + [Clock(WED_1000)], "credit_agent", s10_hub, s10_lww),
    # No step closes the case: neither the hub interface nor the case mock can close one, and the
    # probe is about a customer-scope preference, which a case closure would not touch.
    Scenario(11, opening() + RESCHEDULE + [Clock(DEC_04)], "credit_agent", s11_hub, s11_lww),
    Scenario(12, [Clock(MON_1830), OpenCase("card_dispute"),
                  Claim(CARD, "missed_payment_reason", {"text": "I was in hospital, that's why I missed my payment."},
                        sensitive_categories=["health"])],
             "payments_agent", s12_hub, s12_lww),
]
