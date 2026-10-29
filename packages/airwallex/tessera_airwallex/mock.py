"""In-memory Airwallex fake. This is the contract's source of truth until Day 12 and the
adapter that every test and the demo run against.

Behaviour:
- Balances per currency. Conversions pay a configurable spread; the spread is booked as
  `fees_collected[buy_currency]`, so value is conserved at the mid rate:
      bought + fee == sold * mid_rate        (exactly, after 2-dp quantization)
- Idempotent: the same key and request returns the stored result and changes nothing.
  The same key with a different request raises IdempotencyConflictError.
- Deterministic ids (mock-quote-1, ...) and an injectable clock, so tests are reproducible.
"""

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import TypeVar

from pydantic import BaseModel, SecretStr

from tessera_airwallex.errors import (
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidRequestError,
    NotFoundError,
    QuoteExpiredError,
)
from tessera_airwallex.models import (
    BeneficiaryRef,
    Card,
    ConversionResult,
    Transfer,
    TransferStatus,
)
from tessera_airwallex.port import AirwallexPort
from tessera_core.domain import Balance, Currency, FxQuote, Money

T = TypeVar("T", bound=BaseModel)

_RATE_QUANTUM = Decimal("0.00000001")  # 8 dp
_BPS = Decimal(10_000)

# Mid-market value of one unit of each currency, in USD. Tests may override.
DEFAULT_USD_VALUES: dict[Currency, Decimal] = {
    Currency.USD: Decimal("1"),
    Currency.EUR: Decimal("1.08"),
    Currency.GBP: Decimal("1.27"),
    Currency.INR: Decimal("0.012"),
    Currency.SGD: Decimal("0.74"),
    Currency.AUD: Decimal("0.66"),
}


