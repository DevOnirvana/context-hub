"""Context Hub demo UI (SPEC 14). Run: streamlit run app.py

Left: chat with an agent. Right: what the hub holds and did. Sidebar: the simulated clock.
Three database roles, each with only what its job needs:
* hub_app      the agents' hub calls (and "Try to edit history": the agents' role cannot edit);
* hub_ui       the audit tabs, SELECT only;
* hub_archiver transcripts at the end of a conversation, INSERT only.
The UI never changes memory itself: every change goes through hub/interface.py, via the agent
loop or the shared timeline (replay_to), exactly as in the tests.
"""
import os
from datetime import datetime, timedelta

import psycopg
import streamlit as st
from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo

from agents.llm import LLM, LLMError, load_config
from agents.loop import Conversation, reply_instructions
from hub.archive import archive_conversation
from hub.clock import IST
from hub.errors import HubError
from hub.interface import ContextHub
from scenarios.demo_turns import WED_TURNS
from scenarios.timeline import CUSTOMER, START, advance_to, replay_to
from ui import queries

load_dotenv()

JUMPS = [("Mon 18:30", datetime(2026, 11, 2, 18, 30, tzinfo=IST)), ("Tue 16:00", datetime(2026, 11, 3, 16, 0, tzinfo=IST)),
         ("Tue 19:40", datetime(2026, 11, 3, 19, 40, tzinfo=IST)), ("Tue 20:15", datetime(2026, 11, 3, 20, 15, tzinfo=IST)),
         ("Wed 09:16", datetime(2026, 11, 4, 9, 16, tzinfo=IST)), ("Wed 10:00", datetime(2026, 11, 4, 10, 0, tzinfo=IST))]
PURPOSES = {"credit_agent": "card_dispute_servicing", "payments_agent": "payments_servicing"}
# Record mode is CLI-only on purpose: the UI must not be able to overwrite the demo's replay cache.
LLM_MODES = ["replay", "live"]
ss = st.session_state
# Key columns get room, so "existence_only" and "superseded" are never cut off.
COLUMNS = {"decision": st.column_config.TextColumn(width="medium"),
           "status": st.column_config.TextColumn(width="small"),
           "change_reason": st.column_config.TextColumn(width="medium")}


# --- state changes (button callbacks run before the page renders) ------------------------------

def connect() -> None:
    app_url = os.environ["DATABASE_URL_APP"]
    ui_url = os.environ.get("DATABASE_URL_UI") or make_conninfo(app_url, user="hub_ui")
    # Autocommit: ContextHub manages its own transactions, and a refused statement must not
    # leave the connection in an aborted transaction.
    ss.app_conn = psycopg.connect(app_url, autocommit=True)
    ss.ui_conn = psycopg.connect(ui_url, autocommit=True)
    ss.archiver_conn = psycopg.connect(os.environ["DATABASE_URL_ARCHIVER"], autocommit=True)


def end_conversation() -> None:
    """Archive the transcript (hub_archiver, INSERT only) and start the next chat fresh."""
    if ss.get("conversation") is not None and ss.chat:
        ss.conv_seq += 1
        archive_conversation(ss.archiver_conn, conversation_id=f"CONV-UI-{ss.conv_seq:03d}", customer_id=CUSTOMER,
                             case_id=None, agent_role=ss.conversation.role, channel="chat",
                             started_at=ss.conv_started, transcript=[{"speaker": s, "text": t} for s, t in ss.chat])
    ss.conversation, ss.chat, ss.next_turn = None, [], 0


def go_to(t: datetime) -> None:
    """The clock only moves forward, so any jump is reset plus replay of the shared timeline
    (SPEC 17.18). The threshold setting takes effect here."""
    end_conversation()
    threshold = timedelta(minutes=ss.threshold)
    clock = replay_to(t, threshold)
    ss.hub = ContextHub(ss.app_conn, clock, threshold=threshold)
    ss.view, ss.edit_result = None, None


