"""ORM tables for everything except the audit log (which lives in tessera_audit, next to its chain logic).

Conventions:
- Money is NUMERIC(20, 2) plus a currency code, never a float. 20 digits covers Money's 10**15 sanity bound.
- Free text (notes, names, rationales) is UNTRUSTED DATA. It is stored and returned as data, never interpreted.
- Value sets that mirror a domain enum are CHECK constraints, so a bad value cannot be written even by raw SQL.
  tests/unit/test_store_enums.py keeps each list equal to its Python enum.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tessera_store.base import Base

CURRENCIES = ("USD", "EUR", "GBP", "INR", "SGD", "AUD")
INVOICE_STATUSES = ("OPEN", "PAID", "DISPUTED", "WRITTEN_OFF")
PAYMENT_DIRECTIONS = ("INBOUND", "OUTBOUND")
ACTION_TYPES = ("CONVERT", "HOLD", "PAY_SUPPLIER", "TOPUP_CARD", "FLAG_INVOICE")
VERDICTS = ("APPROVE", "ESCALATE", "BLOCK")
# PENDING_APPROVAL: policy said ESCALATE, a human must decide. APPROVED: ready for the worker, which re-checks
# policy before it executes (approval never executes). BLOCKED and REJECTED are terminal. SUBMITTED, SETTLED and
# FAILED follow the execution (migration 0002). tests/unit/test_store_enums.py ties this list to the lifecycle table.
PROPOSAL_STATUSES = (
    "PENDING_APPROVAL", "APPROVED", "REJECTED", "BLOCKED", "SUBMITTED", "SETTLED", "FAILED",
)  # fmt: skip
DECISION_PHASES = ("plan", "execution")
HUMAN_DECISIONS = ("APPROVED", "REJECTED")
EXECUTION_STATUSES = ("PENDING", "SUBMITTED", "SETTLED", "FAILED")
WEBHOOK_OUTCOMES = ("applied", "ignored", "unmatched")


def in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _now() -> Any:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))  # UNTRUSTED free text
    base_currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = _now()

    __table_args__ = (CheckConstraint(in_list("base_currency", CURRENCIES), name="base_currency"),)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"))
    customer_id: Mapped[str] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    currency: Mapped[str] = mapped_column(String(3))
    issued_on: Mapped[date] = mapped_column(Date)
    due_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), server_default="OPEN")
    notes: Mapped[str | None] = mapped_column(Text)  # UNTRUSTED free text
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(in_list("currency", CURRENCIES), name="currency"),
        CheckConstraint("due_on >= issued_on", name="due_after_issue"),
        CheckConstraint(in_list("status", INVOICE_STATUSES), name="status"),
        Index("ix_invoices_business_id_status_due_on", "business_id", "status", "due_on"),
    )


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"))
    direction: Mapped[str] = mapped_column(String(8))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    currency: Mapped[str] = mapped_column(String(3))
    counterparty_id: Mapped[str] = mapped_column(String(64))
    paid_on: Mapped[date] = mapped_column(Date)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoices.id"))
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint(in_list("direction", PAYMENT_DIRECTIONS), name="direction"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(in_list("currency", CURRENCIES), name="currency"),
        Index("ix_payments_business_id_paid_on", "business_id", "paid_on"),
    )


class Forecast(Base):
    __tablename__ = "forecasts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"))
    currency: Mapped[str] = mapped_column(String(3))
    horizon_weeks: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[date] = mapped_column(Date)
    point: Mapped[Decimal] = mapped_column(Numeric(20, 2))  # net cash flow: may be negative
    lower: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    upper: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    target_coverage: Mapped[float] = mapped_column(Float)
    model_version: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint(in_list("currency", CURRENCIES), name="currency"),
        CheckConstraint("horizon_weeks > 0", name="horizon_positive"),
        CheckConstraint("lower <= point AND point <= upper", name="interval_ordered"),
        CheckConstraint("target_coverage > 0 AND target_coverage < 1", name="coverage_range"),
        UniqueConstraint(
            "business_id", "currency", "horizon_weeks", "as_of", "model_version", name="uq_forecasts_natural_key"
        ),
    )


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # deterministic: the planner derives it
    business_id: Mapped[str] = mapped_column(ForeignKey("businesses.id"))
    action_type: Mapped[str] = mapped_column(String(16))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    rationale: Mapped[str] = mapped_column(Text)  # model-written, UNTRUSTED when a human reads it
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20))
    plan_record: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint(in_list("action_type", ACTION_TYPES), name="action_type"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint(in_list("status", PROPOSAL_STATUSES), name="status"),
        Index("ix_proposals_business_id_status", "business_id", "status"),
    )


class PolicyDecision(Base):
    __tablename__ = "policy_decisions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"))
    phase: Mapped[str] = mapped_column(String(10), server_default="plan")
    verdict: Mapped[str] = mapped_column(String(10))
    reasons: Mapped[list[str]] = mapped_column(JSONB)
    rule_ids: Mapped[list[str]] = mapped_column(JSONB)
    ruleset_hash: Mapped[str | None] = mapped_column(String(64))
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # what the engine saw; None if it could not be built
    decided_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint(in_list("phase", DECISION_PHASES), name="phase"),
        CheckConstraint(in_list("verdict", VERDICTS), name="verdict"),
        Index("ix_policy_decisions_proposal_id", "proposal_id"),
    )


class Approval(Base):
    """A human decision on an escalated proposal. One per proposal, enforced by the database."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"))
    decision: Mapped[str] = mapped_column(String(10))
    actor: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)  # operator-written, untrusted like any text
    decided_at: Mapped[datetime] = _now()

    __table_args__ = (
        CheckConstraint(in_list("decision", HUMAN_DECISIONS), name="decision"),
        UniqueConstraint("proposal_id"),
    )


class Execution(Base):
    """The one attempt to act on an approved proposal (unique per proposal).

    The row, with its idempotency key, is committed BEFORE the vendor call. After a crash the worker finds it still
    PENDING and replays the same key, so the vendor never sees a second request. `vendor_request_id` is the id the
    vendor knows the request by (derived from the key); a webhook that carries it finds the row before `external_id`
    is known.
    """

    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(10), server_default="PENDING")
    external_id: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    vendor_request_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint(in_list("status", EXECUTION_STATUSES), name="status"),
        UniqueConstraint("idempotency_key"),
        UniqueConstraint("proposal_id", name="uq_executions_proposal_id"),
        Index("ix_executions_external_id", "external_id"),
        Index("ix_executions_vendor_request_id", "vendor_request_id"),
    )


class WebhookEvent(Base):
    """Every webhook delivery that passed signature and freshness checks. The primary key IS the dedupe.

    Only the envelope is kept (id, name, ids): a payload can carry bank details, so the body is never stored.
    `unmatched` means no execution knew the event yet; the worker retries those when it reconciles.
    """

    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(128))
    event_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = _now()
    outcome: Mapped[str] = mapped_column(String(10))
    execution_id: Mapped[int | None] = mapped_column(ForeignKey("executions.id"))

    __table_args__ = (
        CheckConstraint(in_list("outcome", WEBHOOK_OUTCOMES), name="outcome"),
        Index("ix_webhook_events_outcome", "outcome"),
    )
