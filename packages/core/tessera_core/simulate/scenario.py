"""The money maths of one simulated outcome, shared by the Monte-Carlo and the replay paths.

float64 is used INSIDE this kernel only (a vectorised NumPy calculation over thousands of paths).
Amounts come in as Decimal and every figure leaves as Money, so no float crosses the boundary.

Notation: S = sell units, P = buy units, r0 = mid rate now (buy per sell), rt = mid rate on the due
date (an array, one entry per path), s = spread as a fraction, f = fixed fee in sell units.
`saving` is always measured in the SELL currency and means "how much better the proposal did than
its comparator"; negative is a loss.

CONVERT   proposal: convert now      comparator: convert S on the due date at the plain mid rate
          buy_now = (S - f) * r0 * (1 - s)        buy_due = S * rt
          saving  = (buy_now - buy_due) / r0
PAY       proposal: fund the payout now          comparator: fund P on the due date at the plain mid rate
          sell_now = P / (r0 * (1 - s)) + f       sell_due = P / rt
          saving   = sell_due - sell_now
HOLD      proposal: wait, convert on the due date    comparator: convert now (both pay the same costs)
          saving = ((S - f) * (1 - s) * (rt - r0)) / r0

The CONVERT and PAY comparator is the frictionless "convert on the due date" benchmark, so the
proposal has to earn back its own spread and fee. With a flat rate the saving is exactly
-(spread cost + fee cost), which is how the zero-volatility check in the tests works. In HOLD both
sides convert once, so the cost cancels and only the rate move remains.
"""

import math
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from tessera_core.domain.actions import ActionType
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money
from tessera_core.simulate.config import SimulationConfig
from tessera_core.simulate.exposure import Exposure
from tessera_core.simulate.series import FloatArray, SimulationInputError


@dataclass(frozen=True, eq=False)
class Outcome:
    saving: FloatArray  # sell currency, one entry per path (one entry for a replay)
    spread_cost: Money  # what the proposal's conversion pays in spread, sell currency
    fee_cost: Money  # what it pays in fixed fee, sell currency


def to_money(value: float, currency: Currency) -> Money:
    """float64 result -> Money at the output boundary. Refuses NaN and infinity."""
    if not math.isfinite(value):
        raise SimulationInputError("simulation produced a non-finite amount")
    return Money(amount=Decimal(value), currency=currency)


def evaluate_outcomes(exposure: Exposure, spot0: float, spot_t: FloatArray, config: SimulationConfig) -> Outcome:
    s = float(config.spread_bps) / 10_000.0
    f = float(config.fee)
    amt = float(exposure.amount)
    action = exposure.action

    if action is ActionType.PAY_SUPPLIER:
        sell_now = amt / (spot0 * (1.0 - s)) + f
        saving = amt / spot_t - sell_now
        spread = amt / (spot0 * (1.0 - s)) - amt / spot0
    else:
        if amt <= f:
            raise SimulationInputError("amount does not cover the conversion fee")
        net = amt - f
        if action is ActionType.CONVERT:
            saving = (net * spot0 * (1.0 - s) - amt * spot_t) / spot0
        else:  # HOLD
            saving = net * (1.0 - s) * (spot_t - spot0) / spot0
        spread = net * s

    if not np.isfinite(saving).all():
        raise SimulationInputError("simulation produced a non-finite amount")
    return Outcome(
        saving=saving,
        spread_cost=to_money(spread, exposure.sell),
        fee_cost=to_money(f, exposure.sell),
    )


def comparator_name(action: ActionType) -> str:
    return "convert_now" if action is ActionType.HOLD else "convert_on_due_date"
