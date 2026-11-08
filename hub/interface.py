"""The hub's only door (SPEC 2). Agents, the timeline and the UI call these methods; nothing
else touches the database.

Every call:
* re-checks the session: role may use the purpose, and the session is authenticated (17.24);
* logs its access decisions to policy.access_log in their own transaction, so a refusal is
  recorded even though the call then fails;
* does its write (version or event, and any routed lifecycle change) in ONE transaction:
  all of it lands, or none of it does.
"""
from datetime import datetime, timedelta
from typing import Literal

from psycopg import Connection
from pydantic import BaseModel, ConfigDict

from hub import lifecycle, policy, store
from hub.acceptance import AcceptanceDecision, check_callback
from hub.admission import AdmissionDecision, admit
from hub.clock import SimClock
from hub.errors import CommitmentRefused, NotAuthenticatedError, PolicyError
from hub.models import Candidate, Case, NewEvent, UnitRef, UnitVersion
from hub.packet import (CaseView, CommitmentHistory, ContextPacket, EventView, LiveFactRef, OtherCase,
                        PreferenceView, UnitView, VersionView)
from systems import casemgmt

EVENTS_PER_CASE = 50  # packet size bound (SPEC 17.26)

# Event types each kind of caller may record. Agents report what they told the customer or that
# they escalated; only systems report outcomes that move a commitment's lifecycle.
AGENT_EVENT_TYPES = frozenset({"customer_informed", "escalated_to_human"})
SYSTEM_EVENT_TYPES = frozenset({"dialler_outcome", "statement_upload_recorded"})

BANK_ACTIONS = frozenset({"callback"})
# Customer promises the hub records, with the read-back phrase for each.
CUSTOMER_ACTIONS = {
    "upload_statement": "you'll upload your bank statement",
    "share_document": "you'll share the requested document",
    "make_payment": "you'll make the payment",
}

# Reference keys stored at case opening, and the tool that fetches the live fact behind each.
REF_KINDS = {
    "dispute_ref": ("dispute", "get_dispute_status"),
    "card_last4": ("statement", "get_statement"),
    "upi_ref": ("upi", "get_upi_status"),
}

NOT_AVAILABLE = "case not available"  # one message for 'no such case' and 'not allowed' (17.24)


class Session(BaseModel):
    model_config = ConfigDict(frozen=True)
    agent_id: str
    agent_role: str
    purpose: str
    customer_id: str
    authenticated: bool


class CaseOpened(BaseModel):
    case_id: str
    business_line: str
    case_type: str
    refs: dict[str, str]


class ProposeResult(BaseModel):
    decision: AdmissionDecision
    unit_id: str | None = None  # claim written
    item_id: str | None = None  # preference written


class AcceptResult(BaseModel):
    unit: UnitView
    read_back: str  # formatted in code; the agent relays it to the customer for confirmation


def _when(dt: datetime) -> str:
    """'Wednesday 4 November at 9:00 AM'."""
    hour = dt.hour % 12 or 12
    return f"{dt:%A} {dt.day} {dt:%B} at {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _unit_view(u: UnitVersion, now: datetime) -> UnitView:
    return UnitView(unit_id=u.unit_id, case_id=u.case_id, unit_type=u.unit_type, version=u.version, owner=u.owner,
                    action=u.action, due_at=u.due_at, status=u.status, payload=u.payload, source=u.source,
                    valid_from=u.valid_from, overdue=lifecycle.is_overdue(u, now))


def _event_view(e) -> EventView:
    return EventView(event_type=e.event_type, occurred_at=e.occurred_at, recorded_at=e.recorded_at,
                     actor_type=e.actor_type, case_id=e.case_id, unit_id=e.unit_id, payload=e.payload)


