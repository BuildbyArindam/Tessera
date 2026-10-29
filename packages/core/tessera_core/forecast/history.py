"""History: the only door through which forecast code sees data.

Leakage control lives here. ``History.as_of(T)`` returns a copy containing
only what was knowable at the end of day T:

* cash flows whose ``cash_date`` is on or before T;
* invoices issued on or before T;
* ``paid_date`` values after T are blanked (the invoice looked open at T).

Everything downstream (features, backtest) must go through ``as_of`` first.

Modelling note: amounts are Decimal-backed ``Money`` in the domain layer. For
statistical modelling they are converted to float64 at the boundary (in the
loader); forecasts are estimates, never ledger values.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

CASHFLOW_COLUMNS = ("business_id", "currency", "cash_date", "amount")
INVOICE_COLUMNS = (
    "business_id",
    "invoice_id",
    "customer_id",
    "currency",
    "amount",
    "issue_date",
    "due_date",
    "paid_date",
)
_DATE_COLS_CASHFLOW = ("cash_date",)
_DATE_COLS_INVOICE = ("issue_date", "due_date", "paid_date")


def _check(df: pd.DataFrame, required: tuple[str, ...], date_cols: tuple[str, ...], name: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{name} is missing columns: {missing}")
    for col in date_cols:
        if not pd.api.types.is_datetime64_any_dtype(df[col]):
            raise ValueError(f"{name}.{col} must be datetime64, got {df[col].dtype}")


@dataclass(frozen=True)
class History:
    """Raw history for many businesses.

    cashflows: one row per realised cash movement; ``amount`` is signed
        (inflow positive, outflow negative) in the row's own currency.
    invoices: receivables; ``paid_date`` is NaT while unpaid.
    """

    cashflows: pd.DataFrame
    invoices: pd.DataFrame

    def __post_init__(self) -> None:
        _check(self.cashflows, CASHFLOW_COLUMNS, _DATE_COLS_CASHFLOW, "cashflows")
        _check(self.invoices, INVOICE_COLUMNS, _DATE_COLS_INVOICE, "invoices")

    def as_of(self, as_of: pd.Timestamp | str) -> History:
        """Return only what was known at the end of day ``as_of`` (inclusive)."""
        next_day = pd.Timestamp(as_of).normalize() + pd.Timedelta(days=1)
        cash = self.cashflows[self.cashflows["cash_date"] < next_day].copy()
        inv = self.invoices[self.invoices["issue_date"] < next_day].copy()
        inv.loc[inv["paid_date"] >= next_day, "paid_date"] = pd.NaT
        return History(cashflows=cash, invoices=inv)
