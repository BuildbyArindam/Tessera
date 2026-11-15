"""Load the Day 2 synthetic parquet files into a core ``History``.

Real Day 2 schema (see generate.py), the only one this module reads:

data/synthetic/payments.parquet   money IN:  payment_id, invoice_id, business_id, pay_date, currency, amount
data/synthetic/outflows.parquet   money OUT: outflow_id, business_id, counterparty_id, kind, issue_date, due_date,
                                  pay_date (null until paid), currency, amount
data/synthetic/invoices.parquet   read through ``risk_data.load_invoice_table`` (it has no paid date; payments do)

Day 3 first wrote this loader against an assumed schema (``direction``/``cash_date``) that the generator never
produced, so it could not read the real files. It is reconciled here, and ``risk_data`` already did the same for
invoices, so invoices are read in one place.

Amounts may be Decimal or numeric; they are converted to float64 here, at the modelling boundary. Inflows become
positive and outflows negative. An outflow with no ``pay_date`` has not happened yet at the data's end date, so it is
a payable (see ``load_open_payables``), not a cash flow.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from tessera_core.forecast.history import CASHFLOW_COLUMNS, INVOICE_COLUMNS, History

from tessera_evalkit.risk_data import load_invoice_table


def _to_float(s: pd.Series) -> pd.Series:
    return s.map(float).astype(float)


def load_cashflows(data_dir: str | Path) -> pd.DataFrame:
    """Every realised cash movement: customer payments in (+), outflows that have been paid (-)."""
    root = Path(data_dir)
    inflows = pd.read_parquet(root / "payments.parquet")
    outflows = pd.read_parquet(root / "outflows.parquet")
    outflows = outflows[outflows["pay_date"].notna()]
    parts = []
    for frame, sign in ((inflows, 1.0), (outflows, -1.0)):
        parts.append(
            pd.DataFrame(
                {
                    "business_id": frame["business_id"].astype(str),
                    "currency": frame["currency"].astype(str),
                    "cash_date": pd.to_datetime(frame["pay_date"]),
                    "amount": _to_float(frame["amount"]) * sign,
                }
            )
        )
    return pd.concat(parts, ignore_index=True)[list(CASHFLOW_COLUMNS)].sort_values("cash_date").reset_index(drop=True)


def load_open_payables(data_dir: str | Path) -> pd.DataFrame:
    """Outflows not yet paid at the data's end date: what the business still owes, with its due date."""
    root = Path(data_dir)
    outflows = pd.read_parquet(root / "outflows.parquet")
    counterparties = pd.read_parquet(root / "counterparties.parquet")[["counterparty_id", "name"]]
    open_rows = outflows[outflows["pay_date"].isna()].merge(counterparties, on="counterparty_id", how="left")
    return open_rows.rename(columns={"name": "counterparty_name"}).reset_index(drop=True)


def load_history(data_dir: str | Path) -> History:
    invoices, _ = load_invoice_table(data_dir)
    return History(cashflows=load_cashflows(data_dir), invoices=invoices[list(INVOICE_COLUMNS)].copy())
