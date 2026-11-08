"""Systems-of-record mocks: read-only, clock-aware, never touching the database."""
from datetime import datetime

import psycopg
import pytest

from hub.clock import IST
from systems import dialler, dispute, documents, statement, upi


def t(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=IST)


def test_dispute_status_follows_the_clock():
    assert dispute.get_dispute_status("DSP-1001", t(2, 18, 29)) is None
    assert dispute.get_dispute_status("DSP-1001", t(2, 18, 30)).status == "investigation_initiated"
    assert dispute.get_dispute_status("DSP-1001", t(4, 7, 59)).status == "investigation_initiated"
    assert dispute.get_dispute_status("DSP-1001", t(4, 8)).status == "provisional_credit_issued"


def test_statement_before_and_after_provisional_credit():
    before = statement.get_statement("4417", t(3, 12))
    assert (before.total_due_inr, before.minimum_due_inr, before.disputed_amount_inr, before.provisional_credit_inr) == (
        32480, 1620, 8500, 0)
    after = statement.get_statement("4417", t(4, 10))
    assert (after.total_due_inr, after.minimum_due_inr, after.provisional_credit_inr) == (23980, 1200, 8500)


def test_upi_status():
    assert upi.get_upi_status("UPI-7781", t(3, 19)) is None
    s = upi.get_upi_status("UPI-7781", t(4, 10))
    assert (s.status, s.amount_inr, s.payee) == ("pending_reversal", 4200, "Ramesh K")


def test_document_upload_window_is_start_exclusive_end_inclusive():
    assert [u.document for u in documents.uploads_between("CUST-001", t(3, 15), t(3, 16))] == ["bank_statement"]
    assert documents.uploads_between("CUST-001", t(3, 16), t(3, 17)) == []


def test_dialler_profiles():
    assert dialler.records_between("CUST-001", t(4, 0), t(4, 23), "none") == []
    (late,) = dialler.records_between("CUST-001", t(4, 0), t(4, 23), "late_completed")
    assert (late.outcome, late.occurred_at, late.recorded_at) == ("completed", t(4, 9, 4), t(4, 9, 40))


def test_mocks_never_touch_the_database(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a systems mock opened a database connection")
    monkeypatch.setattr(psycopg, "connect", refuse)
    dispute.get_dispute_status("DSP-1001", t(4, 10))
    statement.get_statement("4417", t(4, 10))
    upi.get_upi_status("UPI-7781", t(4, 10))
    documents.uploads_between("CUST-001", t(2, 0), t(5, 0))
    dialler.records_between("CUST-001", t(2, 0), t(5, 0), "late_completed")
