"""Archive writes (SPEC 8, 17.29). Raw transcripts, audit only: agents never read them.

Uses a hub_archiver connection, which may INSERT into archive.conversations and nothing else.
It is not part of hub/interface.py on purpose: the app's role (hub_app) has no access to the
archive schema at all.
"""
from datetime import datetime

from psycopg import Connection
from psycopg.types.json import Jsonb


def archive_conversation(conn: Connection, *, conversation_id: str, customer_id: str, case_id: str | None,
                         agent_role: str, channel: str, started_at: datetime, transcript: list[dict]) -> None:
    conn.execute(
        """INSERT INTO archive.conversations
               (conversation_id, customer_id, case_id, agent_role, channel, started_at, transcript)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (conversation_id, customer_id, case_id, agent_role, channel, started_at, Jsonb(transcript)),
    )
