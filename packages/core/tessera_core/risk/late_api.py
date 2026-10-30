"""Read-only tool: late-payment risk and expected payment-date range for open invoices.

``assess_late_payment`` is a pure function of (fitted scorer, invoice table, as_of). It never
writes, never reaches outside the arguments, and cannot see anything after ``as_of`` (the table
is masked first). Its output is a list of frozen Pydantic models, so it serialises straight into
an agent tool result.

Two things are reported per invoice and they come from different models on purpose:
    * ``p_late``      calibrated classifier probability that payment lands > 7 days after due;
    * date range      Kaplan-Meier quantiles (10 / 50 / 90 %) of the payment date.
They are not forced to agree. The range is conditional on the invoice being unpaid today and
describes WHEN a payment arrives IF it arrives; ``p_late`` also carries the risk it never does.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Literal

import pandas as pd

from tessera_core.domain.base import DomainModel
from tessera_core.risk.late_features import (
    LATE_THRESHOLD_DAYS,
    features_at,
    mask_as_of,
    prepare,
    validate_invoices,
)
from tessera_core.risk.late_model import LateScorer, ModelName
from tessera_core.risk.payment_curve import PaymentCurves, Source

RANGE_QUANTILES = (0.10, 0.50, 0.90)


class InvoiceSelectionError(ValueError):
    """Requested invoice ids are unknown or not open as of the requested date."""


class LatePaymentAssessment(DomainModel):
    invoice_id: str
    as_of: date
    due_date: date
    p_late: float
    already_late: bool
    model: Literal["logreg", "lgbm", "deterministic"]
    pay_date_low: date | None = None
    pay_date_median: date | None = None
    pay_date_high: date | None = None
    range_source: Source | None = None
    range_tail_unresolved: bool = False
    note: str | None = None


def _open_rows(prepared: pd.DataFrame, invoice_ids: Sequence[str] | None) -> pd.DataFrame:
    open_rows = prepared[prepared["paid_date"].isna()]
    if invoice_ids is None:
        return open_rows.sort_values("invoice_id")
    wanted = list(dict.fromkeys(invoice_ids))
    bad = sorted(set(wanted) - set(open_rows["invoice_id"]))
    if bad:
        raise InvoiceSelectionError(f"not open (or unknown) as of the requested date: {bad}")
    return open_rows[open_rows["invoice_id"].isin(wanted)].sort_values("invoice_id")


def assess_late_payment(
    scorer: LateScorer,
    invoices: pd.DataFrame,
    as_of: date | str | pd.Timestamp,
    invoice_ids: Sequence[str] | None = None,
    *,
    model: ModelName | None = None,
) -> list[LatePaymentAssessment]:
    """Score open invoices as of ``as_of`` (all of them, or just ``invoice_ids``).

    ``invoices`` needs the columns in ``late_features.INVOICE_COLUMNS``; ``paid_date`` is NaT while
    unpaid. Payments after ``as_of`` are ignored, so passing a longer history is safe.
    """
    t = pd.Timestamp(as_of).normalize()
    masked = mask_as_of(validate_invoices(invoices), t)
    prepared = prepare(masked)
    rows = _open_rows(prepared, invoice_ids)
    if rows.empty:
        return []

    due_plus = rows["due_date"] + pd.Timedelta(days=LATE_THRESHOLD_DAYS)
    already = (t > due_plus).to_numpy()
    p_model = scorer.predict(features_at(prepared, t, rows), model) if (~already).any() else None
    name: Literal["logreg", "lgbm"] = model or scorer.default
    curves = PaymentCurves.fit(masked, t)

    out: list[LatePaymentAssessment] = []
    p_iter = iter(p_model[~already]) if p_model is not None else iter(())
    for (_, row), is_late_now in zip(rows.iterrows(), already, strict=True):
        due = row["due_date"].date()
        p_late = 1.0 if is_late_now else float(next(p_iter))
        picked = curves.pick(str(row["business_id"]), str(row["customer_id"]))
        low = med = high = None
        source: Source | None = None
        unresolved = False
        note: str | None = None
        if picked is None:
            note = "no payment history anywhere yet; no date range available"
        else:
            curve, source = picked
            days, unresolved = curve.conditional_days((t - row["due_date"]).days, RANGE_QUANTILES)
            low, med, high = (due + timedelta(days=d) for d in days)
            if unresolved:
                note = "upper end of the range is a cap: some payments historically never arrive"
        out.append(
            LatePaymentAssessment(
                invoice_id=str(row["invoice_id"]),
                as_of=t.date(),
                due_date=due,
                p_late=p_late,
                already_late=bool(is_late_now),
                model="deterministic" if is_late_now else name,
                pay_date_low=low,
                pay_date_median=med,
                pay_date_high=high,
                range_source=source,
                range_tail_unresolved=unresolved,
                note=note,
            )
        )
    return out
