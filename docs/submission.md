# Tessera: submission

**Tessera is a treasury agent for cross-border SMEs whose authority is provably limited: an LLM proposes, a deterministic policy engine disposes.**

Small exporters, agencies and freelancers earn in one currency, pay in another and hold a third. They run this on spreadsheets and lose margin to FX swings, idle balances and late payments. They cannot hand the job to an autonomous system, because nobody trusts an agent with money unless its authority is bounded and auditable.

**What it does.** Tessera forecasts cash flow per currency with conformal intervals, proposes convert, hold, pay, top-up and flag actions, and checks every proposal against 13 machine-readable rules. The verdict is exactly one of APPROVE, ESCALATE or BLOCK. Approved actions execute through an Airwallex port, and the rest go to a person for one-tap approval. Every step is written to a hash-chained audit log.

**Why it is safe.**
- The planner returns schema-valid proposals that name records by ID. It has no import path to execution, enforced by four import-linter contracts in CI.
- Invoice text and emails are untrusted data: sanitised, delimited and never part of instructions.
- The worker re-runs policy at execution time, so approval is not execution. Idempotency keys are stored before every call.
- Wide forecast intervals or large simulated losses escalate to a person. Autonomy starts advise-only and is earned.

**Results, from the generated reports.**
- 156 attacks, 0 harmful actions executed, block rate 91.7%. The first run let 7 split payments through; we fixed the ledger and added regression tests.
- The forecast beats seasonal-naive at every horizon (WAPE 0.413 against 0.556 at 4 weeks).
- Interval coverage missed its 80% target at the shorter horizons.
- FX saving is not demonstrated: 0.50 [-0.02, 1.35] EUR per 10,000 units converted.

**Honest limits.** Everything runs on synthetic data against a mock of Airwallex. The sandbox client has never run against the real sandbox, and the planner has never run against a real model. The treasurer labels were drafted by Claude. The threat model lists the open risks.

**Built with** Python, FastAPI, LightGBM, PostgreSQL, SQLAlchemy and a Next.js dashboard. `make setup`, `make db-test-up`, `make demo` runs the whole loop.

Repository: https://github.com/BuildbyArindam/Tessera
