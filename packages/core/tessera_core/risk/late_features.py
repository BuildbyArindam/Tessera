"""Labels and leakage-safe features for late-payment scoring.

Definition: an invoice is LATE if it is paid more than ``LATE_THRESHOLD_DAYS`` (7) days after
its due date, or never paid.

When is the label known?
    on time  -> on the day it is paid;
    late     -> on due + 8 days (the first day we can see it is still unpaid past the threshold).
An invoice still inside its 7-day window when the data ends is CENSORED (label unknown) and is
never used for training or evaluation.

Snapshots. A model scores an OPEN invoice as of a date ``T``. Training therefore uses weekly
snapshots of every invoice that was open at ``T`` and not yet known to be late
(``T <= due + 7``), so ``days_to_due`` ranges over what the scorer will see in production.
Every feature uses only facts with ``label_known <= T`` or ``paid_date <= T``; the row's own
label is, by construction, not known until after ``T``.

Money enters only as a float ratio (invoice / customer average), at the modelling boundary.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

LATE_THRESHOLD_DAYS = 7
SHRINK_K = 3.0  # pseudo-observations pulling a customer's late rate toward its business prior
RECENT_N = 5
DEFAULT_PRIOR = 0.25  # used only when nothing at all is known yet
DPD_CLIP = (-30.0, 180.0)
CURRENCIES = ("USD", "EUR", "GBP", "SGD", "AUD", "INR")

INVOICE_COLUMNS = (
    "invoice_id",
    "business_id",
    "customer_id",
    "currency",
    "amount",
    "issue_date",
    "due_date",
    "paid_date",
)
NUMERIC_FEATURES = [
    "days_to_due",
    "terms_days",
    "age_days",
    "log_amount_ratio",
    "cust_n_known",
    "cust_late_rate",
    "cust_recent_late_rate",
    "cust_mean_dpd",
    "cust_n_late_open",
    "cust_max_days_overdue",
    "due_month_sin",
    "due_month_cos",
]
CURRENCY_FEATURES = [f"ccy_{c}" for c in CURRENCIES]
LR_FEATURES: list[str] = NUMERIC_FEATURES + CURRENCY_FEATURES
GBM_FEATURES: list[str] = LR_FEATURES + ["due_month"]


class InvoiceTableError(ValueError):
    """The invoice table is malformed (missing columns, wrong dtypes, bad values)."""


def validate_invoices(inv: pd.DataFrame) -> pd.DataFrame:
    """Return a clean copy; raise :class:`InvoiceTableError` on any structural problem."""
    missing = [c for c in INVOICE_COLUMNS if c not in inv.columns]
    if missing:
        raise InvoiceTableError(f"invoice table is missing columns: {missing}")
    out = inv[list(INVOICE_COLUMNS)].copy()
    for col in ("issue_date", "due_date", "paid_date"):
        if not pd.api.types.is_datetime64_any_dtype(out[col]):
            raise InvoiceTableError(f"{col} must be datetime64, got {out[col].dtype}")
    required = ["invoice_id", "business_id", "customer_id", "currency", "issue_date", "due_date"]
    nulls = [c for c in required if out[c].isna().any()]
    if nulls:
        raise InvoiceTableError(f"null values in required columns: {nulls}")
    if out["invoice_id"].duplicated().any():
        raise InvoiceTableError("invoice_id must be unique")
    amount = pd.to_numeric(out["amount"], errors="coerce").astype(float)
    if amount.isna().any() or not (amount > 0).all():
        raise InvoiceTableError("amount must be numeric and strictly positive")
    out["amount"] = amount
    if (out["due_date"] < out["issue_date"]).any():
        raise InvoiceTableError("due_date precedes issue_date")
    return out.reset_index(drop=True)


def mask_as_of(inv: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """What was knowable at the end of day ``as_of``: later payments are blanked."""
    out = inv[inv["issue_date"] <= as_of].copy()
    out.loc[out["paid_date"] > as_of, "paid_date"] = pd.NaT
    return out


def add_labels(inv: pd.DataFrame) -> pd.DataFrame:
    """Add ``is_late`` (0/1) and ``label_known`` (when that outcome becomes observable)."""
    out = inv.copy()
    due_plus = out["due_date"] + pd.Timedelta(days=LATE_THRESHOLD_DAYS)
    on_time = out["paid_date"].notna() & (out["paid_date"] <= due_plus)
    out["is_late"] = (~on_time).astype(float)
    out["label_known"] = (due_plus + pd.Timedelta(days=1)).where(~on_time, out["paid_date"])
    return out


def log_amount_ratio(inv: pd.DataFrame) -> pd.Series:
    """log(amount / mean amount of the same customer's EARLIER invoices); 0 for a first invoice."""
    s = inv.sort_values(["issue_date", "invoice_id"])
    g = s.groupby(["business_id", "customer_id"], sort=False)["amount"]
    n_prev = g.cumcount().to_numpy(dtype=float)
    prev_mean = (g.cumsum().to_numpy(dtype=float) - s["amount"].to_numpy(dtype=float)) / np.maximum(n_prev, 1.0)
    ratio = np.where(n_prev > 0, np.log(s["amount"].to_numpy(dtype=float) / np.maximum(prev_mean, 1e-12)), 0.0)
    return pd.Series(ratio, index=s.index).reindex(inv.index)


def _customer_stats(labelled: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Per-customer history known at ``as_of`` (``labelled`` already has labels)."""
    known = labelled[labelled["label_known"] <= as_of]
    keys = ["business_id", "customer_id"]
    cols = [*keys, "cust_n_known", "cust_late_rate", "cust_recent_late_rate", "cust_mean_dpd"]
    cols += ["cust_n_late_open", "cust_max_days_overdue", "_prior"]
    if known.empty:
        return pd.DataFrame(columns=cols)
    global_prior = float(known["is_late"].mean())
    biz_prior = known.groupby("business_id")["is_late"].mean().rename("_prior").reset_index()
    ordered = known.sort_values("label_known")
    agg = ordered.groupby(keys)["is_late"].agg(n="count", late="sum").reset_index()
    recent = ordered.groupby(keys).tail(RECENT_N).groupby(keys)["is_late"].agg(n5="count", late5="sum").reset_index()
    paid = known[known["paid_date"].notna() & (known["paid_date"] <= as_of)]
    dpd = (paid["paid_date"] - paid["due_date"]).dt.days.astype(float).clip(*DPD_CLIP)
    mean_dpd = paid.assign(_dpd=dpd).groupby(keys)["_dpd"].mean().rename("cust_mean_dpd").reset_index()
    open_late = known[known["paid_date"].isna() | (known["paid_date"] > as_of)]
    open_late = open_late.assign(_over=(as_of - open_late["due_date"]).dt.days.astype(float))
    ol = open_late.groupby(keys)["_over"].agg(cust_n_late_open="count", cust_max_days_overdue="max").reset_index()
    stats = agg.merge(recent, on=keys, how="left").merge(biz_prior, on="business_id", how="left")
    stats["_prior"] = stats["_prior"].fillna(global_prior)
    p = stats["_prior"]
    stats["cust_n_known"] = stats["n"].astype(float)
    stats["cust_late_rate"] = (stats["late"] + SHRINK_K * p) / (stats["n"] + SHRINK_K)
    stats["cust_recent_late_rate"] = (stats["late5"].fillna(0) + SHRINK_K * p) / (stats["n5"].fillna(0) + SHRINK_K)
    stats = stats.merge(mean_dpd, on=keys, how="left").merge(ol, on=keys, how="left")
    stats[["cust_n_late_open", "cust_max_days_overdue"]] = stats[["cust_n_late_open", "cust_max_days_overdue"]].fillna(
        0.0
    )
    return stats[cols]


def features_at(labelled: pd.DataFrame, as_of: pd.Timestamp, rows: pd.DataFrame) -> pd.DataFrame:
    """Feature matrix (``GBM_FEATURES``) for ``rows`` as of ``as_of``; index matches ``rows``.

    ``labelled`` is the invoice table with labels and ``log_amount_ratio`` added; it may be the full
    history or an ``as_of`` mask of it, the result is identical (checked in tests).
    """
    keys = ["business_id", "customer_id"]
    stats = _customer_stats(labelled, as_of)
    biz = labelled[labelled["label_known"] <= as_of].groupby("business_id")["is_late"].mean()
    known_all = labelled[labelled["label_known"] <= as_of]["is_late"]
    base = float(known_all.mean()) if len(known_all) else DEFAULT_PRIOR
    merged = rows[keys].merge(stats, on=keys, how="left")
    prior = rows["business_id"].map(biz).fillna(base).to_numpy(dtype=float)
    n_known = merged["cust_n_known"].fillna(0.0).to_numpy(dtype=float)
    late_rate = merged["cust_late_rate"].to_numpy(dtype=float)
    recent = merged["cust_recent_late_rate"].to_numpy(dtype=float)
    out = pd.DataFrame(index=rows.index)
    out["days_to_due"] = (rows["due_date"] - as_of).dt.days.astype(float)
    out["terms_days"] = (rows["due_date"] - rows["issue_date"]).dt.days.astype(float)
    out["age_days"] = (as_of - rows["issue_date"]).dt.days.astype(float)
    out["log_amount_ratio"] = rows["log_amount_ratio"].astype(float)
    out["cust_n_known"] = n_known
    out["cust_late_rate"] = np.where(n_known > 0, late_rate, prior)
    out["cust_recent_late_rate"] = np.where(n_known > 0, recent, prior)
    out["cust_mean_dpd"] = merged["cust_mean_dpd"].to_numpy(dtype=float)
    out["cust_n_late_open"] = merged["cust_n_late_open"].fillna(0.0).to_numpy(dtype=float)
    out["cust_max_days_overdue"] = merged["cust_max_days_overdue"].fillna(0.0).to_numpy(dtype=float)
    month = rows["due_date"].dt.month.to_numpy(dtype=float)
    out["due_month_sin"] = np.sin(2 * np.pi * month / 12.0)
    out["due_month_cos"] = np.cos(2 * np.pi * month / 12.0)
    for c in CURRENCIES:
        out[f"ccy_{c}"] = (rows["currency"] == c).to_numpy(dtype=float)
    out["due_month"] = month
    return out[GBM_FEATURES]


def prepare(inv: pd.DataFrame) -> pd.DataFrame:
    """Validated table + labels + amount ratio: the input every other function expects."""
    clean = validate_invoices(inv)
    out = add_labels(clean)
    out["log_amount_ratio"] = log_amount_ratio(out)
    return out


def build_snapshot_dataset(prepared: pd.DataFrame, data_end: pd.Timestamp, step_days: int = 7) -> pd.DataFrame:
    """Weekly open-invoice snapshots with features and labels, censored invoices removed.

    Columns: invoice_id, business_id, customer_id, origin, label_known, y, then ``GBM_FEATURES``.
    """
    if step_days <= 0:
        raise ValueError("step_days must be positive")
    usable = prepared[prepared["label_known"] <= data_end]
    frames: list[pd.DataFrame] = []
    for origin in pd.date_range(prepared["issue_date"].min(), data_end, freq=f"{step_days}D"):
        open_now = usable[
            (usable["issue_date"] <= origin)
            & (usable["paid_date"].isna() | (usable["paid_date"] > origin))
            & (origin <= usable["due_date"] + pd.Timedelta(days=LATE_THRESHOLD_DAYS))
        ]
        if open_now.empty:
            continue
        feats = features_at(prepared, origin, open_now)
        meta = open_now[["invoice_id", "business_id", "customer_id", "label_known"]].copy()
        meta["origin"] = origin
        meta["y"] = open_now["is_late"].astype(float)
        frames.append(pd.concat([meta, feats], axis=1))
    if not frames:
        raise ValueError("no labelled snapshots could be built from this invoice table")
    return pd.concat(frames, ignore_index=True)
