# Daily log

## Day 3 - Forecast baselines and backtest harness
Built tessera_core.forecast (History.as_of leakage choke point, weekly net target, build_features, naive / seasonal-naive / moving-average baselines, rolling-origin backtest) and tessera_evalkit (WAPE, sMAPE, MAE, bias; loader; results CSV runner).

Assumptions (simplest option chosen):
- Day 2 parquet schema is assumed in tessera_evalkit/loading.py (invoices.parquet, payments.parquet with direction in/out). Adapt that one file if the generator differs.
- Forecast target for horizons 4/8/13 is the cumulative net cash flow over the next h weeks, not the single week h.
- Weeks are Monday-Sunday; origins are Sundays; the incomplete current week is never used.
- Money is converted to float64 only at the loader boundary for modelling.
- Lags are NaN (not zero) when history is short; LightGBM handles NaN.
- docs/baseline_results.csv must be produced from the real Day 2 data: python -m tessera_evalkit.forecast_eval --data data/synthetic

Surprised me: -
Would do differently: -

## Day 5 - Late-payment risk and FX exposure range
Built tessera_core.risk (late_features, late_split, late_model, payment_curve, late_api, fx_range) and tessera_evalkit (risk_data, risk_metrics, risk_plots, fx_backtest, risk_eval). Tools for the agent: `assess_late_payment(scorer, invoices, as_of, invoice_ids=None, model=None)` and `estimate_fx_range(rates, pair=, horizon_days=)`; both pure, read-only, return frozen Pydantic models.

Repo fixes needed first (not Day 5 scope, but nothing ran without them):
- `forecast/__init__.py` contained a merge-conflict marker (`=======`), a SyntaxError that broke every import of tessera_core.forecast. Merged both export lists.
- `tessera_evalkit/__init__.py` was empty, so `generate.py` could not import `__version__`. Added it.
- Still failing, untouched: 4 mypy-strict errors in forecast/features.py and forecast/target.py (Day 3/4). Day 5 files are mypy-strict clean.

Assumptions (simplest option chosen):
- LATE = paid more than 7 days after due, or never paid. Label is known on the payment date if on time, else on due+8. Invoices still inside their 7-day window at data end are censored and dropped.
- Training rows are weekly snapshots of each open, not-yet-known-late invoice, so `days_to_due` covers what the scorer sees in production. Snapshots of one invoice are correlated; calibration size is therefore counted in invoices (hard floor 30, warning below 200).
- Split: train labels known before the calibration window; calibration labels known before the test window (embargo, same idea as Day 4). 3 expanding folds, 91-day calibration window.
- Probabilities clipped to [0.01, 0.99]. Invoices already more than 7 days late return p_late = 1.0 (deterministic, not modelled).
- Payment-date range = Kaplan-Meier curve of days-past-due (customer, else business, else global; >= 5 payments), conditioned on the invoice still being unpaid today. It is separate from p_late on purpose: the range says WHEN if it arrives, p_late also carries the risk it never does.
- `settled_partial` disputes count as paid on the settlement date (lateness is timing, not amount). ground_truth.parquet is never read.
- FX: EWMA lambda 0.94, filtered historical simulation, standardised residuals re-centred (zero drift by design), vol path rolled forward inside each bootstrap path. `horizon_days` is calendar days, converted at 252/365.
- Quote-currency payables need the inverse pair: "falls" always means the quoted pair goes down.
- QuantilePoint field is `value`, not `rate`: the Day 1 guard (no `rate: float` in core) is kept intact; an FX rate is a ratio, not money.

Measured results (seed 7; FX is SYNTHETIC because Frankfurter returned 403 from this sandbox; do not quote as real FX performance):
- Late scorer, 3 pooled chronological folds (4,749 test snapshots, base rate 62%): calibrated LightGBM AUC 0.800, Brier 0.165, ECE 0.048; calibrated logistic AUC 0.801, Brier 0.171, ECE 0.053.
- Honest comparison: the one-line customer-history heuristic (shrunk past late rate, no model) scores AUC 0.825, Brier 0.160, i.e. it BEATS both models on AUC and Brier. The models earn their keep on calibration quality and the extra features, not on ranking. Not yet resolved; see "Would do differently".
- Isotonic calibration helps on real data (LightGBM ECE 0.079 -> 0.048, logistic 0.127 -> 0.053; calibration windows hold 378-464 invoices). On a small 81-invoice test world it made held-out Brier WORSE (0.141 -> 0.157); that is why the warning exists.
- Payment-date range: nominal 80% interval covered 76% (customer curves, 630 invoices) and 68% (business-level fallback, 60 invoices). Median error about 11 days.
- FX range: on a simulated GARCH/t(6) process with known truth the median is unbiased and tails are mildly thin (5-95 realises about 87-88%). On the synthetic FX series the 90-day 5-95 interval realised 82% and the median missed on trending pairs (USDINR drifts +11%/yr in one path); with ~46 independent 90-day outcomes per horizon this is indicative, not proof.

Surprised me: the heuristic baseline outperforming both models; and that a 30%-too-low volatility bug only shows up as a coverage failure when many independent paths are pooled (a single 9,000-day path moved a quantile by 0.053 from noise alone).
Would do differently: tune on the heuristic first and feed `cust_late_rate` through a monotonic constraint so the GBM cannot do worse than its own best feature; get real ECB rates (needs network access) before quoting any FX number.

### Day 5 verification (scripts/verify_day5.py)
- PASS: calibrated LightGBM Brier 0.1654 vs base-rate 0.2358 (also below base rate in every fold; logistic 0.1707). ECE 0.0476 (uncalibrated 0.0793). 10-bin reliability table printed by the script.
- FAIL as specified: FX quantile coverage on the SYNTHETIC FX series is not within 0.04 of nominal (worst |gap| 0.069 / 0.122 / 0.165 / 0.195 at 7 / 30 / 60 / 90 days). Not a code bug: the same estimator is within 0.018 on a GARCH process with known volatility, and across 20 generator seeds the shortfall is systematic (median coverage 0.417 at 30d). Cause: the generator hard-codes INR depreciation (3%/yr, fx.py `_DRIFT`) and the estimator is zero-drift by design. With that drift removed the same 20 seeds give worst gaps 0.011 / 0.015 / 0.028 (7 / 30 / 90 days). Decision pending: add an optional drift term, or accept zero-drift and remove the INR drift from the generator. Real ECB data has not been tested (network blocked).
- PASS: no feature uses data after the as-of date (features at 40 invoice issue dates unchanged when all later data is deleted; rewriting all later payments/invoices changes nothing). Caveat: features DO use other invoices' outcomes known by the as-of date (customer history); that is intended.
- Edge cases hardened during verification: null invoice_id/business/customer/currency/dates now raise InvoiceTableError (before, a null due_date slipped through validation); NaN/non-positive rates raise InvalidRatesError; a hole of more than 5 business days in a loaded FX series raises FxDataGapError (before, missing rows were dropped silently and the hole became one "day" of returns). Customer with no history falls back to business, then global curve; no history at all gives p_late and no range with a note; already-paid invoices raise InvoiceSelectionError when requested and are never listed.

