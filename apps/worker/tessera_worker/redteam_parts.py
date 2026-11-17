"""The pieces of the red-team stack that stand in for the outside world: a vendor that keeps a call log, a world
provider that behaves like the production stub (it supplies fresh context but is NOT trusted to), and a vendor
directory that, unlike the dict used in unit tests, is scoped by business.

Nothing here is a defence. If a defence were built into these pieces, the red team would be testing the pieces.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from pydantic import SecretStr
from sqlalchemy.orm import Session
from tessera_airwallex import MockAirwallex
from tessera_airwallex.models import BeneficiaryRef, Card, ConversionResult, Transfer
from tessera_api.planning import PlanningUnavailable, PlanningWorld
from tessera_core.domain import Currency, Money, Proposal
from tessera_core.policy import AutonomyLevel, PolicyContext
from tessera_evalkit.redteam.stack_api import Move
from tessera_evalkit.redteam.world import AS_OF, World

from tessera_worker.dispatch import CardRef

BALANCES = {Currency.USD: Decimal("5000000"), Currency.EUR: Decimal("5000000"), Currency.INR: Decimal("90000000")}


class RecordingPort(MockAirwallex):
    """The mock vendor. `moves` lists each request the vendor ACCEPTED once (a replay of a stored key adds nothing)."""

    def __init__(self) -> None:
        super().__init__(BALANCES)
        self.moves: list[Move] = []
        self._seen: set[str] = set()

    def _note(self, ident: str, move: Move) -> None:
        if ident not in self._seen:
            self._seen.add(ident)
            self.moves.append(move)

    def create_transfer(self, *, beneficiary_id: str, amount: Money, reference: str, idempotency_key: str) -> Transfer:
        out = super().create_transfer(
            beneficiary_id=beneficiary_id, amount=amount, reference=reference, idempotency_key=idempotency_key
        )
        self._note(out.transfer_id, Move("transfer", str(amount.amount), amount.currency.value, beneficiary_id))
        return out

    def execute_conversion(self, *, quote_id: str, idempotency_key: str) -> ConversionResult:
        out = super().execute_conversion(quote_id=quote_id, idempotency_key=idempotency_key)
        sold = out.sold
        self._note(
            out.conversion_id, Move("conversion", str(sold.amount), sold.currency.value, out.bought.currency.value)
        )
        return out

    def create_card(self, *, label: str, monthly_limit: Money, idempotency_key: str) -> Card:
        out = super().create_card(label=label, monthly_limit=monthly_limit, idempotency_key=idempotency_key)
        self._note(out.card_id, Move("card", str(monthly_limit.amount), monthly_limit.currency.value, out.card_id))
        return out

    def set_card_limits(self, *, card_id: str, monthly_limit: Money, idempotency_key: str) -> Card:
        out = super().set_card_limits(card_id=card_id, monthly_limit=monthly_limit, idempotency_key=idempotency_key)
        self._note(
            f"{card_id}:{monthly_limit}", Move("card", str(monthly_limit.amount), monthly_limit.currency.value, card_id)
        )
        return out


class ScopedDirectory:
    """Tessera beneficiary id -> vendor id, per business. A business cannot resolve another business's payee."""

    def __init__(self) -> None:
        self.beneficiaries: dict[tuple[str, str], str] = {}
        self.cards: dict[tuple[str, str], CardRef] = {}

    def beneficiary(self, business_id: str, beneficiary_id: str) -> str | None:
        return self.beneficiaries.get((business_id, beneficiary_id))

    def card(self, business_id: str, vendor_id: str) -> CardRef | None:
        return self.cards.get((business_id, vendor_id))


def register_payees(port: RecordingPort, directory: ScopedDirectory, world: World) -> None:
    for b in world.beneficiaries:
        ref: BeneficiaryRef = port.create_beneficiary(
            name=b.name, currency=b.currency, account_identifier=SecretStr("000111222"), idempotency_key=f"seed-{b.id}"
        )
        directory.beneficiaries[(world.business, b.id)] = ref.beneficiary_id


class StackWorld:
    """A WorldProvider with the handles an attacker, an operator or the outside world could pull.

    It builds the context the way the production stub does: fixed balances, empty spend, no recent approvals, a kill
    switch it reports and a level it may claim. The API must not take the level or the spend on trust.
    """

    def __init__(self, worlds: dict[str, World], clock: Callable[[], datetime]) -> None:
        self.worlds = worlds
        self.clock = clock
        self.kill = False
        self.claim: int | None = None
        self.edit: Callable[[PolicyContext], PolicyContext] | None = None
        self.down = False

    def load(self, session: Session, business_id: str) -> PlanningWorld:
        if self.down or business_id not in self.worlds:
            raise PlanningUnavailable("no planning data for this business")
        world = self.worlds[business_id]

        def context(proposal: Proposal, flags: tuple[str, ...]) -> PolicyContext:
            ctx = world.policy_context(proposal, flags).model_copy(
                update={"kill_switch_engaged": self.kill, "now": self.clock()}
            )
            if self.claim is not None:
                ctx = ctx.model_copy(update={"autonomy_level": AutonomyLevel(self.claim)})
            return self.edit(ctx) if self.edit is not None else ctx

        return PlanningWorld(snapshot=world.snapshot(()), policy_context=context)


__all__ = ["AS_OF", "BALANCES", "RecordingPort", "ScopedDirectory", "StackWorld", "register_payees"]
