"""Load the Day 2 parquet files into the invoice table the risk scorer expects.

Real Day 2 schema (generate.py): ``invoices.parquet`` has ``invoice_date`` / ``due_date`` / ``status``
and NO paid date; payments are in ``payments.parquet`` (``invoice_id``, ``pay_date``). A settled
dispute (``settled_partial``) is paid late and partially; we treat its payment date as the paid
date because lateness is about timing, not amount.

``ground_truth.parquet`` is deliberately NOT read: it holds latent labels and must never be a feature.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def load_invoice_table(data_dir: str | Path) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Return (invoice table in risk-scorer format, data end date from manifest.json)."""
    root = Path(data_dir)
    inv = pd.read_parquet(root / "invoices.parquet")
    pay = pd.read_parquet(root / "payments.parquet")
    manifest = json.loads((root / "manifest.json").read_text())
    data_end = pd.Timestamp(manifest["as_of"])

    first_paid = pay.groupby("invoice_id")["pay_date"].min().rename("paid_date")
    table = pd.DataFrame(
        {
            "invoice_id": inv["invoice_id"].astype(str),
            "business_id": inv["business_id"].astype(str),
            "customer_id": inv["customer_id"].astype(str),
            "currency": inv["currency"].astype(str),
            "amount": inv["amount"].map(float).astype(float),  # float only at the modelling boundary
            "issue_date": pd.to_datetime(inv["invoice_date"]),
            "due_date": pd.to_datetime(inv["due_date"]),
        }
    )
    table = table.merge(first_paid.reset_index(), on="invoice_id", how="left")
    table["paid_date"] = pd.to_datetime(table["paid_date"])
    return table, data_end
