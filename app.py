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
import time
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
# auto (default): recorded answers play back, anything new calls the API once and is saved as a NEW
# entry. Record mode is CLI-only on purpose: the UI must never overwrite the demo's replay cache.
LLM_MODES = ["auto", "replay", "live"]
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


def queue(text: str, at: datetime | None = None, label: str = "typed turn", scripted: int | None = None) -> None:
    """Buttons and the chat box only queue a turn; the page body runs it inside a live status box."""
    ss.pending, ss.retry = {"text": text, "at": at, "label": label, "scripted": scripted}, None


def queue_typed() -> None:
    if ss.chat_box:
        queue(ss.chat_box)


def retry() -> None:
    """Re-run the failed turn. After a hand-over the agent is closed, so it starts a new conversation."""
    pending, ss.retry = ss.retry, None
    if pending.pop("new_conversation", False) or (ss.conversation is not None and ss.conversation.closed):
        end_conversation()
    ss.pending = pending


def get_llm() -> LLM:
    """One LLM per browser session and mode, so the session's spend adds up across conversations."""
    if ss.llm is None or ss.llm.config.mode != ss.llm_mode:
        ss.llm = LLM(load_config(mode=ss.llm_mode))
    return ss.llm


def run_pending() -> None:
    """Run the queued turn, listing each step live (st.status). Errors are shown, never raised."""
    pending, ss.pending = ss.pending, None
    hub = ss.hub
    if pending["scripted"] is not None:
        ss.next_turn = pending["scripted"] + 1  # replay hits only in the recorded order
    if pending["at"] is not None and hub.clock.now() < pending["at"]:
        advance_to(hub, pending["at"])
    before = queries.max_log_id(ss.ui_conn)
    started = time.monotonic()  # UI timing only, not domain time: domain time is the simulated clock

    def elapsed() -> str:
        return f"{time.monotonic() - started:.1f}s"

    with st.status(f"{ss.role} is working", expanded=True) as status:
        def on_event(kind: str, detail) -> None:
            if kind == "thinking":
                status.update(label=f"{ss.role}: model call {detail} ({elapsed()})")
            elif kind == "tool":
                name, ok = detail
                st.write(f"{name}: {'ok' if ok else 'refused'} ({elapsed()})")
            else:
                st.write(f"handed to a human: {detail} ({elapsed()})")

        spent_before = ss.llm.spent_this_session if ss.llm is not None else 0.0
        try:
            llm = get_llm()
            spent_before = llm.spent_this_session
            if ss.conversation is None:
                ss.conversation = Conversation(hub=hub, llm=llm, session=session())
                ss.conv_started = hub.clock.now()
            result = ss.conversation.turn(pending["text"], on_event)
        except (LLMError, HubError, RuntimeError) as exc:
            # The loop rolled its messages back, so Retry resends a clean turn.
            ss.chat += [("customer", pending["text"]), ("error", f"{type(exc).__name__}: {exc}")]
            ss.retry = dict(pending)
            ss.view = {"source": f"{pending['label']} (failed)", "packet": None, "calls": [], "instructions": None,
                       "access": queries.access_log(ss.ui_conn, after=before)}
            status.update(label=f"Failed after {elapsed()}", state="error")
            return
        finally:
            if ss.llm is not None:
                ss.session_spend += ss.llm.spent_this_session - spent_before
        ss.chat += [("customer", pending["text"]), ("agent", result.reply)]
        if ss.conversation.closed:  # a refusal or the step cap: code handed over to a human
            ss.chat.append(("notice", "The agent handed this conversation to a human. Retry starts a new one."))
            ss.retry = {**pending, "new_conversation": True}
        ss.view = {"source": pending["label"], "packet": result.packet, "calls": result.tool_calls,
                   "instructions": result.instructions,
                   "access": queries.access_log(ss.ui_conn, after=before)}
        status.update(label=f"Done in {elapsed()}: {len(result.tool_calls)} tool call(s), {result.steps} model call(s)",
                      state="complete", expanded=False)


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
    ss.setdefault("llm_mode", "auto")  # setdefault: tests and demo_run pin replay before the first run
    ss.role, ss.authenticated = "credit_agent", True
    ss.conversation, ss.chat, ss.conv_seq, ss.view, ss.edit_result, ss.next_turn = None, [], 0, None, None, 0
    ss.pending, ss.retry, ss.llm, ss.session_spend = None, None, None, 0.0
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
             help="auto: recorded answers play back; anything new calls the API once and is saved. "
                  "replay: recorded only, no network. live: always calls the API. record is CLI-only.")
    usage = load_config(mode="replay")  # only for the usage log path and the budget; makes no call
    st.caption(f"Spend this session: ${ss.session_spend:.4f}. Logged total: "
               f"${LLM(usage).spent_usd():.4f} of ${usage.budget_usd:.2f} budget.")
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
        st.caption("Scripted demo turns: the recorded words, times and order, so they replay at no cost. "
                   "Only the next one is enabled; start from the Wed 10:00 jump. After free text they run in auto.")
        for n, turn in enumerate(WED_TURNS):
            st.button(f"Send: {turn.label}", key=f"turn_{n}", on_click=queue,
                      args=(turn.text, turn.at, turn.label, n), disabled=n != ss.next_turn, width="stretch")
    for speaker, text in ss.chat:
        if speaker == "error":
            st.error(text)
        elif speaker == "notice":
            st.warning(text)
        else:
            with st.chat_message("user" if speaker == "customer" else "assistant"):
                st.write(text)
    if ss.retry is not None:
        st.button("Retry in a new conversation" if ss.retry.get("new_conversation") else "Retry", key="retry_button",
                  on_click=retry)
    if ss.pending is not None:
        run_pending()
        st.rerun()  # redraw the chat, tabs and sidebar spend with the turn's results
    st.chat_input("Type as the customer", key="chat_box", on_submit=queue_typed)

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
