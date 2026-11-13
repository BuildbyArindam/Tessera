"""SandboxAirwallex over canned responses: parsing, request bodies, idempotency reconciliation."""

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from airwallex_fixtures import load_fixture
from pydantic import SecretStr
from tessera_airwallex import (
    IdempotencyConflictError,
    InsufficientFundsError,
    InvalidRequestError,
    NotFoundError,
    SandboxAirwallex,
    SandboxConfig,
    UnknownError,
    _json,
)
from tessera_airwallex._http import SandboxHttp
from tessera_airwallex.sandbox import request_id_for
from tessera_core.domain import Currency, Money

BASE = "https://api.sandbox.airwallex.com"
START = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)  # quote fixture is valid until 10:01
USD, EUR = Currency.USD, Currency.EUR
QUOTE_ID = "q-fixture-0001"


def usd(x: str) -> Money:
    return Money(amount=x, currency=USD)


def plain(name: str) -> Any:
    """Fixture with numbers as Decimal (a float round trip would lose digits)."""
    return load_fixture(name)


def send(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=_json.dumps(payload).encode(), headers={"content-type": "application/json"})


class Rig:
    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router
        router.post(f"{BASE}/api/v1/authentication/login").mock(return_value=send(plain("login"), 201))
        self.balances = router.get(f"{BASE}/api/v1/balances/current").mock(return_value=send(plain("balances_current")))
        self.quote_create = router.post(f"{BASE}/api/v1/fx/quotes/create").mock(
            return_value=send(plain("quote_create"), 201)
        )
        self.quote_get = router.get(f"{BASE}/api/v1/fx/quotes/{QUOTE_ID}").mock(
            return_value=send(plain("quote_create"))
        )
        self.conv_create = router.post(f"{BASE}/api/v1/fx/conversions/create").mock(
            return_value=send(plain("conversion_create"), 201)
        )
        self.conv_list = router.get(f"{BASE}/api/v1/fx/conversions").mock(
            return_value=send({"has_more": False, "items": []})
        )
        cfg = SandboxConfig(base_url=BASE, client_id=SecretStr("cid"), api_key=SecretStr("key"))
        self.http = SandboxHttp(cfg, clock=lambda: START, sleep=lambda _s: None)
        self.port = SandboxAirwallex(cfg, http=self.http, clock=lambda: START)

    def convert(self, key: str = "key-1", quote_id: str = QUOTE_ID) -> Any:
        return self.port.execute_conversion(quote_id=quote_id, idempotency_key=key)

    def created_body(self) -> dict[str, Any]:
        return json.loads(self.conv_create.calls.last.request.content)


@pytest.fixture
def rig() -> Iterator[Rig]:
    with respx.mock(assert_all_called=False) as router:
        yield Rig(router)


def conversion(**changes: Any) -> dict[str, Any]:
    return {**plain("conversion_create"), **changes}


class TestBalances:
    def test_cash_only_supported_currencies_and_exact_decimals(self, rig: Rig) -> None:
        by = {b.currency: b.available.amount for b in rig.port.get_balances()}
        assert by[USD] == Decimal("10000.10")  # the 777.77 yield row is ignored
        assert by[EUR] == Decimal("5000.00")
        assert set(by) == set(Currency)  # JPY is ignored, missing currencies are zero
        assert by[Currency.INR] == 0

    def test_only_the_cash_account_type_is_requested(self, rig: Rig) -> None:
        rig.port.get_balances()
        assert rig.balances.calls.last.request.url.params["account_type"] == "cash"

    @pytest.mark.parametrize(
        "payload", [{"x": 1}, [{"currency": "USD"}], [{"currency": "USD", "available_amount": "abc"}]]
    )
    def test_malformed_responses_are_unknown_errors(self, rig: Rig, payload: Any) -> None:
        rig.balances.mock(return_value=send(payload))
        with pytest.raises(UnknownError):
            rig.port.get_balances()


