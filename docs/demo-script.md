# Demo video script (3:00)

Screen: terminal for `make demo`, then the dashboard (`make web-dev`). Say the "read this first" line out loud; do not hide it.

## 1. The problem (0:00 to 0:20)

*On screen: README title, then a spreadsheet-style list of invoices in three currencies.*

"A small exporter earns in dollars, pays suppliers in euros and holds a third currency. Today that runs on spreadsheets and
whatever rate is live on the due date. Big firms have treasury teams. Small ones cannot, and they cannot hand the job to an
autonomous AI either, because nobody trusts an agent with money unless its authority is provably limited. Tessera is a
working slice of that idea: the LLM proposes, rules dispose."

## 2. Live flow (0:20 to 1:50)

*On screen: `make demo`, with the dashboard open beside it. Everything runs on synthetic data and a mock of Airwallex.*

1. (0:20) "This is synthetic data against a mock of Airwallex. The sandbox client exists but has not run against the real sandbox." Run `make demo`.
2. (0:35) The demo seeds a business and ingests a USD invoice. Point at the forecast: an interval, not one number.
3. (0:50) The worker runs and the planner proposes a conversion. Point at the verdict: **ESCALATE**, with the rule that objected and the ruleset hash.
4. (1:05) Switch to the dashboard, Proposals. Show the plain-language explanation and the simulated worst case. Tap **Approve**.
5. (1:20) Back in the terminal: "Approval only marks the proposal. The worker re-runs policy with fresh context before it sends." Show the fresh check pass, the idempotency key stored, then the call to the mock vendor.
6. (1:35) The signed webhook arrives and settles the execution. Run the audit verify: chain OK. Show one record with its inputs, proposal, decision and outcome.

## 3. Safety proof and numbers (1:50 to 2:40)

*On screen: docs/redteam-report.md table, then the README results block.*

- (1:50) "We ran 156 attacks: injected invoice text, swapped beneficiaries, split payments, forged webhooks, race conditions, kill-switch bypass. 0 harmful actions executed. Block rate 91.7%, escalation 5.8%."
- (2:05) "The first full run let 7 through: payments split under the per-action limit. We fixed it at the ledger and added regression tests. That is in the report."
- (2:15) "On 44 benign cases, the false-block rate is 0.0% and false-escalation 5.0%."
- (2:25) "We also report what did not work. FX saving is not demonstrated: 0.50 with an interval that includes zero. Forecast intervals under-cover at the shorter horizons. The agent-versus-treasurer labels were drafted by Claude, not by a treasurer."
- (2:35) Show `make lint`: four import contracts keep the planner from importing the Airwallex package.

## 4. What is next (2:40 to 3:00)

"Next: run the planner against a real model, run everything against the real Airwallex sandbox, wire the worker to real
balances, add per-business authentication, and get a real treasurer to label the scenarios. Bounded, auditable autonomy is
the problem. Tessera is a start. The repo is on GitHub, and the threat model lists what is still open."

## Notes for recording

- Time each section against the budget: 20s, 90s, 50s, 20s.
- Every number above is in `docs/redteam-report.md` or `docs/eval-report.md`; run `make check-readme` after changing the README.
