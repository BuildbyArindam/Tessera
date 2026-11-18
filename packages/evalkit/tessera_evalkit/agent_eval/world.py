"""Turn a Scenario into what the agent and the policy engine see: a PlannerSnapshot and a PolicyContext.

FX history is synthetic and seeded: `n` business days of normal log returns with the scenario's daily volatility,
anchored so the last observation (on AS_OF) equals the scenario's spot. The agent's `get_fx_range` tool and the
FX numbers printed for a labeller both come from `estimate_fx_range` on this same series, so they cannot differ.

The policy side reuses the red-team `World.policy_context` (the same code the worker must follow: simulation run by the
caller, due dates from the obligation records) and overrides what a scenario sets itself: balances and beneficiary
ages. The autonomy level is L3 on purpose, as in the red team: at L0 the level gate would stop everything and say
nothing about whether the OTHER rules contain an unsafe proposal.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from tessera_agent import Obligation, PlannerSnapshot
from tessera_core.domain import Balance, Beneficiary, Currency, Money, Proposal
from tessera_core.forecast import ForecastResult
from tessera_core.policy import PolicyContext
from tessera_core.risk.fx_range import estimate_fx_range
from tessera_core.simulate import FxSeries

from tessera_evalkit.agent_eval.scenario import FX_HORIZONS, Fx, FxRangeRow, Scenario
from tessera_evalkit.redteam.world import AS_OF, BUSINESS, NOW, World

SERIES_DAYS = 750
RANGE_SIMS = 20_000
BENEFICIARY_NEW_AGE = timedelta(hours=3)
BENEFICIARY_OLD = datetime(2026, 1, 5, tzinfo=UTC)


def money(amount: str, currency: str) -> Money:
    return Money(amount=Decimal(amount), currency=Currency(currency))


def make_series(fx: Fx, *, n: int = SERIES_DAYS) -> FxSeries:
    steps = np.random.default_rng(fx.seed).normal(0.0, fx.daily_vol_pct / 100.0, n)
    logs = np.concatenate([[0.0], np.cumsum(steps)])
    values = fx.spot * np.exp(logs - logs[-1])  # the last value is the spot, on AS_OF
    days: list[date] = []
    day = AS_OF
    while len(days) < n + 1:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    return FxSeries.from_observations(fx.pair, list(zip(days, values.tolist(), strict=True)))


def compute_ranges(fx: Fx) -> tuple[FxRangeRow, ...]:
    """The FX range table a labeller reads, from the same estimator and seed the agent's tool uses."""
    values = make_series(fx).values
    rows = []
    for horizon in FX_HORIZONS:
        r = estimate_fx_range(
            values, pair=fx.pair, horizon_days=horizon, drop_thresholds_pct=(1.0, 2.0), n_sims=RANGE_SIMS, seed=0
        )
        q = {p.level: p.change_pct for p in r.quantiles}
        rows.append(
            FxRangeRow(
                horizon_days=horizon,
                p05_pct=round(q[5], 2),
                p50_pct=round(q[50], 2),
                p95_pct=round(q[95], 2),
                p_fall_1pct=round(r.p_fall[0].probability, 3),
                p_fall_2pct=round(r.p_fall[1].probability, 3),
            )
        )
    return tuple(rows)


def _beneficiaries(s: Scenario) -> tuple[Beneficiary, ...]:
    out = []
    for b in s.beneficiaries:
        added = NOW - BENEFICIARY_NEW_AGE if b.status == "new" else BENEFICIARY_OLD
        out.append(
            Beneficiary(
                id=b.id, business_id=BUSINESS, name=f"Supplier {b.id}", currency=Currency(b.currency),
                added_at=added, approved=b.status != "unapproved",
            )
        )  # fmt: skip
    return tuple(out)


def _obligations(s: Scenario) -> tuple[Obligation, ...]:
    return tuple(
        Obligation(
            id=o.id, kind=o.kind, amount=money(o.amount, o.currency), due_on=AS_OF + timedelta(days=o.due_in_days),
            invoice_id=o.invoice_id, beneficiary_id=o.beneficiary_id,
        )
        for o in s.obligations
    )  # fmt: skip


def _forecasts(s: Scenario) -> tuple[ForecastResult, ...]:
    return tuple(
        ForecastResult(
            business_id=BUSINESS, currency=f.currency, horizon_weeks=h, point=Decimal(f.point) * h / 4,
            lower=Decimal(f.lower) * h / 4, upper=Decimal(f.upper) * h / 4,  # scaled linearly from the 4-week summary
            target_coverage=0.8, model_version="scenario-fixture",
        )
        for f in s.forecasts
        for h in (4, 8, 13)
    )  # fmt: skip


def _invoices(s: Scenario) -> pd.DataFrame:
    ids = [o.invoice_id for o in s.obligations if o.invoice_id]
    issued = pd.Timestamp(AS_OF) - pd.Timedelta(days=60)
    return pd.DataFrame(
        {
            "business_id": [BUSINESS] * len(ids),
            "invoice_id": ids,
            "issue_date": pd.Series([issued] * len(ids), dtype="datetime64[ns]"),  # typed even when empty
            "paid_date": pd.Series([pd.NaT] * len(ids), dtype="datetime64[ns]"),
        }
    )


class ScenarioWorld:
    """Everything built once per scenario: the world, the planner snapshot and the policy-context factory."""

    def __init__(self, s: Scenario) -> None:
        self.scenario = s
        self.world = World(
            beneficiaries=_beneficiaries(s), obligations=_obligations(s),
            fx_history={fx.pair: make_series(fx) for fx in s.fx}, forecasts=_forecasts(s), invoices=_invoices(s),
            business=BUSINESS,
        )  # fmt: skip
        self.balances = {Currency(c): money(a, c) for c, a in s.balances.items()}

    def snapshot(self) -> PlannerSnapshot:
        w = self.world
        return PlannerSnapshot(
            business_id=BUSINESS, as_of=AS_OF,
            balances=tuple(Balance(currency=c, available=m) for c, m in self.balances.items()),
            forecasts=w.forecasts, fx_history=w.fx_history, invoices=w.invoices, late_scorer=None,
            obligations=w.obligations, beneficiaries=w.beneficiaries, card_vendor_ids=frozenset(),
        )  # fmt: skip

    def policy_context(self, proposal: Proposal, flag_codes: tuple[str, ...] = ()) -> PolicyContext:
        ctx = self.world.policy_context(proposal, flag_codes)
        return ctx.model_copy(update={"balances": self.balances})
