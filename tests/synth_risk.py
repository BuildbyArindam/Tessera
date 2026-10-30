"""Tiny seeded invoice table with known customer behaviour. Test-only.

Three customer kinds with very different delays, so a working scorer must separate them:
prompt (pays ~2 days after due), typical (~8), chronic (~30). ~3% of invoices are never paid.
Payments after ``data_end`` are not recorded, exactly like the real generator.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

KINDS = (("prompt", 2.0, 0.4), ("typical", 8.0, 0.5), ("chronic", 30.0, 0.4))


def make_invoice_table(
    seed: int = 0, n_customers: int = 24, months: int = 16, start: str = "2025-01-01"
) -> tuple[pd.DataFrame, pd.Timestamp]:
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp(start)
    data_end = t0 + pd.DateOffset(months=months) - pd.Timedelta(days=1)
    rows = []
    for c in range(n_customers):
        _, med, sig = KINDS[c % len(KINDS)]
        biz = f"B{c % 2}"
        terms = int(rng.choice([14, 30, 45]))
        ccy = ("USD", "EUR", "GBP")[c % 3]
        base = float(np.exp(rng.uniform(6, 8)))
        for day in range(0, (data_end - t0).days - 5, 1):
            if rng.random() > 1.0 / 28.0:  # ~1 invoice a month
                continue
            issue = t0 + pd.Timedelta(days=day)
            due = issue + pd.Timedelta(days=terms)
            dpd = int(round(np.exp(rng.normal(np.log(med), sig))))
            paid = due + pd.Timedelta(days=dpd) if rng.random() > 0.03 else pd.NaT
            if pd.notna(paid) and paid > data_end:
                paid = pd.NaT
            rows.append((biz, f"C{c:02d}", ccy, base * float(np.exp(rng.normal(0, 0.3))), issue, due, paid))
    df = pd.DataFrame(
        rows, columns=["business_id", "customer_id", "currency", "amount", "issue_date", "due_date", "paid_date"]
    )
    df.insert(0, "invoice_id", [f"INV-{i:05d}" for i in range(len(df))])
    return df, data_end
