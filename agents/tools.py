"""Agent tools (SPEC 10): what each role may call, and the code that runs each call.

The model chooses a tool and its arguments; everything here is plain Python that calls
hub/interface.py or systems/ only. Guards live in code, not in the prompt:
* every case_id must be one of the cases in this turn's policy-filtered packet, so a guessed or
  injected ID ("PAY-2042") gets the same "case not available" as a case that does not exist;
* live facts are fetched by the reference stored on the case, never by a reference the model
  typed, so the model cannot ask the dispute system about someone else's dispute;
* propose_memory only accepts words that the customer actually said this turn;
* callback times arrive as {day_offset, time} and are resolved against the simulated clock here.
Neither toolset can move money; the credit agent has no UPI tool at all.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Any

from agents.extraction import extract, to_candidates
from agents.llm import LLM
from hub.acceptance import check_callback
from hub.errors import CommitmentRefused, HubError
from hub.interface import CUSTOMER_ACTIONS, NOT_AVAILABLE, ContextHub, Session
from hub.models import UnitRef
from hub.packet import CaseView, ContextPacket
from hub.timeres import RelativeTime, resolve
from systems import dispute, statement, upi

# Fixed reasons for a hand-over. An enum, not free text: the reason is stored in an event, and
# model-written text there could carry anything the customer typed (SPEC 17.17 spirit).
ESCALATION_REASONS = {
    "interest_waiver_request": "interest waiver request",
    "fee_reversal_request": "fee reversal request",
    "chargeback_request": "chargeback request",
    "provisional_credit_request": "provisional credit request",
    "customer_asked_for_a_person": "customer asked for a person",
    "out_of_scope": "out of scope",
}
# Reasons only code uses (SPEC 17.35 and the step cap); the model cannot pick them.
CODE_ESCALATIONS = {"model_refusal": "model declined the request", "step_cap": "agent step cap reached"}


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or list(properties),
            "additionalProperties": False}


CASE_ID = {"type": "string", "description": "A case_id from your context packet."}
# Ranges and formats are checked in code (RelativeTime, _when): strict tool schemas accept only
# a subset of JSON Schema, so constraints live in the description and in Python.
DAY_OFFSET = {"type": "integer",
              "description": "0 = today, 1 = tomorrow (0 to 14), as the customer said it. Never compute dates yourself."}
TIME = {"type": "string", "description": "24-hour IST time as HH:MM, e.g. 16:00."}

# strict: the API guarantees the arguments match the schema; code still validates meaning.
TOOLS: dict[str, dict] = {
    "get_context": {
        "name": "get_context",
        "description": "Refresh your context packet (what the bank remembers about this customer, filtered by "
                       "access policy). You already receive it with every customer turn; call this only after "
                       "you changed something and need the current state.",
        "input_schema": _schema({}),
    },
    "get_statement": {
        "name": "get_statement",
        "description": "Live card statement for a case: due date, total due, minimum due, disputed amount, any "
                       "provisional credit. Always fetch; never answer amounts from memory.",
        "input_schema": _schema({"case_id": CASE_ID}),
    },
    "get_dispute_status": {
        "name": "get_dispute_status",
        "description": "Live status of the dispute on a case. Always fetch; never answer status from memory.",
        "input_schema": _schema({"case_id": CASE_ID}),
    },
    "get_upi_status": {
        "name": "get_upi_status",
        "description": "Live status of the UPI payment on a case.",
        "input_schema": _schema({"case_id": CASE_ID}),
    },
    "propose_memory": {
        "name": "propose_memory",
        "description": "Propose that the bank remembers something the customer said in this turn (a claim about "
                       "their case, or a lasting preference about how to deal with them). Quote the customer's "
                       "words exactly. When the customer corrects something they said earlier (for example a "
                       "wrong amount), pass that claim's unit_id from your packet as corrects_unit_id. Not for "
                       "promises: use record_customer_commitment. The bank's rules decide what is stored; you get "
                       "the decision back.",
        "input_schema": _schema({
            "case_id": {"type": ["string", "null"], "description": "The case the words are about, or null."},
            "customer_words": {"type": "string", "description": "An exact quote from the customer's current turn."},
            "corrects_unit_id": {"type": ["string", "null"],
                                 "description": "The unit_id of the claim being corrected, from your packet, or null."},
        }),
    },
    "request_callback": {
        "name": "request_callback",
        "description": "Ask for a bank callback at the time the customer asked for. kind 'reschedule' moves the "
                       "active callback; 'new' books a new one. The bank's rules accept it or decline it with the "
                       "next valid slot. If accepted, read the read_back text to the customer word for word.",
        "input_schema": _schema({"case_id": CASE_ID, "day_offset": DAY_OFFSET, "time": TIME,
                                 "kind": {"type": "string", "enum": ["new", "reschedule"]}}),
    },
    "record_customer_commitment": {
        "name": "record_customer_commitment",
        "description": "Record something the customer promised to do by a time they gave. Quote the promise "
                       "exactly in customer_words.",
        "input_schema": _schema({"case_id": CASE_ID, "action": {"type": "string", "enum": sorted(CUSTOMER_ACTIONS)},
                                 "day_offset": DAY_OFFSET, "time": TIME,
                                 "customer_words": {"type": "string",
                                                    "description": "An exact quote of the promise from the customer's "
                                                                   "current turn."}}),
    },
    "escalate_to_human": {
        "name": "escalate_to_human",
        "description": "Hand the conversation to a human colleague with your context packet. Use it for anything "
                       "you may not do: interest or fee waivers, chargebacks, provisional credit, or anything out "
                       "of scope. Do not promise the outcome.",
        "input_schema": _schema({"case_id": CASE_ID, "reason": {"type": "string", "enum": sorted(ESCALATION_REASONS)}}),
    },
}
for _tool in TOOLS.values():
    _tool["strict"] = True

TOOLSETS = {
    "credit_agent": ["get_context", "get_statement", "get_dispute_status", "propose_memory", "request_callback",
                     "record_customer_commitment", "escalate_to_human"],
    "payments_agent": ["get_context", "get_dispute_status", "get_upi_status", "propose_memory", "request_callback",
                       "record_customer_commitment", "escalate_to_human"],
}


def tools_for(role: str) -> list[dict]:
    """A fixed list in a fixed order per role: the tool definitions are part of the cached prefix."""
    return [TOOLS[name] for name in TOOLSETS[role]]


class ToolError(Exception):
    """A refusal the model is told about (as an is_error tool result). Never carries hidden data."""


@dataclass
class ToolContext:
    hub: ContextHub
    llm: LLM
    session: Session
    packet: ContextPacket
    customer_turn: str
    escalated: bool = False
    writes: list[str] = field(default_factory=list)
    committed_words: list[str] = field(default_factory=list)  # quotes already recorded as commitments this turn

    def refresh(self) -> ContextPacket:
        self.packet = self.hub.get_context(self.session)
        return self.packet

    def case(self, case_id: str | None) -> CaseView:
        for case in self.packet.cases:
            if case.case_id == case_id:
                return case
        raise ToolError(NOT_AVAILABLE)  # same words for 'no such case' and 'not allowed'

    def ref(self, case_id: str, kind: str) -> str:
        for ref in self.case(case_id).live_fact_refs:
            if ref.kind == kind:
                return ref.ref
        raise ToolError(f"this case has no {kind} record")


def _when(day_offset: int, hhmm: str) -> RelativeTime:
    if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", hhmm):
        raise ToolError("time must be HH:MM, 24-hour")
    hours, minutes = map(int, hhmm.split(":"))
    try:
        return RelativeTime(day_offset=day_offset, time=dtime(hours, minutes))
    except ValueError as exc:  # day_offset outside 0 to 14
        raise ToolError("day_offset must be between 0 and 14") from exc


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def escalate(ctx: ToolContext, case_id: str, reason_phrase: str) -> dict:
    """Shared by the tool and by code (refusal, step cap): the human gets the current packet."""
    packet = ctx.refresh()
    ctx.hub.record_event(ctx.session, case_id=case_id, event_type="escalated_to_human",
                         payload={"reason": reason_phrase, "packet_as_of": packet.as_of.isoformat()})
    ctx.escalated = True
    ctx.writes.append("escalated_to_human")
    return {"escalated": True, "reason": reason_phrase,
            "note": "A colleague now has the full context. Tell the customer, and promise no outcome."}


# --- tool implementations ------------------------------------------------------------------------

def _get_context(ctx: ToolContext) -> Any:
    return ctx.refresh().model_dump(mode="json")


def _get_statement(ctx: ToolContext, case_id: str) -> Any:
    return statement.get_statement(ctx.ref(case_id, "statement"), ctx.hub.clock.now()).model_dump(mode="json")


def _get_dispute_status(ctx: ToolContext, case_id: str) -> Any:
    status = dispute.get_dispute_status(ctx.ref(case_id, "dispute"), ctx.hub.clock.now())
    return None if status is None else status.model_dump(mode="json")


def _get_upi_status(ctx: ToolContext, case_id: str) -> Any:
    status = upi.get_upi_status(ctx.ref(case_id, "upi"), ctx.hub.clock.now())
    return None if status is None else status.model_dump(mode="json")


def _check_quote(ctx: ToolContext, customer_words: str) -> None:
    # The model may only quote the customer, never author a "memory" of its own.
    if not customer_words.strip() or _normalise(customer_words) not in _normalise(ctx.customer_turn):
        raise ToolError("customer_words must be an exact quote from the customer's current turn")


def _overlaps(a: str, b: str) -> bool:
    a, b = _normalise(a), _normalise(b)
    return a in b or b in a


def _corrected_claim(ctx: ToolContext, case_id: str | None, unit_id: str):
    """The claim being corrected must be in this turn's packet (so: this customer, and visible to
    this agent), on the same case. Anything else gets the same 'not available' as a missing case."""
    for unit in ctx.case(case_id).units:
        if unit.unit_id == unit_id and unit.unit_type == "claim":
            return unit
    raise ToolError(NOT_AVAILABLE)


def _propose_memory(ctx: ToolContext, case_id: str | None, customer_words: str,
                    corrects_unit_id: str | None = None) -> Any:
    _check_quote(ctx, customer_words)
    # A promise already recorded this turn is a commitment, not also a claim (rehearsal round 1, c).
    if any(_overlaps(customer_words, words) for words in ctx.committed_words):
        return {"already_recorded": "already recorded as a commitment"}
    if case_id is not None:
        ctx.case(case_id)
    if corrects_unit_id is not None:
        return _correct_claim(ctx, case_id, customer_words, _corrected_claim(ctx, case_id, corrects_unit_id))
    decisions = []
    for candidate in to_candidates(extract(ctx.llm, customer_words)):
        try:
            result = ctx.hub.propose_memory(ctx.session, candidate,
                                            case_id=case_id if candidate.type == "claim" else None)
        except ValueError:  # a claim with no case: nothing is written
            decisions.append({"type": candidate.type, "admitted": False, "reason": "a claim needs a case_id"})
            continue
        stored = result.unit_id or result.item_id
        if stored:
            ctx.writes.append(f"memory {stored}")
        # Only the fixed reason goes back: never the candidate's text (SPEC 17.17).
        decisions.append({"type": candidate.type, "admitted": result.decision.admitted and stored is not None,
                          "reason": result.decision.reason})
    return {"decisions": decisions}


def _correct_claim(ctx: ToolContext, case_id: str, customer_words: str, claim) -> Any:
    """A correction is a new version of the same claim (change_reason correction, a claim_corrected
    event), never a second claim. Only the corrected facts change; the original text is kept and
    the customer's correction is added as correction_text. Admission still runs on it."""
    claims = [c for c in to_candidates(extract(ctx.llm, customer_words)) if c.type == "claim"]
    if not claims:
        return {"decisions": [{"type": "claim", "admitted": False, "reason": "no corrected fact found in the quote"}]}
    facts = {k: v for k, v in claims[0].fields.items() if k in ("amount_inr", "merchant", "payee")}
    candidate = claims[0].model_copy(update={
        "fields": {**facts, "correction_text": customer_words},
        "corrects": UnitRef(unit_id=claim.unit_id, expected_version=claim.version)})
    result = ctx.hub.propose_memory(ctx.session, candidate, case_id=case_id)
    if result.unit_id:
        ctx.writes.append(f"memory {result.unit_id} corrected")
    return {"decisions": [{"type": "claim", "admitted": result.unit_id is not None, "reason": result.decision.reason,
                           "corrected_unit_id": claim.unit_id}]}


