"""A stateful in-memory stand-in for the Airwallex sandbox, served through respx.

It speaks the endpoints in docs/airwallex-notes.md and builds its responses from the fixtures
in tests/fixtures/airwallex. It is strict where the reference is explicit (a quote amount must
be a JSON number, a conversion amount a string, `request_id` at most 64 chars) so a client that
gets those wrong fails here. Where the reference is silent it makes an arbitrary choice: a
duplicate `request_id` is rejected with the made-up code `duplicate_request`, which the client
must not depend on.

`inject(...)` adds faults: a status code, a transport error, or a "lost response" (the request
takes effect, then the client sees a 503).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Any

import httpx
import respx
from airwallex_fixtures import load_fixture
from tessera_airwallex import _json
from tessera_airwallex.mock import DEFAULT_USD_VALUES
from tessera_core.domain import Currency

HOST = "api.sandbox.airwallex.com"
BASE = f"https://{HOST}"
VALIDITY_SECONDS = {
    "MIN_1": 60,
    "MIN_15": 900,
    "MIN_30": 1800,
    "HR_1": 3600,
    "HR_4": 14400,
    "HR_8": 28800,
    "HR_24": 86400,
}
_CENT = Decimal("0.01")


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S+0000")


def reply(status: int, payload: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
    content = b"" if payload is None else _json.dumps(payload).encode()
    return httpx.Response(status, content=content, headers={"content-type": "application/json", **(headers or {})})


def error(status: int, code: str, message: str = "") -> httpx.Response:
    return reply(status, {"code": code, "message": message or code})


@dataclass
class Fault:
    method: str
    path: str  # regex searched in the request path
    times: int
    status: int | None = None  # None: raise a transport error
    execute_first: bool = False  # the request takes effect, the response is lost
    headers: dict[str, str] | None = None


class FakeAirwallex:
    def __init__(
        self,
        router: respx.MockRouter,
        clock: Callable[[], datetime],
        balances: dict[Currency, Decimal],
        *,
        client_id: str = "fake-client-id",
        api_key: str = "fake-api-key",
        spread_bps: int = 50,
    ) -> None:
        self.clock, self.client_id, self.api_key = clock, client_id, api_key
        self.balances = {c: balances.get(c, Decimal(0)) for c in Currency}
        self._spread = Decimal(spread_bps) / 10_000
        self.quotes: dict[str, dict[str, Any]] = {}
        self.used_quotes: set[str] = set()
        self.conversions: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.faults: list[Fault] = []
        self.token_lifetime = timedelta(minutes=30)
        self._tokens: dict[str, datetime] = {}
        self._n = 0
        r = router
        r.post(path="/api/v1/authentication/login", host=HOST).mock(side_effect=self._route(self._login))
        r.get(path="/api/v1/balances/current", host=HOST).mock(side_effect=self._route(self._balances))
        r.post(path="/api/v1/fx/quotes/create", host=HOST).mock(side_effect=self._route(self._quote_create))
        r.get(path__regex=r"^/api/v1/fx/quotes/[^/]+$", host=HOST).mock(side_effect=self._route(self._quote_get))
        r.post(path="/api/v1/fx/conversions/create", host=HOST).mock(side_effect=self._route(self._conv_create))
        r.get(path="/api/v1/fx/conversions", host=HOST).mock(side_effect=self._route(self._conv_list))

    # ------------------------------------------------------------------ plumbing
    def inject(self, method: str, path: str, *, times: int = 1, status: int | None = None, **kw: Any) -> None:
        self.faults.append(Fault(method, path, times, status, **kw))

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p in self.calls if m == method and re.search(path, p))

    def _route(self, handler: Callable[[httpx.Request], httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
        def run(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.calls.append((request.method, path))
            for fault in self.faults:
                if fault.times > 0 and fault.method == request.method and re.search(fault.path, path):
                    fault.times -= 1
                    if fault.execute_first:
                        handler(request)
                    if fault.status is None:
                        raise httpx.ConnectError("injected", request=request)
                    return (
                        error(fault.status, "service_unavailable")
                        if fault.headers is None
                        else reply(fault.status, {"code": "x"}, fault.headers)
                    )
            return handler(request)

        return run

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    def _auth(self, request: httpx.Request) -> httpx.Response | None:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        expiry = self._tokens.get(token)
        if expiry is None:
            return error(401, "credentials_invalid")
        if self.clock() >= expiry:
            return error(401, "credentials_expired")
        return None

    def _body(self, request: httpx.Request) -> dict[str, Any]:
        data = _json.loads(request.content)
        assert isinstance(data, dict)
        return data

    # ------------------------------------------------------------------ endpoints
    def _login(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-client-id") != self.client_id or request.headers.get("x-api-key") != self.api_key:
            return error(401, "credentials_invalid")
        token, expiry = self._id("tok"), self.clock() + self.token_lifetime
        self._tokens[token] = expiry
        return reply(201, {"token": token, "expires_at": stamp(expiry)})

    def _balances(self, request: httpx.Request) -> httpx.Response:
        if (denied := self._auth(request)) is not None:
            return denied
        rows = load_fixture("balances_current")
        template = rows[0]
        cash = [
            {**template, "currency": c.value, "available_amount": a, "total_amount": a}
            for c, a in self.balances.items()
        ]
        return reply(200, cash + rows[2:])  # plus the fixture's yield-account and JPY rows

    def _quote_create(self, request: httpx.Request) -> httpx.Response:
        if (denied := self._auth(request)) is not None:
            return denied
        body = self._body(request)
        sell_amount = body.get("sell_amount")
        if isinstance(sell_amount, bool) or not isinstance(sell_amount, int | Decimal):
            return error(400, "invalid_argument", "sell_amount must be a number")
        if body.get("validity") not in VALIDITY_SECONDS:
            return error(400, "invalid_argument", "bad validity")
        try:
            sell_c, buy_c = Currency(body["sell_currency"]), Currency(body["buy_currency"])
        except (KeyError, ValueError):
            return error(400, "unsupported_currency")
        if sell_c == buy_c or Decimal(sell_amount) <= 0:
            return error(400, "invalid_argument")
        mid = DEFAULT_USD_VALUES[sell_c] / DEFAULT_USD_VALUES[buy_c]
        rate = (mid * (1 - self._spread)).quantize(Decimal("1e-8"), rounding=ROUND_DOWN)
        sell = Decimal(sell_amount).quantize(_CENT)
        buy = (sell * rate).quantize(_CENT, rounding=ROUND_HALF_EVEN)
        now = self.clock()
        quote = {
            **load_fixture("quote_create"),
            "quote_id": self._id("q"),
            "sell_currency": sell_c.value,
            "buy_currency": buy_c.value,
            "sell_amount": sell,
            "buy_amount": buy,
            "client_rate": rate,
            "valid_from_at": stamp(now),
            "valid_to_at": stamp(now + timedelta(seconds=VALIDITY_SECONDS[body["validity"]])),
            "validity": body["validity"],
        }
        self.quotes[quote["quote_id"]] = quote
        return reply(201, quote)

    def _quote_get(self, request: httpx.Request) -> httpx.Response:
        if (denied := self._auth(request)) is not None:
            return denied
        quote = self.quotes.get(request.url.path.rsplit("/", 1)[1])
        return reply(200, quote) if quote else error(404, "not_found")

    def _conv_create(self, request: httpx.Request) -> httpx.Response:
        if (denied := self._auth(request)) is not None:
            return denied
        body = self._body(request)
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
            return error(400, "invalid_argument", "request_id must be 1 to 64 characters")
        if not isinstance(body.get("sell_amount"), str):
            return error(400, "invalid_argument", "sell_amount must be a string")
        if any(c["request_id"] == request_id for c in self.conversions):
            return error(400, "duplicate_request")
        quote = self.quotes.get(str(body.get("quote_id")))
        if quote is None:
            return error(400, "invalid_quote_id")
        if body["quote_id"] in self.used_quotes:
            return error(400, "operation_failed", "quote already used")
        if self.clock() > _parse(quote["valid_to_at"]):
            return error(400, "expired_quote")
        if (body["sell_currency"], body["buy_currency"]) != (quote["sell_currency"], quote["buy_currency"]):
            return error(400, "conflict_original_ccy")
        sell_c, buy_c = Currency(quote["sell_currency"]), Currency(quote["buy_currency"])
        if self.balances[sell_c] < quote["sell_amount"]:
            return error(400, "insufficient_funds")
        self.balances[sell_c] -= quote["sell_amount"]
        self.balances[buy_c] += quote["buy_amount"]
        self.used_quotes.add(body["quote_id"])
        conversion = {
            **load_fixture("conversion_create"),
            "conversion_id": self._id("c"),
            "request_id": request_id,
            "quote_id": quote["quote_id"],
            "sell_currency": sell_c.value,
            "buy_currency": buy_c.value,
            "sell_amount": quote["sell_amount"],
            "buy_amount": quote["buy_amount"],
            "client_rate": quote["client_rate"],
            "created_at": stamp(self.clock()),
        }
        self.conversions.append(conversion)
        return reply(201, conversion)

    def _conv_list(self, request: httpx.Request) -> httpx.Response:
        if (denied := self._auth(request)) is not None:
            return denied
        wanted = request.url.params.get("request_id")
        items = [c for c in reversed(self.conversions) if wanted is None or c["request_id"] == wanted]
        return reply(200, {"has_more": False, "items": items})


def _parse(value: str) -> datetime:
    from tessera_airwallex._timeparse import parse_timestamp

    return parse_timestamp(value)
