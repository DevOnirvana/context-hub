"""Shared fixtures.

Tests run against a separate database, contexthub_test, so running pytest never wipes the demo
data in contexthub. Every database test starts from a fresh reset (schema, grants, seed): tests
cannot depend on each other, and replay_to() can reset freely.

Privilege-sensitive tests connect as hub_app, because the table owner bypasses grants.
"""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import make_conninfo

from hub.clock import SimClock
from scripts.reset_db import reset

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
# Fixed instants from the SPEC 13 timeline: tests never read the wall clock.
MON_1830 = datetime(2026, 11, 2, 18, 30, tzinfo=IST)
TUE_2015 = datetime(2026, 11, 3, 20, 15, tzinfo=IST)
WED_0900 = datetime(2026, 11, 4, 9, 0, tzinfo=IST)
THRESHOLD = timedelta(minutes=15)

TEST_DB = "contexthub_test"
ADMIN_URL = make_conninfo(os.environ["DATABASE_URL_ADMIN"], dbname=TEST_DB)
APP_URL = make_conninfo(os.environ["DATABASE_URL_APP"], dbname=TEST_DB)
ARCHIVER_URL = make_conninfo(os.environ["DATABASE_URL_ARCHIVER"], dbname=TEST_DB)


@pytest.fixture(scope="session")
def test_database():
    with psycopg.connect(os.environ["DATABASE_URL_ADMIN"], autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone() is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))


@pytest.fixture
def fresh_db(test_database):
    """Reset, and return row counts taken before the test writes anything (for the seed check)."""
    reset(ADMIN_URL)
    with psycopg.connect(ADMIN_URL) as conn:
        customers, rules, cases = conn.execute(
            """SELECT (SELECT count(*) FROM customer_context.customers),
                      (SELECT count(*) FROM policy.rules),
                      (SELECT count(*) FROM case_memory.cases)"""
        ).fetchone()
    return {"customers": customers, "rules": rules, "cases": cases}


@pytest.fixture
def admin_conn(fresh_db):
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture
def app_conn(fresh_db):
    # Autocommit so a statement that is refused does not abort the rest of the test.
    with psycopg.connect(APP_URL, autocommit=True) as conn:
        yield conn


@pytest.fixture
def clock():
    """A fresh clock per test: no shared global, nothing to reset."""
    return SimClock(MON_1830)
