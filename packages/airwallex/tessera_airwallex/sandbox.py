"""Real Airwallex sandbox adapter. Balances, FX quotes and conversions live here (Day 12); beneficiaries and
transfers are in _payouts.py and virtual cards in _cards.py (Day 13), mixed into the class below.

Every endpoint and field used here is recorded in docs/airwallex-notes.md with its source.

Idempotency. The port promises that replaying a key returns the original result. Airwallex
only documents that a repeated `request_id` is "rejected", so this adapter:
  1. maps the idempotency key to a `request_id` (the key itself if it is short and plain,
     otherwise its SHA-256);
  2. before creating, looks the conversion up by `request_id` and returns it if it exists
     (this is what makes a restart after a crash safe, even after the quote has expired);
  3. after an unknown outcome or an unclassifiable 4xx on create, looks it up again.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote as url_quote

from tessera_core.domain import Balance, Currency, FxQuote, Money

from tessera_airwallex._cards import CardsMixin
from tessera_airwallex._config import CardConfig, PayoutConfig
from tessera_airwallex._errormap import needs_reconcile
from tessera_airwallex._http import SandboxConfig, SandboxHttp
from tessera_airwallex._payouts import PayoutsMixin
from tessera_airwallex._timeparse import parse_timestamp
from tessera_airwallex._values import MAX_LIST_PAGES, close, dec, money
from tessera_airwallex._values import request_id_for as request_id_for  # re-exported: tests and the recorder use it
from tessera_airwallex.errors import (
    AirwallexError,
    IdempotencyConflictError,
    InvalidRequestError,
    NotFoundError,
    QuoteExpiredError,
    UnknownError,
)
from tessera_airwallex.models import ConversionResult
from tessera_airwallex.port import AirwallexPort

BALANCES_PATH = "/api/v1/balances/current"
QUOTE_CREATE_PATH = "/api/v1/fx/quotes/create"
QUOTE_PATH = "/api/v1/fx/quotes/{quote_id}"
CONVERSION_CREATE_PATH = "/api/v1/fx/conversions/create"
CONVERSIONS_PATH = "/api/v1/fx/conversions"

_QUOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class SandboxAirwallex(PayoutsMixin, CardsMixin, AirwallexPort):
    def __init__(
        self,
        config: SandboxConfig | None = None,
        *,
        http: SandboxHttp | None = None,
        clock: Callable[[], datetime] | None = None,
        quote_validity: str = "MIN_1",
        payouts: PayoutConfig | None = None,
        cards: CardConfig | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._http = http or SandboxHttp(config or SandboxConfig.from_env(), clock=self._clock)
        self._validity = quote_validity
        self._payouts = payouts or PayoutConfig.from_env()
        self._cards = cards or CardConfig.from_env()

    # ------------------------------------------------------------------ reads
    def get_balances(self) -> list[Balance]:
        data = self._http.request("GET", BALANCES_PATH, params={"account_type": "cash"})
        if not isinstance(data, list):
            raise UnknownError("balances response was not a list")
        totals = {c: Decimal(0) for c in Currency}
        try:
            for item in data:
                if item.get("account_type", "cash") != "cash":
                    continue
                try:
                    currency = Currency(item["currency"])
                except ValueError:
                    continue  # a currency Tessera does not trade
                totals[currency] += dec(item["available_amount"])
            return [Balance(currency=c, available=Money(amount=a, currency=c)) for c, a in totals.items()]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise UnknownError("malformed balances response") from exc

    def create_fx_quote(self, *, sell: Money, buy_currency: Currency) -> FxQuote:
        if sell.currency == buy_currency:
            raise InvalidRequestError("sell and buy currencies must differ")
        if not sell.is_positive():
            raise InvalidRequestError("sell amount must be positive")
        body = {
            "buy_currency": buy_currency.value,
            "sell_currency": sell.currency.value,
            "sell_amount": sell.amount,
            "validity": self._validity,
        }
        quote = self._quote_from(self._http.request("POST", QUOTE_CREATE_PATH, body=body))
        if quote.sell != sell or quote.buy.currency != buy_currency:
            # Never hand policy a quote for a different conversion than the one asked for.
            raise UnknownError("quote response does not match the request")
        return quote

    # ------------------------------------------------------------------ mutations
    def execute_conversion(self, *, quote_id: str, idempotency_key: str) -> ConversionResult:
        request_id = request_id_for(idempotency_key)
        existing = self._find_conversion(request_id)
        if existing is not None:
            return self._replayed(existing, quote_id)

        if not _QUOTE_ID.match(quote_id):
            raise NotFoundError(f"unknown quote {quote_id!r}")
        quote = self._quote_from(self._http.request("GET", QUOTE_PATH.format(quote_id=url_quote(quote_id, safe=""))))
        # `>=`, like the mock: the reference calls valid_to_at inclusive, but never submit on the last instant.
        if self._clock() >= quote.expires_at:
            raise QuoteExpiredError(f"quote {quote_id!r} expired at {quote.expires_at.isoformat()}")

        body = {
            "buy_currency": quote.buy.currency.value,
            "sell_currency": quote.sell.currency.value,
            "sell_amount": str(quote.sell.amount),
            "quote_id": quote.quote_id,
            "request_id": request_id,
        }
        try:
            data = self._http.request("POST", CONVERSION_CREATE_PATH, body=body)
        except AirwallexError as exc:
            if not needs_reconcile(exc):
                raise
            found = self._find_conversion(request_id)
            if found is None:
                raise
            return self._replayed(found, quote_id)
        return self._result_from(data, quote_id)

    # ------------------------------------------------------------------ parsing
    def _quote_from(self, data: Any) -> FxQuote:
        try:
            sell = money(data, "sell_amount", "sell_currency")
            buy = money(data, "buy_amount", "buy_currency")
            if not buy.is_positive():
                raise InvalidRequestError("amount too small to convert")
            rate = self._consistent_rate(dec(data["client_rate"]), sell, buy)
            return FxQuote(
                quote_id=str(data["quote_id"]),
                sell=sell,
                buy=buy,
                rate=rate,
                expires_at=parse_timestamp(data["valid_to_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise UnknownError("malformed quote response") from exc

    @staticmethod
    def _consistent_rate(rate: Decimal, sell: Money, buy: Money) -> Decimal:
        """The rate in buy-per-sell terms, checked against the amounts.

        The reference does not say in which direction `client_rate` is quoted, so the amounts
        decide: use the rate if sell * rate matches buy, else its inverse, else refuse.
        """
        if rate <= 0:
            raise ValueError("rate must be positive")
        if close(sell.amount * rate, buy.amount):
            return rate
        inverse = (1 / rate).quantize(Decimal("1e-10"))
        if close(sell.amount * inverse, buy.amount):
            return inverse
        raise ValueError("client_rate does not match the quoted amounts")

    def _result_from(self, data: Any, requested_quote_id: str) -> ConversionResult:
        try:
            return ConversionResult(
                conversion_id=str(data["conversion_id"]),
                quote_id=str(data.get("quote_id") or requested_quote_id),
                sold=money(data, "sell_amount", "sell_currency"),
                bought=money(data, "buy_amount", "buy_currency"),
                executed_at=parse_timestamp(data["created_at"]),
            )
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise UnknownError("malformed conversion response") from exc

    def _replayed(self, item: dict[str, Any], quote_id: str) -> ConversionResult:
        if item.get("status") == "CANCELLED":
            raise InvalidRequestError(f"conversion {item.get('conversion_id')!r} was cancelled", code="cancelled")
        original = item.get("quote_id")
        if original and original != quote_id:
            raise IdempotencyConflictError("idempotency key was already used for a different quote")
        return self._result_from(item, quote_id)

    def _find_conversion(self, request_id: str) -> dict[str, Any] | None:
        """The conversion created with this request_id, if any. Filtering is re-checked client-side."""
        for page in range(MAX_LIST_PAGES):
            params: dict[str, str | int] = {"request_id": request_id, "page_num": page, "page_size": 100}
            data = self._http.request("GET", CONVERSIONS_PATH, params=params)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise UnknownError("conversion list response had an unexpected shape")
            for item in data["items"]:
                if isinstance(item, dict) and item.get("request_id") == request_id:
                    return item
            if not data.get("has_more"):
                return None
        return None
