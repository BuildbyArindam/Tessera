"""Replay the full Tessera decision path over the held-out FX path.

For every obligation (a supplier payout due in AUD or SGD, funded from USD/EUR/GBP) Tessera gets one decision a week
(each Sunday from the invoice until the day before it is due). At each decision:
  1. a SCRIPTED planner (the stand-in for the LLM, constants fixed a priori in eval_config.PlannerConstants) asks the
     Day 5 FX range for P(the sell currency loses 2% against the buy currency before the due date) and, if that is at
     least 20%, proposes a CONVERT of 40% of what is still to be bought;
  2. the Day 8 simulator scores that proposal on FX history up to the decision date only (no later rate is visible);
  3. the REAL policy engine and the shipped YAML ruleset (13 rules) judge it with a full PolicyContext;
  4. APPROVE executes at the mid rate of the last observation on or before the decision date, paying spread and fee;
     ESCALATE and BLOCK do nothing (nobody is there to approve in a replay: the autonomous path only).
Whatever is still unconverted is converted on the due date, exactly as the due-date baseline would.

Costs are in the SELL currency: spread bps of the converted value plus a fixed fee per conversion (assumptions, not
Airwallex prices). The two baselines buy the whole payout in one conversion: on the invoice date, or on the due date.
Decisions are replayable: seeds derive from (seed, obligation, date), never from the variant being run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd
from tessera_core.domain.actions import ActionType, Proposal, Verdict
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money
from tessera_core.policy import AutonomyLevel, ForecastConfidence, InputScreening, PolicyContext, Ruleset, evaluate
from tessera_core.risk.fx_range import estimate_fx_range
from tessera_core.simulate import SimulationConfig, simulate

from tessera_evalkit.eval_config import AUTONOMY_LEVEL, HORIZONS, PLANNER, RANGE_SIMS, SIM_PATHS, PlannerConstants
from tessera_evalkit.eval_types import Costs, Decision, Result, single_conversion
from tessera_evalkit.eval_world import FxWorld, Obligation, balance_proxy
from tessera_evalkit.rng import rng_for

CENT = Decimal("0.01")


def p_adverse(ob: Obligation, day: pd.Timestamp, world: FxWorld, drop_pct: float, seed: int) -> float:
    """P(the sell currency loses `drop_pct`% against the buy currency before the due date), from rates up to `day`."""
    series = world.series(ob.funding, ob.buy)
    as_of = day.date()
    fx = estimate_fx_range(
        series.up_to(as_of).values,
        pair=f"{ob.funding}{ob.buy}",
        horizon_days=(ob.due - day).days,
        drop_thresholds_pct=(drop_pct,),
        n_sims=RANGE_SIMS,
        seed=_seed(seed, "range", ob.id, str(as_of)),
    )
    return fx.p_fall[0].probability


def dev_p_adverse(
    obligations: list[Obligation], world: FxWorld, drop_pct: float, seed: int, start: pd.Timestamp
) -> list[float]:
    """The planner's own input on a DEVELOPMENT period: no savings outcome is computed or read."""
    return [p_adverse(o, d, world, drop_pct, seed) for o in obligations for d in decision_days(o, start)]


def _seed(base: int, *labels: str | int) -> int:
    return int(rng_for(base, *labels).integers(0, 2**31 - 1))


def decision_days(ob: Obligation, start: pd.Timestamp) -> list[pd.Timestamp]:
    first = max(ob.issue, start)
    return [d for d in pd.date_range(first, ob.due - pd.Timedelta(days=1), freq="W-SUN")]


def _context(
    ob: Obligation,
    day: pd.Timestamp,
    now: datetime,
    balance: float,
    spend: float,
    approvals: list[datetime],
    spot: float,
    confidence: float | None,
    sim: object,
    level: int,
) -> PolicyContext:
    sell = Currency(ob.funding)
    pair = f"{ob.funding}/{ob.buy}"
    rate = Decimal(repr(spot))
    return PolicyContext(
        balances={sell: Money(amount=Decimal(str(round(balance, 2))), currency=sell)},
        spend_today={sell: Money(amount=Decimal(str(round(spend, 2))), currency=sell)},
        beneficiary_allowlist=frozenset(),
        forecast_confidence=None
        if confidence is None
        else {sell: ForecastConfidence(relative_width=Decimal(repr(confidence)))},
        fx_reference_rate={pair: rate},
        fx_quote_rate={
            pair: rate
        },  # no intraday quotes exist: the executable quote IS the reference, so the slippage guard never fires here
        autonomy_level=AutonomyLevel(level),
        kill_switch_engaged=False,
        now=now,
        beneficiary_added_at={},
        recent_approval_times=tuple(approvals),
        card_loaded_this_month={},
        simulation=sim,
        input_screening=InputScreening(flag_codes=()),
        obligation_due_dates={ob.id: ob.due.date()},
    )


