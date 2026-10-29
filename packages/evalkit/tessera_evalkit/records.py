"""Pydantic v2 row models. Every generated row passes through one of these.

Money amounts are Decimal with at most 2 decimal places and are always > 0.
FX rates are ratios, not money, so they are float64 (rounded to 6 dp).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._compat import Currency

Amount = Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=2)]
Rate = Annotated[float, Field(gt=0, allow_inf_nan=False)]

Archetype = Literal["solo_freelancer", "small_agency", "product_exporter"]
CustomerType = Literal["prompt", "typical", "chronic_late", "seasonal_late"]
InvoiceStatus = Literal["paid", "open", "disputed", "written_off", "settled_partial"]
Outcome = Literal["paid", "never_paid", "disputed_settled", "disputed_unpaid"]
CounterpartyKind = Literal["payroll", "supplier", "saas_vendor"]
OutflowKind = Literal["payroll", "supplier_invoice", "saas_card"]


def cents_to_decimal(cents: int) -> Decimal:
    """Exact 2-dp Decimal from integer minor units (no float involved)."""
    return Decimal(cents).scaleb(-2)


class Rec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=True)


class BusinessRec(Rec):
    business_id: str
    name: str
    archetype: Archetype
    base_currency: Currency
    payroll_day: int = Field(ge=1, le=31)
    growth_rate: float = Field(ge=-0.5, le=2.0)


class CustomerRec(Rec):
    customer_id: str
    business_id: str
    name: str
    currency: Currency
    payment_terms_days: int = Field(ge=0, le=120)
    onboarded: date


class InvoiceRec(Rec):
    invoice_id: str
    business_id: str
    customer_id: str
    invoice_date: date
    due_date: date
    currency: Currency
    amount: Amount
    status: InvoiceStatus
    memo: str = Field(max_length=200)

    @model_validator(mode="after")
    def _due_not_before_invoice(self) -> InvoiceRec:
        if self.due_date < self.invoice_date:
            raise ValueError("due_date precedes invoice_date")
        return self


class PaymentRec(Rec):
    payment_id: str
    invoice_id: str
    business_id: str
    pay_date: date
    currency: Currency
    amount: Amount


class CounterpartyRec(Rec):
    counterparty_id: str
    business_id: str
    kind: CounterpartyKind
    name: str
    currency: Currency
    added_date: date


class OutflowRec(Rec):
    outflow_id: str
    business_id: str
    counterparty_id: str
    kind: OutflowKind
    issue_date: date
    due_date: date
    pay_date: date | None
    currency: Currency
    amount: Amount

    @model_validator(mode="after")
    def _dates_ordered(self) -> OutflowRec:
        if self.due_date < self.issue_date:
            raise ValueError("due_date precedes issue_date")
        if self.pay_date is not None and self.pay_date < self.issue_date:
            raise ValueError("pay_date precedes issue_date")
        return self


class GroundTruthRec(Rec):
    """Latent truth for evaluation ONLY. Never a model feature."""

    invoice_id: str
    business_id: str
    customer_id: str
    customer_type: CustomerType
    outcome: Outcome
    latent_pay_date: date | None
    days_past_due: int | None


class FxRateRec(Rec):
    date: date
    base: Currency
    quote: Currency
    pair: str
    rate: Rate
    is_observed: bool
    source: Literal["ecb", "synthetic"]

    @model_validator(mode="after")
    def _pair_matches(self) -> FxRateRec:
        if self.pair != f"{self.base}{self.quote}":
            raise ValueError("pair does not match base/quote")
        if self.base == self.quote:
            raise ValueError("base equals quote")
        return self
