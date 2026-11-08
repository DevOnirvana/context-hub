"""Drop and reload the Context Hub database: schema, grants, seed.

Usage: python -m scripts.reset_db

Connects as the admin role (DATABASE_URL_ADMIN) because only the table owner may
drop and create schemas. The app itself never uses this connection.
"""
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

# Order matters: grants need the tables, the seed needs the tables.
SQL_FILES = ["schema.sql", "grants.sql", "seed.sql"]

# Dropping the schemas with CASCADE removes every table, index, sequence and grant in them.
# Roles are cluster-level and are not dropped; grants.sql handles them idempotently.
SCHEMAS = ["case_memory", "events", "customer_context", "archive", "policy"]


def reset(admin_url: str) -> None:
    # One transaction: a failure in any file leaves the previous database intact.
    with psycopg.connect(admin_url) as conn:
        for schema in SCHEMAS:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        for name in SQL_FILES:
            # No parameters, so psycopg sends the whole file as one multi-statement query.
            conn.execute((SQL_DIR / name).read_text())


def main() -> None:
    load_dotenv()
    reset(os.environ["DATABASE_URL_ADMIN"])
    print("contexthub reset: " + ", ".join(SQL_FILES))


if __name__ == "__main__":
    main()
