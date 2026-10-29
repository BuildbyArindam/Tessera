from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from tessera_airwallex import (
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidRequestError,
    MockAirwallex,
    NotFoundError,
    QuoteExpiredError,
    TransferStatus,
)
from tessera_core.domain import Currency, Money

USD, EUR, INR = Currency.USD, Currency.EUR, Currency.INR


def money(x: str | int, c: Currency = USD) -> Money:
    return Money(amount=x, currency=c)


def balance(mock: MockAirwallex, c: Currency) -> Decimal:
    return next(b.available.amount for b in mock.get_balances() if b.currency == c)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def mock() -> MockAirwallex:
    return MockAirwallex({USD: "10000", EUR: "5000"}, spread_bps=50)


class TestBalances:
    def test_initial_balances_cover_every_currency(self, mock: MockAirwallex) -> None:
        by_currency = {b.currency: b.available.amount for b in mock.get_balances()}
        assert set(by_currency) == set(Currency)
        assert by_currency[USD] == Decimal("10000.00") and by_currency[INR] == 0


class TestQuotesAndConversion:
    def test_quote_applies_spread_below_mid(self, mock: MockAirwallex) -> None:
        q = mock.create_fx_quote(sell=money("1000"), buy_currency=EUR)
        mid = mock.mid_rate(USD, EUR)
        assert q.rate < mid
        assert q.rate == (mid * Decimal("0.995")).quantize(Decimal("1e-8"), rounding="ROUND_DOWN")
        assert q.buy.currency == EUR and q.sell == money("1000")

    def test_conversion_moves_balances(self, mock: MockAirwallex) -> None:
        q = mock.create_fx_quote(sell=money("1000"), buy_currency=EUR)
        r = mock.execute_conversion(quote_id=q.quote_id, idempotency_key="k1")
        assert balance(mock, USD) == Decimal("9000.00")
        assert balance(mock, EUR) == Decimal("5000.00") + q.buy.amount
        assert r.sold == q.sell and r.bought == q.buy

    def test_conversion_conserves_value_at_mid_rate(self, mock: MockAirwallex) -> None:
        sold = money("1234.56")
        q = mock.create_fx_quote(sell=sold, buy_currency=EUR)
        mock.execute_conversion(quote_id=q.quote_id, idempotency_key="k1")
        fee = mock.fees_collected[EUR]
        gross = Money(amount=sold.amount * mock.mid_rate(USD, EUR), currency=EUR)
        assert q.buy + fee == gross  # exact: bought + spread == sold at mid
        assert fee.is_positive()

        # total system value at mid (in USD) is unchanged up to one cent of rounding
        def total_usd() -> Decimal:
            v = sum(
                (balance(mock, c) + mock.fees_collected[c].amount) * mock.usd_value(c)
                for c in Currency
            )
            return Decimal(v)

        assert abs(total_usd() - (Decimal("10000") + Decimal("5000") * Decimal("1.08"))) < Decimal(
            "0.02"
        )

    def test_zero_spread_gives_zero_fee(self) -> None:
        m = MockAirwallex({USD: "100"}, spread_bps=0)
        q = m.create_fx_quote(sell=money("100"), buy_currency=EUR)
        m.execute_conversion(quote_id=q.quote_id, idempotency_key="k")
        assert m.fees_collected[EUR].is_zero()

    def test_insufficient_funds_changes_nothing(self, mock: MockAirwallex) -> None:
        q = mock.create_fx_quote(sell=money("999999"), buy_currency=EUR)
        with pytest.raises(InsufficientFundsError):
            mock.execute_conversion(quote_id=q.quote_id, idempotency_key="k")
        assert balance(mock, USD) == Decimal("10000.00") and balance(mock, EUR) == Decimal(
            "5000.00"
        )

    def test_expired_quote_rejected(self) -> None:
        clock = Clock()
        m = MockAirwallex({USD: "1000"}, clock=clock, quote_ttl_seconds=30)
        q = m.create_fx_quote(sell=money("100"), buy_currency=EUR)
        clock.now += timedelta(seconds=31)
        with pytest.raises(QuoteExpiredError):
            m.execute_conversion(quote_id=q.quote_id, idempotency_key="k")

    def test_unknown_quote_and_bad_requests(self, mock: MockAirwallex) -> None:
        with pytest.raises(NotFoundError):
            mock.execute_conversion(quote_id="nope", idempotency_key="k")
        with pytest.raises(InvalidRequestError):
            mock.create_fx_quote(sell=money("10"), buy_currency=USD)
        with pytest.raises(InvalidRequestError):
            mock.create_fx_quote(sell=money("0"), buy_currency=EUR)

    def test_a_quote_cannot_be_executed_twice_under_different_keys(
        self, mock: MockAirwallex
    ) -> None:
        q = mock.create_fx_quote(sell=money("100"), buy_currency=EUR)
        mock.execute_conversion(quote_id=q.quote_id, idempotency_key="a")
        with pytest.raises(InvalidRequestError):
            mock.execute_conversion(quote_id=q.quote_id, idempotency_key="b")
        assert balance(mock, USD) == Decimal("9900.00")


