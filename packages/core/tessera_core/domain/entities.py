"""Business entities. Free-text fields (notes, names) are UNTRUSTED DATA, never instructions."""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, Field, model_validator

from tessera_core.domain.base import DomainModel
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money


def _require_positive(money: Money, field: str) -> None:
    if not money.is_positive():
        raise ValueError(f"{field} must be strictly positive, got {money}")


class InvoiceStatus(StrEnum):
    OPEN = "OPEN"
    PAID = "PAID"
    DISPUTED = "DISPUTED"
    WRITTEN_OFF = "WRITTEN_OFF"


class Invoice(DomainModel):
    id: str = Field(min_length=1)
    business_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    amount: Money
    issued_on: date
    due_on: date
    status: InvoiceStatus = InvoiceStatus.OPEN
    notes: str | None = None  # UNTRUSTED free text; sanitised on Day 10

    @model_validator(mode="after")
    def _check(self) -> "Invoice":
        _require_positive(self.amount, "invoice amount")
        if self.due_on < self.issued_on:
            raise ValueError("due_on must not be before issued_on")
        return self


class PaymentDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class Payment(DomainModel):
    id: str = Field(min_length=1)
    business_id: str = Field(min_length=1)
    direction: PaymentDirection
    amount: Money
    counterparty_id: str = Field(min_length=1)
    paid_on: date
    invoice_id: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "Payment":
        _require_positive(self.amount, "payment amount")
        return self


class Beneficiary(DomainModel):
    """A payout recipient. Bank details are never held here; policy refers to `id` only."""

    id: str = Field(min_length=1)
    business_id: str = Field(min_length=1)
    name: str = Field(min_length=1)  # UNTRUSTED free text
    currency: Currency
    added_at: AwareDatetime
    approved: bool = False


class Balance(DomainModel):
    currency: Currency
    available: Money

    @model_validator(mode="after")
    def _check(self) -> "Balance":
        if self.available.currency != self.currency:
            raise ValueError("balance currency must match its Money currency")
        if self.available.is_negative():
            raise ValueError("available balance must not be negative")
        return self


class FxQuote(DomainModel):
    """An executable conversion quote. `rate` is buy-currency units per 1 sell-currency unit,
    spread included. Policy compares it with a reference rate (quote_slippage_guard, Day 7)."""

    quote_id: str = Field(min_length=1)
    sell: Money
    buy: Money
    rate: Decimal = Field(gt=0)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> "FxQuote":
        if self.sell.currency == self.buy.currency:
            raise ValueError("a quote must convert between two different currencies")
        _require_positive(self.sell, "quote sell amount")
        _require_positive(self.buy, "quote buy amount")
        return self
