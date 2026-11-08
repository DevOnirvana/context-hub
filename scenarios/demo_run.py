"""Demo freeze check (Step 9): click the SPEC 14 demo path through the real app.py, headless.

Usage: python -m scenarios.demo_run

Runs the whole path twice from a fresh reset, in replay mode (no network, no cost), on the
demo database from .env. The output has no wall-clock times, so the two runs must be identical
line for line. Writes results/demo_run.txt; exits non-zero if a check fails or the runs differ.
"""
import sys
from pathlib import Path

from dotenv import load_dotenv
from streamlit.testing.v1 import AppTest

from scenarios.timeline import CUSTOMER
from ui import queries

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "demo_run.txt"
FORBIDDEN = ["PAY-2042", "UPI-7781", "Ramesh", "4,200"]


def run() -> tuple[list[str], list[str]]:
    lines, failures = [], []
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()  # first load: resets the demo data to Mon 02 Nov 18:00

    def click(key):
        at.button(key=key).click().run()
        if at.exception:
            raise RuntimeError(f"{key}: {at.exception}")

    def check(text, ok):
        lines.append(f"    {'ok  ' if ok else 'FAIL'} {text}")
        if not ok:
            failures.append(text)

    def clock():
        return at.session_state["hub"].clock.now().strftime("%a %d %b %H:%M IST")

    def callbacks():
        rows = queries.case_memory(at.session_state["ui_conn"], CUSTOMER, at.session_state["hub"].clock.now())
        return [r for r in rows if r["action"] == "callback"]

    def show_callbacks():
        for r in callbacks():
            lines.append(f"    {r['unit']} v{r['v']}  due {r['due_at']:<16} {r['status']:<11} {r['change_reason']:<10} "
                         f"{'current' if r['current'] else ''}")

    lines.append("1. Moment 1: reschedule 9 PM to 9 AM, history kept")
    for key in ("jump_Mon 18:30", "jump_Tue 16:00", "jump_Tue 19:40", "jump_Tue 20:15"):
        click(key)
    lines.append(f"  clock {clock()}")
    show_callbacks()
    cb = callbacks()
    check("9 PM version shown as superseded, 9 AM version current with change_reason reschedule",
          [(r["status"], r["change_reason"]) for r in cb] == [("superseded", "created"), ("active", "reschedule")])

    lines.append("2. Moment 2: clock to Wed 09:16, no dialler record")
    click("jump_Wed 09:16")
    lines.append(f"  clock {clock()}")
    show_callbacks()
    check("current callback version is missed", [r["status"] for r in callbacks() if r["current"]] == ["missed"])

    lines.append("3. Moments 3, 4, 7, 8: credit agent, four scripted turns (replay)")
    click("jump_Wed 10:00")
    for n in range(4):
        click(f"turn_{n}")
        view = at.session_state["view"]
        customer, agent = at.session_state["chat"][-2], at.session_state["chat"][-1]
        lines.append(f"  [{clock()}] {view['source']}")
        lines.append(f"    customer: {customer[1]}")
        lines.append(f"    tools:    {[(c.name, 'refused' if c.is_error else 'ok') for c in view['calls']]}")
        lines.append(f"    {agent[0]}:    " + agent[1].replace("\n\n", " ").replace("\n", " "))
        pay = sorted({r["decision"] for r in view["access"] if r["case"] == "PAY-2042"})
        lines.append(f"    access-log rows this turn: {len(view['access'])}; PAY-2042 decisions {pay}")
        check(f"turn {n + 1} replied without error and PAY-2042 is existence_only",
              agent[0] == "agent" and pay == ["existence_only"])
    replies = " ".join(t for s, t in at.session_state["chat"] if s == "agent")
    check("no UPI details in any reply", not [s for s in FORBIDDEN if s in replies])
    check("last turn escalated to a human", [c.name for c in at.session_state["view"]["calls"]] == ["escalate_to_human"])

    lines.append("4. Moment 5: preview packets, no model call")
    for role in ("payments_agent", "credit_agent"):
        at.selectbox(key="role").set_value(role).run()
        click("preview")
        packet = at.session_state["view"]["packet"]
        pay = sorted({r["decision"] for r in at.session_state["view"]["access"] if r["case"] == "PAY-2042"})
        lines.append(f"  {role}: cases {[c.case_id for c in packet.cases]}; other cases: "
                     f"{packet.other_cases_summary or 'none'}; PAY-2042 access {pay}")
    check("credit sees PAY-2042 as existence only", pay == ["existence_only"])

    lines.append("5. Moment 6: try to edit history (as hub_app)")
    click("edit_history")
    lines.append(f"  {at.session_state['edit_result']}")
    check("PostgreSQL refused the UPDATE", "permission denied" in at.session_state["edit_result"])

    lines.append("6. Moment 9: MISSED_THRESHOLD_MINUTES 15 -> 30, jump to Wed 09:16 again")
    at.number_input(key="threshold").set_value(30).run()
    click("jump_Wed 09:16")
    show_callbacks()
    check("callback is not missed with threshold 30", [r["status"] for r in callbacks() if r["current"]] == ["active"])

    for name in ("app_conn", "ui_conn", "archiver_conn"):
        at.session_state[name].close()
    return lines, failures


def main() -> None:
    load_dotenv()
    first, failures = run()
    second, failures_2 = run()
    identical = first == second
    out = ["Context Hub demo run: SPEC 14 path through app.py (AppTest, headless), LLM_MODE=replay, demo database.",
           "Run 1:", *first, "",
           f"Run 2: {len(second)} lines, identical to run 1: {'yes' if identical else 'NO'}",
           f"RESULT: {'all checks passed in both runs' if not (failures or failures_2) and identical else 'FAILED'}"]
    if not identical:
        out += ["", "Run 2:", *second]
    OUT.write_text("\n".join(out) + "\n")
    print("\n".join(out))
    sys.exit(0 if identical and not failures and not failures_2 else 1)


if __name__ == "__main__":
    main()
