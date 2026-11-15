"""Response bodies for the dashboard's read endpoints (Day 15). Money is always a string-valued Money, never a float.

`label` and `counterparty` carry names written by third parties: UNTRUSTED text. Render them as text, never as markup.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel
from tessera_core.domain import Currency, Money

from tessera_api.schemas import InvoiceOut, Response


class BusinessOut(Response):
    id: str
    name: str  # UNTRUSTED free text
    base_currency: Currency


class BalanceOut(BaseModel):
    available: Money  # its currency is the currency of the balance
    as_of: datetime  # when the source reported it


class BalancesOut(BaseModel):
    business_id: str
    balances: list[BalanceOut]


ObligationKind = Literal["RECEIVABLE", "PAYABLE"]
PayableKind = Literal["payroll", "supplier_invoice", "saas_card"]


class ObligationOut(BaseModel):
    """Money expected in (a customer's open invoice) or due out (a payable)."""

    id: str
    kind: ObligationKind
    amount: Money
    due_on: date
    days_until_due: int  # relative to `as_of` of the response; negative means overdue
    counterparty: str | None  # a customer id (receivable) or a payee name (payable): UNTRUSTED text
    payable_kind: PayableKind | None = None
    invoice_id: str | None = None  # receivables only


class ObligationsOut(BaseModel):
    business_id: str
    as_of: date  # the date "upcoming" is measured from: the newest forecast's date, else today
    days_ahead: int
    items: list[ObligationOut]


class LateRiskOut(BaseModel):
    p_late: float  # probability the invoice is paid more than 7 days after its due date
    already_late: bool
    model: Literal["logreg", "lgbm", "deterministic"]
    pay_date_low: date | None
    pay_date_median: date | None
    pay_date_high: date | None
    scored_as_of: date


class InvoiceRiskOut(InvoiceOut):
    late_risk: LateRiskOut | None  # None: the invoice has not been scored (yet)
