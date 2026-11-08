"""Expected state after each walkthrough step, written from SPEC section 13 (and 17).

Data, not code paths: each step has a list of (what SPEC 13 says, check). The walkthrough
evaluates them against a snapshot taken right after the step and prints ok / FAIL per line.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hub.clock import IST
from hub.lifecycle import displayed_statuses
from hub.models import ContextItem, Event, UnitVersion
from hub.packet import ContextPacket


@dataclass
class State:
    now: datetime
    units: dict[str, list[UnitVersion]]   # unit_id -> all versions, oldest first
    events: list[Event]                    # all events so far
    new_events: list[Event]                # appended by this step (including system records)
    items: list[ContextItem]               # current customer context
    cases: dict[str, str]                  # case_id -> business_line
    new_access: list[tuple]                # (case_id, unit_id, decision, reason) logged during the step
    commitments_before: int                # commitment units before the step
    result: dict[str, Any] = field(default_factory=dict)  # what a scripted conversation returned
    packet: ContextPacket | None = None    # what the next agent sees


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def cur(st: State, unit_id: str) -> UnitVersion:
    return st.units[unit_id][-1]


def shown(st: State, unit_id: str) -> list[str]:
    return displayed_statuses(st.units[unit_id])


def has_event(st: State, event_type: str, **payload) -> bool:
    return any(e.event_type == event_type and all(e.payload.get(k) == v for k, v in payload.items())
               for e in st.events)


def commitments(st: State) -> int:
    return sum(1 for versions in st.units.values() if versions[0].unit_type == "commitment")


Check = tuple[str, Callable[[State], bool]]

EXPECTED: dict[str, list[Check]] = {
    "Mon 18:30": [
        ("case CASE-CARD-001 opened on the payments line",
         lambda st: st.cases.get("CASE-CARD-001") == "payments"),
        ("claim v1: charged twice, Rs 8,500",
         lambda st: cur(st, "CASE-CARD-001-U1").payload["amount_inr"] == 8500 and cur(st, "CASE-CARD-001-U1").version == 1),
        ("communication event: customer told the investigation has started",
         lambda st: has_event(st, "customer_informed", message="investigation has started")),
        ("customer commitment: upload statement by Tue 18:00, active",
         lambda st: (cur(st, "CASE-CARD-001-U2").owner, cur(st, "CASE-CARD-001-U2").status,
                     cur(st, "CASE-CARD-001-U2").due_at) == ("customer", "active", at(3, 18))),
        ("bank callback v1 active, due Tue 3 Nov 21:00",
         lambda st: (cur(st, "CASE-CARD-001-U3").version, cur(st, "CASE-CARD-001-U3").status,
                     cur(st, "CASE-CARD-001-U3").due_at) == (1, "active", at(3, 21))),
    ],
    "Tue 16:00": [
        ("statement upload recorded by the documents system at Tue 16:00",
         lambda st: any(e.event_type == "statement_upload_recorded" and e.occurred_at == at(3, 16) for e in st.new_events)),
        ("customer commitment fulfilled", lambda st: cur(st, "CASE-CARD-001-U2").status == "fulfilled"),
    ],
    "Tue 19:40": [
        ("case PAY-2042 opened on the payments line", lambda st: st.cases.get("PAY-2042") == "payments"),
        ("claim: UPI failed debit Rs 4,200 to Ramesh K",
         lambda st: (cur(st, "PAY-2042-U1").payload["amount_inr"], cur(st, "PAY-2042-U1").payload["payee"]) == (4200, "Ramesh K")),
        ("communication event: reversal expected by T+1",
         lambda st: has_event(st, "customer_informed", message="reversal expected by T+1")),
    ],
    "Tue 20:15": [
        ("callback v1 superseded, v2 active due Wed 4 Nov 09:00",
         lambda st: shown(st, "CASE-CARD-001-U3") == ["superseded", "active"]
         and cur(st, "CASE-CARD-001-U3").due_at == at(4, 9)),
        ("customer context: preferred_explanation_style = simple English (reason not stored)",
         lambda st: [(i.key, i.value) for i in st.items] == [("preferred_explanation_style", "simple English")]
         and not any("banking terms" in str(e.payload) for e in st.events)),
    ],
    "Wed 09:15": [
        ("no dialler record: callback v3 missed",
         lambda st: shown(st, "CASE-CARD-001-U3") == ["superseded", "active", "missed"]),
        ("commitment_missed event, occurred 09:15",
         lambda st: any(e.event_type == "commitment_missed" and e.occurred_at == at(4, 9, 15) for e in st.new_events)),
    ],
    "Wed 10:00": [
        ("credit agent sees CASE-CARD-001 only, a payments-line case (cross-line)",
         lambda st: [(c.case_id, c.business_line) for c in st.result["packet"].cases] == [("CASE-CARD-001", "payments")]),
        ("context includes the claim, callback history with the miss, and the statement fulfilment",
         lambda st: any(u.unit_type == "claim" for u in st.result["packet"].cases[0].units)
         and any([v.shown_status for v in h.versions] == ["superseded", "active", "missed"]
                 for h in st.result["packet"].cases[0].commitment_history)
         and any(u.action == "upload_statement" and u.status == "fulfilled" for u in st.result["packet"].cases[0].units)),
        ("context includes the simple English preference",
         lambda st: [p.value for p in st.result["packet"].customer_context] == ["simple English"]),
        ("other case by existence only: '1 other open case: Payments'",
         lambda st: st.result["packet"].other_cases_summary == "1 other open case: Payments"),
        ("live dispute status fetched: provisional_credit_issued",
         lambda st: st.result["live"]["dispute"].status == "provisional_credit_issued"),
        ("live statement fetched: payable Rs 23,980 after provisional credit",
         lambda st: st.result["live"]["statement"].total_due_inr == 23980),
        ("new callback Wed 16:00 accepted, linked to the missed one",
         lambda st: (cur(st, "CASE-CARD-001-U4").status, cur(st, "CASE-CARD-001-U4").due_at,
                     cur(st, "CASE-CARD-001-U4").payload) == ("active", at(4, 16), {"follows_unit_id": "CASE-CARD-001-U3"})),
    ],
    "Wed 10:05": [
        ("injection: no UPI details in what the hub returned",
         lambda st: not any(s in st.result["packet"].model_dump_json() for s in ("PAY-2042", "UPI-7781", "Ramesh", "4200"))),
        ("access log shows existence_only for PAY-2042",
         lambda st: any(r[0] == "PAY-2042" and r[2] == "existence_only" for r in st.new_access)),
    ],
    "Wed 10:08": [
        ("interest waiver escalated to a human, payload holds references only",
         lambda st: any(e.event_type == "escalated_to_human" and set(e.payload) == {"reason", "packet_as_of"}
                        for e in st.new_events)),
        ("no promise made: no new commitment", lambda st: commitments(st) == st.commitments_before),
    ],
}
