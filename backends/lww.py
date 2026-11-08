"""Latest-value-wins baseline for the T2 experiment (SCENARIOS.md, method LWW).

Label in all output: "latest-value-wins (LangGraph store put semantics)". It reproduces what
a key-value agent memory store does on put: one record per (namespace, key), and a put on an
existing key replaces the value. It is a plain PostgreSQL table with an upsert, not LangGraph
itself, so the experiment has no extra dependency; only the put semantics matter here.

What the baseline is given, to keep the comparison fair:
* the same step stream as the hub, including system records (document uploads, dialler outcomes);
* the same acceptance decisions (the agent decides; the store only remembers);
* the same read-time rule: any system reference it holds is fetched live for the probe.
What it lacks, by design: versions, lifecycle rules (no sweep), admission tests, access policy.
"""
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from backends.steps import (CallbackRequest, Claim, Clock, CorrectClaim, CustomerPromise, OpenCase, Preference,
                            Step, Told)
from backends.structured import fetch_live
from hub.acceptance import check_callback
from hub.clock import to_ist
from hub.timeres import RelativeTime, resolve
from scenarios.timeline import CUSTOMER
from systems import casemgmt, dialler, documents

LABEL = "latest-value-wins (LangGraph store put semantics)"

DDL = """
DROP SCHEMA IF EXISTS lww CASCADE;
CREATE SCHEMA lww;
CREATE TABLE lww.store (
    namespace  text        NOT NULL,   -- the customer, as in a store namespace (customer_id,)
    key        text        NOT NULL,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL,   -- simulated clock, never now()
    PRIMARY KEY (namespace, key)
);
"""
REF_KINDS = {"dispute_ref": "dispute", "card_last4": "statement", "upi_ref": "upi"}


def iso(dt: datetime) -> str:
    return to_ist(dt).isoformat()


@dataclass
class LwwContext:
    memory: dict           # every key for the customer: no policy filtering
    live: dict             # (kind, ref) -> live fact, fetched at read time from stored references


class LwwBackend:
    name = "LWW"

    def __init__(self, admin_url: str, start: datetime):
        self.conn = psycopg.connect(admin_url, autocommit=True)
        self.conn.execute(DDL)
        self.now = start

    def close(self) -> None:
        self.conn.close()

    # --- the store: put replaces, get reads ----------------------------------------------------

    def put(self, key: str, value) -> None:
        self.conn.execute(
            """INSERT INTO lww.store (namespace, key, value, updated_at) VALUES (%s, %s, %s, %s)
               ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
            (CUSTOMER, key, Jsonb(value), self.now))

    def get(self, key: str):
        row = self.conn.execute("SELECT value FROM lww.store WHERE namespace = %s AND key = %s",
                                (CUSTOMER, key)).fetchone()
        return row[0] if row else None

    def all(self) -> dict:
        return dict(self.conn.execute("SELECT key, value FROM lww.store WHERE namespace = %s ORDER BY key",
                                      (CUSTOMER,)).fetchall())

    # --- steps -> puts ----------------------------------------------------------------------------

    def apply(self, step: Step) -> None:
        match step:
            case Clock(at=at, dialler_profile=profile):
                records = [(u.recorded_at, "upload", u) for u in documents.uploads_between(CUSTOMER, self.now, at)]
                records += [(r.recorded_at, "dialler", r) for r in dialler.records_between(CUSTOMER, self.now, at, profile)]
                for recorded_at, kind, record in sorted(records, key=lambda r: r[0]):
                    self.now = recorded_at
                    if kind == "dialler":
                        self.put("callback_status", record.outcome)
                    elif (promise := self.get("customer_promise")) is not None:
                        self.put("customer_promise", {**promise, "status": "fulfilled"})
                self.now = at
            case OpenCase(case_type=case_type):
                opening = casemgmt.open_case(CUSTOMER, case_type)
                self.put(f"case:{opening.case_id}", {"case_type": case_type, "business_line": opening.business_line,
                                                     "refs": opening.refs})
            case Claim(case_id=case_id, fact=fact, fields=fields) | CorrectClaim(case_id=case_id, fact=fact, fields=fields):
                self.put(fact, {**fields, "case_id": case_id})
            case Told(case_id=case_id, message=message):
                self.put("last_told", {"case_id": case_id, "message": message})
            case CustomerPromise(case_id=case_id, action=action, due_at=due_at):
                self.put("customer_promise", {"case_id": case_id, "action": action, "due_at": iso(due_at),
                                              "status": "open"})
            case CallbackRequest(case_id=case_id, day_offset=day_offset, time=hhmm):
                decision = check_callback(resolve(RelativeTime(day_offset=day_offset, time=hhmm), self.now), self.now)
                self.put("callback_request", {"case_id": case_id, "requested": iso(decision.due_at),
                                              "outcome": "accepted" if decision.accepted else
                                              f"declined: {decision.reason}"})
                if decision.accepted:
                    self.put("callback_due", iso(decision.due_at))
                    self.put("callback_status", "active")
            case Preference(key=key, value=value):
                self.put(f"preference:{key}", value)
            case _:
                raise TypeError(f"unknown step {step!r}")

    def context(self, role: str) -> LwwContext:
        """No access policy: every role gets every key (role is accepted for a common signature)."""
        memory = self.all()
        live = {}
        for key, value in memory.items():
            if key.startswith("case:"):
                for ref_key, ref in value["refs"].items():
                    live[(REF_KINDS[ref_key], ref)] = fetch_live(REF_KINDS[ref_key], ref, self.now)
        return LwwContext(memory=memory, live=live)