class MockAirwallex(AirwallexPort):
    def __init__(
        self,
        balances: Mapping[Currency, Decimal | int | str] | None = None,
        *,
        spread_bps: Decimal | int | str = Decimal(50),
        usd_values: Mapping[Currency, Decimal] | None = None,
        quote_ttl_seconds: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._balances: dict[Currency, Money] = {c: Money.zero(c) for c in Currency}
        for currency, amount in (balances or {}).items():
            self._balances[currency] = Money(amount=amount, currency=currency)
        spread = Decimal(spread_bps)
        if not (Decimal(0) <= spread < _BPS):
            raise ValueError("spread_bps must be in [0, 10000)")
        self._spread = spread / _BPS
        self._usd = dict(usd_values or DEFAULT_USD_VALUES)
        self._ttl = timedelta(seconds=quote_ttl_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

        self.fees_collected: dict[Currency, Money] = {c: Money.zero(c) for c in Currency}
        self._quotes: dict[str, FxQuote] = {}
        self._used_quotes: set[str] = set()
        self._beneficiaries: dict[str, BeneficiaryRef] = {}
        self._cards: dict[str, Card] = {}
        self._idempotency: dict[str, tuple[str, BaseModel]] = {}
        self._counter = 0

    # ------------------------------------------------------------------ helpers
    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"mock-{prefix}-{self._counter}"

    def usd_value(self, currency: Currency) -> Decimal:
        """Mid-market USD value of one unit of `currency` (test helper for value checks)."""
        return self._usd[currency]

    def mid_rate(self, sell: Currency, buy: Currency) -> Decimal:
        return self._usd[sell] / self._usd[buy]

    def _replay(self, key: str, fingerprint: str, model: type[T]) -> T | None:
        """Return the stored result if `key` was seen with the same request."""
        if not key:
            raise InvalidRequestError("idempotency_key must be non-empty")
        seen = self._idempotency.get(key)
        if seen is None:
            return None
        stored_fingerprint, result = seen
        if stored_fingerprint != fingerprint:
            raise IdempotencyConflictError(
                f"idempotency key {key!r} reused for a different request"
            )
        if not isinstance(result, model):  # pragma: no cover - same fingerprint implies same op
            raise IdempotencyConflictError(f"idempotency key {key!r} belongs to another operation")
        return result

    def _remember(self, key: str, fingerprint: str, result: T) -> T:
        self._idempotency[key] = (fingerprint, result)
        return result

    def _debit(self, amount: Money) -> None:
        balance = self._balances[amount.currency]
        if balance < amount:
            raise InsufficientFundsError(f"need {amount}, available {balance}")
        self._balances[amount.currency] = balance - amount

    def _credit(self, amount: Money) -> None:
        self._balances[amount.currency] = self._balances[amount.currency] + amount

    # ------------------------------------------------------------------ reads
    def get_balances(self) -> list[Balance]:
        return [Balance(currency=c, available=m) for c, m in self._balances.items()]

    def create_fx_quote(self, *, sell: Money, buy_currency: Currency) -> FxQuote:
        if sell.currency == buy_currency:
            raise InvalidRequestError("sell and buy currencies must differ")
        if not sell.is_positive():
            raise InvalidRequestError("sell amount must be positive")
        mid = self.mid_rate(sell.currency, buy_currency)
        # Round DOWN so the customer rate can never exceed the mid rate.
        client_rate = (mid * (1 - self._spread)).quantize(_RATE_QUANTUM, rounding=ROUND_DOWN)
        buy = Money(amount=sell.amount * client_rate, currency=buy_currency)
        if not buy.is_positive():
            raise InvalidRequestError("amount too small to convert")
        quote = FxQuote(
            quote_id=self._next_id("quote"),
            sell=sell,
            buy=buy,
            rate=client_rate,
            expires_at=self._clock() + self._ttl,
        )
        self._quotes[quote.quote_id] = quote
        return quote

    # ------------------------------------------------------------------ mutations
    def execute_conversion(self, *, quote_id: str, idempotency_key: str) -> ConversionResult:
        fingerprint = f"convert:{quote_id}"
        replay = self._replay(idempotency_key, fingerprint, ConversionResult)
        if replay is not None:
            return replay

        quote = self._quotes.get(quote_id)
        if quote is None:
            raise NotFoundError(f"unknown quote {quote_id!r}")
        if quote_id in self._used_quotes:
            raise InvalidRequestError(f"quote {quote_id!r} was already executed")
        now = self._clock()
        if now >= quote.expires_at:
            raise QuoteExpiredError(f"quote {quote_id!r} expired at {quote.expires_at}")

        self._debit(quote.sell)  # raises before anything else changes
        self._credit(quote.buy)
        gross = Money(
            amount=quote.sell.amount * self.mid_rate(quote.sell.currency, quote.buy.currency),
            currency=quote.buy.currency,
        )
        fee = gross - quote.buy
        self.fees_collected[fee.currency] = self.fees_collected[fee.currency] + fee
        self._used_quotes.add(quote_id)

        result = ConversionResult(
            conversion_id=self._next_id("conversion"),
            quote_id=quote_id,
            sold=quote.sell,
            bought=quote.buy,
            executed_at=now,
        )
        return self._remember(idempotency_key, fingerprint, result)

    def create_beneficiary(
        self,
        *,
        name: str,
        currency: Currency,
        account_identifier: SecretStr,
        idempotency_key: str,
    ) -> BeneficiaryRef:
        # The account identifier is part of the fingerprint but never stored or echoed.
        secret_digest = hashlib.sha256(account_identifier.get_secret_value().encode()).hexdigest()
        fingerprint = f"beneficiary:{name}:{currency.value}:{secret_digest}"
        replay = self._replay(idempotency_key, fingerprint, BeneficiaryRef)
        if replay is not None:
            return replay
        if not name.strip():
            raise InvalidRequestError("beneficiary name must be non-empty")
        if not account_identifier.get_secret_value().strip():
            raise InvalidRequestError("account identifier must be non-empty")
        ref = BeneficiaryRef(
            beneficiary_id=self._next_id("beneficiary"),
            name=name,
            currency=currency,
            created_at=self._clock(),
        )
        self._beneficiaries[ref.beneficiary_id] = ref
        return self._remember(idempotency_key, fingerprint, ref)

    def create_transfer(
        self,
        *,
        beneficiary_id: str,
        amount: Money,
        reference: str,
        idempotency_key: str,
    ) -> Transfer:
        fingerprint = f"transfer:{beneficiary_id}:{amount}:{reference}"
        replay = self._replay(idempotency_key, fingerprint, Transfer)
        if replay is not None:
            return replay
        beneficiary = self._beneficiaries.get(beneficiary_id)
        if beneficiary is None:
            raise NotFoundError(f"unknown beneficiary {beneficiary_id!r}")
        if amount.currency != beneficiary.currency:
            raise InvalidRequestError("transfer currency must match the beneficiary currency")
        if not amount.is_positive():
            raise InvalidRequestError("transfer amount must be positive")
        self._debit(amount)
        transfer = Transfer(
            transfer_id=self._next_id("transfer"),
            beneficiary_id=beneficiary_id,
            amount=amount,
            reference=reference,
            status=TransferStatus.SUBMITTED,
            created_at=self._clock(),
        )
        return self._remember(idempotency_key, fingerprint, transfer)

    def create_card(self, *, label: str, monthly_limit: Money, idempotency_key: str) -> Card:
        fingerprint = f"card:{label}:{monthly_limit}"
        replay = self._replay(idempotency_key, fingerprint, Card)
        if replay is not None:
            return replay
        if not label.strip():
            raise InvalidRequestError("card label must be non-empty")
        if not monthly_limit.is_positive():
            raise InvalidRequestError("card limit must be positive")
        card = Card(
            card_id=self._next_id("card"),
            label=label,
            monthly_limit=monthly_limit,
            created_at=self._clock(),
        )
        self._cards[card.card_id] = card
        return self._remember(idempotency_key, fingerprint, card)

    def set_card_limits(self, *, card_id: str, monthly_limit: Money, idempotency_key: str) -> Card:
        fingerprint = f"card-limit:{card_id}:{monthly_limit}"
        replay = self._replay(idempotency_key, fingerprint, Card)
        if replay is not None:
            return replay
        card = self._cards.get(card_id)
        if card is None:
            raise NotFoundError(f"unknown card {card_id!r}")
        if not monthly_limit.is_positive():
            raise InvalidRequestError("card limit must be positive")
        if monthly_limit.currency != card.monthly_limit.currency:
            raise InvalidRequestError("card limit currency cannot change")
        updated = card.model_copy(update={"monthly_limit": monthly_limit})
        self._cards[card_id] = updated
        return self._remember(idempotency_key, fingerprint, updated)