def plus_15() -> None:
    advance_to(ss.hub, ss.hub.clock.now() + timedelta(minutes=15))  # delivers system records on the way


def session():
    role = ss.role
    return ss.hub.open_session(agent_id=role, agent_role=role, purpose=PURPOSES[role], customer_id=CUSTOMER,
                               authenticated=ss.authenticated)


def send(text: str, at: datetime | None = None, label: str = "typed turn", scripted: int | None = None) -> None:
    hub = ss.hub
    if scripted is not None:
        ss.next_turn = scripted + 1  # replay hits only for the recorded order
    if at is not None and hub.clock.now() < at:
        advance_to(hub, at)
    before = queries.max_log_id(ss.ui_conn)
    try:
        if ss.conversation is None:
            ss.conversation = Conversation(hub=hub, llm=LLM(load_config(mode=ss.llm_mode)), session=session())
            ss.conv_started = hub.clock.now()
        result = ss.conversation.turn(text)
    except (LLMError, HubError, RuntimeError) as exc:
        # Shown, not raised. The conversation is dropped: its history may now end mid-turn.
        ss.chat.append(("customer", text))
        ss.chat.append(("error", f"{type(exc).__name__}: {exc}"))
        ss.conversation = None
        ss.view = {"source": f"{label} (failed)", "packet": None, "calls": [], "instructions": None,
                   "access": queries.access_log(ss.ui_conn, after=before)}
        return
    ss.chat += [("customer", text), ("agent", result.reply)]
    ss.view = {"source": label, "packet": result.packet, "calls": result.tool_calls,
               "instructions": reply_instructions(result.packet), "access": queries.access_log(ss.ui_conn, after=before)}


def preview_packet() -> None:
    """get_context for the selected role, with no model call: what that agent would see now."""
    before = queries.max_log_id(ss.ui_conn)
    try:
        packet, error = ss.hub.get_context(session()), None
    except HubError as exc:
        packet, error = None, f"{type(exc).__name__}: {exc}"
    ss.view = {"source": f"preview for {ss.role}", "packet": packet, "calls": [], "error": error,
               "instructions": reply_instructions(packet) if packet else None,
               "access": queries.access_log(ss.ui_conn, after=before)}


def try_to_edit_history() -> None:
    """As hub_app, the agents' own role: PostgreSQL refuses, not the app (SPEC 8)."""
    try:
        ss.app_conn.execute("UPDATE events.log SET payload = '{}'::jsonb "
                            "WHERE event_id = (SELECT min(event_id) FROM events.log)")
        ss.edit_result = "UNEXPECTED: the update succeeded"
    except psycopg.Error as exc:
        ss.edit_result = f"{type(exc).__name__}: {str(exc).strip()}"


# --- first load ---------------------------------------------------------------------------------

if "hub" not in ss:
    ss.threshold = int(os.environ.get("MISSED_THRESHOLD_MINUTES", "15"))
    ss.llm_mode, ss.role, ss.authenticated = "replay", "credit_agent", True
    ss.conversation, ss.chat, ss.conv_seq, ss.view, ss.edit_result, ss.next_turn = None, [], 0, None, None, 0
    connect()
    go_to(START)

st.set_page_config(page_title="Context Hub", layout="wide")
now = ss.hub.clock.now()

# --- sidebar: the clock and settings --------------------------------------------------------------

with st.sidebar:
    st.header("Simulated clock")
    st.subheader(now.strftime("%a %d %b %Y, %H:%M IST"))
    cols = st.columns(2)
    for i, (label, t) in enumerate(JUMPS):
        cols[i % 2].button(label, key=f"jump_{label}", on_click=go_to, args=(t,), width="stretch")
    st.button("+15 min", key="plus_15", on_click=plus_15, width="stretch")
    st.divider()
    st.number_input("MISSED_THRESHOLD_MINUTES (applies on the next jump)", min_value=0, max_value=240, step=5,
                    key="threshold")
    st.radio("LLM_MODE", LLM_MODES, key="llm_mode", horizontal=True, on_change=end_conversation,
             help="replay: recorded responses, no network. live: calls the API (costs money). "
                  "record is CLI-only.")
    st.button("Reset demo", key="reset", on_click=go_to, args=(START,), width="stretch")
    st.button("End conversation", key="end_conversation", on_click=end_conversation, width="stretch")
    st.divider()
    st.button("Try to edit history", key="edit_history", on_click=try_to_edit_history, width="stretch",
              help="Runs UPDATE on events.log as hub_app, the agents' role.")
    if ss.edit_result:
        st.error(ss.edit_result)

