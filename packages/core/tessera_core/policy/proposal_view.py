"""Reading money out of a Proposal, strictly.

`Proposal.params` is loose JSON (the LLM's output shape), so every rule that needs an amount goes
through here. Anything malformed raises ProposalFormatError and the rule turns that into a BLOCK.

Day 6 convention (recorded in docs/daily-log.md): for money-moving actions `params` carries
`amount` (string or int, never a float) and `currency` (e.g. "USD"), meaning the value that
LEAVES the account: the sell side of a CONVERT, the payout of a PAY_SUPPLIER, the top-up of a
TOPUP_CARD. Day 7 rules refine per-action fields.
"""

from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money

MONEY_MOVING_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.CONVERT, ActionType.PAY_SUPPLIER, ActionType.TOPUP_CARD}
)


class ProposalFormatError(ValueError):
    """The proposal's params cannot be read as a valid amount."""


def proposed_amount(proposal: Proposal) -> Money | None:
    """The positive amount leaving the account, or None for actions that move no money."""
    if proposal.action_type not in MONEY_MOVING_ACTIONS:
        return None

    raw_amount = proposal.params.get("amount")
    raw_currency = proposal.params.get("currency")
    if raw_amount is None or raw_currency is None:
        raise ProposalFormatError("amount and currency are required for a money-moving action")
    if isinstance(raw_amount, bool) or not isinstance(raw_amount, str | int):
        raise ProposalFormatError("amount must be a string or integer, never a float, bool or nested value")
    if not isinstance(raw_currency, str):
        raise ProposalFormatError("currency must be a string")

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
