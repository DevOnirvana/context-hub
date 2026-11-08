"""Raw SQL access to the hub's stores. No business rules live here; hub/lifecycle.py decides
what may change, this module makes the change safely.

Every function takes a psycopg connection that is logged in as hub_app, never the table owner.
Times are always passed in (from the simulated clock); nothing here reads the wall clock.
"""
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hub.errors import StaleVersionError
from hub.models import Case, ChangeReason, ContextItem, Event, NewEvent, NewUnit, UnitVersion

UNIT_COLUMNS = (
    "unit_id, version, case_id, customer_id, unit_type, owner, action, due_at, status, payload, "
    "source, sensitivity, change_reason, valid_from, valid_to, created_by"
)
ITEM_COLUMNS = "item_id, version, customer_id, key, value, source, change_reason, valid_from, valid_to, expires_at"
EVENT_COLUMNS = (
    "event_id, customer_id, case_id, unit_id, event_type, actor_type, actor_id, occurred_at, recorded_at, payload"
)

# The only fields a new version may change. Identity (unit_id, case, customer, type, owner,
# action) is fixed for the life of a unit; a different owner or action is a different unit.
CHANGEABLE_FIELDS = frozenset({"status", "due_at", "payload"})


def _rows(conn: Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


# --- cases ---------------------------------------------------------------------------------

def create_case(conn: Connection, case: Case) -> None:
    conn.execute(
        """INSERT INTO case_memory.cases (case_id, customer_id, business_line, case_type, opened_at, closed_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (case.case_id, case.customer_id, case.business_line, case.case_type, case.opened_at, case.closed_at),
    )


def list_cases(conn: Connection, customer_id: str) -> list[Case]:
    return [Case(**r) for r in _rows(
        conn, "SELECT * FROM case_memory.cases WHERE customer_id = %s ORDER BY opened_at, case_id", (customer_id,)
    )]


# --- units (claims and commitments) ----------------------------------------------------------

def _next_unit_id(conn: Connection, case_id: str) -> str:
    # Deterministic IDs (CASE-CARD-001-U1, -U2, ...) rather than random UUIDs: IDs appear in
    # agent prompts, and the Step 5 replay cache is keyed on prompt text, so they must be stable.
    # Single-writer demo: a concurrent create would hit the primary key and fail loudly.
    n = conn.execute(
        "SELECT count(DISTINCT unit_id) FROM case_memory.units WHERE case_id = %s", (case_id,)
    ).fetchone()[0]
    return f"{case_id}-U{n + 1}"


def _insert_unit_row(conn: Connection, u: UnitVersion) -> None:
    conn.execute(
        f"""INSERT INTO case_memory.units ({UNIT_COLUMNS})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (u.unit_id, u.version, u.case_id, u.customer_id, u.unit_type, u.owner, u.action, u.due_at,
         u.status, Jsonb(u.payload), u.source, u.sensitivity, u.change_reason, u.valid_from,
         u.valid_to, u.created_by),
    )


def insert_unit(conn: Connection, unit: NewUnit, *, at: datetime) -> UnitVersion:
    """Create version 1 of a new unit."""
    row = UnitVersion(
        **unit.model_dump(), unit_id=_next_unit_id(conn, unit.case_id), version=1,
        change_reason="created", valid_from=at,
    )
    _insert_unit_row(conn, row)
    return row


def current_unit(conn: Connection, unit_id: str) -> UnitVersion | None:
    rows = _rows(conn, f"SELECT {UNIT_COLUMNS} FROM case_memory.units WHERE unit_id = %s AND valid_to IS NULL", (unit_id,))
    return UnitVersion(**rows[0]) if rows else None


def current_units(conn: Connection, *, case_id: str | None = None, customer_id: str | None = None) -> list[UnitVersion]:
    rows = _rows(
        conn,
        f"""SELECT {UNIT_COLUMNS} FROM case_memory.units
            WHERE valid_to IS NULL
              AND (%(case_id)s::text IS NULL OR case_id = %(case_id)s)
              AND (%(customer_id)s::text IS NULL OR customer_id = %(customer_id)s)
            ORDER BY case_id, unit_id""",
        {"case_id": case_id, "customer_id": customer_id},
    )
    return [UnitVersion(**r) for r in rows]


def unit_history(conn: Connection, unit_id: str) -> list[UnitVersion]:
    """All versions, oldest first."""
    rows = _rows(conn, f"SELECT {UNIT_COLUMNS} FROM case_memory.units WHERE unit_id = %s ORDER BY version", (unit_id,))
    return [UnitVersion(**r) for r in rows]


def insert_next_version(
    conn: Connection,
    unit_id: str,
    *,
    expected_version: int,
    changes: dict[str, Any],
    change_reason: ChangeReason,
    at: datetime,
    created_by: str,
) -> UnitVersion:
    """Close version n and insert version n+1, failing if the current version is not n.

    Optimistic concurrency, in one transaction:
      1. Lock the current row (SELECT ... FOR UPDATE). A second writer waits here; when the
         first commits, PostgreSQL re-checks `valid_to IS NULL`, the row no longer matches,
         and the second writer finds nothing.
      2. No row, or a different version than expected: raise StaleVersionError. Nothing is written.
      3. Close version n (UPDATE valid_to only; the close-once trigger guards it).
      4. Insert version n+1 with the changes applied.
    Backstop: if two writers somehow both reach step 4, the unique partial index
    (one open version per unit) raises UniqueViolation, reported as StaleVersionError too.
    """
    unknown = set(changes) - CHANGEABLE_FIELDS
    if unknown:
        raise ValueError(f"{unit_id}: fields {sorted(unknown)} cannot change between versions")
    if change_reason == "created":
        raise ValueError("change_reason 'created' is only for version 1")
    if change_reason == "replacement":
        # Semantics (SPEC 17.21): replacement is for preferences only; claims are corrected,
        # commitments are rescheduled or change status.
        raise ValueError("change_reason 'replacement' is for customer-context items, not units")
    try:
        with conn.transaction():
            rows = _rows(
                conn,
                f"SELECT {UNIT_COLUMNS} FROM case_memory.units WHERE unit_id = %s AND valid_to IS NULL FOR UPDATE",
                (unit_id,),
            )
            if not rows or rows[0]["version"] != expected_version:
                raise StaleVersionError(unit_id, expected_version, rows[0]["version"] if rows else _latest_version(conn, unit_id))
            current = UnitVersion(**rows[0])
            conn.execute(
                "UPDATE case_memory.units SET valid_to = %s WHERE unit_id = %s AND version = %s",
                (at, unit_id, expected_version),
            )
            new = UnitVersion.model_validate({
                **current.model_dump(), **changes,
                "version": expected_version + 1, "change_reason": change_reason,
                "valid_from": at, "valid_to": None, "created_by": created_by,
            })
            _insert_unit_row(conn, new)
    except UniqueViolation as exc:
        raise StaleVersionError(unit_id, expected_version, _latest_version(conn, unit_id)) from exc
    return new


def _latest_version(conn: Connection, unit_id: str) -> int | None:
    # A fresh statement, so it sees a version another writer has just committed.
    return conn.execute("SELECT max(version) FROM case_memory.units WHERE unit_id = %s", (unit_id,)).fetchone()[0]


# --- customer context items ----------------------------------------------------------------

def insert_item(
    conn: Connection, *, customer_id: str, key: str, value: str, source: str, at: datetime,
    expires_at: datetime | None = None,
) -> ContextItem:
    """Create version 1 of a preference. A key that already has a current value must go through
    replace_item instead; the unique index (one current value per key) refuses a second one."""
    n = conn.execute(
        "SELECT count(DISTINCT item_id) FROM customer_context.items WHERE customer_id = %s", (customer_id,)
    ).fetchone()[0]
    item = ContextItem(
        item_id=f"{customer_id}-C{n + 1}", version=1, customer_id=customer_id, key=key, value=value,
        source=source, change_reason="created", valid_from=at, expires_at=expires_at,
    )
    conn.execute(
        f"INSERT INTO customer_context.items ({ITEM_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (item.item_id, item.version, item.customer_id, item.key, item.value, item.source,
         item.change_reason, item.valid_from, item.valid_to, item.expires_at),
    )
    return item


