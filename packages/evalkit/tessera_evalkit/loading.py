"""Load Day 2 synthetic parquet files into a core ``History``.

ASSUMED Day 2 schema (adapt here, and only here, if the generator differs):

data/synthetic/invoices.parquet
    business_id, invoice_id, customer_id, currency, amount (>0),
    issue_date, due_date, paid_date (null while unpaid / never paid)
data/synthetic/payments.parquet
    business_id, currency, amount (>0), direction ("in" | "out"), cash_date

Amounts may be Decimal or numeric; they are converted to float64 here, at the
modelling boundary. Inflows become positive, outflows negative.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from tessera_core.forecast.history import CASHFLOW_COLUMNS, INVOICE_COLUMNS, History


def _to_float(s: pd.Series) -> pd.Series:
    return s.map(float).astype(float)


def load_history(data_dir: str | Path) -> History:
    root = Path(data_dir)
    invoices = pd.read_parquet(root / "invoices.parquet")
    payments = pd.read_parquet(root / "payments.parquet")

    missing = [c for c in ("business_id", "currency", "amount", "direction", "cash_date") if c not in payments.columns]
    if missing:
        raise ValueError(f"payments.parquet missing {missing}; update loading.py to the Day 2 schema")

    sign = payments["direction"].str.lower().map({"in": 1.0, "out": -1.0})
    if sign.isna().any():
        raise ValueError("payments.direction must be 'in' or 'out'")
    cash = pd.DataFrame(
        {
            "business_id": payments["business_id"].astype(str),
            "currency": payments["currency"].astype(str),
            "cash_date": pd.to_datetime(payments["cash_date"]),
            "amount": _to_float(payments["amount"]) * sign,
        }
    )[list(CASHFLOW_COLUMNS)]

    inv = invoices[list(INVOICE_COLUMNS)].copy()
    inv["business_id"] = inv["business_id"].astype(str)
    inv["currency"] = inv["currency"].astype(str)
    inv["amount"] = _to_float(inv["amount"])
    for col in ("issue_date", "due_date", "paid_date"):
        inv[col] = pd.to_datetime(inv[col])
    return History(cashflows=cash, invoices=inv)
