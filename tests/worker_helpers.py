"""Shared builders for the Day 13 worker tests: a flaky mock port, a dict directory and a world that can change."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

from sqlalchemy.orm import Session, sessionmaker
from tessera_agent import FakeLLM
from tessera_airwallex import MockAirwallex, UnknownError
from tessera_airwallex.models import BeneficiaryRef, Card, ConversionResult, Transfer
from tessera_api.planning import PlanningWorld
from tessera_core.domain import Currency, Money, Proposal
from tessera_core.policy import PolicyContext, load_ruleset
from tessera_store import models as m
from tessera_worker.dispatch import CardRef
from tessera_worker.loop import LoopDeps
from tessera_worker.submit import ExecDeps

T = TypeVar("T")
T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
BALANCES = {Currency.EUR: Decimal("50000"), Currency.INR: Decimal("1000000"), Currency.USD: Decimal("20000")}


class FlakyMock(MockAirwallex):
    """The mock, with a log of calls and two ways to fail: before the vendor acts, or after it has (response lost)."""

    def __init__(self) -> None:
        super().__init__(BALANCES)
        self.calls: list[str] = []
        self.fail_before = 0
        self.fail_after = 0
        self.on_call: Callable[[str], None] | None = None

    def _go(self, name: str, fn: Callable[[], T]) -> T:
        self.calls.append(name)
        if self.on_call is not None:
            self.on_call(name)
        if self.fail_before:
            self.fail_before -= 1
            raise UnknownError("timeout before the vendor acted")
        result = fn()
        if self.fail_after:
            self.fail_after -= 1
            raise UnknownError("the vendor acted but the response was lost")
        return result

    def execute_conversion(self, *, quote_id: str, idempotency_key: str) -> ConversionResult:
        return self._go(
            "conversion",
            lambda: super(FlakyMock, self).execute_conversion(quote_id=quote_id, idempotency_key=idempotency_key),
        )

    def create_transfer(self, *, beneficiary_id: str, amount: Money, reference: str, idempotency_key: str) -> Transfer:
        return self._go(
            "transfer",
            lambda: super(FlakyMock, self).create_transfer(
                beneficiary_id=beneficiary_id, amount=amount, reference=reference, idempotency_key=idempotency_key
            ),
        )

    def create_card(self, *, label: str, monthly_limit: Money, idempotency_key: str) -> Card:
        return self._go(
            "card",
            lambda: super(FlakyMock, self).create_card(
                label=label, monthly_limit=monthly_limit, idempotency_key=idempotency_key
            ),
        )

    def set_card_limits(self, *, card_id: str, monthly_limit: Money, idempotency_key: str) -> Card:
        return self._go(
            "limit",
            lambda: super(FlakyMock, self).set_card_limits(
                card_id=card_id, monthly_limit=monthly_limit, idempotency_key=idempotency_key
            ),
        )


class DictDirectory:
    def __init__(self, beneficiaries: dict[str, str] | None = None, cards: dict[str, CardRef] | None = None) -> None:
        self.beneficiaries, self.cards = beneficiaries or {}, cards or {}

    def beneficiary(self, business_id: str, beneficiary_id: str) -> str | None:
        return self.beneficiaries.get(beneficiary_id)

    def card(self, business_id: str, vendor_id: str) -> CardRef | None:
        return self.cards.get(vendor_id)


class ChangingWorld:
    """A WorldProvider whose answer can change between planning and execution. `change` edits the context."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.change: Callable[[PolicyContext], PolicyContext] | None = None
        self.unavailable = False

    def load(self, session: Session, business_id: str) -> PlanningWorld:
        if self.unavailable:
            raise RuntimeError("the books are down")
        world = self.inner.load(session, business_id)
        change = self.change
        if change is None:
            return world

        def context(proposal: Proposal, flags: tuple[str, ...]) -> PolicyContext:
            return change(world.policy_context(proposal, flags))

        return PlanningWorld(snapshot=world.snapshot, policy_context=context)


def ticking_clock(start: datetime = T0) -> Callable[[], datetime]:
    ticks = iter(range(1_000_000))
    return lambda: start + timedelta(seconds=next(ticks))


def exec_deps(
    factory: sessionmaker[Session], port: MockAirwallex, world: Any, directory: DictDirectory | None = None, **over: Any
) -> ExecDeps:
    base: dict[str, Any] = dict(
        session_factory=factory,
        port=port,
        directory=directory or DictDirectory(),
        world_provider=world,
        ruleset=load_ruleset(),
        clock=ticking_clock(),
        max_age=timedelta(hours=24),
    )
    return ExecDeps(**{**base, **over})


def loop_deps(exec_: ExecDeps, llm: Callable[[], FakeLLM]) -> LoopDeps:
    return LoopDeps(exec=exec_, llm_factory=llm)


def seed_proposal(
    session: Session, *, status: str, proposal_id: str = "prp-1", action: str = "PAY_SUPPLIER", business: str = "B0"
) -> None:
    if session.get(m.Business, business) is None:
        session.add(m.Business(id=business, name="Test", base_currency="INR"))
    session.add(
        m.Proposal(
            id=proposal_id,
            business_id=business,
            action_type=action,
            params={},
            rationale="r",
            confidence=0.5,
            status=status,
            plan_record=None,
            proposed_at=T0,
        )
    )
    session.commit()


def balance(port: MockAirwallex, currency: Currency) -> Decimal:
    return next(b.available.amount for b in port.get_balances() if b.available.currency == currency)


__all__ = ["BeneficiaryRef"]  # re-exported for tests that build a beneficiary on the mock
