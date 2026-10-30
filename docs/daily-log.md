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
