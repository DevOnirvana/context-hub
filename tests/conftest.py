"""Shared fixtures for database tests.

The database is reset once per test session. Privilege tests connect as hub_app,
because the table owner bypasses grants and would prove nothing.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest
from dotenv import load_dotenv

from scripts.reset_db import reset

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
# A fixed instant (Mon 2 Nov 2026 18:30 IST): tests never read the wall clock.
MON_1830 = datetime(2026, 11, 2, 18, 30, tzinfo=IST)


@pytest.fixture(scope="session")
def fresh_db():
    """Reset once, and return row counts taken before any test writes, so the seed can be checked."""
    reset(os.environ["DATABASE_URL_ADMIN"])
    with psycopg.connect(os.environ["DATABASE_URL_ADMIN"]) as conn:
        customers, rules, cases = conn.execute(
            """SELECT (SELECT count(*) FROM customer_context.customers),
                      (SELECT count(*) FROM policy.rules),
                      (SELECT count(*) FROM case_memory.cases)"""
        ).fetchone()
    return {"customers": customers, "rules": rules, "cases": cases}


@pytest.fixture
def admin_conn(fresh_db):
    with psycopg.connect(os.environ["DATABASE_URL_ADMIN"], autocommit=True) as conn:
        yield conn


@pytest.fixture
def app_conn(fresh_db):
    # Autocommit so a statement that is refused does not abort the rest of the test.
    with psycopg.connect(os.environ["DATABASE_URL_APP"], autocommit=True) as conn:
        yield conn
