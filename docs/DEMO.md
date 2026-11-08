# Demo runbook

Frozen at tag `demo-v1`. Everything below runs in replay mode: no network, no API cost, identical every time.

## Before the call (once)

```
source .venv/bin/activate
pytest -q                        # expect: all passed
python -m scenarios.demo_run     # clicks the whole path twice from reset; expect: all checks passed in both runs
streamlit run app.py             # opens http://localhost:8501
```

Starting the app resets the demo data to Mon 2 Nov 18:00. Keep `LLM_MODE` on **replay** in the sidebar.

If the page ever looks wrong: click **Reset demo**, then continue from the step you were on (every jump replays the timeline from scratch).

## The 90-second click order

| # | Time | Click | Say | Point at |
|---|---|---|---|---|
| 1 | 0:00 | **Tue 20:15** | "Monday she asked for a 9 PM callback. Tuesday evening she moved it to 9 AM." | Case memory: U3 v1 `superseded`, v2 `active`, `reschedule`. The old promise is kept, not overwritten. |
| 2 | 0:15 | **Wed 09:16** | "Nobody called. The hub notices on its own." | U3 v3 `missed`. Events tab: occurred_at and recorded_at side by side. |
| 3 | 0:25 | **Wed 10:00**, then **Send: Wed 10:00 question** | "A different agent, a different business line, next morning." | The reply: apology for the miss, live amounts, simple English. |
| 4 | 0:40 | **What the agent sees** tab | "This is everything the agent was given. The preference changed the wording; the amounts were fetched live, never stored." | Reply instructions box; `1 other open case: Payments`. |
| 5 | 0:50 | **Send: Wed 10:00 accept callback** | "The model chose the time; code resolved it, checked it and read it back." | Read-back line. |
| 6 | 0:55 | **Send: Wed 10:05 injection attempt** | "A prompt can be subverted. A permission cannot be talked out of." | Reply has no UPI details; access-log rows for PAY-2042 are `existence_only`. |
| 7 | 1:05 | **Send: Wed 10:08 interest waiver** | "It may not waive interest, so it hands over with the context." | Tool `escalate_to_human`; Events tab: `escalated_to_human`. |
| 8 | 1:15 | **Try to edit history** | "The agents' own database role cannot edit history. PostgreSQL refuses, not the app." | `InsufficientPrivilege: permission denied for table log`. |
| 9 | 1:25 | Optional: agent **payments_agent**, **Preview packet**; then **credit_agent**, **Preview packet** | "Same customer, two roles: payments sees PAY-2042 in full, credit sees that it exists." | Access-log decisions `allow` vs `existence_only`. |

Live change if asked (Level 3): set **MISSED_THRESHOLD_MINUTES** to 30, click **Wed 09:16**: the callback is no longer missed.

## Rules that keep replay working

- Send the scripted turns only after the **Wed 10:00** jump, in order. Only the next one is enabled.
- Do not type free text in replay mode: there is no recorded answer, so the app shows a clear `ReplayCacheMiss` error instead of a reply. Typing needs `LLM_MODE` live and costs money.
- Preview packet makes no model call and never affects replay.

## Evidence (committed)

- `results/demo_run.txt`: the full path, two runs from reset, identical.
- `results/s7_1_superseded.png`, `s7_2_missed.png`, `s7_3_packets.png`, `s7_4_edit_denied.png`: regenerate with `pip install -r requirements-dev.txt` then `python -m scripts.screenshots` (uses the installed Google Chrome).
- `results/walkthrough.txt`: the scripted, no-LLM timeline with its checks.
