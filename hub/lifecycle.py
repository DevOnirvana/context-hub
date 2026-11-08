"""Memory lifecycle: every write that creates a version or changes a status (SPEC 5, 7, 17).

Rules that hold for every function here:
* A change is a new version row plus an event, written in one transaction: both or neither.
* Statuses only move along ALLOWED. Anything else raises IllegalTransitionError.
* `now` is passed in (from the simulated clock). Events carry two times: occurred_at (when it
  happened) and recorded_at (when the hub learned it), e.g. a late dialler record 09:04 / 09:40.
* Acceptance (hub/acceptance.py) is the caller's job: these functions record decisions, they do
  not make them.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from psycopg import Connection

from hub import store
from hub.acceptance import AcceptanceDecision
from hub.errors import IllegalTransitionError, StaleVersionError
from hub.models import ContextItem, NewEvent, NewUnit, Source, UnitVersion

log = logging.getLogger("hub.lifecycle")

# Commitment status transitions (SPEC 7 plus 17.8). Reschedule is not here: it is its own path
# (new version, status back to active, change_reason 'reschedule'), allowed from active or attempted.
# attempted -> missed is deliberately absent: the bank called, so it did not miss.
ALLOWED = frozenset({
    ("active", "attempted"),
    ("active", "fulfilled"),
    ("active", "missed"),
    ("active", "cancelled"),
    ("attempted", "fulfilled"),
    ("attempted", "cancelled"),
    ("missed", "fulfilled"),
})
RESCHEDULABLE = frozenset({"active", "attempted"})


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _event(conn: Connection, unit: UnitVersion, event_type: str, *, actor_type: Source, actor_id: str,
           occurred_at: datetime, recorded_at: datetime, payload: dict[str, Any] | None = None) -> int:
    return store.append_event(conn, NewEvent(
        customer_id=unit.customer_id, case_id=unit.case_id, unit_id=unit.unit_id, event_type=event_type,
        actor_type=actor_type, actor_id=actor_id, occurred_at=occurred_at, recorded_at=recorded_at,
        payload=payload or {},
    ))


def _current(conn: Connection, unit_id: str, *, unit_type: str, owner: str | None = None) -> UnitVersion:
    unit = store.current_unit(conn, unit_id)
    if unit is None:
        raise ValueError(f"{unit_id}: no current version")
    if unit.unit_type != unit_type or (owner is not None and unit.owner != owner):
        raise ValueError(f"{unit_id}: is a {unit.owner or ''} {unit.unit_type}, expected {owner or ''} {unit_type}")
    return unit


def _change_status(conn: Connection, unit: UnitVersion, to_status: str, *, now: datetime, actor_type: Source,
                   actor_id: str, event_type: str, occurred_at: datetime | None = None,
                   payload: dict[str, Any] | None = None) -> UnitVersion:
    if (unit.status, to_status) not in ALLOWED:
        raise IllegalTransitionError(unit.unit_id, unit.status, to_status)
    with conn.transaction():
        new = store.insert_next_version(conn, unit.unit_id, expected_version=unit.version,
                                        changes={"status": to_status}, change_reason="status_change",
                                        at=now, created_by=actor_id)
        _event(conn, new, event_type, actor_type=actor_type, actor_id=actor_id,
               occurred_at=occurred_at or now, recorded_at=now,
               payload={"from_status": unit.status, "to_status": to_status, **(payload or {})})
    return new


# --- claims ----------------------------------------------------------------------------------

def create_claim(conn: Connection, *, case_id: str, customer_id: str, fields: dict[str, Any], now: datetime,
                 actor_type: Source, actor_id: str, source: Source = "customer") -> UnitVersion:
    """Store what the customer said. Admission (hub/admission.py) must have passed first."""
    with conn.transaction():
        unit = store.insert_unit(conn, NewUnit(case_id=case_id, customer_id=customer_id, unit_type="claim",
                                               payload=fields, source=source, created_by=actor_id), at=now)
        # Event payloads stay minimal: the unit holds the content, the event records that it happened.
        _event(conn, unit, "claim_recorded", actor_type=actor_type, actor_id=actor_id, occurred_at=now, recorded_at=now)
    return unit


def correct_claim(conn: Connection, unit_id: str, *, expected_version: int, new_fields: dict[str, Any],
                  now: datetime, actor_type: Source, actor_id: str) -> UnitVersion:
    """The earlier value was wrong (Rs 8,500 -> Rs 5,800). v1 is kept and displayed as corrected."""
    unit = _current(conn, unit_id, unit_type="claim")
    with conn.transaction():
        new = store.insert_next_version(conn, unit_id, expected_version=expected_version,
                                        changes={"payload": {**unit.payload, **new_fields}},
                                        change_reason="correction", at=now, created_by=actor_id)
        _event(conn, new, "claim_corrected", actor_type=actor_type, actor_id=actor_id, occurred_at=now,
               recorded_at=now, payload={"changed_fields": sorted(new_fields), "from_version": expected_version})
    return new


# --- commitments -----------------------------------------------------------------------------

def create_commitment(conn: Connection, *, case_id: str, customer_id: str, owner: Literal["bank", "customer"],
                      action: str, due_at: datetime, now: datetime, actor_type: Source, actor_id: str,
                      payload: dict[str, Any] | None = None) -> UnitVersion:
    """Record an accepted commitment (bank) or a customer promise (SPEC 17.9)."""
    # source = whose words created it: the customer's promise, or the bank's (agent's) acceptance.
    source: Source = "customer" if owner == "customer" else "agent"
    with conn.transaction():
        unit = store.insert_unit(conn, NewUnit(
            case_id=case_id, customer_id=customer_id, unit_type="commitment", owner=owner, action=action,
            due_at=due_at, payload=payload or {}, source=source, created_by=actor_id,
        ), at=now)
        _event(conn, unit, "commitment_created", actor_type=actor_type, actor_id=actor_id, occurred_at=now,
               recorded_at=now, payload={"owner": owner, "action": action, "due_at": _iso(due_at)})
    return unit


def reschedule(conn: Connection, unit_id: str, *, expected_version: int, new_due_at: datetime, now: datetime,
               actor_type: Source, actor_id: str) -> UnitVersion:
    """The earlier time was right, then changed. v_n is kept and displayed as superseded."""
    unit = _current(conn, unit_id, unit_type="commitment")
    if unit.version == expected_version and unit.status not in RESCHEDULABLE:
        raise IllegalTransitionError(unit_id, unit.status, "rescheduled")
    with conn.transaction():
        new = store.insert_next_version(conn, unit_id, expected_version=expected_version,
                                        changes={"status": "active", "due_at": new_due_at},
                                        change_reason="reschedule", at=now, created_by=actor_id)
        _event(conn, new, "commitment_rescheduled", actor_type=actor_type, actor_id=actor_id, occurred_at=now,
               recorded_at=now, payload={"from_due_at": _iso(unit.due_at), "to_due_at": _iso(new_due_at),
                                         "from_version": expected_version, "to_version": new.version})
    return new


def cancel(conn: Connection, unit_id: str, *, expected_version: int, now: datetime, actor_type: Source,
           actor_id: str) -> UnitVersion:
    unit = _current(conn, unit_id, unit_type="commitment")
    if unit.version != expected_version:
        raise StaleVersionError(unit_id, expected_version, unit.version)
    return _change_status(conn, unit, "cancelled", now=now, actor_type=actor_type, actor_id=actor_id,
                          event_type="commitment_cancelled")


def apply_dialler_outcome(conn: Connection, unit_id: str, *, outcome: Literal["completed", "attempted_no_answer"],
                          occurred_at: datetime, now: datetime, threshold: timedelta) -> UnitVersion:
    """Apply a dialler record to a bank callback.

    attempted_no_answer: active -> attempted (the bank called; not a miss, so no apology is due).
    completed: active -> fulfilled; a later completed on attempted or missed -> fulfilled with a
    status_corrected event whose on_time says whether the call happened by due_at + threshold.
    """
    if occurred_at > now:
        raise ValueError(f"{unit_id}: dialler record at {occurred_at.isoformat()} is after now {now.isoformat()}")
    unit = _current(conn, unit_id, unit_type="commitment", owner="bank")
    common = dict(now=now, actor_type="system", actor_id="dialler", occurred_at=occurred_at)
    if outcome == "attempted_no_answer":
        return _change_status(conn, unit, "attempted", event_type="commitment_attempted", **common)
    if unit.status in ("attempted", "missed"):
        on_time = occurred_at <= unit.due_at + threshold
        return _change_status(conn, unit, "fulfilled", event_type="status_corrected",
                              payload={"on_time": on_time, "completed_at": _iso(occurred_at)}, **common)
    return _change_status(conn, unit, "fulfilled", event_type="commitment_fulfilled", **common)


def fulfil_customer_commitment(conn: Connection, unit_id: str, *, evidence_event_id: int, occurred_at: datetime,
                               now: datetime, actor_id: str) -> UnitVersion:
    """A system event proves the customer did what they promised (statement upload recorded)."""
    unit = _current(conn, unit_id, unit_type="commitment", owner="customer")
    return _change_status(conn, unit, "fulfilled", now=now, actor_type="system", actor_id=actor_id,
                          event_type="commitment_fulfilled", occurred_at=occurred_at,
                          payload={"evidence_event_id": evidence_event_id})


def sweep(conn: Connection, now: datetime, threshold: timedelta) -> list[str]:
    """Mark bank commitments Missed once now >= due_at + threshold with no dialler record.

    Runs on every clock change. Idempotent: it selects only current, active, bank-owned units, and
    marking one Missed takes it out of that set, so a second run finds nothing and writes nothing.
    No dialler lookup is needed: any dialler record that arrived already moved the unit off 'active'.
    Customer commitments are excluded: a customer missing their own deadline is not a bank breach;
    they show as overdue at read time instead (is_overdue).
    """
    rows = conn.execute(
        """SELECT unit_id FROM case_memory.units
           WHERE valid_to IS NULL AND unit_type = 'commitment' AND owner = 'bank' AND status = 'active'
             AND due_at + %s <= %s
           ORDER BY due_at, unit_id""",
        (threshold, now),
    ).fetchall()
    marked = []
    for (unit_id,) in rows:
        unit = store.current_unit(conn, unit_id)
        if unit is None or unit.status != "active":
            continue  # changed since the SELECT (a concurrent sweep or dialler record); nothing to do
        try:
            _change_status(conn, unit, "missed", now=now, actor_type="system", actor_id="lifecycle_sweep",
                           event_type="commitment_missed", occurred_at=unit.due_at + threshold,
                           payload={"due_at": _iso(unit.due_at), "threshold_minutes": threshold // timedelta(minutes=1)})
        except StaleVersionError:
            continue  # another sweep or a dialler record got there first; nothing left to do
        # One line per unit: the visible evidence of the sweep in the UI (SPEC 17.19).
        log.info("sweep: marked missed unit_id=%s due_at=%s threshold=%dm",
                 unit_id, unit.due_at.isoformat(), threshold // timedelta(minutes=1))
        marked.append(unit_id)
    return marked


# --- requests and preferences ---------------------------------------------------------------

def decline_request(conn: Connection, *, customer_id: str, case_id: str | None, action: str,
                    decision: AcceptanceDecision, now: datetime, actor_type: Source, actor_id: str) -> int:
    """Record a declined request and what was offered instead (SPEC 6). Returns the event id."""
    if decision.accepted:
        raise ValueError("decline_request called with an accepted decision")
    return store.append_event(conn, NewEvent(
        customer_id=customer_id, case_id=case_id, event_type="request_declined", actor_type=actor_type,
        actor_id=actor_id, occurred_at=now, recorded_at=now,
        payload={"action": action, "requested_due_at": _iso(decision.due_at), "reason": decision.reason,
                 "offered_slot": _iso(decision.next_valid_slot)},
    ))


def _current_item(conn: Connection, customer_id: str, key: str) -> ContextItem | None:
    return next((i for i in store.current_items(conn, customer_id) if i.key == key), None)


def _preference_event(conn: Connection, item: ContextItem, event_type: str, *, now: datetime,
                      actor_type: Source, actor_id: str) -> int:
    # The payload names the key, not the value: the item holds the content (one place to erase).
    return store.append_event(conn, NewEvent(
        customer_id=item.customer_id, event_type=event_type, actor_type=actor_type, actor_id=actor_id,
        occurred_at=now, recorded_at=now, payload={"item_id": item.item_id, "key": item.key, "version": item.version},
    ))


def set_preference(conn: Connection, *, customer_id: str, key: str, value: str, now: datetime,
                   actor_type: Source, actor_id: str, source: Source = "customer") -> ContextItem:
    """Store a customer-scope preference. Only the value is stored, never the reason given for it.

    No current value: create (preference_set). A different value: replace (preference_replaced,
    change_reason 'replacement'). The same value: nothing changes, nothing is written.
    """
    with conn.transaction():
        current = _current_item(conn, customer_id, key)
        if current is not None and current.value == value:
            return current
        if current is None:
            item = store.insert_item(conn, customer_id=customer_id, key=key, value=value, source=source, at=now)
            _preference_event(conn, item, "preference_set", now=now, actor_type=actor_type, actor_id=actor_id)
        else:
            item = store.replace_item(conn, current.item_id, expected_version=current.version, value=value,
                                      source=source, at=now)
            _preference_event(conn, item, "preference_replaced", now=now, actor_type=actor_type, actor_id=actor_id)
    return item


def withdraw_preference(conn: Connection, *, customer_id: str, key: str, now: datetime, actor_type: Source,
                        actor_id: str) -> ContextItem:
    """The customer withdraws a preference: close the current version with no successor (SPEC 17.21)."""
    with conn.transaction():
        current = _current_item(conn, customer_id, key)
        if current is None:
            raise ValueError(f"{customer_id}: no current preference {key!r} to withdraw")
        store.withdraw_item(conn, current.item_id, expected_version=current.version, at=now)
        _preference_event(conn, current, "preference_withdrawn", now=now, actor_type=actor_type, actor_id=actor_id)
    return current


# --- read-time derivations (never stored) -----------------------------------------------------

def is_overdue(unit: UnitVersion, now: datetime) -> bool:
    """Derived flag (SPEC 17.16): an active commitment past its due time. Never stored."""
    return unit.unit_type == "commitment" and unit.status == "active" and unit.due_at < now


SHOWN_AS = {"reschedule": "superseded", "correction": "corrected", "replacement": "replaced"}


def displayed_statuses(history: list[UnitVersion]) -> list[str]:
    """Status to show for each unit version, oldest first (SPEC 17.2). A closed version shows as
    superseded, corrected or replaced according to its successor's change_reason; otherwise its
    stored status."""
    shown = []
    for i, version in enumerate(history):
        successor = history[i + 1] if i + 1 < len(history) else None
        shown.append(SHOWN_AS.get(successor.change_reason, version.status) if successor else version.status)
    return shown
