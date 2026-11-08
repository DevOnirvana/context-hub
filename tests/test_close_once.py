"""Step 1b: valid_to may change once, from NULL to a timestamp >= valid_from (SPEC 17.13).

Run against both versioned tables, connected as hub_app. Each test gets its own
fresh row so the cases do not depend on each other.
"""
from datetime import timedelta
from itertools import count

import pytest
from psycopg.errors import CheckViolation

from tests.conftest import MON_1830

_ids = count(1)


def _new_unit(conn):
    """Insert a fresh open claim; return a (table, key column, key value) handle."""
    n = next(_ids)
    case_id = f"CASE-CLOSE-{n}"
    conn.execute(
        """INSERT INTO case_memory.cases (case_id, customer_id, business_line, case_type, opened_at)
           VALUES (%s, 'CUST-001', 'credit', 'card_dispute', %s)""",
        (case_id, MON_1830),
    )
    conn.execute(
        """INSERT INTO case_memory.units
               (unit_id, version, case_id, customer_id, unit_type, status, source, change_reason, valid_from, created_by)
           VALUES (%s, 1, %s, 'CUST-001', 'claim', 'active', 'customer', 'created', %s, 'test')""",
        (f"U-CLOSE-{n}", case_id, MON_1830),
    )
    return "case_memory.units", "unit_id", f"U-CLOSE-{n}"


def _new_item(conn):
    n = next(_ids)
    conn.execute(
        """INSERT INTO customer_context.items
               (item_id, version, customer_id, key, value, source, change_reason, valid_from)
           VALUES (%s, 1, 'CUST-001', %s, 'v', 'customer', 'created', %s)""",
        (f"I-CLOSE-{n}", f"close_test_{n}", MON_1830),
    )
    return "customer_context.items", "item_id", f"I-CLOSE-{n}"


TABLES = pytest.mark.parametrize("make_row", [_new_unit, _new_item], ids=["units", "items"])


def _set_valid_to(conn, row, value):
    table, key_col, key = row
    # Table and column names come from the fixed handles above, never from input.
    return conn.execute(f"UPDATE {table} SET valid_to = %s WHERE {key_col} = %s", (value, key))


@TABLES
def test_normal_close_succeeds(app_conn, make_row):
    row = make_row(app_conn)
    assert _set_valid_to(app_conn, row, MON_1830 + timedelta(hours=1)).rowcount == 1


@TABLES
def test_reopen_closed_version_fails(app_conn, make_row):
    row = make_row(app_conn)
    _set_valid_to(app_conn, row, MON_1830 + timedelta(hours=1))
    with pytest.raises(CheckViolation, match="already closed"):
        _set_valid_to(app_conn, row, None)


@TABLES
def test_reclose_with_different_time_fails(app_conn, make_row):
    row = make_row(app_conn)
    _set_valid_to(app_conn, row, MON_1830 + timedelta(hours=1))
    with pytest.raises(CheckViolation, match="already closed"):
        _set_valid_to(app_conn, row, MON_1830 + timedelta(hours=2))


@TABLES
def test_close_before_valid_from_fails(app_conn, make_row):
    row = make_row(app_conn)
    with pytest.raises(CheckViolation, match="only became valid at"):
        _set_valid_to(app_conn, row, MON_1830 - timedelta(minutes=1))
