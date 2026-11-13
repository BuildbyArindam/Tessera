"""Beneficiary and transfer calls of the Airwallex sandbox adapter (a mixin of SandboxAirwallex).

Every endpoint and field is recorded in docs/airwallex-notes.md with its source and a VERIFIED / UNVERIFIED tag.

Idempotency differs between the two calls, and the adapter keeps the port's promise (a replayed key returns the
original and changes nothing) for both:

* Transfer: Airwallex documents `request_id` ("requests with a request_id used in the past 7 days are treated as
  duplicates"), so the key maps to it, exactly as for conversions: look the transfer up first, create, and look it up
  again after any unknown outcome or unrecognised 4xx.
* Beneficiary: the create endpoint documents NO request_id and no duplicate handling. The key is therefore mapped to a
  deterministic `nickname`; the adapter looks that nickname up before creating and after any unknown outcome, and
  never auto-retries the create (a repeat after a lost response could otherwise make a second beneficiary).
"""

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote as url_quote

from pydantic import SecretStr
from tessera_core.domain import Currency, Money

from tessera_airwallex._config import PayoutConfig
from tessera_airwallex._errormap import needs_reconcile
from tessera_airwallex._http import SandboxHttp
from tessera_airwallex._status import transfer_status_from_vendor
from tessera_airwallex._timeparse import parse_timestamp
from tessera_airwallex._values import MAX_LIST_PAGES, VENDOR_ID, dec, request_id_for
from tessera_airwallex.errors import (
    AirwallexError,
    IdempotencyConflictError,
    InvalidRequestError,
    NotFoundError,
    UnknownError,
)
from tessera_airwallex.models import BeneficiaryRef, Transfer

BENEFICIARY_CREATE_PATH = "/api/v1/beneficiaries/create"
BENEFICIARIES_PATH = "/api/v1/beneficiaries"
BENEFICIARY_PATH = "/api/v1/beneficiaries/{beneficiary_id}"
TRANSFER_CREATE_PATH = "/api/v1/transfers/create"
TRANSFERS_PATH = "/api/v1/transfers"

# A duplicate request_id whose transfer the list call cannot find: the transfer may exist, so the outcome is unknown.
_UNRESOLVED_CODES = frozenset({"request_pending", "request_id_duplicate"})


def nickname_for(idempotency_key: str) -> str:
    """The deterministic beneficiary nickname that stands in for a request_id. Validates the key."""
    request_id_for(idempotency_key)
    return "tsr-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]


def _bank_details(item: dict[str, Any]) -> dict[str, Any]:
    beneficiary = item.get("beneficiary")
    details = beneficiary.get("bank_details") if isinstance(beneficiary, dict) else None
    return details if isinstance(details, dict) else {}


