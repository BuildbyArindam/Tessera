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