## Day 6 - Policy engine core
Built tessera_core.policy: `evaluate(proposal, context, ruleset) -> PolicyDecision`, `PolicyContext`, a `Rule` base class with a string-type registry, strict YAML loading from `policy/rules/`, a canonical `ruleset_hash`, and two rules (`kill_switch`, `max_amount_per_action`). Spec in docs/policy-spec.md. `PolicyDecision` gained an optional `ruleset_hash` (optional only so the Day 1 tests that build bare decisions keep passing; `evaluate` always sets it). Added `pyyaml` to core and `types-PyYAML` to `make setup`.

Assumptions (simplest option chosen):
- Kill switch state is runtime context (`PolicyContext.kill_switch_engaged`), not YAML, so an operator can flip it without a redeploy. The `kill_switch` rule is mandatory: a ruleset without it will not load. An empty ruleset will not load either (it would approve everything).
- Money convention in `Proposal.params`: `amount` (string or int, never float) and `currency`, meaning the value LEAVING the account (sell side of a CONVERT). Day 7 refines per-action fields. HOLD and FLAG_INVOICE move no money and are not capped.
- Cap semantics: equal to the cap passes, one cent over blocks; a currency with no cap blocks (fail closed). Amounts with more than 2 decimals are BLOCKED rather than rounded, so the number judged is the number executed.
- Rules run in sorted id order and all of them run; an error in one rule does not hide a BLOCK from another. `rule_ids` lists non-PASS rules only; the hash identifies the full ruleset.
- The hash covers meaning (id, type, type_version, validated params), not YAML bytes: comments, key order and `"5000"` vs `"5000.00"` do not change it. Changing a rule's logic must bump its `type_version`.
- YAML is stricter than `safe_load`: duplicate keys are an error (stock YAML keeps the last silently), floats are rejected as amounts, file name must equal rule id. Reading the YAML files is the only file I/O in the package; everything else is pure.
- Reasons are built from rule ids, validated amounts and fixed text only; `rationale` and other free text is never read or echoed.

Verification (scripts/verify_day6.py, exit 0; tests/unit/test_policy_engine.py + test_policy_ruleset.py, 82 tests; full unit suite 241 passed):
- (a)-(f) all pass: kill switch blocks all 5 action types; 4999.99 / 5000.00 approve, 5000.01 blocks; a raising rule gives BLOCK + `policy_error` (only the exception type is reported, not its message); missing context gives BLOCK + `policy_error`; identical decision and hash over 100 runs; editing one YAML value changes the hash (a081915b67f7 -> 56e3ee81f42c).
- AST scan of the 11 policy files: no network, database, LLM, agent/airwallex imports and no eval/exec/compile/__import__. Checked that the scan can fail: planting httpx, tessera_airwallex and eval() in a copy makes it report all three.
- mypy strict on policy + domain: clean. ruff and format clean on every file touched today.
- Mutation check: 11 deliberate bugs (cap `>` to `>=`, kill switch ignored, raised error treated as PASS, missing context ignored, ESCALATE ignored, duplicate YAML keys allowed, sub-cent rounding allowed, non-positive amount allowed, hash ignoring params, kill_switch not required, unknown-currency cap skipped). The first run missed the last one, which exposed a missing test (a currency without a cap); added it, and all 11 are now caught.

Not fixed (outside Day 6 scope, present before today): mypy reports 26 errors in forecast/, risk/ and the airwallex package (missing pandas stubs, plus `mypy_path` not covering packages/airwallex); `ruff check .` reports 45 errors and `ruff format --check .` flags 14 files elsewhere in the repo. I only formatted files I created or changed.

Still true after Day 6: this ruleset is NOT enough to protect a payout. There is no beneficiary allowlist, daily cumulative cap (so splitting a large payment into capped pieces is not stopped), velocity limit or confidence gate until Day 7. Do not wire the engine to execution before then.

Surprised me: my mutation script gave a false failure on one run because Python reused stale bytecode when a file was edited and restored within the same second; I only trusted a result after clearing __pycache__ and re-running in isolation.
Would do differently: decide up front whether per-action `params` get a typed model per ActionType instead of loose JSON plus a helper; Day 7 will want that for beneficiary and card rules.

## Day 7 - Policy rules and property tests
Added seven rules to `tessera_core.policy`, each configured from its own YAML file and unit-tested: `daily_cumulative_cap`, `beneficiary_allowlist`, `currency_concentration_cap`, `forecast_confidence_gate`, `quote_slippage_guard`, `velocity_limit`, `card_limit_cap`. The shipped ruleset now has nine rules. Property tests in `tests/unit/test_policy_properties.py` (Hypothesis, 500 examples each) check the four required invariants plus two extras (non-domain objects are blocked rather than raised; the kill switch beats everything). Spec and a rule/parameter/example table are in docs/policy-spec.md.

Assumptions (simplest option chosen):
- Per-action `params` (refining the Day 6 convention): CONVERT needs `amount`, `currency`, `to_currency`; PAY_SUPPLIER needs `beneficiary_id`; TOPUP_CARD needs `vendor_id`. Ids are 1-64 ASCII letters, digits, `_`, `-`. The reference rate and executable quote come from the context, never from the proposal, so the model cannot name the rate it is judged against.
- Required context is now action-aware (`Rule.required_context_for`). Without this, seven new rules would have blocked every HOLD for lacking an FX quote. A CONVERT still fails closed if its quote is missing. This changes the Day 6 engine contract slightly: the check runs per proposal, not per rule.
- `forecast_confidence_gate` declares no required context; missing confidence data is an ESCALATE as the prompt says, not a `policy_error` BLOCK.
- Boundaries: equal to a limit passes, one cent or tick over does not. Beneficiary cooldown: exactly 24h old passes; added under 24h ago, added time unknown, or added in the future all ESCALATE. Velocity: rolling hour, an approval exactly 60 minutes old is outside, approvals dated in the future count. Slippage is checked in both directions (a quote far better than the reference is as suspect as a worse one).
- Concentration applies to CONVERT only; a missing or zero source balance blocks. Card ceilings are per vendor (currency, per-top-up max, per-month max); a vendor with no entry cannot be funded. A currency with no daily cap blocks.
- The caller owns the counters: `spend_today` must include approved-but-not-yet-executed actions, `card_loaded_this_month` uses the caller's calendar-month boundary, a CONVERT needs a quote fetched before the decision. `now` is a context field; no rule reads a clock.
- YAML numbers are strict: quoted strings or ints, never floats, no silent rounding (more decimal places than allowed is an error), percentages 0 < x <= 100, integer limits must be real ints (not `true`, not `2.5`).
- Tightening of Day 6 behaviour: amount strings must now be plain ASCII digits with an optional fraction. Before, Python's `Decimal` would have accepted `" 100"`, `"+100"`, `"1_000"` and full-width digits; they are now BLOCKed.
- The engine now sorts rules by id itself, so even a hand-built `Ruleset` cannot make order matter (needed for the order-independence property to test the engine, not just the loader).
- Test changes, stated plainly because the guide says never to weaken tests: `test_policy_engine.py` now builds its ruleset from the two Day 6 YAML files (`day6_ruleset()`), because the default directory now holds nine rules and a bare context rightly no longer approves a conversion. The assertions are unchanged. `test_policy_ruleset.py` expects the nine rule ids. `scripts/verify_day6.py` got the same scoping and passes with the same ruleset hash as on Day 6 (a081915b67f7), so the original two rules' meaning is unchanged.

