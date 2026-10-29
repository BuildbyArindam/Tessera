"""Tiny deterministic History for tests. NOT the Day 2 generator."""

from __future__ import annotations

import numpy as np
import pandas as pd

from tessera_core.forecast.history import History

START = pd.Timestamp("2025-01-06")  # a Monday
N_WEEKS = 80


def make_history(seed: int = 1, n_businesses: int = 2) -> History:
    rng = np.random.default_rng(seed)
    end = START + pd.Timedelta(weeks=N_WEEKS) - pd.Timedelta(days=1)  # a Sunday
    inv_rows: list[dict[str, object]] = []
    cash_rows: list[dict[str, object]] = []
    n = 0
    for b in range(n_businesses):
        bid = f"biz{b}"
        for w in range(N_WEEKS):
            issue = START + pd.Timedelta(weeks=w, days=int(rng.integers(0, 5)))
            for _ in range(2):
                n += 1
                cust = f"c{int(rng.integers(0, 4))}"
                amount = float(round(1000 * (1 + 0.3 * np.sin(2 * np.pi * w / 52)) * rng.uniform(0.7, 1.3), 2))
                due = issue + pd.Timedelta(days=30)
                paid = due + pd.Timedelta(days=int(rng.integers(-3, 12)) + 5 * int(cust == "c3"))
                paid_val = paid if paid <= end else pd.NaT
                inv_rows.append(
                    dict(business_id=bid, invoice_id=f"i{n}", customer_id=cust, currency="USD",
                         amount=amount, issue_date=issue, due_date=due, paid_date=paid_val)
                )
                if paid <= end:
                    cash_rows.append(dict(business_id=bid, currency="USD", cash_date=paid, amount=amount))
            cash_rows.append(
                dict(business_id=bid, currency="INR", cash_date=START + pd.Timedelta(weeks=w, days=2),
                     amount=-float(round(rng.uniform(200, 600), 2)))
            )
    inv = pd.DataFrame(inv_rows)
    for col in ("issue_date", "due_date", "paid_date"):
        inv[col] = pd.to_datetime(inv[col])
    cash = pd.DataFrame(cash_rows)
    cash["cash_date"] = pd.to_datetime(cash["cash_date"])
    return History(cashflows=cash, invoices=inv)