def replace_item(conn: Connection, item_id: str, *, expected_version: int, value: str, source: str,
                 at: datetime) -> ContextItem:
    """The preference changed (SPEC 17.21): close version n, insert n+1 with change_reason
    'replacement'. Same expected-version check as units: lock, compare, close, insert."""
    with conn.transaction():
        rows = _rows(
            conn,
            f"SELECT {ITEM_COLUMNS} FROM customer_context.items WHERE item_id = %s AND valid_to IS NULL FOR UPDATE",
            (item_id,),
        )
        if not rows or rows[0]["version"] != expected_version:
            latest = conn.execute("SELECT max(version) FROM customer_context.items WHERE item_id = %s", (item_id,)).fetchone()[0]
            raise StaleVersionError(item_id, expected_version, rows[0]["version"] if rows else latest)
        current = ContextItem(**rows[0])
        conn.execute(
            "UPDATE customer_context.items SET valid_to = %s WHERE item_id = %s AND version = %s",
            (at, item_id, expected_version),
        )
        new = current.model_copy(update={"version": expected_version + 1, "value": value, "source": source,
                                         "change_reason": "replacement", "valid_from": at, "valid_to": None})
        conn.execute(
            f"INSERT INTO customer_context.items ({ITEM_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (new.item_id, new.version, new.customer_id, new.key, new.value, new.source,
             new.change_reason, new.valid_from, new.valid_to, new.expires_at),
        )
    return new


