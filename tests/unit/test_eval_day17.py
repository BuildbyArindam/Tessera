"""Day 17 evaluation pieces: bootstrap maths, cost accounting, ruleset surgery, metric formulas on tiny data."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from tessera_evalkit.eval_forecast import summarise
from tessera_evalkit.eval_replay import decision_days
from tessera_evalkit.eval_savings import ruleset_without
from tessera_evalkit.eval_stats import bootstrap_ratio, cluster_weights, paired_difference, verdict
from tessera_evalkit.eval_types import Costs, single_conversion
from tessera_evalkit.eval_world import Obligation


def test_cluster_weights_draw_exactly_n_clusters_per_replicate() -> None:
    w = cluster_weights(7, 50, np.random.default_rng(0))
    assert w.shape == (50, 7)
    assert (w.sum(axis=1) == 7).all()


def test_bootstrap_ratio_point_is_ratio_of_sums_and_interval_brackets_it() -> None:
    rng = np.random.default_rng(1)
    num, den = rng.normal(1.0, 0.5, 40), np.full(40, 10.0)
    point, (lo, hi) = bootstrap_ratio(num, den, cluster_weights(40, 2000, rng), scale=100.0)
    assert point == pytest.approx(100.0 * num.sum() / den.sum())
    assert lo < point < hi


def test_bootstrap_interval_covers_zero_for_pure_noise_and_excludes_it_for_a_real_effect() -> None:
    rng = np.random.default_rng(2)
    den = np.ones(200)
    w = cluster_weights(200, 2000, rng)
    noise = rng.normal(0.0, 1.0, 200)
    noise -= noise.mean()  # exactly zero mean
    assert bootstrap_ratio(noise, den, w)[1][0] < 0 < bootstrap_ratio(noise, den, w)[1][1]
    lo, _ = bootstrap_ratio(rng.normal(1.0, 1.0, 200), den, w)[1]
    assert lo > 0


def test_paired_difference_uses_shared_resamples() -> None:
    w = cluster_weights(30, 500, np.random.default_rng(3))
    x = np.random.default_rng(4).normal(size=30)
    d, (lo, hi) = paired_difference(x, np.ones(30), x, np.ones(30), w)
    assert (
        d == 0.0 and lo == 0.0 and hi == 0.0
    )  # identical arms: a paired interval collapses, an unpaired one would not


def test_verdict_words() -> None:
    assert verdict((0.1, 0.3)).startswith("positive")
    assert verdict((-0.3, -0.1)).startswith("negative")
    assert "includes zero" in verdict((-0.1, 0.2))


def test_single_conversion_pays_spread_and_fee_and_nothing_else() -> None:
    costs = Costs(Decimal(50), Decimal("2.00"))
    paid, spread = single_conversion(1000.0, 0.5, costs)
    assert paid == pytest.approx(1000.0 / (0.5 * 0.995) + 2.0)
    assert spread == pytest.approx(1000.0 / (0.5 * 0.995) - 2000.0)  # sell-currency value lost to the spread
    zero_paid, zero_spread = single_conversion(1000.0, 0.5, Costs(Decimal(0), Decimal(0)))
    assert zero_paid == pytest.approx(2000.0) and zero_spread == pytest.approx(0.0)


def test_decision_days_are_sundays_from_invoice_to_the_day_before_due() -> None:
    ob = Obligation("o", "b", "AUD", 100.0, pd.Timestamp("2026-02-03"), pd.Timestamp("2026-03-05"), "USD")
    days = decision_days(ob, pd.Timestamp("2026-01-04"))
    assert all(d.day_of_week == 6 for d in days)
    assert days[0] >= ob.issue and days[-1] < ob.due
    assert len(days) == 4
    assert decision_days(ob, pd.Timestamp("2026-02-20"))[0] >= pd.Timestamp("2026-02-20")


def test_ruleset_without_removes_only_the_named_rules_and_changes_the_hash() -> None:
    full, no_fc = ruleset_without(), ruleset_without("forecast_confidence_gate")
    assert len(full.rules) == len(no_fc.rules) + 1
    assert {r.rule_id for r in full.rules} - {r.rule_id for r in no_fc.rules} == {"forecast_confidence_gate"}
    assert full.hash != no_fc.hash
    with pytest.raises(ValueError, match="no such rule"):
        ruleset_without("not_a_rule")


def test_loss_ceiling_scale_changes_the_hash_and_keeps_the_rule_count() -> None:
    assert ruleset_without(loss_ceiling_scale=0.1).hash != ruleset_without().hash
    assert len(ruleset_without(loss_ceiling_scale=0.1).rules) == len(ruleset_without().rules)


def _rows() -> pd.DataFrame:
    base = {"currency": "USD", "horizon": 4, "model": "m", "as_of": pd.Timestamp("2026-01-04")}
    recs = []
    for b, (y, pt, lo, up) in {
        "b1": (10.0, 8.0, 5.0, 12.0),
        "b2": (-4.0, 0.0, -3.0, 3.0),
        "b3": (6.0, 6.0, 4.0, 9.0),
    }.items():
        recs.append({**base, "business_id": b, "y": y, "point": pt, "lower": lo, "upper": up})
    return pd.DataFrame(recs)


def test_summarise_matches_hand_computed_wape_bias_coverage_width() -> None:
    s = summarise(_rows(), 0.8, 200, 0)
    r = s[(s["currency"] == "USD")].iloc[0]
    assert r["wape"] == pytest.approx((2 + 4 + 0) / (10 + 4 + 6))
    assert r["bias"] == pytest.approx((-2 + 4 + 0) / 20)
    assert r["coverage"] == pytest.approx(2 / 3)  # b2's -4 is outside [-3, 3]
    assert r["mean_width"] == pytest.approx((7 + 6 + 5) / 3)
    all_row = s[s["currency"] == "ALL"].iloc[0]
    assert all_row["wape"] == pytest.approx(r["wape"])  # one currency: the macro average is that currency


def test_all_row_is_the_unweighted_mean_of_currencies_not_a_pooled_sum() -> None:
    a, b = _rows(), _rows()
    b["currency"], b["y"], b["point"], b["lower"], b["upper"] = (
        "INR",
        b["y"] * 1000,
        b["point"] * 1000,
        b["lower"] * 1000,
        b["upper"] * 1000,
    )
    s = summarise(pd.concat([a, b], ignore_index=True), 0.8, 200, 0)
    per = s[s["currency"] != "ALL"].set_index("currency")
    assert s[s["currency"] == "ALL"].iloc[0]["wape"] == pytest.approx(per["wape"].mean())
