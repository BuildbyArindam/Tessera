"""Day 8: simulate() and replay() behave as the verification prompt requires.

Covers: same seed => identical report; different seeds agree within a stated tolerance; a flat rate
gives exactly minus the cost with zero variance; a proposal identical to the baseline saves nothing;
no observation after as_of is ever used; 5000 paths take well under 2 seconds; replay matches
hand-computed numbers.
"""

import time
from datetime import timedelta
from decimal import Decimal

import numpy as np
import pytest
from sim_helpers import PAIR, business_days, constant_series, convert, hold, make_series, pay, rebuilt, scaled_after
from tessera_core.domain import Currency, Money
from tessera_core.risk.fx_range import horizon_steps
from tessera_core.simulate import SimulationConfig, replay, simulate

N = 5000
USD = Currency.USD


def usd(amount: str) -> Money:
    return Money(amount=amount, currency=USD)


def cents(m: Money) -> Decimal:
    return m.amount


# ---- determinism --------------------------------------------------------------------------
def test_same_seed_gives_an_identical_report_and_different_seeds_do_not() -> None:
    series = make_series()
    as_of = series.last_day - timedelta(days=200)
    run = [
        simulate(convert(), {PAIR: series}, as_of, N, seed, due_date=as_of + timedelta(days=30)) for seed in (7, 7, 8)
    ]
    assert run[0] == run[1]
    assert run[0].model_dump_json() == run[1].model_dump_json()
    assert run[0].expected_saving != run[2].expected_saving


def test_different_seeds_agree_within_the_stated_tolerance_at_5000_paths() -> None:
    """Tolerance: means within 4 standard errors of a difference; 5th percentiles within 0.25 sigma."""
    series = make_series()
    as_of = series.last_day - timedelta(days=200)
    reports = [simulate(convert(), {PAIR: series}, as_of, N, s, due_date=as_of + timedelta(days=30)) for s in range(10)]
    sigma = float(np.mean([float(r.saving_std.amount) for r in reports]))
    means = [float(r.expected_saving.amount) for r in reports]
    p05s = [float(r.saving_p05.amount) for r in reports]
    assert max(means) - min(means) <= 4 * np.sqrt(2.0) * sigma / np.sqrt(N), (means, sigma)
    assert max(p05s) - min(p05s) <= 0.25 * sigma, (p05s, sigma)
    assert sigma > 0  # the series is genuinely volatile, so the comparison above is not vacuous


# ---- a flat rate: saving is exactly minus the cost, with no variance --------------------------
@pytest.fixture
def flat() -> tuple[dict[str, object], object]:
    series = constant_series(n=200, level=0.9)
    return {PAIR: series}, business_days(200)[150]


def test_flat_rate_convert_saving_is_exactly_minus_the_spread(flat) -> None:  # type: ignore[no-untyped-def]
    history, as_of = flat
    r = simulate(convert(), history, as_of, N, 3, due_date=as_of + timedelta(days=30))
    assert r.expected_saving == usd("-50.00")  # 10,000 x 0.50%
    assert r.saving_std == usd("0.00") and r.saving_p05 == usd("-50.00") and r.saving_p50 == usd("-50.00")
    assert r.worst_case_loss == usd("50.00") and r.prob_beats_baseline == 0.0
    assert r.spread_cost == usd("50.00") and r.fee_cost == usd("0.00")


def test_flat_rate_saving_equals_minus_spread_plus_fee(flat) -> None:  # type: ignore[no-untyped-def]
    history, as_of = flat
    cfg = SimulationConfig(spread_bps="25", fee="2.50")
    r = simulate(convert(), history, as_of, N, 3, due_date=as_of + timedelta(days=30), config=cfg)
    assert abs(r.expected_saving.amount + r.spread_cost.amount + r.fee_cost.amount) <= Decimal("0.01")
    assert r.fee_cost == usd("2.50") and r.saving_std == usd("0.00")


