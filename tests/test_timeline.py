"""replay_to(): reset plus the shared timeline up to t (SPEC 17.18)."""
from datetime import datetime, timedelta

import psycopg
import pytest

from hub import lifecycle, store
from hub.clock import IST
from scenarios.timeline import CARD_CASE, CUSTOMER, UPI_CASE, replay_to
from tests.conftest import ADMIN_URL, APP_URL

WED_0916 = datetime(2026, 11, 4, 9, 16, tzinfo=IST)


def replay(t, minutes=15):
    clock = replay_to(t, timedelta(minutes=minutes), admin_url=ADMIN_URL, app_url=APP_URL)
    assert clock.now() == t
    return psycopg.connect(APP_URL, autocommit=True)


def callback(conn):
    (unit,) = [u for u in store.current_units(conn, case_id=CARD_CASE) if u.action == "callback"]
    return unit


@pytest.fixture(autouse=True)
def _db(test_database):
    pass


def test_wed_0916_with_15_minutes_callback_missed():
    with replay(WED_0916) as conn:
        cb = callback(conn)
        assert (cb.status, cb.version, cb.due_at) == ("missed", 3, datetime(2026, 11, 4, 9, 0, tzinfo=IST))
        assert lifecycle.displayed_statuses(store.unit_history(conn, cb.unit_id)) == ["superseded", "active", "missed"]
        (promise,) = [u for u in store.current_units(conn, case_id=CARD_CASE) if u.action == "upload_statement"]
        assert promise.status == "fulfilled"
        assert [(i.key, i.value) for i in store.current_items(conn, CUSTOMER)] == [
            ("preferred_explanation_style", "simple English")]
        assert {c.case_id for c in store.list_cases(conn, CUSTOMER)} == {CARD_CASE, UPI_CASE}


def test_level_3_change_threshold_30_callback_not_missed():
    with replay(WED_0916, minutes=30) as conn:
        assert callback(conn).status == "active"


def test_before_tue_1940_upi_case_does_not_exist_yet():
    with replay(datetime(2026, 11, 3, 19, 0, tzinfo=IST)) as conn:
        assert [c.case_id for c in store.list_cases(conn, CUSTOMER)] == [CARD_CASE]
        cb = callback(conn)
        assert (cb.status, cb.due_at) == ("active", datetime(2026, 11, 3, 21, 0, tzinfo=IST))


def test_replay_is_repeatable():
    def snapshot():
        with replay(WED_0916) as conn:
            return conn.execute(
                "SELECT (SELECT count(*) FROM case_memory.units), (SELECT count(*) FROM events.log), "
                "(SELECT count(*) FROM customer_context.items)"
            ).fetchone()
    assert snapshot() == snapshot()
