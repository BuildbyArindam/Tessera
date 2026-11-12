"""Real Airwallex sandbox adapter. Day 12 implements balances, FX quotes and conversions;
beneficiaries, transfers and cards arrive on Day 13.

Every endpoint and field used here is recorded in docs/airwallex-notes.md with its source.

Idempotency. The port promises that replaying a key returns the original result. Airwallex
only documents that a repeated `request_id` is "rejected", so this adapter:
  1. maps the idempotency key to a `request_id` (the key itself if it is short and plain,
     otherwise its SHA-256);
  2. before creating, looks the conversion up by `request_id` and returns it if it exists
     (this is what makes a restart after a crash safe, even after the quote has expired);
  3. after an unknown outcome or an unclassifiable 4xx on create, looks it up again.
"""

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote as url_quote

from pydantic import SecretStr
from tessera_core.domain import Balance, Currency, FxQuote, Money

from tessera_airwallex._errormap import needs_reconcile
from tessera_airwallex._http import SandboxConfig, SandboxHttp
from tessera_airwallex._timeparse import parse_timestamp
from tessera_airwallex.errors import (
    AirwallexError,
    IdempotencyConflictError,
    InvalidRequestError,
    NotFoundError,
    QuoteExpiredError,
    UnknownError,
)
from tessera_airwallex.models import BeneficiaryRef, Card, ConversionResult, Transfer
from tessera_airwallex.port import AirwallexPort

BALANCES_PATH = "/api/v1/balances/current"
QUOTE_CREATE_PATH = "/api/v1/fx/quotes/create"
QUOTE_PATH = "/api/v1/fx/quotes/{quote_id}"
CONVERSION_CREATE_PATH = "/api/v1/fx/conversions/create"
CONVERSIONS_PATH = "/api/v1/fx/conversions"

_PLAIN_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_QUOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TOLERANCE = Decimal("0.01")  # amounts are 2 dp, so sell * rate may differ from buy by a cent
_MAX_LIST_PAGES = 10
_DAY13 = "arrives on Day 13"


def request_id_for(idempotency_key: str) -> str:
    """Deterministic Airwallex `request_id` (max 64 chars) for a port idempotency key."""
    if not idempotency_key or not idempotency_key.strip():
        raise InvalidRequestError("idempotency_key must be non-empty")
    if _PLAIN_KEY.match(idempotency_key):
        return idempotency_key
    return hashlib.sha256(idempotency_key.encode()).hexdigest()


def _dec(value: object) -> Decimal:
    """Decimal from a parsed JSON number or string. Floats never reach here (see _json)."""
    if isinstance(value, bool | float) or value is None:
        raise ValueError("expected a Decimal, int or str amount")
    try:
        return Decimal(value) if isinstance(value, int | str) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= _TOLERANCE + abs(b) * Decimal("1e-7")


def _money(data: dict[str, Any], amount_key: str, currency_key: str) -> Money:
    return Money(amount=_dec(data[amount_key]), currency=Currency(data[currency_key]))


class SandboxAirwallex(AirwallexPort):
    def __init__(
        self,
        config: SandboxConfig | None = None,
        *,
        http: SandboxHttp | None = None,
        clock: Callable[[], datetime] | None = None,
        quote_validity: str = "MIN_1",
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._http = http or SandboxHttp(config or SandboxConfig.from_env(), clock=self._clock)
        self._validity = quote_validity

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
                totals[currency] += _dec(item["available_amount"])
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

    def create_beneficiary(
        self, *, name: str, currency: Currency, account_identifier: SecretStr, idempotency_key: str
    ) -> BeneficiaryRef:
        raise NotImplementedError(f"create_beneficiary {_DAY13}")

    def create_transfer(self, *, beneficiary_id: str, amount: Money, reference: str, idempotency_key: str) -> Transfer:
        raise NotImplementedError(f"create_transfer {_DAY13}")

    def create_card(self, *, label: str, monthly_limit: Money, idempotency_key: str) -> Card:
        raise NotImplementedError(f"create_card {_DAY13}")

    def set_card_limits(self, *, card_id: str, monthly_limit: Money, idempotency_key: str) -> Card:
        raise NotImplementedError(f"set_card_limits {_DAY13}")

    # ------------------------------------------------------------------ parsing
    def _quote_from(self, data: Any) -> FxQuote:
        try:
            sell = _money(data, "sell_amount", "sell_currency")
            buy = _money(data, "buy_amount", "buy_currency")
            if not buy.is_positive():
                raise InvalidRequestError("amount too small to convert")
            rate = self._consistent_rate(_dec(data["client_rate"]), sell, buy)
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
        if _close(sell.amount * rate, buy.amount):
            return rate
        inverse = (1 / rate).quantize(Decimal("1e-10"))
        if _close(sell.amount * inverse, buy.amount):
            return inverse
        raise ValueError("client_rate does not match the quoted amounts")

    def _result_from(self, data: Any, requested_quote_id: str) -> ConversionResult:
        try:
            return ConversionResult(
                conversion_id=str(data["conversion_id"]),
                quote_id=str(data.get("quote_id") or requested_quote_id),
                sold=_money(data, "sell_amount", "sell_currency"),
                bought=_money(data, "buy_amount", "buy_currency"),
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
        for page in range(_MAX_LIST_PAGES):
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
