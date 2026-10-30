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