def test_flat_rate_pay_supplier_saving_is_exactly_minus_the_cost(flat) -> None:  # type: ignore[no-untyped-def]
    history, as_of = flat
    r = simulate(pay(), history, as_of, N, 3, due_date=as_of + timedelta(days=30))
    # 9,000 EUR at 0.9 EUR per USD needs 10,000 USD at mid; with a 0.5% spread it needs 10,050.25.
    assert r.expected_saving == usd("-50.25") and r.spread_cost == usd("50.25") and r.saving_std == usd("0.00")
    assert r.sell_currency is USD and r.pair == PAIR


def test_flat_rate_hold_costs_nothing_to_wait(flat) -> None:  # type: ignore[no-untyped-def]
    history, as_of = flat
    r = simulate(hold(), history, as_of, N, 3, due_date=as_of + timedelta(days=30))
    assert r.expected_saving == usd("0.00") and r.saving_std == usd("0.00") and r.worst_case_loss == usd("0.00")
    assert r.comparator == "convert_now"


# ---- identical to the baseline ------------------------------------------------------------
def test_a_proposal_identical_to_its_baseline_saves_exactly_nothing() -> None:
    """Due today and no costs: converting now IS converting on the due date, on every path."""
    series = make_series()
    as_of = series.last_day - timedelta(days=200)
    free = SimulationConfig(spread_bps="0", fee="0")
    r = simulate(convert(), {PAIR: series}, as_of, N, 5, due_date=as_of, config=free)
    assert r.steps == 0
    zero = usd("0.00")
    assert (r.expected_saving, r.saving_std, r.saving_p05, r.worst_case_loss) == (zero, zero, zero, zero)
    assert r.prob_beats_baseline == 0.0


def test_waiting_until_today_is_converting_now_even_with_costs() -> None:
    series = make_series()
    as_of = series.last_day - timedelta(days=200)
    r = simulate(hold(), {PAIR: series}, as_of, N, 5, due_date=as_of)
    assert r.expected_saving == usd("0.00") and r.saving_std == usd("0.00")


def test_a_volatile_horizon_has_a_loss_tail_and_a_win_probability_between_zero_and_one() -> None:
    series = make_series(daily_vol=0.01)
    as_of = series.last_day - timedelta(days=200)
    r = simulate(convert(), {PAIR: series}, as_of, N, 5, due_date=as_of + timedelta(days=60))
    assert r.saving_p05 < usd("-50.00") < r.saving_p50 + usd("100.00")
    assert 0.0 < r.prob_beats_baseline < 1.0
    assert r.worst_case_loss == -r.saving_p05 and r.steps == horizon_steps(60) == 41  # round(60 x 252 / 365)


# ---- no look-ahead ------------------------------------------------------------------------
def test_simulate_ignores_every_observation_after_as_of() -> None:
    series = make_series()
    as_of = business_days(800)[600]
    due = as_of + timedelta(days=30)
    base = simulate(convert(), {PAIR: series}, as_of, N, 9, due_date=due)
    for factor in (0.1, 3.0, 25.0):
        assert simulate(convert(), {PAIR: scaled_after(series, as_of, factor)}, as_of, N, 9, due_date=due) == base
    assert simulate(convert(), {PAIR: series.up_to(as_of)}, as_of, N, 9, due_date=due) == base


def test_the_no_look_ahead_test_can_fail_history_up_to_as_of_does_matter() -> None:
    series = make_series()
    days = business_days(800)
    as_of, due = days[600], days[600] + timedelta(days=30)
    base = simulate(convert(), {PAIR: series}, as_of, N, 9, due_date=due)
    bumped = series.values.copy()
    bumped[600] *= 1.05  # the observation ON as_of
    assert simulate(convert(), {PAIR: rebuilt(series, bumped)}, as_of, N, 9, due_date=due).spot != base.spot
    noisy = series.values.copy()
    noisy[:300] *= np.exp(np.random.default_rng(0).normal(0, 0.02, 300))  # older history changes the returns
    assert simulate(convert(), {PAIR: rebuilt(series, noisy)}, as_of, N, 9, due_date=due) != base