# --- left: chat; right: what the hub holds ----------------------------------------------------------

left, right = st.columns([2, 3], gap="large")

with left:
    st.subheader("Chat")
    c1, c2 = st.columns(2)
    c1.selectbox("Agent", list(PURPOSES), key="role", on_change=end_conversation)
    c2.toggle("Customer authenticated", key="authenticated", on_change=end_conversation)
    st.button("Preview packet (no model call)", key="preview", on_click=preview_packet)
    if ss.role == "credit_agent":
        st.caption("Scripted demo turns: the recorded words, times and order, so replay always matches. "
                   "Only the next one is enabled; start from the Wed 10:00 jump.")
        for n, turn in enumerate(WED_TURNS):
            st.button(f"Send: {turn.label}", key=f"turn_{n}", on_click=send,
                      args=(turn.text, turn.at, turn.label, n),
                      disabled=n != ss.next_turn or now > turn.at, width="stretch")
    for speaker, text in ss.chat:
        if speaker == "error":
            st.error(text)
        else:
            with st.chat_message("user" if speaker == "customer" else "assistant"):
                st.write(text)
    typed = st.chat_input("Type as the customer")
    if typed:
        send(typed)
        st.rerun()

with right:
    tabs = st.tabs(["Case memory", "Events", "Customer context", "Access log", "What the agent sees"])
    with tabs[0]:
        st.caption("Every version of every unit. A change is a new version; the old one gets valid_to.")
        st.dataframe(queries.case_memory(ss.ui_conn, CUSTOMER, now), hide_index=True, width="stretch", column_config=COLUMNS)
    with tabs[1]:
        st.caption("Ordered by when it happened. occurred_at vs recorded_at: when it happened vs when the bank learned it.")
        st.dataframe(queries.events(ss.ui_conn, CUSTOMER), hide_index=True, width="stretch", column_config=COLUMNS)
    with tabs[2]:
        st.dataframe(queries.customer_context(ss.ui_conn, CUSTOMER), hide_index=True, width="stretch", column_config=COLUMNS)
    with tabs[3]:
        decision = st.selectbox("Decision", ["all", "allow", "existence_only", "deny"], key="decision_filter")
        st.dataframe(queries.access_log(ss.ui_conn, decision=None if decision == "all" else decision),
                     hide_index=True, width="stretch", column_config=COLUMNS)
    with tabs[4]:
        view = ss.view
        if view is None:
            st.info("Send a turn or click Preview packet.")
        else:
            st.caption(f"Source: {view['source']}")
            if view.get("error"):
                st.error(view["error"])
            if view["instructions"]:
                st.markdown("**Reply instructions from customer context**")
                st.info(view["instructions"])
            if view["packet"] is not None:
                packet = view["packet"]
                st.markdown(f"**Context packet** (as of {queries.fmt(packet.as_of)}): "
                            f"cases {[c.case_id for c in packet.cases]}; other cases: "
                            f"{packet.other_cases_summary or 'none'}")
                st.json(packet.model_dump(mode="json"), expanded=False)
            for call in view["calls"]:
                with st.expander(f"Tool: {call.name} {call.args}{' (refused)' if call.is_error else ''}"):
                    st.code(call.result, language="json")
            st.markdown("**Access-log rows written by this turn**")
            st.dataframe(view["access"], hide_index=True, width="stretch", column_config=COLUMNS)
