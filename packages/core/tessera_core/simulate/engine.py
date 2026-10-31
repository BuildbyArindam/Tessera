"""simulate() and replay(): judge a proposal against historical FX before anything runs.

simulate(proposal, fx_history, as_of, n_paths, seed, *, due_date, config)
    1. Read the exposure from the proposal (exposure.py) and the dates from the CALLER.
    2. Slice the pair's history to `as_of` (series.up_to) so no later observation can be seen.
    3. Block-bootstrap future paths to `due_date` (bootstrap.py), seeded, vectorised.
    4. Score each path against the comparator (scenario.py) and summarise.

`due_date` is a keyword argument that comes from the obligation the worker is working on, NEVER
from the proposal. If the model could name the due date it could set it equal to today and make the
worst-case loss zero, which would switch the worst_case_loss_gate off.

Deterministic: the same inputs and seed give an identical report. `replay` deliberately reads
history AFTER `as_of` (up to `due_date`): it answers "what would this proposal have done?" for
backtests and shares the cost model, but it is a different function with a different result type.
"""

from collections.abc import Mapping
from datetime import date, datetime

import numpy as np

from tessera_core.domain.actions import Proposal
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money
from tessera_core.simulate.bootstrap import block_bootstrap_log_returns
from tessera_core.simulate.config import SimulationConfig
from tessera_core.simulate.exposure import Exposure, exposure_from_proposal
from tessera_core.simulate.report import ReplayReport, SimulationReport
from tessera_core.simulate.scenario import comparator_name, evaluate_outcomes, to_money
from tessera_core.simulate.series import FxSeries, SimulationInputError

MIN_RETURNS = 60  # same floor as the Day 5 FX range
MAX_HORIZON_DAYS = 365
MIN_PATHS, MAX_PATHS = 100, 200_000
TRADING_DAYS = 252  # calendar days -> business-day steps at 252/365, the Day 5 convention
NO_FX_NOTE = "no_fx_conversion_needed"


def _steps(days: int) -> int:
    return 0 if days == 0 else max(1, round(days * TRADING_DAYS / 365))


def _check_dates(as_of: date, due_date: date) -> int:
    for name, value in (("as_of", as_of), ("due_date", due_date)):
        if not isinstance(value, date) or isinstance(value, datetime):
            raise SimulationInputError(f"{name} must be a calendar date, not a datetime")
    days = (due_date - as_of).days
    if days < 0:
        raise SimulationInputError("due_date must not be before as_of")
    if days > MAX_HORIZON_DAYS:
        raise SimulationInputError(f"due_date is more than {MAX_HORIZON_DAYS} days after as_of")
    return days


def _series_for(exposure: Exposure, fx_history: Mapping[str, FxSeries]) -> FxSeries:
    series = fx_history.get(exposure.pair)
    if series is None:
        raise SimulationInputError(f"no FX history supplied for {exposure.pair}")
    if series.pair != exposure.pair:
        raise SimulationInputError(f"history under {exposure.pair} is labelled {series.pair}")
    return series


def _zero(currency: Currency) -> Money:
    return Money.zero(currency)


def simulate(
    proposal: Proposal,
    fx_history: Mapping[str, FxSeries],
    as_of: date,
    n_paths: int,
    seed: int,
    *,
    due_date: date,
    config: SimulationConfig | None = None,
) -> SimulationReport:
    cfg = config or SimulationConfig()
    exposure = exposure_from_proposal(proposal)
    days = _check_dates(as_of, due_date)
    if not MIN_PATHS <= n_paths <= MAX_PATHS:
        raise SimulationInputError(f"n_paths must be between {MIN_PATHS} and {MAX_PATHS}")
    if seed < 0:
        raise SimulationInputError("seed must not be negative")
    steps = _steps(days)

    common = {
        "proposal_id": proposal.id,
        "action_type": exposure.action,
        "comparator": comparator_name(exposure.action),
        "amount": exposure.amount_money,
        "sell_currency": exposure.sell,
        "as_of": as_of,
        "due_date": due_date,
        "horizon_days": days,
        "steps": steps,
        "n_paths": n_paths,
        "seed": seed,
        "block_length": cfg.block_length,
    }
    if not exposure.needs_fx:
        zero = _zero(exposure.sell)
        return SimulationReport(
            **common, pair=None, n_returns=0, spot=None, expected_saving=zero, saving_std=zero, saving_p05=zero,
            saving_p50=zero, worst_case_loss=zero, prob_beats_baseline=0.0, spread_cost=zero, fee_cost=zero,
            notes=(NO_FX_NOTE,),
        )  # fmt: skip

    visible = _series_for(exposure, fx_history).up_to(as_of)  # nothing after as_of survives this line
    _, spot0 = visible.latest_on_or_before(as_of)
    returns = visible.log_returns()
    if len(returns) < MIN_RETURNS:
        raise SimulationInputError(f"need at least {MIN_RETURNS} daily returns before {as_of}, got {len(returns)}")

    rng = np.random.default_rng(seed)
    cumulative = block_bootstrap_log_returns(returns, steps, n_paths, cfg.block_length, rng, cfg.demean)
    outcome = evaluate_outcomes(exposure, spot0, spot0 * np.exp(cumulative), cfg)

    saving = outcome.saving
    p05 = float(np.quantile(saving, 0.05))
    sell = exposure.sell
    return SimulationReport(
        **common,
        pair=exposure.pair,
        n_returns=len(returns),
        spot=spot0,
        expected_saving=to_money(float(saving.mean()), sell),
        saving_std=to_money(float(saving.std(ddof=1)), sell),
        saving_p05=to_money(p05, sell),
        saving_p50=to_money(float(np.quantile(saving, 0.5)), sell),
        worst_case_loss=to_money(max(0.0, -p05), sell),
        prob_beats_baseline=float(np.mean(saving > 0.0)),
        spread_cost=outcome.spread_cost,
        fee_cost=outcome.fee_cost,
    )


def replay(
    proposal: Proposal,
    fx_history: Mapping[str, FxSeries],
    as_of: date,
    *,
    due_date: date,
    config: SimulationConfig | None = None,
) -> ReplayReport:
    """Run the proposal against the rates that actually occurred between `as_of` and `due_date`."""
    cfg = config or SimulationConfig()
    exposure = exposure_from_proposal(proposal)
    _check_dates(as_of, due_date)
    common = {
        "proposal_id": proposal.id,
        "action_type": exposure.action,
        "comparator": comparator_name(exposure.action),
        "amount": exposure.amount_money,
        "sell_currency": exposure.sell,
        "as_of": as_of,
        "due_date": due_date,
    }
    if not exposure.needs_fx:
        zero = _zero(exposure.sell)
        return ReplayReport(
            **common, pair=None, spot_day=None, due_day=None, spot=None, spot_at_due=None, realised_saving=zero,
            spread_cost=zero, fee_cost=zero, notes=(NO_FX_NOTE,),
        )  # fmt: skip

    series = _series_for(exposure, fx_history)
    spot_day, spot0 = series.latest_on_or_before(as_of)
    due_day, spot_due = series.latest_on_or_before(due_date)
    outcome = evaluate_outcomes(exposure, spot0, np.array([spot_due]), cfg)
    return ReplayReport(
        **common,
        pair=exposure.pair,
        spot_day=spot_day,
        due_day=due_day,
        spot=spot0,
        spot_at_due=spot_due,
        realised_saving=to_money(float(outcome.saving[0]), exposure.sell),
        spread_cost=outcome.spread_cost,
        fee_cost=outcome.fee_cost,
    )