def test_replay_does_use_the_realised_future_and_simulate_does_not() -> None:
    series = make_series()
    days = business_days(800)
    as_of, due = days[600], days[630]
    shocked = scaled_after(series, as_of, 1.2)
    a = replay(convert(), {PAIR: series}, as_of, due_date=due)
    b = replay(convert(), {PAIR: shocked}, as_of, due_date=due)
    assert a.realised_saving != b.realised_saving and a.spot == b.spot and a.spot_at_due != b.spot_at_due


# ---- speed ---------------------------------------------------------------------------------
def test_5000_paths_over_90_days_run_in_under_two_seconds() -> None:
    series = make_series()
    as_of = series.last_day - timedelta(days=200)
    started = time.perf_counter()
    simulate(convert(), {PAIR: series}, as_of, N, 1, due_date=as_of + timedelta(days=90))
    assert time.perf_counter() - started < 2.0


# ---- replay against hand-computed numbers -------------------------------------------------
def _stepped(factor: float):  # type: ignore[no-untyped-def]
    """200 flat days at 0.9, then `factor` x that from day 130 on. Returns (history, as_of, due)."""
    base = constant_series(n=200, level=0.9)
    values = base.values.copy()
    values[130:] *= factor
    days = business_days(200)
    return {PAIR: rebuilt(base, values)}, days[100], days[130]


def test_replay_convert_against_a_10_percent_rise() -> None:
    history, as_of, due = _stepped(1.1)
    r = replay(convert(), history, as_of, due_date=due)
    # (10,000 x 0.9 x 0.995 - 10,000 x 0.99) / 0.9 = -1,050: converting early missed the rise and paid the spread.
    assert r.realised_saving == usd("-1050.00") and r.spot_day == as_of and r.due_day == due
    assert r.spot == pytest.approx(0.9) and r.spot_at_due == pytest.approx(0.99)


def test_replay_hold_against_a_10_percent_rise_and_a_10_percent_fall() -> None:
    up, as_of, due = _stepped(1.1)
    assert replay(hold(), up, as_of, due_date=due).realised_saving == usd("995.00")  # 10,000 x 0.995 x 0.1
    down, as_of, due = _stepped(0.9)
    assert replay(hold(), down, as_of, due_date=due).realised_saving == usd("-995.00")


def test_replay_pay_supplier_against_a_10_percent_rise() -> None:
    history, as_of, due = _stepped(1.1)
    r = replay(pay(), history, as_of, due_date=due)
    expected = 9000 / 0.99 - 9000 / (0.9 * 0.995)  # USD needed on the due date minus USD needed now
    assert float(r.realised_saving.amount) == pytest.approx(expected, abs=0.01) and expected < -959


def test_horizon_steps_match_the_day_5_fx_range_convention() -> None:
    from tessera_core.simulate.engine import _steps

    assert [_steps(d) for d in (1, 7, 30, 60, 90, 180, 365)] == [horizon_steps(d) for d in (1, 7, 30, 60, 90, 180, 365)]
    assert _steps(0) == 0


# ---- drift: the default judges risk, not the past trend ---------------------------------------------
def test_default_config_removes_historical_drift_and_keep_drift_does_not() -> None:
    """A pair that rose 0.2% a day: waiting looks wonderful only if the trend is carried forward."""
    series = make_series(daily_vol=0.001, drift=0.002)
    as_of = series.last_day - timedelta(days=200)
    due = as_of + timedelta(days=30)
    centred = simulate(hold(), {PAIR: series}, as_of, N, 4, due_date=due)
    sigma = float(centred.saving_std.amount)
    assert abs(float(centred.expected_saving.amount)) < 4 * sigma / np.sqrt(N) + 0.01
    drifting = simulate(hold(), {PAIR: series}, as_of, N, 4, due_date=due, config=SimulationConfig(demean=False))
    assert drifting.expected_saving > usd("300.00")  # roughly 10,000 x 0.2% x 21 days
    # With the trend kept, even the 5th percentile is a gain, so the worst-case LOSS is zero, never negative.
    assert drifting.saving_p05 > usd("0.00") and drifting.worst_case_loss == usd("0.00")