def _request_callback(ctx: ToolContext, case_id: str, day_offset: int, time: str, kind: str) -> Any:
    ctx.case(case_id)
    now = ctx.hub.clock.now()
    due_at = resolve(_when(day_offset, time), now)  # code resolves time, never the model (SPEC 10)
    decision = check_callback(due_at, now)
    if not decision.accepted:
        ctx.hub.decline_request(ctx.session, case_id=case_id, action="callback", decision=decision)
        ctx.writes.append("callback declined")
        return {"accepted": False, "reason": decision.reason,
                "next_valid_slot": decision.next_valid_slot.isoformat() if decision.next_valid_slot else None,
                "note": "Offer the next valid slot; book it only if the customer agrees."}
    ctx.refresh()  # current versions, after any earlier write in this turn
    case = ctx.case(case_id)
    callbacks = [u for u in case.units if u.unit_type == "commitment" and u.owner == "bank" and u.action == "callback"]
    reschedule_of = follows = None
    if kind == "reschedule":
        active = [u for u in callbacks if u.status == "active"]
        if not active:
            raise ToolError("there is no active callback to reschedule; use kind 'new'")
        reschedule_of = UnitRef(unit_id=active[-1].unit_id, expected_version=active[-1].version)
    else:
        missed = [u for u in callbacks if u.status == "missed"]
        follows = missed[-1].unit_id if missed else None  # a new callback after a miss links to it (SPEC 17.8)
    try:
        accepted = ctx.hub.accept_commitment(ctx.session, case_id=case_id, owner="bank", action="callback",
                                             due_at=decision.due_at, reschedule_of=reschedule_of,
                                             follows_unit_id=follows)
    except CommitmentRefused as exc:
        raise ToolError(f"callback refused: {exc.decision.reason}") from exc
    ctx.writes.append(f"callback {accepted.unit.unit_id} v{accepted.unit.version}")
    return {"accepted": True, "due_at": accepted.unit.due_at.isoformat(), "read_back": accepted.read_back}


