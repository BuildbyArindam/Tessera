"""Leakage-safe feature builder.

``build_features(history, as_of)`` returns one row per (business, currency)
describing the world at the end of day ``as_of``, ready to predict the weeks
that follow. The first thing it does is ``history.as_of(as_of)``; nothing
after that line can see the future.

Missing values: lags are NaN when a series is shorter than the lag.
LightGBM handles NaN natively, so we do not invent zeros.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tessera_core.forecast.history import History
from tessera_core.forecast.target import last_complete_week_start, series_by_key, weekly_net_cashflow

N_LAGS = 12
ROLL_WINDOWS = (4, 12)
LATE_THRESHOLD_DAYS = 7
LATE_WINDOW_DAYS = 182
MIN_CUSTOMER_OBS = 3
OPEN_HORIZONS_WEEKS = (4, 13)

FEATURE_COLUMNS: list[str] = (
    ["as_of"]
    + [f"lag_{k}" for k in range(1, N_LAGS + 1)]
    + [f"roll_{stat}_{w}" for w in ROLL_WINDOWS for stat in ("mean", "std")]
    + ["week_of_year", "month_end_flag", "open_total"]
    + [f"open_expected_{w}w" for w in OPEN_HORIZONS_WEEKS]
    + ["trailing_late_rate"]
)


def _expected_delay_days(inv: pd.DataFrame) -> np.ndarray:
    """Typical days-past-due per invoice row, from invoices already paid.

    Customer median if the customer has >= MIN_CUSTOMER_OBS paid invoices,
    else the business median, else 0.
    """
    paid = inv[inv["paid_date"].notna()]
    if paid.empty:
        return np.zeros(len(inv))
    delay = (paid["paid_date"] - paid["due_date"]).dt.days.astype(float)
    paid = paid.assign(delay=delay)
    stats = paid.groupby(["business_id", "customer_id"])["delay"].agg(["median", "count"])
    cust = stats[stats["count"] >= MIN_CUSTOMER_OBS]["median"].rename("cust_delay").reset_index()
    biz = paid.groupby("business_id")["delay"].median().rename("biz_delay").reset_index()
    merged = inv[["business_id", "customer_id"]].merge(cust, how="left", on=["business_id", "customer_id"])
    merged = merged.merge(biz, how="left", on="business_id")
    out = np.where(merged["cust_delay"].isna(), merged["biz_delay"], merged["cust_delay"])
    return np.nan_to_num(out.astype(float), nan=0.0)


def _invoice_features(inv: pd.DataFrame, business_id: str, currency: str, as_of: pd.Timestamp) -> dict[str, float]:
    sel = inv[(inv["business_id"] == business_id) & (inv["currency"] == currency)]
    open_inv = sel[sel["paid_date"].isna()]
    expected = open_inv["due_date"] + pd.to_timedelta(open_inv["exp_delay"], unit="D")
    expected = expected.clip(lower=as_of + pd.Timedelta(days=1))  # overdue => cash is "due any day"
    feats: dict[str, float] = {"open_total": float(open_inv["amount"].sum())}
    for w in OPEN_HORIZONS_WEEKS:
        within = expected <= as_of + pd.Timedelta(weeks=w)
        feats[f"open_expected_{w}w"] = float(open_inv.loc[within, "amount"].sum())
    recent = sel[sel["paid_date"] > as_of - pd.Timedelta(days=LATE_WINDOW_DAYS)]
    if recent.empty:
        feats["trailing_late_rate"] = float("nan")
    else:
        late = (recent["paid_date"] - recent["due_date"]).dt.days > LATE_THRESHOLD_DAYS
        feats["trailing_late_rate"] = float(late.mean())
    return feats


def build_features(history: History, as_of: pd.Timestamp | str) -> pd.DataFrame:
    """Features at ``as_of`` for every (business_id, currency); index is that pair."""
    as_of_ts = pd.Timestamp(as_of).normalize()
    known = history.as_of(as_of_ts)  # single leakage choke point

    weekly = weekly_net_cashflow(known.cashflows, as_of=as_of_ts)
    inv = known.invoices.copy()
    inv["exp_delay"] = _expected_delay_days(inv)

    next_start = last_complete_week_start(as_of_ts) + pd.Timedelta(weeks=1)
    target_week = pd.date_range(next_start, periods=7, freq="D")
    week_of_year = int(next_start.isocalendar().week)
    month_end = int(any(d.is_month_end for d in target_week))

    rows: list[dict[str, object]] = []
    for (business_id, currency), series in series_by_key(weekly).items():
        y = series.to_numpy(dtype=float)
        row: dict[str, object] = {"business_id": business_id, "currency": currency, "as_of": as_of_ts}
        for k in range(1, N_LAGS + 1):
            row[f"lag_{k}"] = float(y[-k]) if len(y) >= k else float("nan")
        for w in ROLL_WINDOWS:
            tail = y[-w:]
            row[f"roll_mean_{w}"] = float(tail.mean())
            row[f"roll_std_{w}"] = float(tail.std(ddof=1)) if len(tail) > 1 else float("nan")
        row["week_of_year"] = week_of_year
        row["month_end_flag"] = month_end
        row.update(_invoice_features(inv, business_id, currency, as_of_ts))
        rows.append(row)

    out = pd.DataFrame(rows, columns=["business_id", "currency", *FEATURE_COLUMNS])
    return out.set_index(["business_id", "currency"])