class TestQuotes:
    def test_amounts_and_rate_are_decimals_never_floats(self, rig: Rig) -> None:
        quote = rig.port.create_fx_quote(sell=usd("1000"), buy_currency=EUR)
        assert quote.rate == Decimal("0.92345678901234567")  # 17 digits: a float would round it
        assert quote.sell.amount == Decimal("1000.00") and quote.buy.amount == Decimal("923.46")
        assert quote.expires_at == datetime(2026, 11, 2, 10, 1, tzinfo=UTC)

    def test_the_amount_goes_out_as_a_json_number(self, rig: Rig) -> None:
        body = {**plain("quote_create"), "sell_amount": Decimal("1000.50"), "buy_amount": Decimal("923.92")}
        rig.quote_create.mock(return_value=send(body, 201))
        rig.port.create_fx_quote(sell=usd("1000.50"), buy_currency=EUR)
        raw = rig.quote_create.calls.last.request.content
        assert re.search(rb'"sell_amount":1000\.50[,}]', raw), raw
        sent = json.loads(raw)
        assert (sent["sell_currency"], sent["buy_currency"], sent["validity"]) == ("USD", "EUR", "MIN_1")

    def test_an_inverted_client_rate_is_normalised_using_the_amounts(self, rig: Rig) -> None:
        body = {
            **plain("quote_create"),
            "sell_amount": Decimal(1000),
            "buy_amount": Decimal(925),
            "client_rate": Decimal("1.081081081"),
        }
        rig.quote_create.mock(return_value=send(body, 201))
        quote = rig.port.create_fx_quote(sell=usd("1000"), buy_currency=EUR)
        assert abs(quote.rate - Decimal("0.925")) < Decimal("1e-8")

    def test_a_rate_that_matches_neither_direction_is_refused(self, rig: Rig) -> None:
        rig.quote_create.mock(return_value=send({**plain("quote_create"), "client_rate": 2}, 201))
        with pytest.raises(UnknownError):
            rig.port.create_fx_quote(sell=usd("1000"), buy_currency=EUR)

    @pytest.mark.parametrize("changes", [{"sell_amount": 999}, {"buy_currency": "GBP"}, {"sell_currency": "SGD"}])
    def test_a_quote_for_a_different_conversion_is_refused(self, rig: Rig, changes: dict[str, Any]) -> None:
        rig.quote_create.mock(return_value=send({**plain("quote_create"), **changes}, 201))
        with pytest.raises(UnknownError):
            rig.port.create_fx_quote(sell=usd("1000"), buy_currency=EUR)

    def test_bad_requests_never_reach_the_network(self, rig: Rig) -> None:
        with pytest.raises(InvalidRequestError):
            rig.port.create_fx_quote(sell=usd("1000"), buy_currency=USD)
        with pytest.raises(InvalidRequestError):
            rig.port.create_fx_quote(sell=Money.zero(USD), buy_currency=EUR)
        assert rig.quote_create.call_count == 0


class TestRequestId:
    def test_plain_short_keys_are_used_as_is(self) -> None:
        assert request_id_for("tessera-exec_01-abc") == "tessera-exec_01-abc"

    @pytest.mark.parametrize("key", ["x" * 65, "has space", "unicode-é", "a/b", "dot.ted"])
    def test_other_keys_are_hashed_to_64_hex_chars(self, key: str) -> None:
        out = request_id_for(key)
        assert out == hashlib.sha256(key.encode()).hexdigest() and len(out) == 64

    def test_the_mapping_is_deterministic_and_distinct(self) -> None:
        assert request_id_for("x" * 65) == request_id_for("x" * 65) != request_id_for("x" * 66)

    @pytest.mark.parametrize("key", ["", "   "])
    def test_empty_keys_are_rejected(self, key: str) -> None:
        with pytest.raises(InvalidRequestError):
            request_id_for(key)


