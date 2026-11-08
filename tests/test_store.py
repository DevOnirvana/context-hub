"""Versioned writes and the expected-version check."""
import threading
from datetime import datetime, timedelta

import psycopg
import pytest

from hub import store
from hub.errors import ClockError, StaleVersionError
from hub.models import Case, NewUnit
from tests.conftest import APP_URL, MON_1830


@pytest.fixture
def claim(app_conn):
    store.create_case(app_conn, Case(case_id="CASE-CARD-001", customer_id="CUST-001", business_line="payments",
                                     case_type="card_dispute", opened_at=MON_1830))
    return store.insert_unit(app_conn, NewUnit(case_id="CASE-CARD-001", customer_id="CUST-001", unit_type="claim",
                                               payload={"amount_inr": 8500}, source="customer",
                                               created_by="test"), at=MON_1830)


def _bump(conn, unit_id, expected, minutes=10):
    return store.insert_next_version(conn, unit_id, expected_version=expected,
                                     changes={"payload": {"amount_inr": 5800}}, change_reason="correction",
                                     at=MON_1830 + timedelta(minutes=minutes), created_by="test")


def test_ids_are_deterministic(app_conn, claim):
    assert claim.unit_id == "CASE-CARD-001-U1"
    second = store.insert_unit(app_conn, NewUnit(case_id="CASE-CARD-001", customer_id="CUST-001",
                                                 unit_type="claim", source="customer", created_by="test"), at=MON_1830)
    assert second.unit_id == "CASE-CARD-001-U2"


def test_next_version_closes_previous(app_conn, claim):
    v2 = _bump(app_conn, claim.unit_id, 1)
    v1, current = store.unit_history(app_conn, claim.unit_id)
    assert (v1.valid_to, current.version, current.valid_to) == (v2.valid_from, 2, None)


def test_stale_expected_version_raises_and_writes_nothing(app_conn, claim):
    _bump(app_conn, claim.unit_id, 1)
    with pytest.raises(StaleVersionError, match="CASE-CARD-001-U1: expected version 1, current is 2"):
        _bump(app_conn, claim.unit_id, 1, minutes=20)
    assert len(store.unit_history(app_conn, claim.unit_id)) == 2


def test_concurrent_writers_exactly_one_wins(app_conn, claim):
    """Two writers both expect version 1. A locks the row and holds its transaction open;
    B blocks on the lock, then finds the version gone and gets StaleVersionError."""
    outcome = {}
    a = psycopg.connect(APP_URL)
    b = psycopg.connect(APP_URL, autocommit=True)

    def writer_b():
        try:
            _bump(b, claim.unit_id, 1, minutes=20)
            outcome["b"] = "won"
        except StaleVersionError as exc:
            outcome["b"] = exc

    try:
        with a.transaction():
            _bump(a, claim.unit_id, 1)
            t = threading.Thread(target=writer_b)
            t.start()
            t.join(timeout=0.5)
            assert t.is_alive(), "B should be waiting on A's row lock"
        t.join(timeout=5)
    finally:
        a.close()
        b.close()
    assert isinstance(outcome["b"], StaleVersionError) and outcome["b"].actual == 2
    assert [v.version for v in store.unit_history(app_conn, claim.unit_id)] == [1, 2]


def test_identity_fields_cannot_change(app_conn, claim):
    with pytest.raises(ValueError, match="cannot change"):
        store.insert_next_version(app_conn, claim.unit_id, expected_version=1, changes={"owner": "bank"},
                                  change_reason="correction", at=MON_1830, created_by="test")


def test_naive_datetime_refused_at_the_model(app_conn, claim):
    with pytest.raises(ClockError, match="naive"):
        store.insert_unit(app_conn, NewUnit(case_id="CASE-CARD-001", customer_id="CUST-001", unit_type="commitment",
                                            owner="bank", action="callback", due_at=datetime(2026, 11, 3, 21, 0),
                                            source="agent", created_by="test"), at=MON_1830)
