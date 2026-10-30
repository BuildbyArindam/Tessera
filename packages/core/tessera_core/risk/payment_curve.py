"""Expected payment-date range from a Kaplan-Meier curve of days-past-due (DPD).

Why Kaplan-Meier and not "the median of paid invoices": at any date T the invoices already paid
are the fast ones. Averaging only those understates delays. KM treats an invoice that is still
unpaid at T as censored at ``T - due`` (we know it survived at least that long) and corrects for it.

Time axis: DPD = pay_date - due_date in days (negative = early). S(t) = P(not yet paid by the end
of day ``due + t``). For an open invoice already ``s0`` days past due we condition on survival:

    P(paid by day t | unpaid through s0) = 1 - S(t) / S(s0),   t > s0

so the range can never start in the past. Never-paid invoices stay censored forever, so the curve
may plateau above 0; a quantile that is never reached is reported at ``MAX_DPD`` and flagged.

Curve choice: the customer's own curve if it has >= ``MIN_EVENTS`` payments, else its business
pool, else all businesses, else no curve (the caller reports "no range").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import pandas as pd

GRID_MIN = -60
MAX_DPD = 365
MIN_EVENTS = 5
Source = Literal["customer", "business", "global"]
FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class SurvivalCurve:
    """KM survival on the integer day grid ``GRID_MIN .. MAX_DPD``."""

    survival: FloatArray
    n_events: int
    n_obs: int

    def conditional_days(self, past_due_days: int, quantiles: tuple[float, ...]) -> tuple[tuple[int, ...], bool]:
        """DPD at which each quantile of the remaining wait is reached, given ``past_due_days`` unpaid.

        Returns (days, tail_unresolved). Every returned day is > ``past_due_days``.
        """
        s0 = int(np.clip(past_due_days, GRID_MIN, MAX_DPD))
        i0 = s0 - GRID_MIN
        base = float(self.survival[i0])
        if base <= 1e-12 or i0 >= len(self.survival) - 1:
            return tuple(s0 + 1 for _ in quantiles), False
        cond = 1.0 - self.survival[i0 + 1 :] / base
        days: list[int] = []
        unresolved = False
        for q in quantiles:
            k = int(np.searchsorted(cond, q, side="left"))
            if k >= len(cond):
                unresolved = True
                days.append(MAX_DPD)
            else:
                days.append(s0 + 1 + k)
        return tuple(days), unresolved


def km_curve(durations: npt.ArrayLike, observed: npt.ArrayLike) -> SurvivalCurve:
    """Kaplan-Meier survival from integer durations and an event indicator (True = paid)."""
    d = np.asarray(durations, dtype=np.int64)
    e = np.asarray(observed, dtype=bool)
    if d.shape != e.shape or d.ndim != 1:
        raise ValueError("durations and observed must be 1-D arrays of equal length")
    idx = np.clip(d, GRID_MIN, MAX_DPD) - GRID_MIN
    size = MAX_DPD - GRID_MIN + 1
    events = np.bincount(idx[e], minlength=size).astype(float)
    total = np.bincount(idx, minlength=size).astype(float)
    at_risk = np.cumsum(total[::-1])[::-1]
    hazard = np.divide(events, at_risk, out=np.zeros(size), where=at_risk > 0)
    return SurvivalCurve(np.cumprod(1.0 - hazard), int(e.sum()), int(len(d)))


@dataclass(frozen=True)
class PaymentCurves:
    """Curves fitted on what was known at ``as_of``."""

    customers: dict[tuple[str, str], SurvivalCurve]
    businesses: dict[str, SurvivalCurve]
    overall: SurvivalCurve | None

    @classmethod
    def fit(cls, masked: pd.DataFrame, as_of: pd.Timestamp) -> PaymentCurves:
        """``masked`` must already be an ``as_of`` view (later payments blanked)."""
        paid = masked["paid_date"].notna()
        end = masked["paid_date"].where(paid, as_of)
        dur = (end - masked["due_date"]).dt.days.to_numpy(dtype=np.int64)
        frame = pd.DataFrame(
            {
                "b": masked["business_id"].to_numpy(),
                "c": masked["customer_id"].to_numpy(),
                "d": dur,
                "e": paid.to_numpy(),
            }
        )
        customers = {
            (str(b), str(c)): km_curve(g["d"], g["e"])
            for (b, c), g in frame.groupby(["b", "c"])
            if int(g["e"].sum()) >= MIN_EVENTS
        }
        businesses = {
            str(b): km_curve(g["d"], g["e"]) for b, g in frame.groupby("b") if int(g["e"].sum()) >= MIN_EVENTS
        }
        overall = km_curve(frame["d"], frame["e"]) if int(frame["e"].sum()) >= MIN_EVENTS else None
        return cls(customers, businesses, overall)

    def pick(self, business_id: str, customer_id: str) -> tuple[SurvivalCurve, Source] | None:
        cust = self.customers.get((business_id, customer_id))
        if cust is not None:
            return cust, "customer"
        biz = self.businesses.get(business_id)
        if biz is not None:
            return biz, "business"
        if self.overall is not None:
            return self.overall, "global"
        return None
