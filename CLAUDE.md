# Context Hub prototype

## What this is
A local, low-fidelity but production-shaped prototype of a customer-memory "Context Hub" for a bank's AI servicing agents (Payments and Credit Card Servicing). Built by Baan (Anirban) as a working prototype. It is demoed live on a call, and Baan must be able to explain and modify every line on request.

Before any design-related work, read `docs/SPEC.md`. Test scenarios: `docs/SCENARIOS.md`. Build steps and acceptance checks: `docs/BUILD_PLAN.md`.

## Non-negotiables
- IMPORTANT: The design in docs/SPEC.md is frozen. Do not change domain rules, memory types, lifecycle states, policy semantics or the schema without asking Baan first. Put new ideas in `LATER.md`.
- The model decides the next step; the hub decides what is remembered and who sees it. The LLM is used only to (a) propose candidate memories as structured output, (b) choose tools inside the agent loop, (c) write replies. Admission tests, time resolution, commitment acceptance, lifecycle and access policy are plain Python.
- Agents never touch the database. They call `hub/interface.py` only.
- Live facts (dispute status, statement, balances, UPI status) are fetched from `systems/` mocks at read time and are never stored as memory.
- All time comes from `hub/clock.py` (simulated clock). Never call `datetime.now()` in domain code. All datetimes are timezone-aware, Asia/Kolkata.
- No vector database. Retrieval is by customer, case and memory type.
- Simplicity over cleverness: small files, raw SQL with psycopg 3 (no ORM), no agent framework, no metaclasses. Comment the reason behind every rule.
- Synthetic data only.
- Any user-facing text, docs or slide copy: no em dashes.

## Stack
Python 3.12, PostgreSQL 16 (Homebrew, native, no Docker), psycopg 3, pydantic v2, Streamlit, anthropic SDK, pytest. Optional: langsmith (tracing and evaluation). No LangGraph: the T2 latest-value-wins baseline is a plain PostgreSQL upsert (SPEC 17.38).

## Commands
- Setup: `python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Database: `createdb contexthub && psql contexthub -f sql/schema.sql -f sql/grants.sql -f sql/seed.sql`
- Reset demo data: `python -m scripts.reset_db`
- Tests: `pytest -q`
- Scripted walkthrough (no LLM): `python -m scenarios.walkthrough`
- Experiment harness: `python -m scenarios.run_harness`
- App: `streamlit run app.py`

## Environment (.env, never committed)
`DATABASE_URL_ADMIN`, `DATABASE_URL_APP` (role hub_app), `ANTHROPIC_API_KEY`, `MODEL_ID` (agent replies and tool choice, default `claude-sonnet-5-5`), `EXTRACTION_MODEL_ID` (memory extraction and SUM-baseline grading, default `claude-haiku-5-5`), `LLM_MODE` (`replay` | `record` | `live`, default `replay`), `MISSED_THRESHOLD_MINUTES` (default 15), `LANGSMITH_TRACING` (default false), `LANGSMITH_API_KEY`.

## API cost discipline (budget: $10 of credits)
- Enable prompt caching (`cache_control`) on every agent system prompt and on tool definitions.
- Use EXTRACTION_MODEL_ID for extraction and grading; MODEL_ID only where reply quality matters.
- Default to LLM_MODE=replay; use record only when a prompt or flow has changed.

## Workflow
- Work one step of docs/BUILD_PLAN.md at a time. Start each step in plan mode and wait for Baan's approval.
- Finish every step by running its acceptance check and showing the output. Never claim done without evidence.
- Commit after each passing step with message `step N: <what>`.
- When compacting, preserve: current build step, files modified, failing checks, open questions.

## Gotchas
- The app must connect as `hub_app`, never as the table owner (owners bypass grants). hub_app has no UPDATE or DELETE on `events.log`, and may UPDATE only the `valid_to` column of `case_memory.units` and `customer_context.items`.
- Every change to a memory unit (time change, status change) is a new version row; the previous row gets `valid_to` set. Never edit a version in place.
- Relative times ("tomorrow at 9") are resolved in code against the simulated clock at the moment they are said, then stored as exact timestamps.