def forecast_width(table: pd.DataFrame, ob: Obligation, day: pd.Timestamp) -> float | None:
    """(upper - lower) / |point| of the funding currency's forecast, for the horizon that covers the wait."""
    weeks = max(1, -(-(ob.due - day).days // 7))
    h = next((x for x in HORIZONS if x >= weeks), HORIZONS[-1])
    try:
        r = table.loc[(ob.business, ob.funding, day, h)]
    except KeyError:
        return None
    point = float(r["point"])
    return None if point == 0.0 else (float(r["upper"]) - float(r["lower"])) / abs(point)


def replay_policy(
    obligations: list[Obligation],
    world: FxWorld,
    ruleset: Ruleset,
    costs: Costs,
    cashflows: pd.DataFrame,
    forecasts: pd.DataFrame,
    start: pd.Timestamp,
    seed: int,
    planner: PlannerConstants = PLANNER,
    level: int = AUTONOMY_LEVEL,
) -> list[Result]:
    cfg = SimulationConfig(spread_bps=costs.spread_bps, fee=costs.fee)
    state = {o.id: _new_result(o, world, costs) for o in obligations}
    remaining = {o.id: o.amount for o in obligations}
    paid = {o.id: 0.0 for o in obligations}
    spread = {o.id: 0.0 for o in obligations}
    n_conv = {o.id: 0 for o in obligations}
    spend: dict[tuple[str, pd.Timestamp], float] = {}
    approvals: dict[str, list[datetime]] = {}
    agenda = sorted(
        ((d, o) for o in obligations for d in decision_days(o, start)), key=lambda t: (t[0], t[1].business, t[1].id)
    )
    seq: dict[pd.Timestamp, int] = {}
    for day, ob in agenda:
        need = remaining[ob.id]
        if need < 0.01:
            continue
        series = world.series(ob.funding, ob.buy)
        as_of = day.date()
        spot = series.latest_on_or_before(as_of)[1]
        rates = series.up_to(as_of).values
        days_left = (ob.due - day).days
        fx = estimate_fx_range(
            rates,
            pair=f"{ob.funding}{ob.buy}",
            horizon_days=days_left,
            drop_thresholds_pct=(planner.drop_threshold_pct,),
            n_sims=RANGE_SIMS,
            seed=_seed(seed, "range", ob.id, str(as_of)),
        )
        p_adv = fx.p_fall[0].probability
        if planner.min_p_adverse is None:
            raise ValueError("planner.min_p_adverse must be set (calibrate_planner) before replaying")
        if p_adv < planner.min_p_adverse:
            continue
        amt = (Decimal(repr(planner.fraction * need / (spot * (1.0 - costs.s)))) + costs.fee).quantize(
            CENT, ROUND_HALF_UP
        )
        if amt <= costs.fee:
            continue
        seq[day] = seq.get(day, 0) + 1
        now = datetime(day.year, day.month, day.day, 12, tzinfo=UTC) + timedelta(minutes=seq[day])
        prop = Proposal(
            id=f"prp-{ob.id}-{day:%Y%m%d}",
            action_type=ActionType.CONVERT,
            params={"amount": str(amt), "currency": ob.funding, "to_currency": ob.buy, "obligation_id": ob.id},
            rationale=f"scripted planner: P(adverse move) {p_adv:.2f}",
            confidence=min(0.99, max(0.01, p_adv)),
            created_at=now,
        )
        report = simulate(
            prop,
            {f"{ob.funding}/{ob.buy}": series},
            as_of,
            SIM_PATHS,
            _seed(seed, "sim", ob.id, str(as_of)),
            due_date=ob.due.date(),
            config=cfg,
        )
        eur = 1.0 / world.per_eur(ob.funding, day)
        width = forecast_width(forecasts, ob, day)
        ctx = _context(
            ob,
            day,
            now,
            balance_proxy(cashflows, ob.business, ob.funding, day),
            spend.get((ob.business, day), 0.0),
            approvals.get(ob.business, []),
            spot,
            width,
            report.summary(),
            level,
        )
        verdict = evaluate(prop, ctx, ruleset)
        state[ob.id].decisions.append(
            Decision(
                ob.id,
                day,
                ob.funding,
                p_adv,
                float(amt),
                verdict.verdict.value,
                verdict.rule_ids,
                float(report.worst_case_loss.amount),
                eur,
                rel_width=width,
            )
        )
        if verdict.verdict is not Verdict.APPROVE:
            continue
        net = float(amt) - costs.f
        bought = net * spot * (1.0 - costs.s)
        remaining[ob.id] -= bought
        paid[ob.id] += float(amt)
        spread[ob.id] += net * costs.s
        n_conv[ob.id] += 1
        state[ob.id].n_early += 1
        due_rate = series.latest_on_or_before(ob.due.date())[1]
        state[ob.id].decisions[-1].tranche_saving = bought / (due_rate * (1.0 - costs.s)) + costs.f - float(amt)
        spend[(ob.business, day)] = spend.get((ob.business, day), 0.0) + float(amt)
        approvals.setdefault(ob.business, []).append(now)
    for o in obligations:
        r = state[o.id]
        left = remaining[o.id]
        due_sell, due_spread = (0.0, 0.0)
        if left >= 0.01:
            due_sell, due_spread = single_conversion(
                left, world.series(o.funding, o.buy).latest_on_or_before(o.due.date())[1], costs
            )
            n_conv[o.id] += 1
        r.sell_tessera, r.spread_tessera, r.n_conversions_tessera = (
            paid[o.id] + due_sell,
            spread[o.id] + due_spread,
            n_conv[o.id],
        )
    return [state[o.id] for o in obligations]


def _new_result(o: Obligation, world: FxWorld, costs: Costs) -> Result:
    s = world.series(o.funding, o.buy)
    sell_due, sp_due = single_conversion(o.amount, s.latest_on_or_before(o.due.date())[1], costs)
    sell_inv, sp_inv = single_conversion(o.amount, s.latest_on_or_before(o.issue.date())[1], costs)
    return Result(
        ob=o,
        sell_due=sell_due,
        sell_invoice=sell_inv,
        sell_tessera=0.0,
        spread_due=sp_due,
        spread_invoice=sp_inv,
        spread_tessera=0.0,
        n_conversions_tessera=0,
        eur_per_sell=1.0 / world.per_eur(o.funding, o.due),
    )
