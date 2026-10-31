"""PlannerSnapshot: the frozen, read-only world one planning run sees, and the context JSON built from it.

The worker assembles a snapshot (balances from the Airwallex port, forecasts, FX history, invoices,
obligations) and hands it over; the agent package never fetches anything itself. Nothing in a snapshot
is ever written back, and the tools only read from it.

No free text enters the context today: no customer or counterparty names, no invoice notes. Only ids,
currencies, amounts and dates. Untrusted text, once it is passed at all, goes through the Day 10 sanitiser
and a delimited data block.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd
from pydantic import JsonValue
from tessera_core.domain import Balance, Beneficiary, Currency, Money
from tessera_core.domain.base import DomainModel
from tessera_core.forecast import ForecastResult
from tessera_core.risk import LateScorer
from tessera_core.simulate import FxSeries

from tessera_agent.schemas import MAX_PROPOSALS

ObligationKind = Literal["RECEIVABLE", "PAYABLE"]
CONTEXT_VERSION = 1


class Obligation(DomainModel):
    """Money expected in (RECEIVABLE, a customer invoice) or due out (PAYABLE). Ids and numbers only."""

    id: str
    kind: ObligationKind
    amount: Money
    due_on: date
    invoice_id: str | None = None  # receivables
    beneficiary_id: str | None = None  # payables


@dataclass(frozen=True, eq=False)
class PlannerSnapshot:
    business_id: str
    as_of: date
    balances: tuple[Balance, ...]
    forecasts: tuple[ForecastResult, ...]
    # "SELL/BUY" -> OBSERVED business days only (no forward-filled weekends), see simulate.FxSeries.
    fx_history: Mapping[str, FxSeries]
    # The Day 5 invoice table (all businesses allowed; every tool filters to `business_id`).
    invoices: pd.DataFrame
    late_scorer: LateScorer | None
    obligations: tuple[Obligation, ...]
    beneficiaries: tuple[Beneficiary, ...]
    card_vendor_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        stray = {f.business_id for f in self.forecasts} - {self.business_id}
        if stray:
            raise ValueError(f"forecasts for other businesses are not allowed: {sorted(stray)}")
        mine = {b.business_id for b in self.beneficiaries} - {self.business_id}
        if mine:
            raise ValueError(f"beneficiaries of other businesses are not allowed: {sorted(mine)}")

    # ---- what the model may refer to ---------------------------------------------------------
    def open_invoice_ids(self) -> frozenset[str]:
        frame = self.invoices
        own = frame[frame["business_id"] == self.business_id]
        t = pd.Timestamp(self.as_of)
        open_now = (own["issue_date"] <= t) & (own["paid_date"].isna() | (own["paid_date"] > t))
        return frozenset(str(i) for i in own.loc[open_now, "invoice_id"])

    def obligation(self, obligation_id: str) -> Obligation | None:
        return next((o for o in self.obligations if o.id == obligation_id), None)

    def known_ids(self) -> dict[str, frozenset[str]]:
        """Every id a proposal may reference, by kind. Anything else is rejected by the planner."""
        return {
            "beneficiary": frozenset(b.id for b in self.beneficiaries),
            "invoice": self.open_invoice_ids() | {o.invoice_id for o in self.obligations if o.invoice_id},
            "vendor": self.card_vendor_ids,
            "obligation": frozenset(o.id for o in self.obligations),
        }


def _json_list(items: Iterable[str]) -> list[JsonValue]:
    return [*items]


def build_context(snapshot: PlannerSnapshot) -> dict[str, JsonValue]:
    """The context JSON the model receives. Deterministic: same snapshot, same dict."""
    balances = {b.currency.value: str(b.available.amount) for b in sorted(snapshot.balances, key=lambda b: b.currency)}
    beneficiaries: list[JsonValue] = [
        {"id": b.id, "currency": b.currency.value, "approved": b.approved}
        for b in sorted(snapshot.beneficiaries, key=lambda b: b.id)
    ]
    forecast_currencies = sorted({f.currency for f in snapshot.forecasts})
    return {
        "context_version": CONTEXT_VERSION,
        "business_id": snapshot.business_id,
        "as_of": snapshot.as_of.isoformat(),
        "currencies": _json_list(c.value for c in Currency),
        "balances": dict(balances),
        "forecast_currencies": _json_list(forecast_currencies),
        "fx_pairs": _json_list(sorted(snapshot.fx_history)),
        "beneficiaries": beneficiaries,
        "card_vendor_ids": _json_list(sorted(snapshot.card_vendor_ids)),
        "open_obligations": len(snapshot.obligations),
        "limits": {"max_proposals": MAX_PROPOSALS},
    }
