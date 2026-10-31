"""Reading money out of a Proposal, strictly.

`Proposal.params` is loose JSON (the LLM's output shape), so every rule that needs an amount goes
through here. Anything malformed raises ProposalFormatError and the rule turns that into a BLOCK.

Day 6 convention (recorded in docs/daily-log.md): for money-moving actions `params` carries
`amount` (string or int, never a float) and `currency` (e.g. "USD"), meaning the value that
LEAVES the account: the sell side of a CONVERT, the payout of a PAY_SUPPLIER, the top-up of a
TOPUP_CARD. Day 7 rules refine per-action fields.
"""

import re
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money

MONEY_MOVING_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.CONVERT, ActionType.PAY_SUPPLIER, ActionType.TOPUP_CARD}
)


# What an id (beneficiary, vendor) may look like. Anything else is rejected before it can be looked
# up or echoed into a reason, because params are untrusted.
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# What an amount string may look like: ASCII digits with an optional fraction. This refuses forms
# Python's Decimal would accept but other parsers read differently: whitespace, "+", "1_000",
# "1e3", and non-ASCII digits such as fullwidth "１００".
_AMOUNT_PATTERN = re.compile(r"[0-9]{1,17}(\.[0-9]+)?")


class ProposalFormatError(ValueError):
    """The proposal's params cannot be read as a valid amount, id or currency."""


def proposed_amount(proposal: Proposal) -> Money | None:
    """The positive amount leaving the account, or None for actions that move no money."""
    if proposal.action_type not in MONEY_MOVING_ACTIONS:
        return None
    return read_amount(proposal)


def read_amount(proposal: Proposal) -> Money:
    """Strictly read `params.amount` / `params.currency`, whatever the action type.

    `proposed_amount` is this plus the "does this action move money" filter. The simulator also
    needs it for a HOLD, which moves nothing but is judged against the position it leaves unconverted.
    """
    raw_amount = proposal.params.get("amount")
    raw_currency = proposal.params.get("currency")
    if raw_amount is None or raw_currency is None:
        raise ProposalFormatError("amount and currency are required for a money-moving action")
    if isinstance(raw_amount, bool) or not isinstance(raw_amount, str | int):
        raise ProposalFormatError("amount must be a string or integer, never a float, bool or nested value")
    if not isinstance(raw_currency, str):
        raise ProposalFormatError("currency must be a string")

    if isinstance(raw_amount, str) and _AMOUNT_PATTERN.fullmatch(raw_amount) is None:
        raise ProposalFormatError("amount must be plain ASCII digits with an optional fraction")

    try:
        exact = Decimal(raw_amount)
    except InvalidOperation:
        raise ProposalFormatError("amount is not a valid decimal number") from None
    try:
        money = Money(amount=raw_amount, currency=Currency(raw_currency))
    except (ValidationError, ValueError) as exc:
        raise ProposalFormatError(f"amount or currency rejected: {type(exc).__name__}") from None

    # Money quantizes to 2 dp. Refuse amounts it would silently round, so the number the policy
    # judged is exactly the number that would be executed.
    if money.amount != exact:
        raise ProposalFormatError("amount has more than 2 decimal places")
    if not money.is_positive():
        raise ProposalFormatError("amount must be strictly positive")
    return money


def proposal_id(proposal: Proposal, key: str) -> str:
    """An id such as `beneficiary_id` or `vendor_id`: 1-64 ASCII letters, digits, '_' or '-'."""
    raw = proposal.params.get(key)
    if not isinstance(raw, str) or _ID_PATTERN.fullmatch(raw) is None:
        raise ProposalFormatError(f"{key} is required and must be 1-64 letters, digits, '_' or '-'")
    return raw


def currency_param(proposal: Proposal, key: str) -> Currency:
    """Any currency-valued param, e.g. `from_currency`. Unknown or non-string values are rejected."""
    raw = proposal.params.get(key)
    if not isinstance(raw, str):
        raise ProposalFormatError(f"{key} is required and must be a currency code")
    try:
        return Currency(raw)
    except ValueError:
        raise ProposalFormatError(f"{key} is not a supported currency") from None


def buy_currency(proposal: Proposal) -> Currency:
    """The currency a CONVERT buys (`to_currency`)."""
    raw = proposal.params.get("to_currency")
    if not isinstance(raw, str):
        raise ProposalFormatError("to_currency is required and must be a currency code")
    try:
        return Currency(raw)
    except ValueError:
        raise ProposalFormatError("to_currency is not a supported currency") from None
