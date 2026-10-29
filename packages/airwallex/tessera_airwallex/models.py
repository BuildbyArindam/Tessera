"""Result models returned by the port. FxQuote and Balance live in tessera_core.domain
because the policy engine needs them and core must not import this package."""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from tessera_core.domain import Currency, Money


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConversionResult(_Frozen):
    conversion_id: str = Field(min_length=1)
    quote_id: str = Field(min_length=1)
    sold: Money
    bought: Money
    executed_at: AwareDatetime


class BeneficiaryRef(_Frozen):
    beneficiary_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    currency: Currency
    created_at: AwareDatetime


class TransferStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    SETTLED = "SETTLED"
    FAILED = "FAILED"


class Transfer(_Frozen):
    transfer_id: str = Field(min_length=1)
    beneficiary_id: str = Field(min_length=1)
    amount: Money
    reference: str
    status: TransferStatus
    created_at: AwareDatetime


class Card(_Frozen):
    card_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    monthly_limit: Money
    created_at: AwareDatetime
