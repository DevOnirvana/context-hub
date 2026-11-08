"""Step 1 acceptance: the database, not the app, enforces what hub_app may change."""
import psycopg
import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege
from psycopg.types.json import Jsonb

from tests.conftest import APP_URL, MON_1830


@pytest.fixture
def case_and_unit(fresh_db):
    """Create one case and one claim as hub_app, the way scenario steps will (SPEC 17.4)."""
    with psycopg.connect(APP_URL, autocommit=True) as conn:
        conn.execute(
            """INSERT INTO case_memory.cases
                   (case_id, customer_id, business_line, case_type, opened_at)
               VALUES ('CASE-GRANTS-001', 'CUST-001', 'credit', 'card_dispute', %s)""",
            (MON_1830,),
        )
        conn.execute(
            """INSERT INTO case_memory.units
                   (unit_id, version, case_id, customer_id, unit_type, status, payload,
                    source, change_reason, valid_from, created_by)
               VALUES ('U-GRANTS-1', 1, 'CASE-GRANTS-001', 'CUST-001', 'claim', 'active', %s,
                       'customer', 'created', %s, 'test')""",
            (Jsonb({"text": "charged twice", "amount_inr": 8500}), MON_1830),
        )
    return "CASE-GRANTS-001", "U-GRANTS-1"


def _insert_event(conn, case_id):
    return conn.execute(
        """INSERT INTO events.log
               (customer_id, case_id, event_type, actor_type, actor_id, occurred_at, recorded_at, payload)
           VALUES ('CUST-001', %s, 'test_event', 'system', 'test', %s, %s, %s)
           RETURNING event_id""",
        (case_id, MON_1830, MON_1830, Jsonb({})),
    ).fetchone()[0]


# --- events.log is append-only -------------------------------------------------------

def test_hub_app_can_insert_event(app_conn, case_and_unit):
    assert _insert_event(app_conn, case_and_unit[0]) > 0


def test_hub_app_cannot_update_event(app_conn, case_and_unit):
    event_id = _insert_event(app_conn, case_and_unit[0])
    with pytest.raises(InsufficientPrivilege):
        app_conn.execute("UPDATE events.log SET event_type = 'edited' WHERE event_id = %s", (event_id,))


def test_hub_app_cannot_delete_event(app_conn, case_and_unit):
    event_id = _insert_event(app_conn, case_and_unit[0])
    with pytest.raises(InsufficientPrivilege):
        app_conn.execute("DELETE FROM events.log WHERE event_id = %s", (event_id,))


# --- versioned units: only valid_to may change -------------------------------------------

def test_hub_app_cannot_update_unit_status(app_conn, case_and_unit):
    with pytest.raises(InsufficientPrivilege):
        app_conn.execute("UPDATE case_memory.units SET status = 'active' WHERE unit_id = %s", (case_and_unit[1],))


def test_hub_app_can_close_unit_version(app_conn, case_and_unit):
    cur = app_conn.execute(
        "UPDATE case_memory.units SET valid_to = %s WHERE unit_id = %s AND version = 1 AND valid_to IS NULL",
        (MON_1830, case_and_unit[1]),
    )
    assert cur.rowcount == 1


def test_hub_app_cannot_delete_unit(app_conn, case_and_unit):
    with pytest.raises(InsufficientPrivilege):
        app_conn.execute("DELETE FROM case_memory.units WHERE unit_id = %s", (case_and_unit[1],))


def test_customer_context_items_only_valid_to_updatable(app_conn, fresh_db):
    app_conn.execute(
        """INSERT INTO customer_context.items
               (item_id, version, customer_id, key, value, source, change_reason, valid_from)
           VALUES ('I-GRANTS-1', 1, 'CUST-001', 'test_pref', 'x', 'customer', 'created', %s)""",
        (MON_1830,),
    )
    with pytest.raises(InsufficientPrivilege):
        app_conn.execute("UPDATE customer_context.items SET value = 'y' WHERE item_id = 'I-GRANTS-1'")
    cur = app_conn.execute(
        "UPDATE customer_context.items SET valid_to = %s WHERE item_id = 'I-GRANTS-1'", (MON_1830,)
    )
    assert cur.rowcount == 1


# --- archive: hub_app has no access; hub_archiver may only insert ------------------------

def test_hub_app_cannot_read_archive(app_conn):
    with pytest.raises(InsufficientPrivilege):
        app_conn.execute("SELECT * FROM archive.conversations")


def test_hub_archiver_is_insert_only(admin_conn):
    row = admin_conn.execute(
        """SELECT has_table_privilege('hub_archiver', 'archive.conversations', 'INSERT'),
                  has_table_privilege('hub_archiver', 'archive.conversations', 'SELECT')"""
    ).fetchone()
    assert row == (True, False)


# --- guards on the design decisions themselves -------------------------------------------

def test_hub_app_owns_nothing_and_is_not_superuser(admin_conn):
    # A table owner or superuser bypasses grants, which would make every test above meaningless.
    owned = admin_conn.execute("SELECT count(*) FROM pg_tables WHERE tableowner = 'hub_app'").fetchone()[0]
    super_or_bypass = admin_conn.execute(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = 'hub_app'"
    ).fetchone()[0]
    assert owned == 0 and super_or_bypass is False


def test_no_time_defaults_in_schema(admin_conn):
    # Every timestamp must come from the simulated clock (SPEC 17.12).
    rows = admin_conn.execute(
        """SELECT table_schema, table_name, column_name FROM information_schema.columns
           WHERE table_schema IN ('case_memory', 'events', 'customer_context', 'archive', 'policy')
             AND (column_default ILIKE '%now()%' OR column_default ILIKE '%current_%')"""
    ).fetchall()
    assert rows == []


def test_superseded_is_not_a_stored_status(app_conn, case_and_unit):
    # Superseded and corrected are derived from change_reason, never stored (SPEC 17.2).
    with pytest.raises(CheckViolation):
        app_conn.execute(
            """INSERT INTO case_memory.units
                   (unit_id, version, case_id, customer_id, unit_type, owner, action, due_at, status,
                    source, change_reason, valid_from, created_by)
               VALUES ('U-GRANTS-2', 1, %s, 'CUST-001', 'commitment', 'bank', 'callback', %s, 'superseded',
                       'agent', 'created', %s, 'test')""",
            (case_and_unit[0], MON_1830, MON_1830),
        )


def test_seed_has_customer_and_rules_but_no_cases(fresh_db):
    # Cases are created by the timeline, not seeded (SPEC 17.4). Counts were taken right after reset.
    assert fresh_db == {"customers": 1, "rules": 5, "cases": 0}
