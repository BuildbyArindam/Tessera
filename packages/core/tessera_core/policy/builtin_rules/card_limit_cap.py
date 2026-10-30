"""card_limit_cap: virtual-card top-ups stay inside a per-vendor ceiling and a monthly ceiling.

Each vendor with a card is configured with its currency and two ceilings:
- `per_top_up_max`: the most one TOPUP_CARD may add (the per-vendor limit);
- `per_month_max`: the most that may be loaded onto that vendor's card in a calendar month. The
  caller supplies `card_loaded_this_month` and decides where the month starts. A vendor absent from
  it has had nothing loaded this month.

A vendor that is not configured, a proposal in another currency, or a missing/invalid `vendor_id`
is BLOCK: a card is only ever funded for a vendor someone chose to allow. Equal to a ceiling
passes. Only TOPUP_CARD is checked.
"""

import re
from typing import Any

from pydantic import field_validator, model_validator

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money
from tessera_core.policy.context import PolicyContext, require
from tessera_core.policy.proposal_view import ProposalFormatError, proposal_id, proposed_amount
from tessera_core.policy.registry import register_rule
from tessera_core.policy.rule import Rule, RuleConfig, RuleOutcome, RuleResult

_VENDOR_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class VendorCeilings(RuleConfig):
    currency: Currency
    per_top_up_max: Money
    per_month_max: Money

    @model_validator(mode="before")
    @classmethod
    def _wrap_amounts(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError("each vendor needs currency, per_top_up_max and per_month_max")
        out = dict(data)
        for key in ("per_top_up_max", "per_month_max"):
            if key in out:
                out[key] = {"amount": out[key], "currency": data.get("currency")}
        return out

    @model_validator(mode="after")
    def _check(self) -> "VendorCeilings":
        if not (self.per_top_up_max.is_positive() and self.per_month_max.is_positive()):
            raise ValueError("ceilings must be strictly positive")
        if self.per_top_up_max > self.per_month_max:
            raise ValueError("per_top_up_max must not exceed per_month_max")
        return self


class CardLimitConfig(RuleConfig):
    vendors: dict[str, VendorCeilings]

    @field_validator("vendors")
    @classmethod
    def _vendor_ids(cls, value: dict[str, VendorCeilings]) -> dict[str, VendorCeilings]:
        if not value:
            raise ValueError("define at least one vendor")
        for vendor_id in value:
            if re.fullmatch(_VENDOR_ID, vendor_id) is None:
                raise ValueError(f"invalid vendor id {vendor_id!r}")
        return value


@register_rule
class CardLimitCap(Rule[CardLimitConfig]):
    type_name = "card_limit_cap"
    config_model = CardLimitConfig

    def required_context_for(self, proposal: Proposal) -> tuple[str, ...]:
        return ("card_loaded_this_month",) if proposal.action_type is ActionType.TOPUP_CARD else ()

    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        if proposal.action_type is not ActionType.TOPUP_CARD:
            return self.result(RuleOutcome.PASS)
        try:
            amount = proposed_amount(proposal)
            vendor_id = proposal_id(proposal, "vendor_id")
        except ProposalFormatError as exc:
            return self.result(RuleOutcome.BLOCK, f"invalid card top-up: {exc}")
        if amount is None:
            return self.result(RuleOutcome.BLOCK, "invalid card top-up: no amount")

        vendor = self.config.vendors.get(vendor_id)
        if vendor is None:
            return self.result(RuleOutcome.BLOCK, f"vendor {vendor_id} has no card ceilings configured")
        if amount.currency != vendor.currency:
            return self.result(RuleOutcome.BLOCK, f"vendor {vendor_id} cards are in {vendor.currency.value} only")
        if amount > vendor.per_top_up_max:
            return self.result(RuleOutcome.BLOCK, f"{amount} exceeds the {vendor.per_top_up_max} top-up ceiling")

        loaded = require(context.card_loaded_this_month, "card_loaded_this_month").get(
            vendor_id, Money.zero(vendor.currency)
        )
        if loaded + amount > vendor.per_month_max:
            return self.result(
                RuleOutcome.BLOCK,
                f"{loaded} already loaded this month plus {amount} exceeds the {vendor.per_month_max} monthly ceiling",
            )
        return self.result(RuleOutcome.PASS)