class ContextHub:
    def __init__(self, conn: Connection, clock: SimClock, *, threshold: timedelta) -> None:
        # Each method manages its own transaction; an outer implicit one would merge them.
        if not conn.autocommit:
            raise ValueError("ContextHub needs an autocommit connection")
        self.conn, self.clock, self.threshold = conn, clock, threshold
        # The lifecycle sweep runs on every clock change (SPEC 7).
        clock.add_listener(lambda old, new: lifecycle.sweep(conn, new, threshold))

    # --- sessions and gates ------------------------------------------------------------------

    def open_session(self, *, agent_id: str, agent_role: str, purpose: str, customer_id: str,
                     authenticated: bool) -> Session:
        session = Session(agent_id=agent_id, agent_role=agent_role, purpose=purpose, customer_id=customer_id,
                          authenticated=authenticated)
        self._check_purpose(session, "open_session")
        return session

    def _log(self, s: Session, case_id: str | None, unit_id: str | None, decision: str, reason: str) -> None:
        with self.conn.transaction():
            store.log_access(self.conn, at=self.clock.now(), agent_id=s.agent_id, agent_role=s.agent_role,
                             purpose=s.purpose, customer_id=s.customer_id, case_id=case_id, unit_id=unit_id,
                             decision=decision, reason=reason)

    def _check_purpose(self, s: Session, call: str) -> None:
        try:
            policy.check_role_purpose(s.agent_role, s.purpose)
        except PolicyError as exc:
            self._log(s, None, None, "deny", f"{call}: {exc}")
            raise

    def _gate(self, s: Session, call: str) -> None:
        """Every call: the role may use the purpose, and the session is authenticated."""
        self._check_purpose(s, call)
        if not s.authenticated:
            self._log(s, None, None, "deny", f"{call}: not authenticated")
            raise NotAuthenticatedError(f"{call}: session is not authenticated")

    def _refuse(self, s: Session, call: str, reason: str, case_id: str | None = None,
                public_reason: str | None = None) -> None:
        """Log the full reason; tell the caller only public_reason when given. The log is for
        auditors, the exception goes back to a model that may be under attacker control."""
        self._log(s, case_id, None, "deny", f"{call}: {reason}")
        raise PolicyError(f"{call}: {public_reason or reason}")

    def _agent_only(self, s: Session, call: str) -> None:
        if s.agent_role == "system":
            self._refuse(s, call, "system sessions may only record system events")

    def _writable_case(self, s: Session, call: str, case_id: str) -> Case:
        """A write needs an 'allow' decision on the case. 'No such case' and 'not allowed' get the
        same message, so a guessed or injected case ID cannot confirm that a case exists."""
        now = self.clock.now()
        case = store.get_case(self.conn, case_id)
        if case is None or case.customer_id != s.customer_id or case.opened_at > now:
            self._refuse(s, call, NOT_AVAILABLE)
        if s.agent_role == "system":
            self._log(s, case_id, None, "allow", f"{call}: system event writer")
            return case
        d = policy.resolve(store.load_rules(self.conn), agent_role=s.agent_role, purpose=s.purpose,
                           case_type=case.case_type, sensitivity="normal")
        if d.decision != "allow":
            self._refuse(s, call, f"{NOT_AVAILABLE} ({d.reason})", case_id, public_reason=NOT_AVAILABLE)
        self._log(s, case_id, None, "allow", f"{call}: {d.reason}")
        return case

    # --- read ----------------------------------------------------------------------------------

    def get_context(self, s: Session) -> ContextPacket:
        self._gate(s, "get_context")
        now = self.clock.now()
        cases, others = [], []
        with self.conn.transaction():  # reads and their access-log rows, together
            rules = store.load_rules(self.conn)
            for case in store.cases_as_of(self.conn, s.customer_id, now):
                case_d = policy.resolve(rules, agent_role=s.agent_role, purpose=s.purpose,
                                        case_type=case.case_type, sensitivity="normal")
                self._log(s, case.case_id, None, case_d.decision, f"case: {case_d.reason}")
                allowed = []
                for unit in store.units_as_of(self.conn, case.case_id, now):
                    unit_d = policy.resolve(rules, agent_role=s.agent_role, purpose=s.purpose,
                                            case_type=case.case_type, sensitivity=unit.sensitivity)
                    effective = policy.stricter(case_d, unit_d)  # a unit is never more visible than its case
                    self._log(s, case.case_id, unit.unit_id, effective.decision, f"unit: {effective.reason}")
                    if effective.decision == "allow":
                        allowed.append(unit)
                if case_d.decision == "allow":
                    cases.append(self._case_view(s, case, allowed, now))
                elif case_d.decision == "existence_only" and case.is_open(now):
                    others.append(OtherCase(business_line=case.business_line))
                # deny: nothing, not even existence

            prefs, customer_events = [], []
            context_allowed = s.purpose in policy.SERVICING_PURPOSES
            for item in store.items_as_of(self.conn, s.customer_id, now):
                decision = "allow" if context_allowed else "deny"
                self._log(s, None, item.item_id, decision, f"customer_context: {decision} for purpose {s.purpose}")
                if context_allowed:
                    prefs.append(PreferenceView(key=item.key, value=item.value, valid_from=item.valid_from))
            if context_allowed:
                customer_events = [_event_view(e) for e in
                                   store.events_as_of(self.conn, customer_id=s.customer_id, case_id=None, now=now)]

        summary = None
        if others:
            lines = ", ".join(sorted({o.business_line.title() for o in others}))
            summary = f"{len(others)} other open case{'s' if len(others) > 1 else ''}: {lines}"
        return ContextPacket(customer_id=s.customer_id, as_of=now, agent_role=s.agent_role, purpose=s.purpose,
                             cases=cases, customer_context=prefs, customer_events=customer_events,
                             other_cases=others, other_cases_summary=summary)

    def _case_view(self, s: Session, case: Case, units: list[UnitVersion], now: datetime) -> CaseView:
        allowed_ids = {u.unit_id for u in units}
        history = []
        for u in units:
            if u.unit_type == "commitment":
                versions = store.versions_as_of(self.conn, u.unit_id, now)
                shown = lifecycle.displayed_statuses(versions)
                history.append(CommitmentHistory(unit_id=u.unit_id, owner=u.owner, action=u.action, versions=[
                    VersionView(version=v.version, shown_status=st, due_at=v.due_at, change_reason=v.change_reason,
                                valid_from=v.valid_from, valid_to=v.valid_to if v.valid_to and v.valid_to <= now else None)
                    for v, st in zip(versions, shown)]))
        # Events about units this agent may not see are left out with the units themselves.
        events = [e for e in store.events_as_of(self.conn, customer_id=s.customer_id, case_id=case.case_id, now=now)
                  if e.unit_id is None or e.unit_id in allowed_ids]
        kept = events[-EVENTS_PER_CASE:]
        refs = [LiveFactRef(kind=REF_KINDS[k][0], ref=v, fetch_with=REF_KINDS[k][1])
                for k, v in store.case_refs(self.conn, case.case_id, now).items() if k in REF_KINDS]
        return CaseView(case_id=case.case_id, business_line=case.business_line, case_type=case.case_type,
                        opened_at=case.opened_at, closed_at=case.closed_at,
                        units=[_unit_view(u, now) for u in units], commitment_history=history,
                        events=[_event_view(e) for e in kept], events_truncated=len(events) - len(kept),
                        live_fact_refs=refs)

    # --- writes ------------------------------------------------------------------------------

    def open_case(self, s: Session, *, case_type: str) -> CaseOpened:
        """The case ID and references come from the case-management system, never the model."""
        self._gate(s, "open_case")
        self._agent_only(s, "open_case")
        if not policy.can_open(s.agent_role, case_type):
            self._refuse(s, "open_case", f"{s.agent_role} may not open {case_type} cases")
        opening = casemgmt.open_case(s.customer_id, case_type)
        now = self.clock.now()
        with self.conn.transaction():
            store.create_case(self.conn, Case(case_id=opening.case_id, customer_id=s.customer_id,
                                              business_line=opening.business_line, case_type=case_type, opened_at=now))
            store.append_event(self.conn, NewEvent(
                customer_id=s.customer_id, case_id=opening.case_id, event_type="case_opened", actor_type="agent",
                actor_id=s.agent_id, occurred_at=now, recorded_at=now, payload={"refs": opening.refs}))
        self._log(s, opening.case_id, None, "allow", f"open_case: {s.agent_role} may open {case_type}")
        return CaseOpened(case_id=opening.case_id, business_line=opening.business_line, case_type=case_type,
                          refs=opening.refs)

    def propose_memory(self, s: Session, candidate: Candidate, *, case_id: str | None = None) -> ProposeResult:
        """Admission decides; a rejection writes nothing at all (SPEC 17.17)."""
        self._gate(s, "propose_memory")
        self._agent_only(s, "propose_memory")
        decision = admit(candidate)
        if not decision.admitted or decision.scope == "session":
            return ProposeResult(decision=decision)
        now = self.clock.now()
        if decision.scope == "customer":
            if s.purpose not in policy.SERVICING_PURPOSES:
                self._refuse(s, "propose_memory", "customer context needs a servicing purpose")
            with self.conn.transaction():
                item = lifecycle.set_preference(self.conn, customer_id=s.customer_id, key=candidate.fields["key"],
                                                value=str(candidate.fields["value"]), now=now, actor_type="agent",
                                                actor_id=s.agent_id, source=candidate.source)
            return ProposeResult(decision=decision, item_id=item.item_id)
        if case_id is None:
            raise ValueError("a claim needs a case_id")
        self._writable_case(s, "propose_memory", case_id)
        with self.conn.transaction():
            if candidate.corrects is not None:
                unit = store.current_unit(self.conn, candidate.corrects.unit_id)
                if unit is None or unit.case_id != case_id:
                    raise PolicyError(f"propose_memory: {NOT_AVAILABLE}")
                unit = lifecycle.correct_claim(self.conn, unit.unit_id, expected_version=candidate.corrects.expected_version,
                                               new_fields=candidate.fields, now=now, actor_type="agent", actor_id=s.agent_id)
            else:
                unit = lifecycle.create_claim(self.conn, case_id=case_id, customer_id=s.customer_id,
                                              fields=candidate.fields, now=now, actor_type="agent",
                                              actor_id=s.agent_id, source=candidate.source)
        return ProposeResult(decision=decision, unit_id=unit.unit_id)

    def accept_commitment(self, s: Session, *, case_id: str, owner: Literal["bank", "customer"], action: str,
                          due_at: datetime, reschedule_of: UnitRef | None = None,
                          follows_unit_id: str | None = None) -> AcceptResult:
        """Record a promise. Bank callbacks re-run the acceptance policy here, so a model that
        skipped the check cannot create a promise the bank cannot keep."""
        self._gate(s, "accept_commitment")
        self._agent_only(s, "accept_commitment")
        self._writable_case(s, "accept_commitment", case_id)
        now = self.clock.now()
        if owner == "bank":
            if action not in BANK_ACTIONS:
                self._refuse(s, "accept_commitment", f"bank action {action} is not allowed", case_id)
            decision = check_callback(due_at, now)
            if not decision.accepted:
                raise CommitmentRefused(decision)
        else:
            if action not in CUSTOMER_ACTIONS:
                self._refuse(s, "accept_commitment", f"customer action {action} is not allowed", case_id)
            if due_at <= now:  # the customer's own deadline: no servicing hours, but it must be ahead
                raise CommitmentRefused(AcceptanceDecision(accepted=False, reason="in_the_past", due_at=due_at))
        with self.conn.transaction():
            if reschedule_of is not None:
                current = store.current_unit(self.conn, reschedule_of.unit_id)
                if current is None or current.case_id != case_id or (current.owner, current.action) != (owner, action):
                    raise PolicyError(f"accept_commitment: {NOT_AVAILABLE}")
                unit = lifecycle.reschedule(self.conn, current.unit_id, expected_version=reschedule_of.expected_version,
                                            new_due_at=due_at, now=now, actor_type="agent", actor_id=s.agent_id)
            else:
                payload = {}
                if follows_unit_id is not None:  # a new callback after a missed one (SPEC 17.8)
                    earlier = store.current_unit(self.conn, follows_unit_id)
                    if earlier is None or earlier.case_id != case_id or earlier.unit_type != "commitment":
                        raise PolicyError(f"accept_commitment: {NOT_AVAILABLE}")
                    payload = {"follows_unit_id": follows_unit_id}
                unit = lifecycle.create_commitment(self.conn, case_id=case_id, customer_id=s.customer_id, owner=owner,
                                                   action=action, due_at=due_at, now=now, actor_type="agent",
                                                   actor_id=s.agent_id, payload=payload)
        if owner == "customer":
            read_back = f"Noted: {CUSTOMER_ACTIONS[action]} by {_when(unit.due_at)}."
        elif reschedule_of is not None:
            read_back = f"I've moved your callback to {_when(unit.due_at)}. Is that right?"
        else:
            read_back = f"I've scheduled your callback for {_when(unit.due_at)}. Is that right?"
        return AcceptResult(unit=_unit_view(unit, now), read_back=read_back)

    def record_customer_commitment(self, s: Session, *, case_id: str, action: str, due_at: datetime) -> AcceptResult:
        """A customer's promise (SPEC 17.9): accept_commitment with owner 'customer'."""
        return self.accept_commitment(s, case_id=case_id, owner="customer", action=action, due_at=due_at)

    def decline_request(self, s: Session, *, case_id: str, action: str, decision: AcceptanceDecision) -> int:
        self._gate(s, "decline_request")
        self._agent_only(s, "decline_request")
        self._writable_case(s, "decline_request", case_id)
        with self.conn.transaction():
            return lifecycle.decline_request(self.conn, customer_id=s.customer_id, case_id=case_id, action=action,
                                             decision=decision, now=self.clock.now(), actor_type="agent",
                                             actor_id=s.agent_id)

    def record_event(self, s: Session, *, case_id: str, event_type: str, payload: dict,
                     occurred_at: datetime | None = None) -> int:
        """Append an event; system outcomes also move the lifecycle, in the same transaction."""
        self._gate(s, "record_event")
        is_system = s.agent_role == "system"
        if event_type not in (SYSTEM_EVENT_TYPES if is_system else AGENT_EVENT_TYPES):
            self._refuse(s, "record_event", f"{s.agent_role} may not record {event_type}", case_id)
        self._writable_case(s, "record_event", case_id)
        now = self.clock.now()
        # Only systems report past times (a dialler record learned late); an agent's event is now.
        occurred = occurred_at if (is_system and occurred_at is not None) else now
        if occurred > now:
            raise ValueError(f"record_event: occurred_at {occurred.isoformat()} is in the future")
        with self.conn.transaction():
            event_id = store.append_event(self.conn, NewEvent(
                customer_id=s.customer_id, case_id=case_id, unit_id=payload.get("unit_id"), event_type=event_type,
                actor_type="system" if is_system else "agent", actor_id=s.agent_id, occurred_at=occurred,
                recorded_at=now, payload=payload))
            if event_type == "dialler_outcome":
                unit = store.current_unit(self.conn, payload["unit_id"])
                if unit is None or unit.case_id != case_id:
                    raise PolicyError(f"record_event: {NOT_AVAILABLE}")
                lifecycle.apply_dialler_outcome(self.conn, unit.unit_id, outcome=payload["outcome"],
                                                occurred_at=occurred, now=now, threshold=self.threshold)
            elif event_type == "statement_upload_recorded":
                for unit in store.current_units(self.conn, case_id=case_id):
                    if (unit.owner, unit.action, unit.status) == ("customer", "upload_statement", "active"):
                        lifecycle.fulfil_customer_commitment(self.conn, unit.unit_id, evidence_event_id=event_id,
                                                             occurred_at=occurred, now=now, actor_id=s.agent_id)
        return event_id