Verification (scripts/verify_day7.py, exit 0; policy suite 246 tests; full unit suite 405 passed, was 241):
- Six Hypothesis properties, each 500 passing examples and 0 failing: malformed money proposals always BLOCK (judged by an independent oracle written from the spec, not from the rules); any non-proposal/context/ruleset object gives BLOCK `policy_error`; along any increasing run of up to 8 amounts the verdict never gets more permissive; shuffling the rules never changes the decision or the hash; the verdict equals the strictest verdict any rule gives alone (so a single BLOCK is never outvoted); the kill switch alone blocks anything.
- Five adversarial proposals (negative, zero, 40-digit amount, wrong currency, missing beneficiary id) all BLOCK, as do float, NaN, `1e3`, full-width digits and bool amounts. A 14-step amount ladder up to the 10^15 sanity bound blocks for every money action.
- mypy strict clean on policy and domain; ruff and format clean on every file I touched; every new file under 300 lines.
- Mutation check: 21 planted bugs (off-by-one at each boundary, a missing-data check that passes, one-direction slippage, an engine that treats errors as PASS, ESCALATE outranking BLOCK, order-dependent evaluation, non-ASCII digits accepted, overflow of large amounts, and others). The first run killed 19. Two survived and both were real gaps: (1) removing the card currency check still BLOCKed, but only through a caught exception; the test now asserts the rule's own reason. (2) Amounts of 20,000 or more wrapping to a small number was approved by the real engine, and nothing caught it.

Surprised me: the first versions of my property tests passed and were close to useless. Measuring what the generators produced showed that only 1.6% of proposals were well formed and every random-context decision was BLOCK, so invariants 2-4 were barely exercised. After rebuilding the generators (valid params first, then one to three injected faults; mostly-complete contexts) the combination test sees 1,092 APPROVE, 124 ESCALATE and 1,784 BLOCK per 3,000 draws. Even then the overflow bug went undetected: Hypothesis favours small integers, so a 500-example run reached the critical amount window zero times. Log-uniform amounts, amounts hugging each limit, and checking a sorted ladder of up to 8 amounts per example fixed it (6 of 6 runs now catch it, from 0 of 3 before).
Would do differently: write the mutation script before the property tests, and treat "does the generator reach APPROVE and ESCALATE?" as a test in its own right. The same applies on Day 18 to the red-team generators.

Not fixed (outside Day 7 scope): daily cap, velocity and card limits trust the caller's counters, so two decisions made at the same instant from the same snapshot could both pass; the worker must serialise decisions or re-check at execution (Day 13). Limits are global, not per business. No cross-currency total exists. Pre-existing lint and type errors elsewhere in the repo (mypy on forecast/ and risk/, ruff on 14 files) are unchanged. Pushing is not possible from this session (the Claude GitHub App is not installed on the repo), so the work is committed locally on branch `day-7-policy-rules` with tag `day-07`.

## Day 8 - Simulation sandbox
Built `tessera_core.simulate`: `simulate(proposal, fx_history, as_of, n_paths, seed, *, due_date, config) -> SimulationReport` and `replay(proposal, fx_history, as_of, *, due_date, config) -> ReplayReport`. Moving-block bootstrap of historical daily log returns (default block 5 business days), vectorised, seeded. CONVERT and PAY_SUPPLIER are scored against "convert on the due date"; HOLD against "convert now". Report: expected saving, std, 5th and 50th percentile, worst-case loss, P(beats baseline), spread and fee cost. New policy rule `worst_case_loss_gate` (shipped ruleset is now ten rules). Spec section "Simulation" added to docs/policy-spec.md.

