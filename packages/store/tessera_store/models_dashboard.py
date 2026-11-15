"""Read models for the dashboard (Day 15): the latest balances, upcoming payables and late-payment scores.

These are SNAPSHOTS, not ledgers. Whatever owns the data keeps them fresh and the API only reads them:
- `balance_snapshots`: the worker's ingest step copies balances in from the Airwallex port (the API may not import
  the vendor package, so it cannot fetch balances itself). One row per business and currency, replaced on each ingest.
- `payables`: money the business owes, with a due date. Receivables are not copied here: they are the OPEN rows of
  `invoices`, so there is one source of truth for them.
- `late_risk_scores`: the Day 5 scorer's output for an open invoice, written by whatever runs the scorer. The scorer
  is a fitted model, too heavy to run inside a web request.

Free text (counterparty names) is UNTRUSTED DATA: stored and returned as data, never interpreted.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from tessera_store.base import Base
from tessera_store.models import CURRENCIES, in_list

PAYABLE_KINDS = ("payroll", "supplier_invoice", "saas_card")
PAYABLE_STATUSES = ("OPEN", "PAID")
LATE_MODELS = ("logreg", "lgbm", "deterministic")  # mirrors tessera_core.risk LatePaymentAssessment.model


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    available: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when the source reported it

    __table_args__ = (
        CheckConstraint(in_list("currency", CURRENCIES), name="currency"),
        CheckConstraint("available >= 0", name="available_nonnegative"),
    )


class Payable(Base):
    __tablename__ = "payables"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"))
    kind: Mapped[str] = mapped_column(String(20))
    counterparty_id: Mapped[str] = mapped_column(String(64))
    counterparty_name: Mapped[str | None] = mapped_column(String(200))  # UNTRUSTED free text
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    currency: Mapped[str] = mapped_column(String(3))
    due_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(8), server_default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(in_list("kind", PAYABLE_KINDS), name="kind"),
        CheckConstraint(in_list("currency", CURRENCIES), name="currency"),
        CheckConstraint(in_list("status", PAYABLE_STATUSES), name="status"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_payables_business_id_status_due_on", "business_id", "status", "due_on"),
    )


class LateRiskScore(Base):
    __tablename__ = "late_risk_scores"

    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), primary_key=True)
    p_late: Mapped[float] = mapped_column(Float)  # P(paid more than 7 days after the due date)
    already_late: Mapped[bool] = mapped_column(Boolean)
    model: Mapped[str] = mapped_column(String(16))
    pay_date_low: Mapped[date | None] = mapped_column(Date)
    pay_date_median: Mapped[date | None] = mapped_column(Date)
    pay_date_high: Mapped[date | None] = mapped_column(Date)
    scored_as_of: Mapped[date] = mapped_column(Date)  # the date the scorer treated as "today"
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("p_late >= 0 AND p_late <= 1", name="p_late_range"),
        CheckConstraint(in_list("model", LATE_MODELS), name="model"),
    )