def _record_customer_commitment(ctx: ToolContext, case_id: str, action: str, day_offset: int, time: str,
                                customer_words: str) -> Any:
    _check_quote(ctx, customer_words)
    ctx.case(case_id)
    due_at = resolve(_when(day_offset, time), ctx.hub.clock.now())
    try:
        result = ctx.hub.record_customer_commitment(ctx.session, case_id=case_id, action=action, due_at=due_at)
    except CommitmentRefused as exc:
        raise ToolError(f"not recorded: {exc.decision.reason}") from exc
    ctx.writes.append(f"customer commitment {result.unit.unit_id}")
    ctx.committed_words.append(customer_words)
    return {"recorded": True, "read_back": result.read_back}


def _escalate_to_human(ctx: ToolContext, case_id: str, reason: str) -> Any:
    ctx.case(case_id)
    return escalate(ctx, case_id, ESCALATION_REASONS[reason])


HANDLERS = {
    "get_context": _get_context, "get_statement": _get_statement, "get_dispute_status": _get_dispute_status,
    "get_upi_status": _get_upi_status, "propose_memory": _propose_memory, "request_callback": _request_callback,
    "record_customer_commitment": _record_customer_commitment, "escalate_to_human": _escalate_to_human,
}


def run_tool(ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]:
    """Run one tool call. Returns (JSON text for the tool_result, is_error).

    A tool outside the role's toolset is refused here even if the model names it: the toolset
    sent to the API is a convenience, this check is the rule."""
    if name not in TOOLSETS[ctx.session.agent_role]:
        return json.dumps({"error": f"{name} is not available to you"}), True
    try:
        return json.dumps(HANDLERS[name](ctx, **args), sort_keys=True, ensure_ascii=False), False
    except (ToolError, HubError) as exc:  # refusals from the guards or the hub: tell the model, plainly
        return json.dumps({"error": str(exc)}), True
    except TypeError as exc:  # wrong arguments (strict schemas make this rare)
        return json.dumps({"error": f"bad arguments: {exc}"}), True
