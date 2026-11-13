"""Virtual card calls of the Airwallex sandbox adapter (a mixin of SandboxAirwallex).

Every endpoint and field is recorded in docs/airwallex-notes.md with its source and a VERIFIED / UNVERIFIED tag.

Idempotency:
* create_card: Airwallex documents `request_id` ("requests with the same request_id will be ignored"), so the key maps
  to it. What a repeated request RETURNS is not documented; if the response carries no card_id the adapter raises
  UnknownError and the caller must reconcile by hand. The card list endpoint was not read, so there is no lookup.
* set_card_limits: the update endpoint documents no request_id. Setting an absolute value is naturally repeatable (the
  same call twice leaves the same limit), so the key is only validated, and the call may be retried safely.

The limit is expressed as `authorization_controls.transaction_limits.limits[{amount, interval: MONTHLY}]`.
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote as url_quote

from tessera_core.domain import Currency, Money

from tessera_airwallex._config import CardConfig
from tessera_airwallex._http import SandboxHttp
from tessera_airwallex._timeparse import parse_timestamp
from tessera_airwallex._values import VENDOR_ID, dec, request_id_for
from tessera_airwallex.errors import IdempotencyConflictError, InvalidRequestError, NotFoundError, UnknownError
from tessera_airwallex.models import Card

CARD_CREATE_PATH = "/api/v1/issuing/cards/create"
CARD_PATH = "/api/v1/issuing/cards/{card_id}"
CARD_UPDATE_PATH = "/api/v1/issuing/cards/{card_id}/update"
MONTHLY = "MONTHLY"
_DEFAULT_LIMIT_CURRENCY = "USD"  # the reference: transaction_limits.currency "defaults to USD if not set"


def _transaction_limits(card: object) -> dict[str, Any]:
    controls = card.get("authorization_controls") if isinstance(card, dict) else None
    limits = controls.get("transaction_limits") if isinstance(controls, dict) else None
    return limits if isinstance(limits, dict) else {}


def _limit_list(transaction_limits: dict[str, Any]) -> list[dict[str, Any]]:
    raw = transaction_limits.get("limits")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _limit_body(amount: Decimal) -> dict[str, Any]:
    return {"amount": amount, "interval": MONTHLY}  # a Decimal is written as a bare JSON number (see _json)


class CardsMixin:
    _http: SandboxHttp
    _clock: Callable[[], datetime]
    _cards: CardConfig

    def create_card(self, *, label: str, monthly_limit: Money, idempotency_key: str) -> Card:
        request_id = request_id_for(idempotency_key)
        if not label.strip():
            raise InvalidRequestError("card label must be non-empty")
        if not monthly_limit.is_positive():
            raise InvalidRequestError("card limit must be positive")
        cfg = self._cards
        if cfg.cardholder_id is None or cfg.created_by is None:
            raise InvalidRequestError(
                "card issuing is not configured (AIRWALLEX_CARDHOLDER_ID, AIRWALLEX_CARD_CREATED_BY)"
            )
        body = {
            "request_id": request_id,
            "cardholder_id": cfg.cardholder_id,
            "created_by": cfg.created_by,
            "form_factor": "VIRTUAL",
            "is_personalized": cfg.is_personalized,
            "nick_name": label,
            "program": {"purpose": cfg.purpose},
            "authorization_controls": {
                "allowed_transaction_count": "MULTIPLE",
                "transaction_limits": {
                    "currency": monthly_limit.currency.value,
                    "limits": [_limit_body(monthly_limit.amount)],
                },
            },
        }
        data = self._http.request("POST", CARD_CREATE_PATH, body=body)
        return self._card_from(data, label, monthly_limit, reused_key=True)

    def set_card_limits(self, *, card_id: str, monthly_limit: Money, idempotency_key: str) -> Card:
        request_id_for(idempotency_key)  # validates the key; see the module docstring for why it is not sent
        if not VENDOR_ID.match(card_id):
            raise NotFoundError(f"unknown card {card_id!r}")
        if not monthly_limit.is_positive():
            raise InvalidRequestError("card limit must be positive")

        quoted = url_quote(card_id, safe="")
        current = _transaction_limits(self._http.request("GET", CARD_PATH.format(card_id=quoted)))
        held_currency = current.get("currency", _DEFAULT_LIMIT_CURRENCY)
        if held_currency != monthly_limit.currency.value:
            raise InvalidRequestError("card limit currency cannot change")
        # The reference does not say whether a `limits` array replaces or merges, so the limits that are not being
        # changed are sent back as they are: the result is the same under either reading.
        kept = [item for item in _limit_list(current) if item.get("interval") != MONTHLY]
        updated: dict[str, Any] = {"currency": held_currency, "limits": [*kept, _limit_body(monthly_limit.amount)]}
        if "cash_withdrawal_limits" in current:
            updated["cash_withdrawal_limits"] = current["cash_withdrawal_limits"]
        body = {"authorization_controls": {"transaction_limits": updated}}
        data = self._http.request("POST", CARD_UPDATE_PATH.format(card_id=quoted), body=body)
        return self._card_from(data, None, monthly_limit, fallback_id=card_id)

    def _card_from(
        self, data: Any, label: str | None, requested: Money, fallback_id: str | None = None, reused_key: bool = False
    ) -> Card:
        """A Card from the vendor's object.

        With `reused_key` (card create), a card that differs from the request is what a repeated request_id returns
        (the vendor "ignores" the repeat and hands back the original), so it is IdempotencyConflictError.
        """
        if not isinstance(data, dict):
            raise UnknownError("card response was not an object")
        card_id = data.get("card_id") or fallback_id
        if not isinstance(card_id, str) or not card_id:
            raise UnknownError("card response had no card_id")
        try:
            created = data.get("created_at")
            nick = data.get("nick_name")
            if reused_key and label is not None and isinstance(nick, str) and nick != label:
                raise IdempotencyConflictError("idempotency key was already used for a different card")
            return Card(
                card_id=card_id,
                label=nick if isinstance(nick, str) and nick else (label or card_id),
                monthly_limit=self._monthly_limit(data, requested, reused_key),
                created_at=parse_timestamp(created) if isinstance(created, str) else self._clock(),
            )
        except (TypeError, ValueError) as exc:
            raise UnknownError("malformed card response") from exc

    @staticmethod
    def _monthly_limit(data: dict[str, Any], requested: Money, reused_key: bool = False) -> Money:
        """The monthly limit the vendor reports, which must equal the one asked for; if it reports none, the request."""
        limits = _transaction_limits(data)
        for item in _limit_list(limits):
            if item.get("interval") == MONTHLY:
                held = Money(
                    amount=dec(item["amount"]), currency=Currency(limits.get("currency", _DEFAULT_LIMIT_CURRENCY))
                )
                if held != requested:
                    if reused_key:
                        raise IdempotencyConflictError("idempotency key was already used for a different card")
                    raise UnknownError("the card limit in the response does not match the request")
                return held
        return requested
