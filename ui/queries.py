"""Read-only queries for the demo UI's audit tabs, run on a hub_ui connection (SELECT only).

This is an operator's view of the tables, not an agent path: agents only ever see what
hub/interface.py get_context() returns. Version history and the access log are shown here
because the demo is about proving what the hub did, and an auditor would see exactly this.
"""
import json
from datetime import datetime

from psycopg import Connection
from psycopg.rows import dict_row

from hub import lifecycle, store
from hub.clock import to_ist


def fmt(dt: datetime | None) -> str:
    return "" if dt is None else to_ist(dt).strftime("%a %d %b %H:%M")


def _rows(conn: Connection, sql: str, params=None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def case_memory(conn: Connection, customer_id: str, now: datetime) -> list[dict]:
    """Every version of every unit, oldest first, with the status the hub shows for it (SPEC 17.2)."""
    out = []
    for case in store.cases_as_of(conn, customer_id, now):
        unit_ids = [r["unit_id"] for r in _rows(conn, "SELECT DISTINCT unit_id FROM case_memory.units "
                                                      "WHERE case_id = %s ORDER BY unit_id", (case.case_id,))]
        for unit_id in unit_ids:
            versions = store.versions_as_of(conn, unit_id, now)
            for v, shown in zip(versions, lifecycle.displayed_statuses(versions)):
                current = v.valid_to is None or v.valid_to > now
                out.append({"case": case.case_id, "unit": unit_id, "v": v.version, "current": "yes" if current else "",
                            "type": v.unit_type, "owner": v.owner or "", "action": v.action or "",
                            "due_at": fmt(v.due_at), "status": shown, "change_reason": v.change_reason,
                            "valid_from": fmt(v.valid_from), "valid_to": fmt(v.valid_to),
                            "payload": json.dumps(v.payload)})
    return out


def events(conn: Connection, customer_id: str) -> list[dict]:
    """occurred_at and recorded_at side by side, ordered by when it happened (SPEC 17.32)."""
    return [{"occurred_at": fmt(r["occurred_at"]), "recorded_at": fmt(r["recorded_at"]), "event": r["event_type"],
             "case": r["case_id"] or "", "unit": r["unit_id"] or "", "actor": f'{r["actor_type"]}:{r["actor_id"]}',
             "payload": json.dumps(r["payload"])}
            for r in _rows(conn, "SELECT * FROM events.log WHERE customer_id = %s ORDER BY occurred_at, event_id",
                           (customer_id,))]


def customer_context(conn: Connection, customer_id: str) -> list[dict]:
    return [{"item": r["item_id"], "v": r["version"], "key": r["key"], "value": r["value"], "source": r["source"],
             "change_reason": r["change_reason"], "valid_from": fmt(r["valid_from"]), "valid_to": fmt(r["valid_to"])}
            for r in _rows(conn, "SELECT * FROM customer_context.items WHERE customer_id = %s "
                                 "ORDER BY key, version", (customer_id,))]


def max_log_id(conn: Connection) -> int:
    return conn.execute("SELECT coalesce(max(log_id), 0) FROM policy.access_log").fetchone()[0]


def access_log(conn: Connection, *, after: int = 0, decision: str | None = None) -> list[dict]:
    """Newest first. after: only rows written after that log_id (one turn's rows)."""
    rows = _rows(conn, "SELECT * FROM policy.access_log WHERE log_id > %s AND (%s::text IS NULL OR decision = %s) "
                       "ORDER BY log_id DESC", (after, decision, decision))
    # The decision first: on a narrow screen it is the column that must stay visible.
    return [{"id": r["log_id"], "at": fmt(r["at"]), "case": r["case_id"] or "", "unit": r["unit_id"] or "",
             "decision": r["decision"], "reason": r["reason"], "role": r["agent_role"], "purpose": r["purpose"]}
            for r in rows]
