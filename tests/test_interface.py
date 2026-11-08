"""Step 3: hub/interface.py. Gates, policy-filtered context, and one transaction per write."""
from datetime import datetime, timedelta

import psycopg
import pytest

from hub import store
from hub.acceptance import check_callback
from hub.clock import IST, SimClock
from hub.errors import CommitmentRefused, NotAuthenticatedError, PolicyError, StaleVersionError
from hub.interface import ContextHub
from hub.models import Candidate, UnitRef
from hub.policy import ROLE_LINE
from scenarios.timeline import CARD_CASE, CUSTOMER, UPI_CASE, replay_to
from tests.conftest import ADMIN_URL, APP_URL, MON_1830, THRESHOLD

WED_1000 = datetime(2026, 11, 4, 10, 0, tzinfo=IST)


def t(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


# --- fixtures --------------------------------------------------------------------------------

@pytest.fixture
def demo_at(test_database):
    """demo_at(t): replay the SPEC 13 timeline to t, return a ContextHub at t on its own connection."""
    conns = []

    def make(when):
        replay_to(when, THRESHOLD, admin_url=ADMIN_URL, app_url=APP_URL)
        conn = psycopg.connect(APP_URL, autocommit=True)
        conns.append(conn)
        return ContextHub(conn, SimClock(when), threshold=THRESHOLD)

    yield make
    for conn in conns:
        conn.close()


@pytest.fixture
def hub(app_conn):
    """An empty hub (seed only) at Mon 18:30."""
    return ContextHub(app_conn, SimClock(MON_1830), threshold=THRESHOLD)


def session(hub, role="credit_agent", authenticated=True, purpose=None):
    purpose = purpose or {"credit_agent": "card_dispute_servicing", "payments_agent": "payments_servicing",
                          "system": "system_event"}[role]
    return hub.open_session(agent_id=role, agent_role=role, purpose=purpose, customer_id=CUSTOMER,
                            authenticated=authenticated)


def access_rows(hub, after=0):
    return hub.conn.execute(
        "SELECT log_id, case_id, unit_id, decision, reason FROM policy.access_log WHERE log_id > %s ORDER BY log_id",
        (after,)).fetchall()


def last_log_id(hub):
    return hub.conn.execute("SELECT coalesce(max(log_id), 0) FROM policy.access_log").fetchone()[0]


def table_counts(hub):
    return hub.conn.execute(
        """SELECT (SELECT count(*) FROM case_memory.cases), (SELECT count(*) FROM case_memory.units),
                  (SELECT count(*) FROM customer_context.items), (SELECT count(*) FROM events.log)""").fetchone()


# --- BUILD_PLAN Step 3 acceptance ------------------------------------------------------------

def test_unauthenticated_get_context_raises_and_is_logged(demo_at):
    hub = demo_at(WED_1000)
    s = session(hub, authenticated=False)
    mark = last_log_id(hub)
    with pytest.raises(NotAuthenticatedError):
        hub.get_context(s)
    (row,) = access_rows(hub, mark)
    assert row[3:] == ("deny", "get_context: not authenticated")


def test_credit_agent_sees_card_case_and_only_existence_of_payments_case(demo_at):
    hub = demo_at(WED_1000)
    mark = last_log_id(hub)
    packet = hub.get_context(session(hub))

    assert [c.case_id for c in packet.cases] == [CARD_CASE]
    assert {u.unit_type for u in packet.cases[0].units} == {"claim", "commitment"}
    assert packet.other_cases_summary == "1 other open case: Payments"
    text = packet.model_dump_json()
    for leaked in (UPI_CASE, "UPI-7781", "Ramesh", "4200", "reversal expected"):
        assert leaked not in text, leaked

    rows = access_rows(hub, mark)
    upi_rows = [r for r in rows if r[1] == UPI_CASE]
    assert upi_rows and all(r[3] == "existence_only" for r in upi_rows)
    assert any(r[2] is None for r in upi_rows) and any(r[2] is not None for r in upi_rows)  # case row and unit rows


def test_payments_agent_sees_both_cases(demo_at):
    hub = demo_at(WED_1000)
    packet = hub.get_context(session(hub, "payments_agent"))
    assert [c.case_id for c in packet.cases] == [CARD_CASE, UPI_CASE]
    assert packet.other_cases == [] and packet.other_cases_summary is None


# --- the fix and the packet contents ---------------------------------------------------------

def test_credit_agent_reads_a_case_from_another_business_line(demo_at):
    # SPEC 17.22: the cross-line moment. The dispute belongs to payments; credit reads it.
    hub = demo_at(WED_1000)
    packet = hub.get_context(session(hub))
    assert any(c.business_line != ROLE_LINE["credit_agent"] for c in packet.cases)
    assert packet.cases[0].business_line == "payments"


def test_credit_agent_gets_the_whole_story(demo_at):
    hub = demo_at(WED_1000)
    (card,) = hub.get_context(session(hub)).cases
    (cb,) = [h for h in card.commitment_history if h.action == "callback"]
    assert [v.shown_status for v in cb.versions] == ["superseded", "active", "missed"]
    (promise,) = [u for u in card.units if u.action == "upload_statement"]
    assert promise.status == "fulfilled"
    assert "customer_informed" in [e.event_type for e in card.events]
    packet = hub.get_context(session(hub))
    assert [(p.key, p.value) for p in packet.customer_context] == [("preferred_explanation_style", "simple English")]


def test_packet_carries_references_never_live_facts(demo_at):
    hub = demo_at(WED_1000)
    packet = hub.get_context(session(hub))
    refs = {(r.kind, r.ref, r.fetch_with) for r in packet.cases[0].live_fact_refs}
    assert refs == {("dispute", "DSP-1001", "get_dispute_status"), ("statement", "4417", "get_statement")}
    text = packet.model_dump_json()
    for fact in ("provisional_credit_issued", "investigation_initiated", "pending_reversal", "total_due", "minimum_due"):
        assert fact not in text, fact


def test_one_access_log_row_per_case_unit_and_item(demo_at):
    hub = demo_at(WED_1000)
    mark = last_log_id(hub)
    hub.get_context(session(hub))
    n_cases, n_units, n_items = hub.conn.execute(
        """SELECT (SELECT count(*) FROM case_memory.cases),
                  (SELECT count(*) FROM case_memory.units WHERE valid_to IS NULL),
                  (SELECT count(*) FROM customer_context.items WHERE valid_to IS NULL)""").fetchone()
    rows = access_rows(hub, mark)
    assert len(rows) == n_cases + n_units + n_items
    assert all(r[4] for r in rows)  # every row has a reason


def test_overdue_customer_promise_is_flagged_at_read_time(hub):
    s = session(hub, "payments_agent")
    case = hub.open_case(s, case_type="card_dispute")
    hub.record_customer_commitment(s, case_id=case.case_id, action="upload_statement", due_at=t(3, 18))
    hub.clock.set(t(3, 18, 1))
    (promise,) = hub.get_context(s).cases[0].units
    assert (promise.status, promise.overdue) == ("active", True)


def test_events_are_capped_at_50_per_case(hub):
    s = session(hub, "payments_agent")
    case = hub.open_case(s, case_type="card_dispute")  # 1 case_opened event
    for i in range(54):
        hub.clock.advance(1)
        hub.record_event(s, case_id=case.case_id, event_type="customer_informed", payload={"message": f"update {i}"})
    (view,) = hub.get_context(s).cases
    assert (len(view.events), view.events_truncated) == (50, 5)
    assert view.events[-1].payload == {"message": "update 53"}  # the newest are kept


# --- exclusion of the future -----------------------------------------------------------------

def test_nothing_dated_after_now_is_visible(demo_at):
    hub = demo_at(WED_1000)
    later = t(5, 12)
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute("""INSERT INTO case_memory.cases VALUES ('CASE-FUTURE', 'CUST-001', 'payments', 'card_dispute', %s, NULL)""", (later,))
        admin.execute("""INSERT INTO case_memory.units (unit_id, version, case_id, customer_id, unit_type, status, payload,
                         source, change_reason, valid_from, created_by)
                         VALUES ('FUTURE-U1', 1, %s, 'CUST-001', 'claim', 'active', '{"text": "future claim"}',
                                 'customer', 'created', %s, 'test')""", (CARD_CASE, later))
        admin.execute("""INSERT INTO customer_context.items (item_id, version, customer_id, key, value, source,
                         change_reason, valid_from) VALUES ('FUTURE-C1', 1, 'CUST-001', 'preferred_language', 'Tamil',
                         'customer', 'created', %s)""", (later,))
        admin.execute("""INSERT INTO events.log (customer_id, case_id, event_type, actor_type, actor_id, occurred_at,
                         recorded_at, payload) VALUES ('CUST-001', %s, 'customer_informed', 'agent', 'x', %s, %s,
                         '{"message": "future event"}')""", (CARD_CASE, WED_1000, later))
    mark = last_log_id(hub)
    text = hub.get_context(session(hub, "payments_agent")).model_dump_json()
    for future in ("CASE-FUTURE", "FUTURE-U1", "future claim", "Tamil", "future event"):
        assert future not in text, future
    assert not [r for r in access_rows(hub, mark) if r[1] == "CASE-FUTURE" or r[2] in ("FUTURE-U1", "FUTURE-C1")]


# --- gates and refusals ----------------------------------------------------------------------

def test_role_cannot_claim_another_roles_purpose(hub):
    mark = last_log_id(hub)
    with pytest.raises(PolicyError, match="not defined for role credit_agent"):
        session(hub, "credit_agent", purpose="payments_servicing")
    (row,) = access_rows(hub, mark)
    assert row[3] == "deny"


def test_unauthenticated_writes_are_refused_and_write_nothing(demo_at):
    hub = demo_at(WED_1000)
    s = session(hub, "payments_agent", authenticated=False)
    before = table_counts(hub)
    calls = [
        lambda: hub.open_case(s, case_type="card_dispute"),
        lambda: hub.propose_memory(s, Candidate(type="claim", fields={"text": "x"}, source="customer"), case_id=CARD_CASE),
        lambda: hub.accept_commitment(s, case_id=CARD_CASE, owner="bank", action="callback", due_at=t(4, 15)),
        lambda: hub.record_customer_commitment(s, case_id=CARD_CASE, action="share_document", due_at=t(5, 12)),
        lambda: hub.decline_request(s, case_id=CARD_CASE, action="callback", decision=check_callback(t(5, 3), WED_1000)),
        lambda: hub.record_event(s, case_id=CARD_CASE, event_type="customer_informed", payload={"message": "x"}),
    ]
    for call in calls:
        with pytest.raises(NotAuthenticatedError):
            call()
    assert table_counts(hub) == before


def test_credit_agent_cannot_write_into_existence_only_case(demo_at):
    hub = demo_at(WED_1000)
    s = session(hub)
    before = table_counts(hub)
    with pytest.raises(PolicyError, match="case not available"):
        hub.propose_memory(s, Candidate(type="claim", fields={"text": "x"}, source="customer"), case_id=UPI_CASE)
    with pytest.raises(PolicyError, match="case not available"):
        hub.record_event(s, case_id="PAY-9999", event_type="customer_informed", payload={"message": "x"})
    assert table_counts(hub) == before


def test_write_refusal_does_not_confirm_a_case_exists(demo_at):
    """An injected prompt guessing case IDs gets the same answer for a real existence-only case
    and a made-up one. The detailed reason goes only to the access log."""
    hub = demo_at(WED_1000)
    s = session(hub)
    messages = []
    for case_id in (UPI_CASE, "PAY-9999"):
        with pytest.raises(PolicyError) as exc:
            hub.record_event(s, case_id=case_id, event_type="customer_informed", payload={"message": "x"})
        messages.append(str(exc.value))
    assert messages == ["record_event: case not available"] * 2
    logged = hub.conn.execute("SELECT reason FROM policy.access_log WHERE case_id = %s AND decision = 'deny'",
                              (UPI_CASE,)).fetchone()[0]
    assert "existence_only" in logged


def test_credit_agent_may_not_open_cases(hub):
    with pytest.raises(PolicyError, match="may not open card_dispute"):
        hub.open_case(session(hub), case_type="card_dispute")


def test_agent_cannot_record_system_events_and_system_cannot_read(demo_at):
    hub = demo_at(WED_1000)
    with pytest.raises(PolicyError, match="may not record dialler_outcome"):
        hub.record_event(session(hub, "payments_agent"), case_id=CARD_CASE, event_type="dialler_outcome",
                         payload={"unit_id": "x", "outcome": "completed"})
    packet = hub.get_context(session(hub, "system"))
    assert packet.cases == [] and packet.customer_context == [] and packet.other_cases == []


def test_hub_rechecks_acceptance_so_no_false_promise(demo_at):
    hub = demo_at(WED_1000)
    s = session(hub, "payments_agent")
    before = table_counts(hub)
    with pytest.raises(CommitmentRefused) as exc:
        hub.accept_commitment(s, case_id=CARD_CASE, owner="bank", action="callback", due_at=t(5, 3))  # 3 AM
    assert (exc.value.decision.reason, exc.value.decision.next_valid_slot) == ("outside_servicing_hours", t(5, 8))
    with pytest.raises(CommitmentRefused, match="too_soon"):
        hub.accept_commitment(s, case_id=CARD_CASE, owner="bank", action="callback", due_at=t(4, 10, 10))
    assert table_counts(hub) == before


# --- writes ----------------------------------------------------------------------------------

def test_open_case_ids_and_refs_come_from_case_management(hub):
    opened = hub.open_case(session(hub, "payments_agent"), case_type="card_dispute")
    assert (opened.case_id, opened.business_line, opened.refs) == (
        "CASE-CARD-001", "payments", {"dispute_ref": "DSP-1001", "card_last4": "4417"})


def test_read_backs_are_formatted_in_code(hub):
    s = session(hub, "payments_agent")
    case = hub.open_case(s, case_type="card_dispute")
    first = hub.accept_commitment(s, case_id=case.case_id, owner="bank", action="callback", due_at=t(3, 21))
    assert first.read_back == "I've scheduled your callback for Tuesday 3 November at 9:00 PM. Is that right?"
    hub.clock.set(t(3, 20, 15))
    moved = hub.accept_commitment(s, case_id=case.case_id, owner="bank", action="callback", due_at=t(4, 9),
                                  reschedule_of=UnitRef(unit_id=first.unit.unit_id, expected_version=1))
    assert moved.read_back == "I've moved your callback to Wednesday 4 November at 9:00 AM. Is that right?"
    promise = hub.record_customer_commitment(s, case_id=case.case_id, action="upload_statement", due_at=t(4, 18))
    assert promise.read_back == "Noted: you'll upload your bank statement by Wednesday 4 November at 6:00 PM."


def test_stale_reschedule_is_refused(demo_at):
    hub = demo_at(WED_1000)
    s = session(hub, "payments_agent")
    (card, _) = hub.get_context(s).cases
    (cb,) = [u for u in card.units if u.action == "callback"]
    with pytest.raises(StaleVersionError):
        hub.accept_commitment(s, case_id=CARD_CASE, owner="bank", action="callback", due_at=t(4, 15),
                              reschedule_of=UnitRef(unit_id=cb.unit_id, expected_version=1))


def test_new_callback_after_a_miss_links_to_the_missed_one(demo_at):
    hub = demo_at(WED_1000)
    s = session(hub, "payments_agent")
    (card, _) = hub.get_context(s).cases
    (missed,) = [u for u in card.units if u.action == "callback"]
    new = hub.accept_commitment(s, case_id=CARD_CASE, owner="bank", action="callback", due_at=t(4, 15),
                                follows_unit_id=missed.unit_id)
    assert new.unit.unit_id != missed.unit_id and new.unit.payload == {"follows_unit_id": missed.unit_id}


def test_dialler_outcome_routes_to_the_lifecycle(demo_at):
    hub = demo_at(datetime(2026, 11, 4, 9, 40, tzinfo=IST))
    (cb,) = [u for u in store.current_units(hub.conn, case_id=CARD_CASE) if u.action == "callback"]
    hub.record_event(session(hub, "system"), case_id=CARD_CASE, event_type="dialler_outcome",
                     payload={"unit_id": cb.unit_id, "outcome": "completed"}, occurred_at=t(4, 9, 4))
    assert store.current_unit(hub.conn, cb.unit_id).status == "fulfilled"
    (corrected,) = [e for e in store.list_events(hub.conn, unit_id=cb.unit_id) if e.event_type == "status_corrected"]
    assert corrected.payload["on_time"] is True


def test_claim_correction_through_propose_memory(demo_at):
    hub = demo_at(WED_1000)
    s = session(hub, "payments_agent")
    (claim,) = [u for u in hub.get_context(s).cases[0].units if u.unit_type == "claim"]
    hub.propose_memory(s, Candidate(type="claim", fields={"amount_inr": 5800}, source="customer",
                                    corrects=UnitRef(unit_id=claim.unit_id, expected_version=1)), case_id=CARD_CASE)
    assert store.current_unit(hub.conn, claim.unit_id).payload["amount_inr"] == 5800


def test_rejected_memory_writes_nothing(demo_at):
    hub = demo_at(WED_1000)
    before = table_counts(hub)
    result = hub.propose_memory(session(hub), Candidate(type="claim", source="customer",
                                fields={"text": "I was in hospital, that's why I missed my payment."}), case_id=CARD_CASE)
    assert (result.decision.admitted, result.unit_id) == (False, None)
    assert table_counts(hub) == before
    logged = hub.conn.execute("SELECT count(*) FROM policy.access_log WHERE reason ILIKE '%hospital%'").fetchone()[0]
    assert logged == 0


# --- one transaction per write ---------------------------------------------------------------

WRITES = {
    "open_case": lambda hub, s: hub.open_case(s, case_type="upi_failed_debit"),
    "propose_claim": lambda hub, s: hub.propose_memory(
        s, Candidate(type="claim", fields={"text": "another charge"}, source="customer"), case_id=CARD_CASE),
    "propose_preference": lambda hub, s: hub.propose_memory(
        s, Candidate(type="customer_context", fields={"key": "preferred_language", "value": "Hindi"}, source="customer")),
    "accept_commitment": lambda hub, s: hub.accept_commitment(
        s, case_id=CARD_CASE, owner="bank", action="callback", due_at=t(3, 21)),
    "record_customer_commitment": lambda hub, s: hub.record_customer_commitment(
        s, case_id=CARD_CASE, action="share_document", due_at=t(4, 12)),
    "decline_request": lambda hub, s: hub.decline_request(
        s, case_id=CARD_CASE, action="callback", decision=check_callback(t(3, 3), hub.clock.now())),
    "record_event": lambda hub, s: hub.record_event(
        s, case_id=CARD_CASE, event_type="customer_informed", payload={"message": "x"}),
    "record_system_event": lambda hub, _s: hub.record_event(
        hub.open_session(agent_id="documents", agent_role="system", purpose="system_event", customer_id=CUSTOMER,
                         authenticated=True),
        case_id=CARD_CASE, event_type="statement_upload_recorded", payload={"document": "bank_statement"}),
}


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
def test_each_write_is_one_transaction_with_its_event(demo_at, monkeypatch, write):
    """Let each event INSERT succeed, then fail: the whole write must roll back, leaving no
    case, unit, item or event behind."""
    hub = demo_at(t(2, 18, 30))  # Mon 18:30, after the first step: callback and promise exist
    s = session(hub, "payments_agent")
    real_append = store.append_event

    def append_then_fail(conn, event):
        real_append(conn, event)
        raise RuntimeError("simulated failure after the event insert")

    monkeypatch.setattr(store, "append_event", append_then_fail)
    before = table_counts(hub)
    with pytest.raises(RuntimeError, match="simulated failure"):
        write(hub, s)
    assert table_counts(hub) == before
