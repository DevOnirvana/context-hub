# Context Hub

Remember the promise, not the conversation.

A customer-memory service for a bank's AI servicing agents (Payments and Credit Card Servicing).
Any authorised agent can continue a customer's story across conversations and business lines,
while code, not the model, decides what is remembered and who may see it. Synthetic data only.

## Run

```
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
createdb contexthub && psql contexthub -f sql/schema.sql -f sql/grants.sql -f sql/seed.sql   # copy .env.example to .env first
streamlit run app.py
```

The default mode is `auto`: recorded answers replay for free, anything new calls the API once and is saved. Tests always run offline: `pytest -q`. The click order for a 90-second demo is in [docs/DEMO.md](docs/DEMO.md).

## Architecture

```
customer -> agent loop (Claude tool use, step cap 6)        agents/loop.py, agents/tools.py
              | the model proposes; every tool is plain Python
              v
           hub/interface.py   the only door: get_context, propose_memory, accept_commitment, ...
              |-- admission.py   three tests decide what becomes memory (no live facts, no sensitive data)
              |-- policy.py      who sees what: allow, existence_only or deny, logged per read
              |-- lifecycle.py   commitments: active, superseded, missed, attempted, fulfilled
              v
           PostgreSQL         versioned rows (never edited in place), append-only events, access log
systems/   mock dispute, statement, UPI, dialler: live facts are fetched at read time, never stored
```

## Where to look

- [hub/interface.py](hub/interface.py): the six calls agents can make, each re-checking authentication and purpose.
- [hub/admission.py](hub/admission.py): what may become memory, as three plain tests with fixed reasons.
- [hub/policy.py](hub/policy.py): which role sees which case: full, existence only, or nothing.
- [hub/lifecycle.py](hub/lifecycle.py): every change is a new version; the sweep marks missed callbacks.
- [agents/loop.py](agents/loop.py): the bounded agent: the model chooses tools, code runs them and hands over on refusal.

## How it was built

The design was written first ([docs/SPEC.md](docs/SPEC.md)) and frozen; decisions made later are logged in its section 17. Each build step ([docs/BUILD_PLAN.md](docs/BUILD_PLAN.md)) started in plan mode in Claude Code, was approved before any code was written, and finished only when its acceptance tests passed and the output was reviewed. Database roles enforce the rules too: the app's role cannot edit history, and the UI's role can only read.

## Results: type-aware memory vs latest-value-wins

Scenarios 1 to 12 from [docs/SCENARIOS.md](docs/SCENARIOS.md), deterministic probes, no LLM. Run: `python -m scenarios.run_harness` (own database). Full table: [results/t2.csv](results/t2.csv).

| # | Probe | Hub | Latest-value-wins |
|---|---|---|---|
| 1 | When is the callback due? | Pass | Pass |
| 2 | Was the callback kept? | Pass | Fail: no rule marks a miss |
| 3 | Did the bank miss the callback? | Pass | Pass |
| 4 | Was the callback kept (late dialler record)? | Pass | Pass |
| 5 | What amount is disputed? | Pass | Pass |
| 6 | What did the bank originally promise? | Pass | Fail: overwritten |
| 7 | Did we refuse anything? | Pass | Fail: overwritten |
| 8 | Is anything outstanding from the customer? | Pass | Pass |
| 9 | What may the credit agent see? | Pass | Fail: UPI details leak |
| 10 | What is the dispute status? | Pass | Pass |
| 11 | How should we communicate? | Pass | Pass |
| 12 | Is health information stored? | Pass | Fail: stored |

Hub 12/12, latest-value-wins (LangGraph store put semantics) 7/12. Both were fed the same steps, system records and live lookups. The same author wrote the hub and the scenarios, so this shows where simpler designs fail; it does not prove general superiority.

## Limitations

- Synthetic data, one customer, two business lines; no case closure.
- A changed explanation-style preference is stored correctly, but the reply instructions still assume simple English ([results/free_chat_r2.md](results/free_chat_r2.md), prompt l).
- Fail-closed sensitive-data check: a merchant named "Apollo Hospital Pharmacy" is rejected as health data.
- Summary-based memory (T1) was not built; its results are predictions only.
- The replay cache holds synthetic conversation text for demo determinism; a production system would keep no such cache.
- Production pieces named, not built: per-customer encryption keys, a consent registry, a policy engine, a network service in front of the hub.
