"""Reading the FX exposure out of a proposal, strictly.

What each action means to the simulator (the amounts go through the same strict parser the policy
rules use, so the number simulated is the number judged):

| Action        | params read                          | `amount` is...                  |
| CONVERT       | amount, currency, to_currency        | SELL units given up, now        |
| PAY_SUPPLIER  | amount, currency, from_currency      | BUY units the supplier receives |
| HOLD          | amount, currency, to_currency        | SELL units left unconverted     |

`from_currency` on a PAY_SUPPLIER is the currency the business funds the payout from. It is read
from the proposal today because nothing else carries it yet; the worker must replace it with the
funding currency of the real account before anything executes (see docs/daily-log.md, Day 8).
A payout funded in its own currency needs no conversion, so it has no FX exposure.

TOPUP_CARD and FLAG_INVOICE have no FX decision to simulate and are refused here.
"""

from dataclasses import dataclass
from decimal import Decimal

from tessera_core.domain.actions import ActionType, Proposal
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money
from tessera_core.policy.proposal_view import ProposalFormatError, buy_currency, currency_param, read_amount
from tessera_core.simulate.series import SimulationInputError

SIMULATED_ACTIONS: frozenset[ActionType] = frozenset({ActionType.CONVERT, ActionType.PAY_SUPPLIER, ActionType.HOLD})


@dataclass(frozen=True)
class Exposure:
    action: ActionType
    sell: Currency
    buy: Currency
    amount: Decimal  # SELL units for CONVERT and HOLD; BUY units (the payout) for PAY_SUPPLIER

    @property
    def pair(self) -> str:
        return f"{self.sell.value}/{self.buy.value}"

    @property
    def amount_money(self) -> Money:
        """`amount` with the currency the proposal wrote it in (the payout currency for PAY_SUPPLIER)."""
        return Money(amount=self.amount, currency=self.buy if self.action is ActionType.PAY_SUPPLIER else self.sell)

    @property
    def needs_fx(self) -> bool:
        return self.sell != self.buy


def exposure_from_proposal(proposal: Proposal) -> Exposure:
    action = proposal.action_type
    if action not in SIMULATED_ACTIONS:
        raise SimulationInputError(f"{action.value} has no FX exposure to simulate")
    try:
        money = read_amount(proposal)
        if action is ActionType.PAY_SUPPLIER:
            sell, buy = currency_param(proposal, "from_currency"), money.currency
        else:
            sell, buy = money.currency, buy_currency(proposal)
    except ProposalFormatError as exc:
        raise SimulationInputError(f"cannot simulate: {exc}") from None
    if action is not ActionType.PAY_SUPPLIER and sell == buy:
        raise SimulationInputError("cannot simulate: sell and buy currency must differ")
    return Exposure(action=action, sell=sell, buy=buy, amount=money.amount)
