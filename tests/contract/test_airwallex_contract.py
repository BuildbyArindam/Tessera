"""ONE behavioural contract for AirwallexPort, run against every adapter.

  mock             tessera_airwallex.MockAirwallex
  sandbox-replay   SandboxAirwallex over tests/fake_airwallex.py (respx; no network; runs in CI)
  sandbox-live     SandboxAirwallex against the real sandbox; skipped without credentials

A test that cannot be meaningful on an adapter skips on a missing capability:
  clock             the test can move time (not possible against the live sandbox)
  known_funds       starting balances are USD 10000 / EUR 5000 (live wallets are unknown)
  instant_balances  a conversion shows up in get_balances at once (live: SCHEDULED, settles later)
Live conversions run only with AIRWALLEX_LIVE_EXECUTE=1 and a funded USD wallet.

Docs/airwallex-notes.md lists the known mock/sandbox differences; this suite is their executable form.
"""

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import respx
from fake_airwallex import FakeAirwallex
from pydantic import SecretStr
from tessera_airwallex import (
    AirwallexPort,
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidRequestError,
    MockAirwallex,
    NotFoundError,
    QuoteExpiredError,
    SandboxAirwallex,
    SandboxConfig,
)
from tessera_airwallex._http import SandboxHttp
from tessera_core.domain import Currency, Money

USD, EUR = Currency.USD, Currency.EUR
START = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
FUNDS = {USD: Decimal("10000"), EUR: Decimal("5000")}
LIVE_ENV = ("AIRWALLEX_BASE_URL", "AIRWALLEX_CLIENT_ID", "AIRWALLEX_API_KEY")


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Harness:
    name: str
    port: AirwallexPort
    caps: frozenset[str]
    clock: Clock | None = None
    fake: FakeAirwallex | None = None

    def need(self, *caps: str) -> None:
        missing = [c for c in caps if c not in self.caps]
        if missing:
            pytest.skip(f"{self.name} lacks capability: {', '.join(missing)}")

    def need_execute(self) -> None:
        """Skip unless this adapter can run a conversion of 1000 USD."""
        if "live_execute" in self.caps:
            if _available(self.port, USD) < Decimal(1000):
                pytest.skip("live wallet holds less than 1000 USD")
            return
        self.need("known_funds")

    def now(self) -> datetime:
        return self.clock() if self.clock else datetime.now(UTC)


def _available(port: AirwallexPort, currency: Currency) -> Decimal:
    return next(b.available.amount for b in port.get_balances() if b.currency == currency)


def usd(amount: str | int) -> Money:
    return Money(amount=amount, currency=USD)


def new_key() -> str:
    return f"contract-{uuid.uuid4().hex[:20]}"


@pytest.fixture(params=["mock", "sandbox-replay", "sandbox-live"])
def h(request: pytest.FixtureRequest) -> Iterator[Harness]:
    kind: str = request.param
    clock = Clock()
    simulated = frozenset({"clock", "known_funds", "instant_balances"})
    if kind == "mock":
        yield Harness(kind, MockAirwallex(FUNDS, spread_bps=50, clock=clock), simulated, clock)
    elif kind == "sandbox-replay":
        with respx.mock(assert_all_called=False) as router:
            fake = FakeAirwallex(router, clock, FUNDS)
            config = SandboxConfig(
                base_url="https://api.sandbox.airwallex.com",
                client_id=SecretStr(fake.client_id),
                api_key=SecretStr(fake.api_key),
            )
            http = SandboxHttp(config, clock=clock, sleep=lambda _s: None)
            yield Harness(kind, SandboxAirwallex(config, http=http, clock=clock), simulated, clock, fake)
    else:
        if any(not os.environ.get(name) for name in LIVE_ENV):
            pytest.skip("no Airwallex sandbox credentials in the environment")
        caps = {"live_execute"} if os.environ.get("AIRWALLEX_LIVE_EXECUTE") == "1" else set()
        yield Harness(kind, SandboxAirwallex(SandboxConfig.from_env()), frozenset(caps))


# ---------------------------------------------------------------------------- balances
def test_balances_cover_every_currency(h: Harness) -> None:
    balances = h.port.get_balances()
    assert {b.currency for b in balances} == set(Currency)
    for b in balances:
        assert b.available.currency == b.currency
        assert not b.available.is_negative()


def test_known_starting_balances(h: Harness) -> None:
    h.need("known_funds")
    assert _available(h.port, USD) == Decimal("10000.00")
    assert _available(h.port, EUR) == Decimal("5000.00")


# ---------------------------------------------------------------------------- quotes
def test_quote_describes_the_requested_conversion(h: Harness) -> None:
    quote = h.port.create_fx_quote(sell=usd(1000), buy_currency=EUR)
    assert quote.sell == usd(1000)
    assert quote.buy.currency == EUR and quote.buy.is_positive()
    assert quote.rate > 0
    assert abs(quote.sell.amount * quote.rate - quote.buy.amount) <= Decimal("0.01")
    assert quote.expires_at > h.now()


