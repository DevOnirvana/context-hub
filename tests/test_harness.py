"""Step 8: the T2 harness (Hub vs latest-value-wins), no LLM."""
import os

from backends.lww import LwwBackend
from scenarios.run_harness import run
from scenarios.timeline import START
from tests.conftest import ADMIN_URL


def test_lww_put_replaces_the_value(test_database):
    """The baseline's one property: one record per key, and a put on an existing key replaces it."""
    lww = LwwBackend(ADMIN_URL, START)
    try:
        lww.put("callback_due", "Tue 21:00")
        lww.put("callback_due", "Wed 09:00")
        assert lww.all() == {"callback_due": "Wed 09:00"}
        assert lww.conn.execute("SELECT count(*) FROM lww.store").fetchone()[0] == 1
    finally:
        lww.conn.execute("DROP SCHEMA lww CASCADE")
        lww.close()


def test_harness_hub_passes_all_and_every_difference_is_explained():
    rows = run(os.environ["DATABASE_URL_ADMIN"], os.environ["DATABASE_URL_APP"])  # its own database
    assert [r["scenario"] for r in rows] == list(range(1, 13))
    assert all(r["hub"] == "Pass" for r in rows)
    assert not [r for r in rows if "UNEXPLAINED" in r["difference_note"]]
