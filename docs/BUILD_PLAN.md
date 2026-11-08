# Build plan

One step per Claude Code session where possible. Start each step in plan mode. A step is done only when its acceptance check runs and the output is shown. Commit after each passing step.

Target layout:
```
context-hub/
  sql/          schema.sql, grants.sql, seed.sql
  hub/          interface.py, admission.py, acceptance.py, lifecycle.py, policy.py, clock.py, store.py, models.py
  systems/      dispute.py, statement.py, documents.py, dialler.py, upi.py, fixtures/*.json
  agents/       llm.py, loop.py, tools.py, prompts/payments.md, prompts/credit.md
  backends/     base.py, structured.py, summary.py, lww.py
  scenarios/    walkthrough.py, run_harness.py, cases/*.py
  scripts/      reset_db.py
  tests/
  app.py
  LATER.md
```

## Step 0: Environment
Homebrew PostgreSQL 16 running; venv; requirements.txt; .env.example; git repo; empty pytest passes.
Acceptance: `psql contexthub -c 'select 1'` and `pytest -q` both succeed.

## Step 1: Schema, grants, seed
Write sql/schema.sql, sql/grants.sql (hub_app and hub_archiver roles), sql/seed.sql (customer, cases, policy rules). scripts/reset_db.py drops and reloads.
Acceptance (pytest, connecting as hub_app):
- INSERT into events.log succeeds; UPDATE and DELETE on events.log raise InsufficientPrivilege.
- UPDATE of case_memory.units.status raises; UPDATE of valid_to succeeds.
- hub_app SELECT on archive.conversations raises.

## Step 2: Core domain without any LLM
clock.py (simulated clock, set/advance, listeners), store.py (versioned insert with expected-version check, append event), admission.py (three tests, scope), acceptance.py (servicing hours 08:00 to 21:00 IST, 7-day limit, next valid slot), lifecycle.py (transitions and sweep), time resolution helper.
Acceptance (pytest):
- Reschedule creates v2 Active and closes v1 as Superseded; event appended.
- Sweep at Wed 09:16 marks Missed; with MISSED_THRESHOLD_MINUTES=30 it does not.
- attempted_no_answer gives Attempted; later completed gives Fulfilled.
- Late completed at 09:40 for a 09:04 call flips Missed to Fulfilled with status_corrected event.
- "tomorrow at 9 PM" said Mon 18:30 resolves to Tue 3 Nov 21:00 IST.
- 3 AM request is declined with next slot 08:00 offered; request_declined event appended.
- Concurrent write with stale expected version fails.
- Hospital statement is rejected by test 3.

## Step 3: Policy and interface
policy.py and interface.py: propose_memory, accept_commitment, decline_request, record_event, get_context with authentication gate, existence_only, access log.
Acceptance (pytest):
- Unauthenticated get_context raises.
- credit_agent context includes CASE-CARD-001 units and "1 other open case: Payments", and nothing from PAY-2042; access_log row exists with decision existence_only.
- payments_agent sees both cases.

## Step 4: Systems mocks and scripted walkthrough
systems/ mocks driven by the clock and fixtures. scenarios/walkthrough.py replays SPEC section 13 with scripted actions (no LLM) and prints, after each step, the four stores plus what the next agent would see.
Acceptance: printed state matches SPEC section 13 at every step, including Missed at Wed 09:15 and live dispute status provisional_credit_issued at Wed 10:00.

## Step 5: LLM layer
agents/llm.py with record / replay / live, pydantic extraction schema, optional LangSmith tracing.
Acceptance: extraction test passes in replay mode with no network; a cache miss in replay mode fails loudly.

## Step 6: Agents with bounded agency
agents/loop.py (tool-use loop, step cap 6), tools.py (toolsets per role), prompts. request_callback goes through time resolution, acceptance policy, then accept_commitment or decline_request, then read-back.
Acceptance (replay mode):
- Wed 10:00 credit conversation: reply acknowledges the missed callback, uses simple English, uses live dispute status, mentions the other case only by existence.
- Injection attempt: no UPI-7781 details in any tool result or reply; access log shows existence_only.
- Interest waiver: escalate_to_human called; no commitment created.

## Step 7: Streamlit UI
Left: chat with agent selector (payments / credit) and authenticated toggle. Right: tabs for Case memory (current plus versions), Events, Customer context, Access log, What the agent sees (context packet). Sidebar: simulated clock with jump buttons (Mon 18:30, Tue 16:00, Tue 19:40, Tue 20:15, Wed 09:16, Wed 10:00), +15 min, settings (MISSED_THRESHOLD_MINUTES, LLM_MODE), "Reset demo", "Try to edit history" (attempts UPDATE on events.log as hub_app and shows the PostgreSQL error).
Acceptance: the full SPEC section 14 demo path is clickable end to end in replay mode.

## Step 8: Baselines and experiment harness
Narrowed on 8 Nov (SPEC 17.38): backends/structured.py, backends/lww.py (plain PostgreSQL upsert with LangGraph store put semantics), scenarios/run_harness.py running scenarios 1 to 12 against S and LWW on its own database.
Acceptance: a 12 x 2 pass/fail table is printed and saved to results/t2.csv, every cell that differs from the prediction marked and explained. SUM (T1) not built.

## Step 9: Demo hardening
Record the full demo path into the cache (LLM_MODE=record once, then replay). README with run-of-show. Adversarial review of the diff by a subagent against SPEC.md, flagging only correctness gaps. Three clean end-to-end runs.
Acceptance: three consecutive clean runs in replay mode with networking off.
