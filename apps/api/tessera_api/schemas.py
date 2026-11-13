"""Request and response bodies. Requests forbid unknown fields; money is always a string, never a float."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, JsonValue, StringConstraints, model_validator
from tessera_core.domain import Currency, Invoice, InvoiceStatus, Money

IDENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
Ident = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=IDENT_PATTERN)]
ProposalId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=IDENT_PATTERN)]


def _clean_text(value: str) -> str:
    """Reject what Postgres cannot store (NUL) and what cannot be encoded (lone surrogates)."""
    if "\x00" in value:
        raise ValueError("NUL characters are not allowed")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("text must be valid Unicode") from exc
    return value


Text = Annotated[str, AfterValidator(_clean_text)]
ProposalStatus = Literal["PENDING_APPROVAL", "APPROVED", "REJECTED", "BLOCKED", "SUBMITTED", "SETTLED", "FAILED"]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Response(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- invoices -------------------------------------------------------------------------------------
class InvoiceIn(Request):
    id: Ident
    business_id: Ident
    customer_id: Ident
    amount: Money
    issued_on: date
    due_on: date
    notes: Annotated[Text, StringConstraints(max_length=2000)] | None = None  # UNTRUSTED free text

    @model_validator(mode="after")
    def _valid_invoice(self) -> InvoiceIn:
        self.to_domain()  # one source of truth for what a valid invoice is: the domain model
        return self

    def to_domain(self) -> Invoice:
        return Invoice(
            id=self.id,
            business_id=self.business_id,
            customer_id=self.customer_id,
            amount=self.amount,
            issued_on=self.issued_on,
            due_on=self.due_on,
            notes=self.notes,
        )


class InvoiceOut(Response):
    id: str
    business_id: str
    customer_id: str
    amount: Money
    issued_on: date
    due_on: date
    status: InvoiceStatus
    notes: str | None
    created_at: datetime


# ---- forecasts ------------------------------------------------------------------------------------
class ForecastOut(Response):
    id: int
    business_id: str
    currency: Currency
    horizon_weeks: int
    as_of: date
    point: Money
    lower: Money
    upper: Money
    target_coverage: float
    model_version: str


# ---- proposals ------------------------------------------------------------------------------------
class DecisionOut(Response):
    phase: str
    verdict: str
    reasons: list[str]
    rule_ids: list[str]
    ruleset_hash: str | None
    decided_at: datetime


class ApprovalOut(Response):
    decision: str
    actor: str
    note: str | None
    decided_at: datetime


class ProposalOut(Response):
    id: str
    business_id: str
    action_type: str
    params: dict[str, JsonValue]
    rationale: str  # model-written: untrusted when shown to a person
    confidence: float
    status: ProposalStatus
    proposed_at: datetime
    decision: DecisionOut | None = None
    approval: ApprovalOut | None = None


class ApprovalIn(Request):
    note: Annotated[Text, StringConstraints(max_length=500)] | None = None


# ---- plan runs ------------------------------------------------------------------------------------
class PlanRunIn(Request):
    business_id: Ident


class PlanRunOut(BaseModel):
    outcome: str
    model: str
    prompt_hash: str
    input_flags: list[str]
    created: list[ProposalOut]
    already_known: list[str]  # proposal ids that existed before this run (planning is idempotent)


# ---- audit ----------------------------------------------------------------------------------------
class AuditRecordOut(BaseModel):
    seq: int
    timestamp: datetime
    event_type: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str


class AuditPage(BaseModel):
    events: list[AuditRecordOut]
    next_after_seq: int | None


class ChainStatusOut(BaseModel):
    ok: bool
    checked: int
    head_seq: int | None
    head_hash: str | None
    broken_seq: int | None
    reason: str | None