def _beneficiary_id_of(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    for key in ("id", "beneficiary_id"):  # the current pages say `id`; the 2024-04-30 list page says `beneficiary_id`
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _differs(item: dict[str, Any], key: str, expected: str) -> bool:
    value = item.get(key)
    return isinstance(value, str) and value != expected


class PayoutsMixin:
    _http: SandboxHttp
    _clock: Callable[[], datetime]
    _payouts: PayoutConfig

    # ------------------------------------------------------------------ beneficiaries
    def create_beneficiary(
        self, *, name: str, currency: Currency, account_identifier: SecretStr, idempotency_key: str
    ) -> BeneficiaryRef:
        nickname = nickname_for(idempotency_key)
        account = account_identifier.get_secret_value().strip()
        if not name.strip():
            raise InvalidRequestError("beneficiary name must be non-empty")
        if not account:
            raise InvalidRequestError("account identifier must be non-empty")
        country = self._payouts.bank_country.get(currency)
        if country is None:
            raise InvalidRequestError(f"no bank country is configured for {currency.value}")

        existing = self._find_beneficiary(nickname)
        if existing is not None:
            return self._beneficiary_replayed(existing, name, currency, account)
        body = {
            "nickname": nickname,
            "transfer_methods": list(self._payouts.transfer_methods),
            "beneficiary": {
                "type": "BANK_ACCOUNT",
                "entity_type": "COMPANY",
                "company_name": name,
                "bank_details": {
                    "account_currency": currency.value,
                    "account_name": name,
                    "account_number": account,
                    "bank_country_code": country,
                },
            },
        }
        try:
            data = self._http.request("POST", BENEFICIARY_CREATE_PATH, body=body, retry=False)
        except AirwallexError as exc:
            if not needs_reconcile(exc):
                raise
            found = self._find_beneficiary(nickname)
            if found is None:
                raise
            return self._beneficiary_replayed(found, name, currency, account)
        ident = _beneficiary_id_of(data)
        if ident is None:
            raise UnknownError("beneficiary response had no id")
        return BeneficiaryRef(beneficiary_id=ident, name=name, currency=currency, created_at=self._clock())

    def _beneficiary_replayed(
        self, item: dict[str, Any], name: str, currency: Currency, account: str
    ) -> BeneficiaryRef:
        ident = _beneficiary_id_of(item)
        if ident is None:
            raise UnknownError("beneficiary list item had no id")
        details = _bank_details(item)
        held = details.get("account_number") or details.get("iban")
        if (
            (isinstance(held, str) and held != account)
            or _differs(details, "account_currency", currency.value)
            or _differs(details, "account_name", name)
        ):
            raise IdempotencyConflictError("idempotency key was already used for a different beneficiary")
        # The vendor documents no created_at for a beneficiary; policy ages beneficiaries from Tessera's own records.
        return BeneficiaryRef(beneficiary_id=ident, name=name, currency=currency, created_at=self._clock())

    def _find_beneficiary(self, nickname: str) -> dict[str, Any] | None:
        """The beneficiary with this nickname, if any. The filter is re-checked client-side."""
        for page in range(MAX_LIST_PAGES):
            params: dict[str, str | int] = {"nick_name": nickname, "page_num": page, "page_size": 100}
            data = self._http.request("GET", BENEFICIARIES_PATH, params=params)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise UnknownError("beneficiary list response had an unexpected shape")
            for item in data["items"]:
                if isinstance(item, dict) and item.get("nickname") == nickname:
                    return item
            if not data.get("has_more"):
                return None
        return None

    # ------------------------------------------------------------------ transfers
    def create_transfer(self, *, beneficiary_id: str, amount: Money, reference: str, idempotency_key: str) -> Transfer:
        request_id = request_id_for(idempotency_key)
        if not VENDOR_ID.match(beneficiary_id):
            raise NotFoundError(f"unknown beneficiary {beneficiary_id!r}")
        if not amount.is_positive():
            raise InvalidRequestError("transfer amount must be positive")
        if not reference.strip():
            raise InvalidRequestError("transfer reference must be non-empty")
        reason = self._payouts.transfer_reason
        if reason is None:
            raise InvalidRequestError("AIRWALLEX_TRANSFER_REASON is not configured")

        existing = self._find_transfer(request_id)
        if existing is not None:
            return self._transfer_replayed(existing, beneficiary_id, amount, reference)
        self._check_beneficiary_currency(beneficiary_id, amount.currency)
        body = {
            "request_id": request_id,
            "beneficiary_id": beneficiary_id,
            # Funded from the same currency on purpose: a conversion is a separate, policy-checked action.
            "source_currency": amount.currency.value,
            "transfer_currency": amount.currency.value,
            "transfer_amount": str(amount.amount),
            "reason": reason,
            "reference": reference,
        }
        try:
            data = self._http.request("POST", TRANSFER_CREATE_PATH, body=body)
        except AirwallexError as exc:
            if not needs_reconcile(exc):
                raise
            found = self._find_transfer(request_id)
            if found is not None:
                return self._transfer_replayed(found, beneficiary_id, amount, reference)
            if exc.code in _UNRESOLVED_CODES:
                raise UnknownError("the transfer may exist but could not be found", code=exc.code) from exc
            raise
        if _mismatch(data, beneficiary_id, amount):
            raise UnknownError("transfer response does not match the request")
        return self._transfer_from(data, beneficiary_id, amount, reference)

    def _check_beneficiary_currency(self, beneficiary_id: str, currency: Currency) -> None:
        path = BENEFICIARY_PATH.format(beneficiary_id=url_quote(beneficiary_id, safe=""))
        data = self._http.request("GET", path)
        held = _bank_details(data).get("account_currency") if isinstance(data, dict) else None
        if not isinstance(held, str):
            raise InvalidRequestError("the beneficiary has no bank account currency to check the transfer against")
        if held != currency.value:
            raise InvalidRequestError("transfer currency must match the beneficiary currency")

    def _transfer_replayed(self, item: dict[str, Any], beneficiary_id: str, amount: Money, reference: str) -> Transfer:
        if _mismatch(item, beneficiary_id, amount):
            raise IdempotencyConflictError("idempotency key was already used for a different transfer")
        return self._transfer_from(item, beneficiary_id, amount, reference)

    def _transfer_from(self, data: Any, beneficiary_id: str, amount: Money, reference: str) -> Transfer:
        try:
            created = data.get("created_at")
            return Transfer(
                transfer_id=str(data["id"]),
                beneficiary_id=beneficiary_id,
                amount=amount,
                reference=reference,
                status=transfer_status_from_vendor(data.get("status")),
                created_at=parse_timestamp(created) if isinstance(created, str) else self._clock(),
            )
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise UnknownError("malformed transfer response") from exc

    def _find_transfer(self, request_id: str) -> dict[str, Any] | None:
        """The transfer created with this request_id, if any. The filter is re-checked client-side."""
        params: dict[str, str | int] = {"request_id": request_id, "page_size": 100}
        data = self._http.request("GET", TRANSFERS_PATH, params=params)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise UnknownError("transfer list response had an unexpected shape")
        for item in data["items"]:
            if isinstance(item, dict) and item.get("request_id") == request_id:
                return item
        return None


def _mismatch(data: Any, beneficiary_id: str, amount: Money) -> bool:
    """True when a transfer the vendor describes is demonstrably not the one that was asked for."""
    if not isinstance(data, dict):
        return False
    try:
        wrong_amount = data.get("transfer_amount") is not None and dec(data["transfer_amount"]) != amount.amount
    except ValueError:
        return True
    return (
        _differs(data, "beneficiary_id", beneficiary_id)
        or _differs(data, "transfer_currency", amount.currency.value)
        or wrong_amount
    )