class TestIdempotency:
    def test_same_key_same_request_moves_money_once(self, mock: MockAirwallex) -> None:
        q = mock.create_fx_quote(sell=money("1000"), buy_currency=EUR)
        first = mock.execute_conversion(quote_id=q.quote_id, idempotency_key="same")
        usd_after, eur_after = balance(mock, USD), balance(mock, EUR)
        second = mock.execute_conversion(quote_id=q.quote_id, idempotency_key="same")
        assert second == first
        assert (balance(mock, USD), balance(mock, EUR)) == (usd_after, eur_after)
        assert usd_after == Decimal("9000.00")

    def test_same_key_different_request_conflicts(self, mock: MockAirwallex) -> None:
        q1 = mock.create_fx_quote(sell=money("100"), buy_currency=EUR)
        q2 = mock.create_fx_quote(sell=money("200"), buy_currency=EUR)
        mock.execute_conversion(quote_id=q1.quote_id, idempotency_key="k")
        with pytest.raises(IdempotencyConflictError):
            mock.execute_conversion(quote_id=q2.quote_id, idempotency_key="k")

    def test_key_cannot_be_reused_across_operations(self, mock: MockAirwallex) -> None:
        q = mock.create_fx_quote(sell=money("100"), buy_currency=EUR)
        mock.execute_conversion(quote_id=q.quote_id, idempotency_key="k")
        with pytest.raises(IdempotencyConflictError):
            mock.create_card(label="x", monthly_limit=money("10"), idempotency_key="k")

    def test_empty_key_rejected(self, mock: MockAirwallex) -> None:
        q = mock.create_fx_quote(sell=money("100"), buy_currency=EUR)
        with pytest.raises(InvalidRequestError):
            mock.execute_conversion(quote_id=q.quote_id, idempotency_key="")


class TestBeneficiariesAndTransfers:
    def make_beneficiary(self, mock: MockAirwallex, key: str = "b1") -> str:
        ref = mock.create_beneficiary(
            name="Acme Pte",
            currency=Currency.SGD,
            account_identifier=SecretStr("123456789"),
            idempotency_key=key,
        )
        return ref.beneficiary_id

    def test_beneficiary_is_idempotent_and_never_echoes_account(self, mock: MockAirwallex) -> None:
        a = self.make_beneficiary(mock)
        b = self.make_beneficiary(mock)
        assert a == b
        ref = mock.create_beneficiary(
            name="Acme Pte",
            currency=Currency.SGD,
            account_identifier=SecretStr("123456789"),
            idempotency_key="b1",
        )
        assert "123456789" not in repr(ref) and "123456789" not in ref.model_dump_json()

    def test_transfer_debits_once(self) -> None:
        m = MockAirwallex({Currency.SGD: "500"})
        bid = self.make_beneficiary(m)
        t1 = m.create_transfer(
            beneficiary_id=bid,
            amount=money("200", Currency.SGD),
            reference="inv-1",
            idempotency_key="t1",
        )
        t2 = m.create_transfer(
            beneficiary_id=bid,
            amount=money("200", Currency.SGD),
            reference="inv-1",
            idempotency_key="t1",
        )
        assert t1 == t2 and t1.status is TransferStatus.SUBMITTED
        assert balance(m, Currency.SGD) == Decimal("300.00")

    def test_transfer_validation(self) -> None:
        m = MockAirwallex({Currency.SGD: "100", USD: "100"})
        bid = self.make_beneficiary(m)
        with pytest.raises(NotFoundError):
            m.create_transfer(
                beneficiary_id="ghost",
                amount=money("1", Currency.SGD),
                reference="r",
                idempotency_key="t1",
            )
        with pytest.raises(InvalidRequestError):  # wrong currency
            m.create_transfer(
                beneficiary_id=bid, amount=money("1", USD), reference="r", idempotency_key="t2"
            )
        with pytest.raises(InsufficientFundsError):
            m.create_transfer(
                beneficiary_id=bid,
                amount=money("101", Currency.SGD),
                reference="r",
                idempotency_key="t3",
            )
        assert balance(m, Currency.SGD) == Decimal("100.00")


class TestCards:
    def test_create_and_update_limit_idempotently(self, mock: MockAirwallex) -> None:
        card = mock.create_card(label="SaaS", monthly_limit=money("100"), idempotency_key="c1")
        assert (
            mock.create_card(label="SaaS", monthly_limit=money("100"), idempotency_key="c1") == card
        )
        raised = mock.set_card_limits(
            card_id=card.card_id, monthly_limit=money("250"), idempotency_key="c2"
        )
        assert raised.monthly_limit == money("250") and raised.card_id == card.card_id

    def test_card_validation(self, mock: MockAirwallex) -> None:
        with pytest.raises(InvalidRequestError):
            mock.create_card(label="x", monthly_limit=money("0"), idempotency_key="c1")
        with pytest.raises(NotFoundError):
            mock.set_card_limits(card_id="ghost", monthly_limit=money("1"), idempotency_key="c2")
        card = mock.create_card(label="x", monthly_limit=money("10"), idempotency_key="c3")
        with pytest.raises(InvalidRequestError):  # currency change
            mock.set_card_limits(
                card_id=card.card_id, monthly_limit=money("10", EUR), idempotency_key="c4"
            )


def test_spread_bps_validated() -> None:
    with pytest.raises(ValueError, match="spread_bps"):
        MockAirwallex(spread_bps=10_000)
    with pytest.raises(ValueError, match="spread_bps"):
        MockAirwallex(spread_bps=-1)
