"""Hub backend for the T2 experiment: a thin adapter over hub/interface.py (SCENARIOS.md, method S).

Every step goes through the same interface calls the agents and the timeline use, so the
hub's rules (admission, acceptance policy, lifecycle sweep, access policy) all apply. The
context for a probe is the agent's policy-filtered packet plus the live facts its tools would
fetch from the packet's references (live facts are never stored, SPEC 2).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from backends.steps import (CallbackRequest, Claim, Clock, CorrectClaim, CustomerPromise, OpenCase, Preference,
                            Step, Told)
from hub.acceptance import check_callback
from hub.clock import SimClock
from hub.interface import ContextHub, Session
from hub.models import Candidate, UnitRef
from hub.packet import ContextPacket
from hub.timeres import RelativeTime, resolve
from scenarios.timeline import CUSTOMER, advance_to
from systems import dispute, statement, upi

PURPOSES = {"payments_agent": "payments_servicing", "credit_agent": "card_dispute_servicing"}


def fetch_live(kind: str, ref: str, now: datetime):
    """What an agent's live-fact tools return for a reference (shared with the baseline)."""
    if kind == "dispute":
        return dispute.get_dispute_status(ref, now)
    if kind == "statement":
        return statement.get_statement(ref, now)
    return upi.get_upi_status(ref, now)


@dataclass
class HubContext:
    packet: ContextPacket
    live: dict             # (kind, ref) -> live fact, fetched at read time


class HubBackend:
    name = "Hub"

    def __init__(self, app_url: str, start: datetime, threshold: timedelta):
        self.conn = psycopg.connect(app_url, autocommit=True)
        self.hub = ContextHub(self.conn, SimClock(start), threshold=threshold)

    def close(self) -> None:
        self.conn.close()

    def session(self, role: str = "payments_agent") -> Session:
        return self.hub.open_session(agent_id=role, agent_role=role, purpose=PURPOSES[role], customer_id=CUSTOMER,
                                     authenticated=True)

    def _units(self, case_id: str):
        (case,) = [c for c in self.hub.get_context(self.session()).cases if c.case_id == case_id]
        return case.units

    def apply(self, step: Step) -> None:
        hub, s = self.hub, self.session()
        now = hub.clock.now()
        match step:
            case Clock(at=at, dialler_profile=profile):
                advance_to(hub, at, dialler_profile=profile)  # delivers system records, runs the sweep
            case OpenCase(case_type=case_type):
                hub.open_case(s, case_type=case_type)
            case Claim(case_id=case_id, fields=fields, sensitive_categories=categories):
                # Admission decides; a rejected claim writes nothing (that is scenario 12).
                hub.propose_memory(s, Candidate(type="claim", fields=fields, sensitive_categories=categories,
                                                source="customer"), case_id=case_id)
            case CorrectClaim(case_id=case_id, fields=fields):
                (claim,) = [u for u in self._units(case_id) if u.unit_type == "claim"]
                hub.propose_memory(s, Candidate(type="claim", fields=fields, source="customer",
                                                corrects=UnitRef(unit_id=claim.unit_id, expected_version=claim.version)),
                                   case_id=case_id)
            case Told(case_id=case_id, message=message):
                hub.record_event(s, case_id=case_id, event_type="customer_informed", payload={"message": message})
            case CustomerPromise(case_id=case_id, action=action, due_at=due_at):
                hub.record_customer_commitment(s, case_id=case_id, action=action, due_at=due_at)
            case CallbackRequest(case_id=case_id, day_offset=day_offset, time=hhmm, reschedule=reschedule):
                decision = check_callback(resolve(RelativeTime(day_offset=day_offset, time=hhmm), now), now)
                if not decision.accepted:
                    hub.decline_request(s, case_id=case_id, action="callback", decision=decision)
                    return
                ref = None
                if reschedule:
                    (active,) = [u for u in self._units(case_id) if u.action == "callback" and u.status == "active"]
                    ref = UnitRef(unit_id=active.unit_id, expected_version=active.version)
                hub.accept_commitment(s, case_id=case_id, owner="bank", action="callback", due_at=decision.due_at,
                                      reschedule_of=ref)
            case Preference(key=key, value=value):
                hub.propose_memory(s, Candidate(type="customer_context", fields={"key": key, "value": value},
                                                source="customer"))
            case _:
                raise TypeError(f"unknown step {step!r}")

    def context(self, role: str) -> HubContext:
        packet = self.hub.get_context(self.session(role))
        now = self.hub.clock.now()
        live = {(r.kind, r.ref): fetch_live(r.kind, r.ref, now) for c in packet.cases for r in c.live_fact_refs}
        return HubContext(packet=packet, live=live)