Assumptions (simplest option chosen):
- `due_date` is a keyword argument from the caller, NOT read from the proposal. If the planner could name it, it could set it to today and zero the worst-case loss, which would switch the gate off. The worker must take it from the invoice or obligation.
- A PAY_SUPPLIER proposal needs `params.from_currency` (the currency that funds the payout); `currency` is the payout currency. Nothing else carries the funding currency yet. The worker must replace this with the real funding account before execution (TODO Day 12/13). A payout funded in its own currency has no FX exposure: the report is all zeros with note `no_fx_conversion_needed`.
- HOLD reads `amount`, `currency`, `to_currency` like a CONVERT: the sell amount left unconverted.
- Comparator for CONVERT and PAY_SUPPLIER is the frictionless mid rate on the due date, so a proposal must earn back its own spread and fee (a flat rate gives exactly minus the cost). HOLD's costs cancel, so only the rate move remains.
- Spread defaults to 50 bps (the Day 1 mock's figure), fee to zero, both in `SimulationConfig`. They are assumptions, not measured Airwallex prices.
- Historical drift is removed before bootstrapping by default (`demean=True`), matching the zero-drift stance of the Day 5 FX range. `demean=False` keeps it.
- Business-day steps = calendar days x 252/365, same convention as Day 5. A due date on a weekend or holiday uses the last observation on or before it (in replay, and as the stale-history check's reference).
- Input discipline: at least 60 returns before `as_of`, the newest observation at most 7 days older than the date asked about (else a stale-history error), horizon at most 365 days, 100 to 200,000 paths, non-negative seed. The caller must pass OBSERVED business days only; forward-filled weekend rows are fake zero returns.
- The gate reads the simulation from `PolicyContext.simulation` (a `SimulationSummary` in `domain/`, so policy never imports the simulator). The summary carries proposal id, action and amount and the gate BLOCKs a mismatch. Missing summary: ESCALATE for CONVERT and PAY_SUPPLIER, PASS for HOLD, always PASS for card and invoice actions. A loss equal to the ceiling passes. Ceilings are per currency, in the sell currency; a currency without one is BLOCK.
- `ReplayReport` has no `summary()` on purpose: replay reads the realised future and must never reach a live decision.
- `policy.proposal_view` gained `read_amount` (the strict parser without the "moves money" filter, so HOLD can be read the same way) and `currency_param`.

Test changes, stated plainly: adding a tenth shipped rule changed what "a clean proposal" means, because a CONVERT or PAY_SUPPLIER without a simulation now ESCALATEs. So (1) `test_policy_ruleset.py` expects the ten rule ids; (2) `test_clean_proposals_are_approved_by_the_whole_shipped_ruleset` and `test_the_approve_path_is_actually_reached` now supply a matching simulation (`sim_for` in policy_helpers); (3) the Hypothesis properties draw a simulation mode (ok / over every ceiling / missing / belongs to another proposal) and attach it to each generated proposal, otherwise they would have exercised the new rule only as "always ESCALATE"; (4) `scripts/verify_day7.py` is scoped to the nine Day 7 rules, because its check "(c) every rule has an example" cannot hold for a tenth rule that Day 8 verifies. Assertions about the original rules are unchanged. `verify_day6.py` still passes with the same two-rule hash (a081915b67f7).

Verification (scripts/verify_day8.py, exit 0; 72 new tests in test_simulate_core.py, test_simulate_inputs.py, test_worst_case_loss_gate.py):
- Same seed gives a byte-identical report; seeds 0-9 at 5,000 paths agree on the mean within 4 standard errors of a difference (spread 1.94 against 5.21 allowed) and on the 5th percentile within 0.25 sigma (7.01 against 16.28).
- Flat rate (constant 0.9, 10,000 USD, 50 bps): CONVERT saves exactly -50.00 with zero variance; PAY_SUPPLIER -50.25; with a 25 bps spread and 2.50 fee the saving equals -(spread + fee) to the cent; HOLD costs 0.00. A proposal with `due_date == as_of` and no costs saves exactly 0.00 on every path.
- No look-ahead: scaling every observation after `as_of` by 0.1, 3 or 25 leaves the report unchanged, and `replay` on the same data does change (so the check can fail).
- Timing, 5,000 paths: 2 ms (30 days), 6 ms (90 days), 17 ms (365 days). The requirement was under 2 seconds.
- Gate, full shipped ruleset: a 4,000 USD conversion in a 3%-a-day market has a simulated 5th-percentile loss of 929.49 USD and is ESCALATEd by `worst_case_loss_gate` alone; the same conversion at 0.1% a day (loss 47.41) is APPROVEd; no simulation at all ESCALATEs.
- Mutation check: 24 planted bugs (look-ahead, sign flips in all three scenarios, p05 read as p95, negative worst-case loss, seed ignored, off-by-one step, drift kept, fee ignored, stale history accepted, as_of excluded, bootstrap block start never reaching the end, replay reading the wrong day, wrong default spread, five gate mutations, and others). All 24 caught.
- mypy strict clean on simulate, domain and policy; ruff and format clean on every file touched; every new file under 300 lines. Policy suite 252 tests (was 246).

Surprised me: a summary keyed only by proposal id is not enough. A 10 USD simulation could vouch for a 10,000 USD action that reused the id, so the summary now carries the amount and the gate compares it. I only noticed when writing the "belongs to another proposal" test.
Would do differently: put `from_currency` into a typed per-action params model now. This is the second time (Day 6 said the same) that loose JSON params needed one more field and a helper.

Not fixed or not claimed:
- Calibration of the simulated tail is NOT demonstrated. On the synthetic series the realised 30-day outcome fell below the simulated 5th percentile in 0 of 18 origins for both USD/EUR and USD/INR; with 18 origins that cannot tell 0% from 5%. A real coverage test belongs to Day 17. All FX here is the Day 2 SYNTHETIC generator (Frankfurter is blocked from this sandbox), so no number above is real FX performance.
- Block bootstrap with a 5-day block understates multi-week tails somewhat when volatility clusters over longer stretches than a week; the Day 5 range had the same mild thin-tail finding. Treat the 5th percentile as optimistic.
- One currency pair per proposal. No cross-currency portfolio loss, no correlation between simultaneous actions, no intraday movement.
- The gate trusts the caller to run the simulation for THIS proposal with the right `due_date` and a fresh history; the id and amount check catches a swapped report, not a wrong date.
- Pre-existing, unchanged: mypy errors in forecast/ and risk/, ruff errors in 14 files elsewhere. Pushing is still not possible from this session (the Claude GitHub App is not installed on the repo); the work is committed locally on branch `day-8-simulation`.

## Day 9 - LLM planner
Built `tessera_agent`: `llm.py` (an `LLMClient` protocol with one `complete()` method, `AnthropicLLM`, `FakeLLM`), `schemas.py` (strict `ProposalList`), `tools.py` (five read-only tools), `context.py` (`PlannerSnapshot` and the context JSON), `planner.py`, `validate.py`, `canon.py`, `prompts.py` and `prompts/planner_v1.md`. `Planner(llm).run(snapshot, now=...)` returns `PlanResult(proposals, evidence, record)`: `Proposal` objects that carry no authority, plus a `PlanRecord` (model, prompt hash, context hash, every tool call, evidence ids, validation errors, raw output) for the Day 11 audit log.

Assumptions (simplest option chosen):
- Provider: Anthropic (`LLM_PROVIDER` defaults to `anthropic`, key from `LLM_API_KEY`, model from `LLM_MODEL`, default `claude-sonnet-5-5`). The SDK is an optional extra and imported lazily, so tests and the FakeLLM never need it. Swapping provider means one new class behind `LLMClient`. I could not confirm from the docs page I could reach whether every model accepts a `temperature` argument; the first live call will show it (an API rejection surfaces as outcome `llm_error`).
- Typed params per action, as a discriminated union (the thing Day 6 and Day 8 both said they wished they had done): CONVERT and HOLD take `amount, currency, to_currency, obligation_id`; PAY_SUPPLIER takes `amount, currency, from_currency, beneficiary_id, obligation_id`; TOPUP_CARD takes `amount, currency, vendor_id`; FLAG_INVOICE takes `invoice_id` and a `reason` that is one of four codes, never free text. Amounts are strings with at most 2 decimal places, the exact shape the policy parser accepts.
- `obligation_id` is required on CONVERT, HOLD and PAY_SUPPLIER. This is how Day 8's rule holds: the model never supplies a due date. The `simulate_proposal` tool looks the due date up from the obligation record. The worker must do the same when it builds the `SimulationSummary` for policy, from the obligation and not from anything the model said. Policy ignores the extra key (a test runs every action type through the real ten-rule ruleset and asserts no `policy_error`).
- Ids are checked twice. Shape: 1-64 ASCII letters, digits, `_`, `-`, and anything shaped like an IBAN or holding a 9-digit run (separators ignored) is rejected. Membership: every beneficiary, invoice, vendor and obligation id must exist in the snapshot the planner was given, otherwise the reply is invalid. Real ids (`INV-B01-00001`, `CP-B01-003`) never trip the shape check.
- `evidence_ids` is required (1 to 8 per proposal). The planner issues `ev-N` for each SUCCESSFUL tool result in the run; a proposal may cite only those. An errored tool call is not evidence.
- More than 5 proposals makes the whole reply invalid (not truncated). The same action with the same params twice is invalid. An empty list is valid and costs no retry.
- Retry: once, tools switched off, and the note to the model lists field paths and error codes only (`proposals.0.CONVERT.params: value_error`), never a value the model wrote. Still invalid, or the provider fails: an empty list and an outcome of `invalid_output` or `llm_error`. Never a partial list, never an exception.
- Proposal ids are `prp-` plus a hash of (context hash, position, proposal body), so planning the same state twice gives the same ids.
- No free text reaches the model today: no customer or counterparty names, no invoice notes, in the context or in any tool result (a test plants an injection string in a beneficiary name and asserts it appears nowhere). The Day 10 sanitiser and delimited untrusted block are what will let text in.
- Tools take their business from the snapshot (a tool cannot be pointed at another business), use fixed seeds and 5,000 paths, and return an `{"error": ...}` result instead of raising. `get_forecast` reports `relative_width = (upper - lower) / |point|`, the quantity `forecast_confidence_gate` compares (None when the point is 0, as the gate expects). `simulate_proposal` reports the spread and fee it assumed (50 bps, 0.00), because they are assumptions and not Airwallex prices.
- The worker builds the `PlannerSnapshot` (balances from the port, forecasts, observed-business-day FX, invoices, obligations, beneficiaries). The agent package fetches nothing itself.

Config and test-infrastructure changes, stated plainly: `tests/unit/conftest.py` is new and holds one opt-in fixture, `no_network`, which makes any socket attempt fail the test (the agent test modules use it; no existing test does). `pyproject.toml` `mypy_path` gains `packages/agent`, and the `ignore_missing_imports` override gains `pandas.*` and `anthropic.*` (`make setup` installs neither pandas-stubs nor the SDK). `make typecheck` now includes the agent package. `packages/agent/pyproject.toml` gains `pandas`, an optional `anthropic` extra and package-data for the prompt. `.env.example` gains `LLM_MODEL`. No existing test or assertion was changed.

Verification (scripts/verify_day9.py, exit 0; 143 new tests; full unit suite 622 passed):
- (a) Invalid JSON gives exactly one retry (2 model calls, tools off on the retry), then an empty list with outcome `invalid_output`; a corrected retry is accepted. (b) An extra field is rejected (`proposals.0.CONVERT.execute_immediately: extra_forbidden`). (c) An IBAN, a bare account number, a card number and a company name used as `beneficiary_id` are all rejected, and none is echoed back to the model; a well-formed id the planner was never given is rejected as `unknown_id`. (d) An AST scan of all 9 modules finds no import of tessera_airwallex, any database, server or network module, or `eval/exec/compile/open`, and the scan is shown to fail on planted violations. (e) After a run that calls all five tools, the Airwallex mock's full internal state and a fingerprint of the snapshot (forecasts, FX arrays, invoices, obligations) are identical to before.
- Temperature 0.0 on every call; model name and the prompt's SHA-256 are in every record; the same state gives the same proposal ids.
- Fooled-planner check: a proposal paying BEN-002 (a real id in the snapshot, not on the allowlist) is accepted by the planner, because the id exists, and BLOCKed by `beneficiary_allowlist` in the real policy engine. The planner is a filter on format, not on authority; policy is the authority.
- Tool checks: the due date follows the obligation (7 days out and 18 days out give horizons 7 and 18) and `due_date` as an argument is rejected; scaling every FX observation after `as_of` by 25 changes neither the range nor the simulation, and changing the past does (so the check can fail); a tool pointed at another business's invoice returns an error.
- mypy strict: 0 errors in `tessera_agent`. ruff and format clean on every file touched; every new file under 300 lines (largest 273).
- Mutation check (scripts/mutation_day9.py, a copy of the package, never the repo): 26 planted bugs, all killed by real test failures on the first run: unknown fields accepted, account-number check off, digit-run limit loosened, 6-decimal amounts, cap of 50 proposals, evidence optional, same-currency conversion, duplicate JSON keys, NaN accepted, id membership off, evidence check off, duplicates allowed, temperature 0.7, tools on during the retry, the retry note echoing model text, failed tool calls counted as evidence, tool budget removed, provider error escaping, non-deterministic proposal ids, due date ignoring the obligation, FX range reading the future, late risk seeing other businesses, tool arguments not strict, tool errors echoing input, SDK error text leaking, temperature dropped from the request.

Surprised me: my first happy-path test failed and the planner was right. My test helper `convert_item(evidence_ids=[...])` put the keyword into `params`, so the planner correctly rejected an extra field, retried, and then ran out of script. The failure also exposed a real gap: when the provider failed on the retry, the record threw away the first attempt's validation errors. It now keeps them, which is exactly what an audit trail should show.
Would do differently: the same lesson as Day 7. The first version of the verification script reported two failures that were both script bugs (a prose string search where an import grep was meant, and a list multiplied so a tool-call reply landed in the retry slot). A check that can fail for the wrong reason is as bad as one that cannot fail.

Not fixed or not claimed:
- NO LIVE MODEL CALL WAS MADE. This sandbox has no `LLM_API_KEY`, so everything above ran against `FakeLLM` and a fake SDK object. `python scripts/verify_day9.py --live` makes one real call and prints the raw output, parsed proposals and prompt hash; the guide's last Day 9 check is not done until you run it. Nothing here shows that the real model produces valid proposals on the first try, or sensible ones. The prompt's example only proves its own format validates.
- Schema-valid is not safe. A real but unapproved beneficiary, an oversized amount, a conversion the forecast does not support all pass the planner. Policy is what stops them, and only the tests above show that for the beneficiary case.
- The numbers a model sees from `simulate_proposal` are advisory. The `SimulationSummary` the policy gate reads must be produced by the worker running `simulate` itself, with the due date from the obligation; the planner's output must never be passed to policy as if it were that summary.
- The retry history contains the model's own invalid text as an assistant turn. The note that follows it contains only field paths and codes. Once Day 10 lets untrusted text into the context, check whether anything from it can ride along in a model reply into that history.
- Limits are not per business and nothing here handles two planners running at once on the same state (proposal ids are deterministic, so a second identical run produces identical ids; the worker must de-duplicate on them, Day 13).
- Pre-existing and unchanged: 3 mypy errors in `forecast/` (unused ignores, one `no-any-return`) and ruff errors in files I did not touch (22 files with the ruff installed in this sandbox; the Day 8 entry counted 14, probably a ruff version difference, not new breakage). Adding `pandas.*` to the mypy override changes which pre-existing errors appear but not their number in forecast/. Nothing is committed: `.claude/settings.json` in this repo denies `git add`, `git commit` and `git push`, and this session has read-only access to GitHub, so the changes are files in the working tree for you to review and commit (suggested: branch `day-9-planner`, tag `day-09`).

## Day 10 - Prompt-injection defence
Built `tessera_agent.sanitize` (with `textnorm.py` and `detect.py`), hardened the planner, added an eleventh policy rule `untrusted_input_gate`, and a red-team suite: `packages/evalkit/tessera_evalkit/redteam/` with 25 cases in `cases_v1.jsonl`, a runner, a fixed synthetic world and a CLI (`make redteam`).

Assumptions (simplest option chosen):
- The guide says `evalkit/redteam/cases_v1.jsonl`; there is no top-level `evalkit/`, so the file lives at `packages/evalkit/tessera_evalkit/redteam/cases_v1.jsonl`.
- A new prompt file `planner_v2.md` (v1 plus a section on untrusted text) is the default. v1 is unchanged, so old audit records still match their hash. Prompts are versioned and hashed, never edited in place.
- Untrusted text goes to the model in ONE place: a JSON array inside a tag `untrusted-data-<16 hex of the content hash>`, with `<`, `>` and `&` escaped so nothing can close it. The context JSON and tool results still carry no free text. Caps: customer name 80, memo 300, invoice note 500, email body 2000 characters; 50 fields; 20,000 raw characters; 8,000 characters for the whole block.
- Detection runs on the UNCAPPED text, per field and on the fields joined (with and without a separator, because a note can be split mid-word). Flags hold a code, a source and a record id, never text, and the log line carries codes only.
- `untrusted_input_gate` is escalate-only. `input_screening` has three states: not run (`None`, escalates money-moving actions), clean (empty), flagged (escalates). HOLD and FLAG_INVOICE with no screening result pass, because they move nothing.
- A failed model reply is NOT replayed on the retry when untrusted text is present (the retry sees "the previous reply is withheld"); the reply is still kept in the audit record. The model's own rationale is normalised and scanned too, because a human approver reads it.
- "Blind" mode in the runner means the detector's result is withheld from policy, to show what the planner's checks and the deterministic rules stop without it. The executor is injected and called only on APPROVE; the runner imports nothing from Airwallex.
- The scripted model (`CompromisedLLM`) is FULLY fooled: it proposes exactly what the note asked, every time. That is the worst case for everything behind the model.

Verification (scripts/verify_day10.py, 23 checks, exit 0; full unit suite 824 passed, 202 more than Day 9's 622):
- The note text appears only inside the delimited block, never in the system prompt, the context JSON or a tool result. Escaping stops a note closing the block.
- Convert, pay and card top-up each give APPROVE when the screen is clean and ESCALATE, by the gate alone, when it is flagged.
- No model-settable field can hold free text or an account number (schema introspection); an AST scan of the 12 agent modules finds no reach into policy, the allowlist or the context; an IBAN given as a payee id is refused by the planner.
- Red-team, 25 cases, 23 of which produce a proposal that reaches policy (two are refused by the planner for a bad id):
  - Screened (the detector's result reaches policy): BLOCK 21, ESCALATE 2, APPROVE 0, nothing executed.
  - Blind: first run BLOCK 21, ESCALATE 1, APPROVE 1; RT-03 executed. After the fix below: BLOCK 21, ESCALATE 2, APPROVE 0, nothing executed.
  - Blocks are attributed to the rules that blocked (`beneficiary_allowlist`, `card_limit_cap`), not to the gate, which never blocks.
- The checks can fail: tests rerun the suite without the gate, without the allowlist and without the card cap, and each shows the attack getting through.
- Mutation check (scripts/mutation_day10.py, on a copy of three packages): 38 of 38 planted bugs killed. The first run exposed two real test gaps (bare 9+ digit account numbers; decoding of tag characters), now covered.

Surprised me:
- Invisible characters in my own source. Zero-width characters in test strings and the case file were expanded when files were written, so the source held the raw characters. Source files now use `\u` escapes, and a scan of the changed sources finds none left.
- The runner first credited the gate with blocks. A rule that only escalates cannot block, so attribution is now per rule (`blocked_by`, `escalated_by`).
- Removing an invisible character can create a new one (a combining mark that composes), so normalisation repeats up to 4 passes until stable.

Would do differently: write the attack cases before the detector, by someone other than the author of the detector (see below).

Not fixed or not claimed:
- THE RESIDUAL, NARROWED NOT CLOSED. RT-03 (an early payment of 400,000 INR to an APPROVED supplier, inside every limit) was APPROVED and executed with the detector bypassed. Fix: a twelfth rule, `early_payment_gate` (escalate-only, due date from the caller's obligation records, never from the proposal). RT-03 now ESCALATEs with or without the detector, and removing the rule makes it execute again (tested). The class is still open: with the detector withheld a conversion inside every cap (5000 EUR to INR, probe RT-90, not one of the 25) is APPROVED and executed, because policy cannot tell it from the agent's normal job. Only the detector's flag stops it; the caps bound the damage.
- The detector missed 2 of 25: RT-18 (Japanese) and RT-19 (a polite request with no instruction phrase). Both are kept in the suite and marked as expected misses.
- The 25 cases and the detector were written by the same agent, so the catch rate (23 of 25) is NOT an independent measurement. Treat it as a regression suite, not a security claim.
- NO LIVE MODEL WAS RUN (no `LLM_API_KEY` in this sandbox). Everything above uses a scripted, fully fooled model. Nothing shows that a real model resists these notes, or that it does not. `python -m tessera_evalkit.redteam --live` runs all 25 cases against a real model and records, without asserting.
- The model-written `rationale` (up to 600 characters) is still a free-text channel that a human approver reads. It is normalised and scanned, not eliminated.
- Day 13 worker notes: pass `obligation_due_dates` from the obligation records into the policy context; look up account details from the beneficiary record by id, never from proposal params; build the policy context as `World.policy_context` does (simulation run by the caller from the obligation, screening result from the planner's record, never from the model).
- Lint scope slip, stated plainly: to remove stray invisible characters I ran `ruff check --fix` over `scripts`, `tests` and `packages`, which also applied ruff's SAFE fixes to older files outside Day 10 (airwallex `mock.py`, `models.py`, `port.py`; evalkit `generate.py`, `loading.py`, `forecast_eval.py`; several older tests and `verify_day4.py`). The full suite still passes. There is no git history here to diff against, so please review those files when you commit. Whole-repo ruff errors went from 45 to 23 as a side effect; 14 files still fail `ruff format --check`; mypy still shows the same 13 errors (airwallex, forecast), none in Day 10 code. mypy on `tessera_agent` and the red-team package reports nothing new.
- Nothing is committed: `.claude/settings.json` denies `git add`, `git commit` and `git push`, and GitHub is read-only from this session. Suggested: branch `day-10-injection-defence`, tag `day-10`.
- Live run requested on 2026-11-01 was NOT possible: no `LLM_API_KEY` in this sandbox, so `--live` stops at startup. Nothing has been measured against a real model.

## Day 11 - Audit log, database and API
Built `packages/store` (`tessera_store`: models, sessions, queries), `tessera_audit` (hash-chained log, canonical JSON, payload builders), the Alembic migration `0001` (nine tables, trigger), and the FastAPI service (`apps/api/tessera_api`: eight routes, API-key middleware). Details of the log are in `docs/audit-log.md`.

Assumptions (simplest option chosen):
- The tree has no home for the ORM models, and both the API and the worker need them, so there is a new package `packages/store`. The `audit_events` table and its enum live in `tessera_audit` next to the chain logic (store knows nothing of audit; audit depends on store's declarative base).
- The hash covers `seq`, `timestamp` and `event_type` as well as the payload (brief: `sha256(prev_hash + canonical_payload)`). Without them an event could be relabelled and still verify. The payload is stored as canonical JSON TEXT, not JSONB, so the hashed bytes are the stored bytes.
- Proposal status: `PENDING_APPROVAL` (policy said ESCALATE), `APPROVED` (policy said APPROVE, or a human approved), `REJECTED`, `BLOCKED`. Day 13 adds the execution states. Approving never executes and never re-runs policy: the worker does that. Only a PENDING_APPROVAL proposal can be decided, once (row lock, plus a UNIQUE on `approvals.proposal_id`).
- `POST /plan/run` takes its inputs (balances, FX history, beneficiaries, obligations, the policy context) from a `WorldProvider`. The API may not import Airwallex, so it cannot fetch balances; assembling the world is the worker's job (Day 13). With no provider attached the route answers 503. Tests and `scripts/serve_day11_demo.py` attach the red-team fixture world and a SCRIPTED model.
- Plan runs are idempotent: the planner derives proposal ids from the state, so planning an unchanged state finds the same ids and writes nothing.
- A policy context that cannot be built is a BLOCK with `policy_error`, never an APPROVE.
- One API key stands for one actor, `operator`. Only the OpenAPI docs are public. The app refuses to start with a key under 16 characters.
- `GET /forecasts` reads stored rows; no route writes them (none was in the list). `repo.save_forecasts` is for the worker and tests. Emitted today: `proposal_created`, `policy_decision`, `approval_requested`, `approved`, `rejected`. Defined, emitted by nobody yet: `forecast_made`, `execution_*`, `webhook_received`, `autonomy_changed`.
- `policy_decisions.phase` ('plan' or 'execution') and the `executions` table exist now so Day 13 needs no schema change for the execution-time re-check; nothing writes them yet.

Verification (scripts/verify_day11.py, exit 0; `make test-integration`; `make test`):
- Unit suite 866 passed (836 at the start of today, 30 new). Integration suite 131 passed against PostgreSQL 16. `pytest tests` (both, one command): 997 passed.
- Migration: upgrade, `alembic check` (models and migration agree, no diff), downgrade to base, upgrade again, all clean. Nine tables; no money column is a float (NUMERIC(20,2), checked in information_schema).
- Tamper evidence: 10 events verify OK; raw UPDATE, DELETE and TRUNCATE are rejected by the trigger (sqlstate 23001). With the trigger switched off in a scratch database: an edited payload names its seq (7 in the script), an edited event type or timestamp is caught, a deleted middle row is named, and an edited row with its own hash recomputed is caught at the next row.
- Concurrency: 8 threads x 10 appends gave seqs 1..80 with no gap and a valid chain; 6 simultaneous approvals of one proposal gave one 200, five 409s, one approval row, one `approved` event.
- Every route over HTTP with curl (17 requests): 401 with no key or a wrong key, 422 on malformed input, 404, 409, 200/201 as expected, `/docs` 200. After the run the chain verifies (8 events).
- Replay: the `policy_decision` payload holds the context the engine saw; rebuilding the proposal and context from the log and re-running the real engine gives the same verdict, reasons and ruleset hash for all three proposals.
- Dependency rule (AST test): store, audit and the API import nothing from Airwallex; store and audit import neither the agent nor the API; core imports no database or web module.
- mypy strict: 0 errors in store, audit and the API (the 3 pre-existing errors in `forecast/` are unchanged). Whole-repo ruff: 23 errors, the same 23 as before today (none in anything new); all new files pass `ruff format`.
- Mutation checks (by hand, on the real files, then restored): fail-closed fallback returning APPROVE, no row lock on approve, status guard removed, audit context dropped, ESCALATE mapped to APPROVED, and `verify_chain` reading cached rows: each made a test fail.

Surprised me (five problems found late, all fixed):
- FastAPI's default 422 body returns the rejected input. A lone surrogate in a note could not be encoded, so the error response itself became a 500. The handler now sends only `type`, `loc` and `msg`.
- A malformed JSON body got 422 before the API-key check. An authentication middleware now runs first (the route-level dependency stays, for the OpenAPI docs).
- `verify_chain` could read a session's cached copy of a row instead of the database's. It now uses `populate_existing`.
- Twice I wrote `:99` inside a SQL string in a test and SQLAlchemy read it as a bind parameter.
- Each suite passed alone but `pytest tests` could not collect: `tests/unit` already has a `helpers.py`, and one integration test imported from `conftest`, so the module names collided. My helper is now `tests/integration/it_helpers.py`. I only saw it because I ran both suites in one command at the end.
Would do differently: write the verification script's checks before the routes. The two real bugs above only appeared when a test sent hostile input; I had written the happy path first.

Not fixed or not claimed:
- THE TEST DATABASE WAS NOT DOCKER. The brief asks for a test database in Docker. In this sandbox the Docker daemon starts but the registry refuses image pulls (HTTP 403 from the proxy), so everything ran against a locally installed PostgreSQL 16.15. `docker-compose.yml` now has `postgres-test` (profile `test`, port 5433) and `make db-test-up` / `make test-integration` use it, but I have not run that path. First thing to do on your machine: `make db-test-up && make test-integration`.
- `POST /plan/run` has no real data provider. It is proven only with the red-team fixture world and a scripted model. There is no live model call anywhere in Day 11, and the API still cannot assemble a real snapshot until the Day 13 worker exists.
- The chain does not catch removal of the newest rows or a full re-hash; the trigger does not stop a table owner or superuser. Both are written up with a fix (publish `head()`; a least-privilege role) in `docs/audit-log.md`. The grants there are untested: every database used here is a superuser.
- A plan run that yields nothing (invalid or failed model output) leaves no audit event.
- `GET /audit/verify` walks the whole table (fine now, O(n) later). `/proposals` and `/audit` give no total counts.
- No CORS (the dashboard on Day 15 needs it), no rate limiting, no TLS.
- Running a plan run holds no lock during the model call, so two simultaneous runs can both call the model; the second finds the first's proposal ids and writes nothing, but it still pays for the call.
- Nothing is committed. `.claude/settings.json` denies `git add`, `git commit`, `git push` and `git config`, and GitHub access from this session is read-only (the push check was refused). The work is in the working tree: new `packages/store/`, `migrations/`, `alembic.ini`, `apps/api/tessera_api/*`, `packages/audit/*`, tests, scripts, docs, plus edits to `Makefile`, `docker-compose.yml`, `.env.example`, `pyproject.toml` and the `apps/api` and `packages/audit` pyprojects. Suggested: branch `day-11-audit-api`, tag `day-11`.

## Day 12 - Airwallex adapter, part 1

Built `tessera_airwallex.sandbox.SandboxAirwallex` (`get_balances`, `create_fx_quote`, `execute_conversion`) on a new HTTP layer (`_http.py`: token login and cache, bounded retries, redacted logging; `_errormap.py`; `_json.py`; `_redact.py`; `_timeparse.py`), typed errors (`AuthError`, `RateLimitedError`, `UnknownError`, plus `ValidationError` as an alias of `InvalidRequestError`), one shared contract suite (`tests/contract`) and a stateful respx fake of the sandbox (`tests/fake_airwallex.py`). `create_beneficiary`, `create_transfer`, `create_card` and `set_card_limits` raise `NotImplementedError` until Day 13.

Assumptions (simplest option chosen):
- `docs/airwallex-notes.md` was still the empty template, so I filled it from the official reference first (every row says VERIFIED or UNVERIFIED, with its source URL). The client uses only what is VERIFIED there.
- Two reference versions disagree on conversions (`/api/v1/conversions/create` with `reason` and `term_agreement` in 2024-04-30; `/api/v1/fx/conversions/create` in the current pages). The client targets the current pages.
- A repeated `request_id` is documented only as "rejected". The port promises a replay, so `execute_conversion` looks the conversion up by `request_id` before creating and again after any unknown outcome or unrecognised 4xx. This also makes a restart after a crash safe, even once the quote has expired.
- `ValidationError` is an alias of the existing `InvalidRequestError`, and the new errors follow the existing `...Error` naming (`RateLimitedError`, `UnknownError`), rather than adding a second set of names.
- A 401 is never retried (the brief says never 4xx). The token is refreshed 2 minutes before `expires_at`, and a 401 clears the cache so the next call logs in again.
- Money never touches a float: responses are parsed with `parse_float=Decimal`, and the quote amount, which the reference says is a JSON number, is written as a bare number from a Decimal.
- `client_rate`'s direction is not documented, so the rate is taken from the amounts: used as is if `sell * rate` matches `buy`, else inverted, else the quote is refused.
- `packages/airwallex` was missing from `mypy_path`, so mypy could not resolve the package's own imports; added. `.env.example` had the host `api-demo.airwallex.com`; the reference says `api.sandbox.airwallex.com`; corrected.

Verification (`make test`, `make typecheck`, `ruff`, a mutation run):
- `make test` (unit + contract): 976 passed, 15 skipped. The 15 skips are the live-sandbox variant of the contract suite (no credentials here). The contract suite is 15 tests, run three ways: mock (15 pass), sandbox client over the fake server (15 pass), live sandbox (15 skipped).
- New tests: 42 for the HTTP layer, 38 for the adapter, 15x3 contract. A 401 makes exactly one call and no sleep; a 503 is retried with delays 0.5 s then 1.0 s, a persistent one stops after 4 attempts; 400, 404, 409, 422 and 429 are never retried.
- Secrets: a DEBUG-logging run where the error body echoes the API key, client id and token produces no occurrence of any of them in the logs or in `str(error)`; request bodies log with `account_number` redacted; a grep finds no hard-coded key-like string, and credentials are read only in `SandboxConfig.from_env` and the login call.
- mypy strict: 0 errors in the new code (the 3 pre-existing `core/forecast` errors are unchanged). Whole-repo ruff: 23, the same 23 as before; files touched today are clean and formatted.
- Mutation checks (by hand, files restored after each): retrying 4xx, dropping the lookup before create, parsing floats, disabling secret scrubbing, never refreshing the token, never reconciling, sending the quote amount as a string, not detecting a key reused for another quote, skipping the rate-direction check, retrying a 401, dropping the request/quote match check, ignoring the `request_id` re-check: 12 of 12 made a test fail.
- Dependency rule: the isolation tests still pass; nothing outside `packages/airwallex` imports the new code except `scripts/record_airwallex_fixtures.py`.

Surprised me:
- The reference pages I could read do not say how a duplicate `request_id` fails, what an error body looks like, or which header selects the API version. I would have had to guess all three. The first two are now handled without needing the answer; the version header is an optional env var, off by default, and is the first thing to check with real credentials.
- My first `dumps` for "a Decimal as a bare JSON number" used a control-character marker that `json.dumps` escapes, so it silently never matched. It now uses a random per-process marker, which also means text in a payload (an invoice note) cannot forge a number.
- `httpx`'s `json=` argument in my own tests quietly turned a 17-digit rate into a float and lost digits; the adapter was right and the test fixture was wrong. Tests now serve fixtures through the Decimal-safe encoder.
Would do differently: read the reference (and fill the notes) before writing a line, as the Day 1 checklist said; the 30 minutes I skipped then cost about the same now.

Not fixed or not claimed:
- NOTHING HAS BEEN RUN AGAINST THE REAL SANDBOX. No credentials in this session. The live contract tests, the recorder (`scripts/record_airwallex_fixtures.py`) and every UNVERIFIED row in `docs/airwallex-notes.md` are untested. Run `AIRWALLEX_BASE_URL=... AIRWALLEX_CLIENT_ID=... AIRWALLEX_API_KEY=... pytest tests/contract` (add `AIRWALLEX_LIVE_EXECUTE=1` for the conversion tests; it needs a funded USD wallet), then the recorder.
- The fixtures in `tests/fixtures/airwallex` are derived from the reference's field lists, not recorded. The sandbox-replay variant therefore proves the client against my reading of the reference, not against the vendor.
- The fake enforces rules I inferred (amount types, a 64-character `request_id`, single-use quotes) and invents the duplicate-request code. If the real sandbox differs, the contract tests will say so.
- Unknown until a live run: the API version header and the default version (a 404 or `invalid_endpoint` is the likely symptom), whether `get_balances` shows a conversion before it settles, and whether a negative or overdraft balance exists (the `Balance` model would reject it, so `get_balances` would raise `UnknownError`).
- Only the 2022-09-09 balances page was reachable; the current one may differ.
- `execute_conversion` costs one extra list call per execution (the lookup before creating). Worth it for crash safety; a cache keyed by idempotency key in the executions table (Day 13) could avoid it.
- Nothing is committed: `.claude/settings.json` denies `git add`, `git commit` and `git push`, and this session's GitHub access is read-only. Suggested: branch `day-12-airwallex-sandbox`, tag `day-12`. Changed or new: `docs/airwallex-notes.md`, `.env.example`, `Makefile`, `pyproject.toml`, `packages/airwallex/*` (7 new/changed files), `tests/airwallex_fixtures.py`, `tests/fake_airwallex.py`, `tests/fixtures/`, `tests/contract/`, two new unit test files, `scripts/record_airwallex_fixtures.py`, this log.

### Day 12 verification pass (2026-11-03)

- Credentials: none of `AIRWALLEX_*` were set in the session and `api.sandbox.airwallex.com` was unreachable from the container (HTTP 000), so the live contract tests, a real balance fetch and a real quote were NOT run. 18 contract tests x 3 adapters: mock 18 pass, sandbox client over the fake server 18 pass, live 18 skipped.
- Mocked server: a 401 makes 1 call and 0 sleeps; 503, 503, 200 makes 3 calls with sleeps 0.5 s and 1.0 s; a permanent 503 stops after 4 calls with sleeps 0.5, 1.0, 2.0 and raises `UnknownError`.
- Secrets: a DEBUG run with sentinel API key, client id, account number, IBAN and token, against a server that echoes them back in an error message, left 0 occurrences in 33 log lines and 6 exception texts. Static grep of source, scripts and tracked files: no key values (the `AIRWALLEX_API_KEY=...` lines are placeholders). Account numbers only appear in a request body in a unit test, where the log shows `[REDACTED]`. The sandbox client does not send account numbers yet (beneficiaries are Day 13).
- Mock vs sandbox: ran 14 edge cases through both. Found and fixed three differences (expiry boundary, whitespace-only key, sub-cent amount), each with a contract test that fails when the fix is reverted. Remaining differences are listed in `docs/airwallex-notes.md`; four methods are unimplemented in the client until Day 13.
- `make test`: 982 passed, 18 skipped.