def item_history(conn: Connection, item_id: str) -> list[ContextItem]:
    rows = _rows(conn, f"SELECT {ITEM_COLUMNS} FROM customer_context.items WHERE item_id = %s ORDER BY version", (item_id,))
    return [ContextItem(**r) for r in rows]


def withdraw_item(conn: Connection, item_id: str, *, expected_version: int, at: datetime) -> None:
    """Withdraw a preference: close its current version with no successor."""
    cur = conn.execute(
        "UPDATE customer_context.items SET valid_to = %s WHERE item_id = %s AND version = %s AND valid_to IS NULL",
        (at, item_id, expected_version),
    )
    if cur.rowcount != 1:
        latest = conn.execute("SELECT max(version) FROM customer_context.items WHERE item_id = %s", (item_id,)).fetchone()[0]
        raise StaleVersionError(item_id, expected_version, latest)


def current_items(conn: Connection, customer_id: str) -> list[ContextItem]:
    rows = _rows(
        conn,
        f"SELECT {ITEM_COLUMNS} FROM customer_context.items WHERE customer_id = %s AND valid_to IS NULL ORDER BY key",
        (customer_id,),
    )
    return [ContextItem(**r) for r in rows]


# --- events (append-only) -------------------------------------------------------------------

def append_event(conn: Connection, event: NewEvent) -> int:
    return conn.execute(
        """INSERT INTO events.log (customer_id, case_id, unit_id, event_type, actor_type, actor_id,
                                   occurred_at, recorded_at, payload)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING event_id""",
        (event.customer_id, event.case_id, event.unit_id, event.event_type, event.actor_type,
         event.actor_id, event.occurred_at, event.recorded_at, Jsonb(event.payload)),
    ).fetchone()[0]


def list_events(
    conn: Connection, *, customer_id: str | None = None, case_id: str | None = None, unit_id: str | None = None,
) -> list[Event]:
    rows = _rows(
        conn,
        f"""SELECT {EVENT_COLUMNS} FROM events.log
            WHERE (%(customer_id)s::text IS NULL OR customer_id = %(customer_id)s)
              AND (%(case_id)s::text IS NULL OR case_id = %(case_id)s)
              AND (%(unit_id)s::text IS NULL OR unit_id = %(unit_id)s)
            ORDER BY occurred_at, event_id""",
        {"customer_id": customer_id, "case_id": case_id, "unit_id": unit_id},
    )
    return [Event(**r) for r in rows]
