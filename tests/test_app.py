"""Step 7 acceptance: the SPEC 14 demo path, clicked end to end in replay mode (no network).

Streamlit's AppTest runs app.py headless in this process, against the test database. Each
check reads what the page rendered (tables, chat) or the page's own session state.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import ADMIN_URL, APP_URL, ARCHIVER_URL, UI_URL

FORBIDDEN = ["PAY-2042", "UPI-7781", "Ramesh", "4,200"]


@pytest.fixture
def app(test_database, monkeypatch):
    # load_dotenv() in app.py does not override variables that are already set.
    for key, url in (("DATABASE_URL_ADMIN", ADMIN_URL), ("DATABASE_URL_APP", APP_URL),
                     ("DATABASE_URL_ARCHIVER", ARCHIVER_URL), ("DATABASE_URL_UI", UI_URL)):
        monkeypatch.setenv(key, url)
    monkeypatch.setenv("MISSED_THRESHOLD_MINUTES", "15")
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=60)
    at.session_state["llm_mode"] = "replay"  # tests never spend: a miss must fail, not call the API
    at.run()
    assert not at.exception
    yield at
    for name in ("app_conn", "ui_conn", "archiver_conn"):
        at.session_state[name].close()


def click(at, key):
    at.button(key=key).click().run()
    assert not at.exception, at.exception
    return at


def table(at, column, value):
    """True if any rendered table has `value` in `column`."""
    return any(column in df.value.columns and (df.value[column] == value).any() for df in at.dataframe)


def test_demo_path_in_replay(app):
    at = app
    click(at, "jump_Tue 20:15")
    assert table(at, "status", "superseded") and table(at, "change_reason", "reschedule")   # 9 PM -> 9 AM, history kept

    click(at, "jump_Wed 09:16")
    assert table(at, "status", "missed")                                                     # moment 2

    click(at, "jump_Wed 10:00")
    for n in range(4):
        assert [at.button(key=f"turn_{k}").disabled for k in range(4)] == [k != n for k in range(4)]  # in order
        click(at, f"turn_{n}")
    assert all(at.button(key=f"turn_{k}").disabled for k in range(4))                        # no double send
    replies = [text for speaker, text in at.session_state["chat"] if speaker == "agent"]
    assert len(replies) == 4 and not [t for t in at.session_state["chat"] if t[0] == "error"]
    assert "sorry" in replies[0].lower() and "23,980" in replies[0]                          # moments 3 and 4
    assert "Is that right?" in replies[1]
    assert not [s for s in FORBIDDEN if s in " ".join(replies)]                               # moment 7
    view = at.session_state["view"]
    assert [c.name for c in view["calls"]] == ["escalate_to_human"]                           # moment 8
    assert table(at, "event", "escalated_to_human")
    assert view["access"] and {r["decision"] for r in view["access"] if r["case"] == "PAY-2042"} == {"existence_only"}

    click(at, "edit_history")                                                                  # moment 6
    assert "InsufficientPrivilege" in at.session_state["edit_result"] and "permission denied" in \
        at.session_state["edit_result"]


def test_preview_packet_payments_full_credit_existence_only(app):
    at = click(app, "jump_Wed 10:00")
    at.selectbox(key="role").set_value("payments_agent").run()
    click(at, "preview")
    packet = at.session_state["view"]["packet"]
    assert "PAY-2042" in [c.case_id for c in packet.cases]                                   # payments: in full
    assert table(at, "case", "PAY-2042") and table(at, "decision", "allow")

    at.selectbox(key="role").set_value("credit_agent").run()
    click(at, "preview")
    packet = at.session_state["view"]["packet"]
    assert "PAY-2042" not in packet.model_dump_json()                                         # credit: existence only
    assert packet.other_cases_summary == "1 other open case: Payments"
    pay = {r["decision"] for r in at.session_state["view"]["access"] if r["case"] == "PAY-2042"}
    assert pay == {"existence_only"}                                                           # moment 5


def test_unauthenticated_preview_gets_nothing_and_is_logged(app):
    at = click(app, "jump_Wed 10:00")
    at.toggle(key="authenticated").set_value(False).run()
    click(at, "preview")
    view = at.session_state["view"]
    assert view["packet"] is None and "NotAuthenticatedError" in view["error"]
    assert [r["decision"] for r in view["access"]] == ["deny"]


def test_threshold_30_means_no_miss_at_0916(app):
    at = app
    at.number_input(key="threshold").set_value(30).run()                                       # moment 9
    click(at, "jump_Wed 09:16")
    assert not table(at, "status", "missed")
    assert table(at, "due_at", "Wed 04 Nov 09:00") and table(at, "status", "active")


def test_default_is_auto_and_record_is_not_offered(test_database, monkeypatch):
    """A fresh UI defaults to auto. No turn is sent here, so nothing can be spent."""
    for key, url in (("DATABASE_URL_ADMIN", ADMIN_URL), ("DATABASE_URL_APP", APP_URL),
                     ("DATABASE_URL_ARCHIVER", ARCHIVER_URL), ("DATABASE_URL_UI", UI_URL)):
        monkeypatch.setenv(key, url)
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=60)
    at.run()
    assert at.radio(key="llm_mode").options == ["auto", "replay", "live"]
    assert at.radio(key="llm_mode").value == "auto"
    for name in ("app_conn", "ui_conn", "archiver_conn"):
        at.session_state[name].close()


def test_free_text_error_is_inline_with_retry(app):
    """Replay mode, unrecorded words, any agent and time: an inline error and a Retry button, no crash."""
    at = click(app, "jump_Tue 19:40")
    at.selectbox(key="role").set_value("payments_agent").run()
    at.chat_input(key="chat_box").set_value("Has my UPI money come back?").run()
    assert not at.exception
    assert at.session_state["chat"][-1][0] == "error" and "ReplayCacheMiss" in at.session_state["chat"][-1][1]
    assert at.button(key="retry_button")
    click(at, "retry_button")                                 # still a miss in replay: shown again, still no crash
    assert at.session_state["chat"][-1][0] == "error"
    assert at.session_state["conversation"].messages == []  # rolled back: nothing half-sent remains