class TestConversion:
    def test_request_body_and_result(self, rig: Rig) -> None:
        result = rig.convert("key-1")
        assert rig.created_body() == {
            "buy_currency": "EUR",
            "sell_currency": "USD",
            "sell_amount": "1000.00",  # a string, as the reference documents
            "quote_id": QUOTE_ID,
            "request_id": "key-1",
        }
        assert (result.conversion_id, result.quote_id) == ("c-fixture-0001", QUOTE_ID)
        assert result.sold == usd("1000") and result.bought == Money(amount="923.46", currency=EUR)
        assert result.executed_at == datetime(2026, 11, 2, 10, 0, 20, tzinfo=UTC)

    def test_an_existing_conversion_is_returned_without_creating_another(self, rig: Rig) -> None:
        rig.conv_list.mock(return_value=send({"has_more": False, "items": [conversion(request_id="key-1")]}))
        assert rig.convert("key-1").conversion_id == "c-fixture-0001"
        assert rig.conv_create.call_count == 0 and rig.quote_get.call_count == 0
        assert rig.conv_list.calls.last.request.url.params["request_id"] == "key-1"

    def test_list_items_with_another_request_id_are_ignored(self, rig: Rig) -> None:
        rig.conv_list.mock(return_value=send({"has_more": False, "items": [conversion(request_id="someone-else")]}))
        rig.convert("key-1")
        assert rig.conv_create.call_count == 1

    def test_the_same_key_for_another_quote_is_a_conflict(self, rig: Rig) -> None:
        rig.conv_list.mock(
            return_value=send({"has_more": False, "items": [conversion(request_id="key-1", quote_id="q-other")]})
        )
        with pytest.raises(IdempotencyConflictError):
            rig.convert("key-1")
        assert rig.conv_create.call_count == 0

    def test_a_cancelled_conversion_is_not_replayed_as_success(self, rig: Rig) -> None:
        rig.conv_list.mock(
            return_value=send({"has_more": False, "items": [conversion(request_id="key-1", status="CANCELLED")]})
        )
        with pytest.raises(InvalidRequestError):
            rig.convert("key-1")

    def test_a_quote_id_that_could_alter_the_path_is_not_found_without_a_call(self, rig: Rig) -> None:
        with pytest.raises(NotFoundError):
            rig.convert("key-1", quote_id="../balances/current")
        assert rig.quote_get.call_count == 0 and rig.balances.call_count == 0

    def test_a_missing_quote_is_not_found(self, rig: Rig) -> None:
        rig.quote_get.mock(return_value=send({"code": "not_found"}, 404))
        with pytest.raises(NotFoundError):
            rig.convert()


class TestReconciliation:
    """The reference only says a repeated request_id is 'rejected'. These prove the adapter does not depend on how."""

    def _found_second_time(self, rig: Rig) -> None:
        empty = send({"has_more": False, "items": []})
        found = send({"has_more": False, "items": [conversion(request_id="key-1")]})
        rig.conv_list.mock(side_effect=[empty, found])

    def test_a_lost_response_is_recovered_by_lookup(self, rig: Rig) -> None:
        rig.conv_create.mock(return_value=httpx.Response(503))
        self._found_second_time(rig)
        assert rig.convert("key-1").conversion_id == "c-fixture-0001"
        assert rig.conv_create.call_count == 4  # first try + 3 retries, all with the same request_id
        assert {json.loads(c.request.content)["request_id"] for c in rig.conv_create.calls} == {"key-1"}

    def test_an_unrecognised_rejection_is_recovered_by_lookup(self, rig: Rig) -> None:
        rig.conv_create.mock(return_value=send({"code": "some_code_nobody_documented"}, 400))
        self._found_second_time(rig)
        assert rig.convert("key-1").conversion_id == "c-fixture-0001"

    def test_a_rejection_with_no_body_is_recovered_by_lookup(self, rig: Rig) -> None:
        rig.conv_create.mock(return_value=httpx.Response(409))
        self._found_second_time(rig)
        assert rig.convert("key-1").conversion_id == "c-fixture-0001"

    def test_nothing_found_means_the_original_error_is_raised(self, rig: Rig) -> None:
        rig.conv_create.mock(return_value=send({"code": "some_code_nobody_documented"}, 400))
        with pytest.raises(InvalidRequestError) as err:
            rig.convert("key-1")
        assert err.value.code == "some_code_nobody_documented" and rig.conv_list.call_count == 2

    def test_an_ambiguous_failure_with_nothing_found_stays_unknown(self, rig: Rig) -> None:
        rig.conv_create.mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(UnknownError):
            rig.convert("key-1")

    def test_a_documented_business_error_is_raised_without_a_second_lookup(self, rig: Rig) -> None:
        rig.conv_create.mock(return_value=send({"code": "insufficient_funds"}, 400))
        with pytest.raises(InsufficientFundsError):
            rig.convert("key-1")
        assert rig.conv_list.call_count == 1 and rig.conv_create.call_count == 1  # only the pre-check lookup

    def test_a_malformed_created_response_is_unknown(self, rig: Rig) -> None:
        rig.conv_create.mock(return_value=send({"conversion_id": "c1"}, 201))
        with pytest.raises(UnknownError):
            rig.convert()

    def test_lookup_pages_are_followed_but_bounded(self, rig: Rig) -> None:
        rig.conv_list.mock(return_value=send({"has_more": True, "items": []}))
        rig.convert("key-1")  # never found: creates
        assert rig.conv_list.call_count == 10 and rig.conv_create.call_count == 1
