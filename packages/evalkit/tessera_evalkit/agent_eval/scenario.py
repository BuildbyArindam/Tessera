"""Labeled treasury scenarios (Day 19): the data model, the loader and the plain-language rendering.

A scenario is a small, fixed treasury situation: balances, a 4-week forecast per currency, obligations (receivables and
payables, due dates relative to AS_OF), the beneficiaries, and an FX range per pair. Exactly ONE item is "in focus" (an
obligation id); the label is the one action a competent treasurer takes about it today. Other obligations are context.

Everything a labeller sees is also what the planner can see, with one exception that matters and is kept on purpose:
the planner's context says whether a beneficiary is approved but not how old the approval is, while a labeller sees
"added 3 hours ago". The policy engine knows the age; whether the policy then stops the agent is measured, not assumed.

The FX ranges are COMPUTED (world.py, build_scenarios.py) from a seeded synthetic series with the same estimator the
agent's `get_fx_range` tool uses, so the labeller and the agent are shown the same numbers.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ACTIONS = ("CONVERT", "HOLD", "PAY_SUPPLIER", "FLAG_INVOICE", "NO_ACTION")
MONEY_ACTIONS = frozenset({"CONVERT", "PAY_SUPPLIER"})
Action = Literal["CONVERT", "HOLD", "PAY_SUPPLIER", "FLAG_INVOICE", "NO_ACTION"]
Ccy = Literal["USD", "EUR", "GBP", "INR", "SGD", "AUD"]
FX_HORIZONS = (3, 7, 14, 30)
SCENARIO_COUNT = 40


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Obl(_M):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
    kind: Literal["RECEIVABLE", "PAYABLE"]
    amount: str = Field(pattern=r"^[0-9]{1,15}(\.[0-9]{1,2})?$")
    currency: Ccy
    due_in_days: int = Field(ge=-120, le=120)  # negative: overdue
    beneficiary_id: str | None = None  # payables
    invoice_id: str | None = None  # receivables


class Ben(_M):
    id: str
    currency: Ccy
    status: Literal["approved", "new", "unapproved"]  # new: approved but added a few hours ago


class Fc(_M):
    currency: Ccy
    point: str
    lower: str
    upper: str  # 80% interval of the next-4-weeks net cash flow


class FxRangeRow(_M):
    horizon_days: int
    p05_pct: float
    p50_pct: float
    p95_pct: float
    p_fall_1pct: float
    p_fall_2pct: float


class Fx(_M):
    pair: str = Field(pattern=r"^[A-Z]{3}/[A-Z]{3}$")  # SELL/BUY: BUY units per 1 SELL unit
    spot: float = Field(gt=0)
    daily_vol_pct: float = Field(gt=0)
    seed: int
    ranges: tuple[FxRangeRow, ...] = ()  # filled in by build_scenarios.py


class Choice(_M):
    """An action about the focus item: what a labeller chose, or what the agent proposed."""

    action_type: Action
    amount: str | None = None  # CONVERT: units SOLD. PAY_SUPPLIER: payout units. HOLD: units left unconverted.
    currency: Ccy | None = None
    to_currency: Ccy | None = None  # CONVERT, HOLD
    obligation_id: str | None = None
    beneficiary_id: str | None = None  # PAY_SUPPLIER
    invoice_id: str | None = None  # FLAG_INVOICE

    @model_validator(mode="after")
    def _shape(self) -> Choice:
        if self.action_type == "NO_ACTION":
            if any(v is not None for v in (self.amount, self.currency, self.to_currency, self.obligation_id)):
                raise ValueError("NO_ACTION carries no parameters")
        elif self.action_type == "FLAG_INVOICE":
            if self.invoice_id is None:
                raise ValueError("FLAG_INVOICE needs invoice_id")
        else:
            if self.obligation_id is None or self.currency is None:
                raise ValueError(f"{self.action_type} needs obligation_id and currency")
            if self.action_type in MONEY_ACTIONS and self.amount is None:
                raise ValueError(f"{self.action_type} needs an amount")
            if self.action_type in {"CONVERT", "HOLD"} and self.to_currency is None:
                raise ValueError(f"{self.action_type} needs to_currency")
            if self.action_type == "PAY_SUPPLIER" and self.beneficiary_id is None:
                raise ValueError("PAY_SUPPLIER needs beneficiary_id")
        if self.amount is not None and Decimal(self.amount) <= 0:
            raise ValueError("amount must be positive")
        return self


class Label(Choice):
    reason: str = Field(min_length=10, max_length=200, pattern=r"^[^\n\r]+$")  # one line


class Scenario(_M):
    id: str = Field(pattern=r"^S[0-9]{2}$")
    title: str
    focus: str  # the obligation id (or the invoice id of a receivable) the decision is about
    balances: dict[Ccy, str]
    forecasts: tuple[Fc, ...]
    obligations: tuple[Obl, ...]
    beneficiaries: tuple[Ben, ...]
    fx: tuple[Fx, ...]
    label: Label
    labeler: str  # who wrote the label; see docs/labeling-rubric.md, "Provenance"
    reviewed_by: str | None = None  # a person who read the label and stands behind it

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        ids = {o.id for o in self.obligations}
        if len(ids) != len(self.obligations):
            raise ValueError("duplicate obligation id")
        invoices = {o.invoice_id for o in self.obligations if o.invoice_id}
        if self.focus not in ids | invoices:
            raise ValueError("focus must name an obligation or an invoice of this scenario")
        bens = {b.id for b in self.beneficiaries}
        for o in self.obligations:
            if o.beneficiary_id is not None and o.beneficiary_id not in bens:
                raise ValueError(f"{o.id}: unknown beneficiary {o.beneficiary_id}")
        lab = self.label
        if lab.obligation_id is not None and lab.obligation_id not in ids:
            raise ValueError("label names an unknown obligation")
        if lab.invoice_id is not None and lab.invoice_id not in invoices:
            raise ValueError("label names an unknown invoice")
        if lab.beneficiary_id is not None and lab.beneficiary_id not in bens:
            raise ValueError("label names an unknown beneficiary")
        return self


def load_scenarios(path: Path) -> list[Scenario]:
    rows = [Scenario.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]
    if len({r.id for r in rows}) != len(rows):
        raise ValueError("duplicate scenario id")
    return rows


def dump_scenarios(rows: list[Scenario], path: Path) -> None:
    path.write_text("".join(json.dumps(r.model_dump(mode="json"), sort_keys=True) + "\n" for r in rows))


def _fmt(amount: str) -> str:
    return f"{Decimal(amount):,.2f}"


def describe(s: Scenario) -> str:
    """The scenario as a labeller reads it. No label, and nothing the planner cannot also see (except beneficiary age)."""
    lines = [f"{s.id} - {s.title}", "Balances: " + ", ".join(f"{c} {_fmt(a)}" for c, a in sorted(s.balances.items()))]
    for f in s.forecasts:
        lines.append(
            f"Forecast net cash flow, next 4 weeks, {f.currency}: {_fmt(f.point)} (80% range {_fmt(f.lower)} to {_fmt(f.upper)})"
        )
    for o in s.obligations:
        when = f"due in {o.due_in_days} days" if o.due_in_days >= 0 else f"OVERDUE by {-o.due_in_days} days"
        who = f" to beneficiary {o.beneficiary_id}" if o.beneficiary_id else ""
        inv = f" (invoice {o.invoice_id})" if o.invoice_id else ""
        mark = "  <== DECIDE ABOUT THIS" if s.focus in (o.id, o.invoice_id) else ""
        lines.append(f"{o.kind.title()} {o.id}{inv}: {o.currency} {_fmt(o.amount)}{who}, {when}{mark}")
    notes = {
        "approved": "approved, long established",
        "new": "approved, added 3 hours ago",
        "unapproved": "NOT approved",
    }
    for b in s.beneficiaries:
        lines.append(f"Beneficiary {b.id} ({b.currency}): {notes[b.status]}")
    for fx in s.fx:
        lines.append(f"FX {fx.pair}: spot {fx.spot:.4f}; the pair is SELL/BUY, BUY units per 1 SELL unit")
        for r in fx.ranges:
            lines.append(
                f"  over {r.horizon_days:>2}d: 5th pct {r.p05_pct:+.2f}%, median {r.p50_pct:+.2f}%, 95th pct {r.p95_pct:+.2f}%; "
                f"P(falls 1%+) {r.p_fall_1pct:.0%}, P(falls 2%+) {r.p_fall_2pct:.0%}"
            )
    return "\n".join(lines)
