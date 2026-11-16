"""Result types and the one-conversion cost formula shared by the replay and the savings tables."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from tessera_evalkit.eval_world import Obligation


@dataclass
class Decision:
    obligation: str
    day: pd.Timestamp
    sell: str
    p_adverse: float
    amount: float  # sell currency
    verdict: str
    rule_ids: tuple[str, ...]
    worst_case_loss: float  # simulated, sell currency
    eur_per_sell: float
    rel_width: float | None = None  # forecast interval width / |point| the gate saw (None: unavailable)
    tranche_saving: float | None = None  # sell currency, vs buying the same units on the due date; set if executed


@dataclass
class Result:
    ob: Obligation
    sell_due: float
    sell_invoice: float
    sell_tessera: float
    spread_due: float
    spread_invoice: float
    spread_tessera: float
    n_conversions_tessera: int
    eur_per_sell: float  # 1 sell unit in EUR on the due date
    n_early: int = 0  # approved conversions made before the due date
    decisions: list[Decision] = field(default_factory=list)


@dataclass(frozen=True)
class Costs:
    spread_bps: Decimal
    fee: Decimal

    @property
    def s(self) -> float:
        return float(self.spread_bps) / 10_000.0

    @property
    def f(self) -> float:
        return float(self.fee)


def single_conversion(buy_units: float, rate: float, costs: Costs) -> tuple[float, float]:
    """(sell units paid, spread cost in sell units) to buy `buy_units` in one conversion at mid `rate`."""
    net = buy_units / (rate * (1.0 - costs.s))
    return net + costs.f, net - buy_units / rate
