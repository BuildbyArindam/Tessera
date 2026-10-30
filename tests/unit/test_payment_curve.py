"""Kaplan-Meier payment curve and conditional date quantiles."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from tessera_core.risk.payment_curve import GRID_MIN, MAX_DPD, PaymentCurves, km_curve


def _s(curve, day: int) -> float:  # noqa: ANN001
    return float(curve.survival[day - GRID_MIN])


def test_km_matches_hand_calculation() -> None:
    # paid at dpd 1, 2, 2, 4; one invoice still unpaid at dpd 3 (censored)
    c = km_curve([1, 2, 2, 4, 3], [True, True, True, True, False])
    assert _s(c, 0) == pytest.approx(1.0)
    assert _s(c, 1) == pytest.approx(1 - 1 / 5)  # 5 at risk, 1 paid
    assert _s(c, 2) == pytest.approx(0.8 * (1 - 2 / 4))  # 4 at risk, 2 paid
    assert _s(c, 3) == pytest.approx(0.4)  # censoring alone does not move S
    assert _s(c, 4) == pytest.approx(0.0)  # the 1 at risk pays: S -> 0
    assert (c.n_events, c.n_obs) == (4, 5)


def test_censoring_matters_versus_dropping_unpaid() -> None:
    paid = [2] * 6
    naive = km_curve(paid, [True] * 6)
    with_unpaid = km_curve(paid + [30] * 6, [True] * 6 + [False] * 6)  # six still open after 30 days
    # dropping the unpaid makes everyone look quick; KM keeps them in the risk set
    assert _s(naive, 2) == 0.0
    assert _s(with_unpaid, 2) == pytest.approx(0.5)


def test_km_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError, match="equal length"):
        km_curve([1, 2], [True])


def test_conditional_quantiles_are_after_today_and_ordered() -> None:
    rng = np.random.default_rng(0)
    d = np.round(rng.lognormal(np.log(10), 0.5, 400)).astype(int)
    c = km_curve(d, np.ones(400, bool))
    for past in (-5, 0, 12, 25):
        days, unresolved = c.conditional_days(past, (0.1, 0.5, 0.9))
        assert not unresolved
        assert past < days[0] <= days[1] <= days[2]


def test_conditioning_on_survival_shifts_the_range_later() -> None:
    rng = np.random.default_rng(1)
    d = np.round(rng.lognormal(np.log(10), 0.6, 600)).astype(int)
    c = km_curve(d, np.ones(600, bool))
    early = c.conditional_days(0, (0.5,))[0][0]
    late = c.conditional_days(20, (0.5,))[0][0]
    assert late > early and late > 20


def test_unreachable_quantile_is_capped_and_flagged() -> None:
    # half the invoices are never paid: the curve plateaus at 0.5, so q90 is never reached
    c = km_curve([5] * 10 + [50] * 10, [True] * 10 + [False] * 10)
    days, unresolved = c.conditional_days(0, (0.4, 0.9))
    assert unresolved and days[1] == MAX_DPD and days[0] == 5


def test_fully_paid_out_curve_returns_tomorrow() -> None:
    c = km_curve([3, 4, 5], [True] * 3)
    days, unresolved = c.conditional_days(10, (0.1, 0.9))  # already past every observed payment
    assert days == (11, 11) and not unresolved


def test_pick_falls_back_customer_business_global_none() -> None:
    def frame(n: int, biz: str, cust: str) -> pd.DataFrame:
        due = pd.Timestamp("2025-01-01")
        return pd.DataFrame(
            {"business_id": biz, "customer_id": cust, "due_date": due, "paid_date": due + pd.Timedelta(days=5)},
            index=range(n),
        )

    masked = pd.concat([frame(6, "B1", "C1"), frame(3, "B1", "C2"), frame(6, "B2", "C3"), frame(2, "B3", "C4")])
    curves = PaymentCurves.fit(masked, pd.Timestamp("2025-06-01"))
    assert curves.pick("B1", "C1")[1] == "customer"
    assert curves.pick("B1", "C2")[1] == "business"  # 3 payments < 5, B1 pool has 9
    assert curves.pick("B3", "C4")[1] == "global"  # 2 payments, B3 pool too small
    empty = PaymentCurves.fit(frame(2, "B9", "C9"), pd.Timestamp("2025-06-01"))
    assert empty.pick("B9", "C9") is None
