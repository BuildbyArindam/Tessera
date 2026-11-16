"""The replay world: FX series, supplier obligations, funding currency and a balance stand-in.

Obligations are the synthetic supplier invoices (AUD and SGD, 30-day terms): money the business must pay out in a
currency it does not earn. Payroll is INR (the base currency, no FX) and SaaS card spend is tiny and due the day it is
issued, so neither has a conversion decision to make.

Assumption: a business funds an AUD/SGD payout from the USD/EUR/GBP receivable currency it invoiced most in BEFORE the
held-out period (so the choice cannot see the period it is tested on). The synthetic generator has no balances; the
balance stand-in is the Day 15 one (four weeks of average gross weekly movement over the last 13 weeks).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from tessera_core.forecast.history import History
from tessera_core.simulate.series import FxSeries

from tessera_evalkit.eval_config import FUNDING_CURRENCIES


@dataclass(frozen=True)
class Obligation:
    id: str
    business: str
    buy: str  # payout currency (AUD or SGD)
    amount: float  # payout, in the buy currency
    issue: pd.Timestamp
    due: pd.Timestamp
    funding: str  # sell currency


class FxWorld:
    """Observed business-day rates of every currency per 1 EUR, and FxSeries for any SELL/BUY pair."""

    def __init__(self, fx_parquet: str | Path) -> None:
        df = pd.read_parquet(fx_parquet)
        obs = df[df["is_observed"]]
        wide = obs.pivot(index="date", columns="quote", values="rate")
        wide["EUR"] = 1.0
        wide.index = pd.to_datetime(wide.index)
        self.wide = wide.sort_index().dropna()
        self._series: dict[str, FxSeries] = {}

    def series(self, sell: str, buy: str) -> FxSeries:
        """Buy units per 1 sell unit (the policy / simulator convention)."""
        key = f"{sell}/{buy}"
        if key not in self._series:
            values = (self.wide[buy] / self.wide[sell]).to_numpy(dtype=np.float64)
            days = self.wide.index.to_numpy(dtype="datetime64[D]")
            self._series[key] = FxSeries(key, days, values)
        return self._series[key]

    def per_eur(self, currency: str, on: pd.Timestamp) -> float:
        """Units of `currency` per 1 EUR on the last observation on or before `on`."""
        return float(self.wide[currency].loc[:on].iloc[-1])


def funding_currency(history: History, business: str, before: pd.Timestamp) -> str:
    inv = history.invoices
    sel = inv[
        (inv["business_id"] == business) & (inv["issue_date"] < before) & inv["currency"].isin(FUNDING_CURRENCIES)
    ]
    if sel.empty:
        raise ValueError(f"{business}: no receivables before {before.date()} to choose a funding currency from")
    return str(sel.groupby("currency")["amount"].sum().idxmax())


def load_obligations(
    data_dir: str | Path, history: History, start: pd.Timestamp, end: pd.Timestamp
) -> list[Obligation]:
    """Supplier invoices issued on or after `start` and due on or before `end` (their whole life is in-period)."""
    out = pd.read_parquet(Path(data_dir) / "outflows.parquet")
    out = out[out["kind"] == "supplier_invoice"].copy()
    out["issue_date"], out["due_date"] = pd.to_datetime(out["issue_date"]), pd.to_datetime(out["due_date"])
    out = out[(out["issue_date"] >= start) & (out["due_date"] <= end)].sort_values(["due_date", "outflow_id"])
    funding = {b: funding_currency(history, b, start) for b in sorted(out["business_id"].unique())}
    return [
        Obligation(
            id=str(r.outflow_id),
            business=str(r.business_id),
            buy=str(r.currency),
            amount=float(r.amount),
            issue=r.issue_date,
            due=r.due_date,
            funding=funding[str(r.business_id)],
        )
        for r in out.itertuples()
    ]


def balance_proxy(cashflows: pd.DataFrame, business: str, currency: str, as_of: pd.Timestamp) -> float:
    """Four weeks of average gross weekly movement over the 13 weeks to `as_of` (nothing after it is read)."""
    c = cashflows[(cashflows["business_id"] == business) & (cashflows["currency"] == currency)]
    d = c["cash_date"]
    window = c[(d <= as_of) & (d > as_of - pd.Timedelta(weeks=13))]
    return float(window["amount"].abs().sum()) / 13.0 * 4.0
