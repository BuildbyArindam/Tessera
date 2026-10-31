"""The only shape a model reply may take: a strict, typed list of proposals.

Design rules (each one closes a door the model could otherwise use):
  * Every model is `extra="forbid"`: an unknown field is an error, never ignored.
  * Records are referenced by ID only (beneficiary, invoice, vendor, obligation). There is no field
    that can hold a name, an IBAN or an account number, and an ID that LOOKS like one is rejected.
  * Params are typed per action (a discriminated union on `action_type`), so a CONVERT cannot carry
    a beneficiary and a PAY_SUPPLIER cannot omit one.
  * Amounts are decimal STRINGS with at most 2 decimal places, the exact form the policy engine's
    strict parser accepts, so the number the planner validated is the number policy judges.
  * The model never sets a due date, a rate, a quote or a simulation: those come from the caller.
    `obligation_id` names the obligation a money action serves; the due date is looked up from it.
  * At most MAX_PROPOSALS per reply; more is invalid, not silently truncated.
  * `evidence_ids` must be present: a proposal has to cite the tool outputs that support it.

Passing this schema proves FORMAT only. That the ids exist and the evidence is real is checked
against the planner's snapshot in `validate.py`; whether the action is ALLOWED is the policy engine's job.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from tessera_core.domain import Currency

MAX_PROPOSALS = 5
MAX_EVIDENCE = 8

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
AMOUNT_PATTERN = r"^[0-9]{1,17}(\.[0-9]{1,2})?$"
EVIDENCE_PATTERN = r"^ev-[0-9]{1,3}$"

_DIGIT_RUN = re.compile(r"[0-9]{9,}")
_IBAN_SHAPE = re.compile(r"^[A-Za-z]{2}[0-9]{2}[A-Za-z0-9]{11,30}$")


def looks_like_account_number(value: str) -> bool:
    """True for strings shaped like an IBAN, card or bank account number (separators ignored).

    Real record ids in Tessera (INV-B01-00001, CP-B01-003) never contain a 9-digit run, so this rejects
    a pasted account number without rejecting a real id. It is a shape check, a second line of defence
    behind the membership check against the snapshot.
    """
    squeezed = value.replace("-", "").replace("_", "")
    return _IBAN_SHAPE.match(squeezed) is not None or _DIGIT_RUN.search(squeezed) is not None


def _checked_id(value: str) -> str:
    if looks_like_account_number(value):
        raise ValueError("looks like an account number; reference records by their id")
    return value


RecordId = Annotated[str, StringConstraints(pattern=ID_PATTERN), Field(max_length=64)]
Amount = Annotated[str, StringConstraints(pattern=AMOUNT_PATTERN)]
EvidenceId = Annotated[str, StringConstraints(pattern=EVIDENCE_PATTERN)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _WithRecordIds(Strict):
    """Params base: every field named `*_id` is shape-checked as a record id."""

    @model_validator(mode="after")
    def _ids_are_not_account_numbers(self) -> _WithRecordIds:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if name.endswith("_id") and isinstance(value, str):
                _checked_id(value)
        return self


class ConvertParams(_WithRecordIds):
    amount: Amount  # sell units, now
    currency: Currency  # the currency sold
    to_currency: Currency
    obligation_id: RecordId  # the obligation this conversion serves; its due date drives the simulation

    @model_validator(mode="after")
    def _two_currencies(self) -> ConvertParams:
        if self.currency == self.to_currency:
            raise ValueError("currency and to_currency must differ")
        return self


class HoldParams(ConvertParams):
    """Same fields as a conversion: the position left unconverted (policy and simulator read it alike)."""


class PaySupplierParams(_WithRecordIds):
    amount: Amount  # payout units, in `currency`
    currency: Currency  # the payout currency
    from_currency: Currency  # the currency that funds it (equal to `currency` means no conversion)
    beneficiary_id: RecordId
    obligation_id: RecordId


class TopupCardParams(_WithRecordIds):
    amount: Amount
    currency: Currency
    vendor_id: RecordId


class FlagInvoiceParams(_WithRecordIds):
    invoice_id: RecordId
    reason: Literal["likely_late", "dispute_risk", "amount_mismatch", "other"]  # a code, never free text


class _ProposalBase(Strict):
    rationale: Annotated[str, StringConstraints(min_length=10, max_length=600)]
    confidence: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False, strict=True)]
    evidence_ids: Annotated[list[EvidenceId], Field(min_length=1, max_length=MAX_EVIDENCE)]


class ConvertProposal(_ProposalBase):
    action_type: Literal["CONVERT"]
    params: ConvertParams


class HoldProposal(_ProposalBase):
    action_type: Literal["HOLD"]
    params: HoldParams


class PaySupplierProposal(_ProposalBase):
    action_type: Literal["PAY_SUPPLIER"]
    params: PaySupplierParams


class TopupCardProposal(_ProposalBase):
    action_type: Literal["TOPUP_CARD"]
    params: TopupCardParams


class FlagInvoiceProposal(_ProposalBase):
    action_type: Literal["FLAG_INVOICE"]
    params: FlagInvoiceParams


ProposedAction = Annotated[
    ConvertProposal | HoldProposal | PaySupplierProposal | TopupCardProposal | FlagInvoiceProposal,
    Field(discriminator="action_type"),
]


class ProposalList(Strict):
    proposals: Annotated[list[ProposedAction], Field(max_length=MAX_PROPOSALS)]


def summarize_errors(exc: ValidationError, limit: int = 10) -> list[str]:
    """Field paths and error types only. Never the offending value: the model's own output (and, once
    Day 10 lands, text that came from an invoice) must not be echoed back into the next prompt."""
    lines = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False)[:limit]:
        path = ".".join(str(part) for part in err["loc"]) or "(root)"
        lines.append(f"{path}: {err['type']}")
    return lines


REFERENCE_FIELDS: dict[str, str] = {
    "beneficiary_id": "beneficiary",
    "invoice_id": "invoice",
    "vendor_id": "vendor",
    "obligation_id": "obligation",
}


def references(
    action: ConvertProposal | HoldProposal | PaySupplierProposal | TopupCardProposal | FlagInvoiceProposal,
) -> list[tuple[str, str]]:
    """(kind, id) for every record the proposal points at, e.g. ('beneficiary', 'sup_old')."""
    found: list[tuple[str, str]] = []
    for name, kind in REFERENCE_FIELDS.items():
        value = getattr(action.params, name, None)
        if isinstance(value, str):
            found.append((kind, value))
    return found