def test_quote_rejects_same_currency(h: Harness) -> None:
    with pytest.raises(InvalidRequestError):
        h.port.create_fx_quote(sell=usd(1000), buy_currency=USD)


def test_quote_rejects_non_positive_amount(h: Harness) -> None:
    with pytest.raises(InvalidRequestError):
        h.port.create_fx_quote(sell=Money.zero(USD), buy_currency=EUR)


def test_quote_rejects_an_amount_too_small_to_convert(h: Harness) -> None:
    with pytest.raises(InvalidRequestError):
        h.port.create_fx_quote(sell=Money(amount="0.01", currency=Currency.INR), buy_currency=USD)


def test_whitespace_only_idempotency_key_is_rejected(h: Harness) -> None:
    quote = h.port.create_fx_quote(sell=usd(100), buy_currency=EUR)
    with pytest.raises(InvalidRequestError):
        h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key="   ")


# ---------------------------------------------------------------------------- conversions
def test_conversion_result_matches_the_quote(h: Harness) -> None:
    h.need_execute()
    quote = h.port.create_fx_quote(sell=usd(1000), buy_currency=EUR)
    result = h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=new_key())
    assert result.quote_id == quote.quote_id
    assert result.sold == quote.sell and result.bought == quote.buy
    assert result.conversion_id


def test_conversion_moves_balances(h: Harness) -> None:
    h.need("known_funds", "instant_balances")
    quote = h.port.create_fx_quote(sell=usd(1000), buy_currency=EUR)
    h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=new_key())
    assert _available(h.port, USD) == Decimal("9000.00")
    assert _available(h.port, EUR) == Decimal("5000.00") + quote.buy.amount


def test_same_key_replays_the_original_and_moves_money_once(h: Harness) -> None:
    h.need_execute()
    key = new_key()
    quote = h.port.create_fx_quote(sell=usd(1000), buy_currency=EUR)
    first = h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=key)
    after_first = _available(h.port, USD)
    second = h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=key)
    assert second == first
    if "instant_balances" in h.caps:
        assert _available(h.port, USD) == after_first


def test_replay_still_works_after_the_quote_expired(h: Harness) -> None:
    """A worker that crashed and restarts after the quote expired must get the original back."""
    h.need("known_funds", "clock")
    assert h.clock is not None
    key = new_key()
    quote = h.port.create_fx_quote(sell=usd(1000), buy_currency=EUR)
    first = h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=key)
    h.clock.advance(3600)
    assert h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=key) == first


def test_key_reused_for_a_different_quote_conflicts(h: Harness) -> None:
    h.need_execute()
    key = new_key()
    first = h.port.create_fx_quote(sell=usd(100), buy_currency=EUR)
    second = h.port.create_fx_quote(sell=usd(200), buy_currency=EUR)
    h.port.execute_conversion(quote_id=first.quote_id, idempotency_key=key)
    with pytest.raises(IdempotencyConflictError):
        h.port.execute_conversion(quote_id=second.quote_id, idempotency_key=key)


def test_executing_a_used_quote_under_a_new_key_is_rejected(h: Harness) -> None:
    h.need("known_funds")
    quote = h.port.create_fx_quote(sell=usd(100), buy_currency=EUR)
    h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=new_key())
    with pytest.raises(InvalidRequestError):
        h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=new_key())


def test_unknown_quote_is_not_found(h: Harness) -> None:
    with pytest.raises(NotFoundError):
        h.port.execute_conversion(quote_id="no-such-quote", idempotency_key=new_key())


def test_empty_idempotency_key_is_rejected(h: Harness) -> None:
    quote = h.port.create_fx_quote(sell=usd(100), buy_currency=EUR)
    with pytest.raises(InvalidRequestError):
        h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key="")


def test_expired_quote_is_refused(h: Harness) -> None:
    h.need("known_funds", "clock")
    assert h.clock is not None
    quote = h.port.create_fx_quote(sell=usd(100), buy_currency=EUR)
    h.clock.advance(120)
    with pytest.raises(QuoteExpiredError):
        h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=new_key())
    assert _available(h.port, USD) == Decimal("10000.00")


def test_a_quote_is_refused_at_its_exact_expiry_instant(h: Harness) -> None:
    h.need("known_funds", "clock")
    assert h.clock is not None
    quote = h.port.create_fx_quote(sell=usd(100), buy_currency=EUR)
    h.clock.advance((quote.expires_at - h.clock()).total_seconds())
    assert h.clock() == quote.expires_at
    with pytest.raises(QuoteExpiredError):
        h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=new_key())


def test_insufficient_funds_changes_nothing(h: Harness) -> None:
    h.need("known_funds", "instant_balances")
    quote = h.port.create_fx_quote(sell=usd(20000), buy_currency=EUR)
    with pytest.raises(InsufficientFundsError):
        h.port.execute_conversion(quote_id=quote.quote_id, idempotency_key=new_key())
    assert _available(h.port, USD) == Decimal("10000.00")
    assert _available(h.port, EUR) == Decimal("5000.00")
