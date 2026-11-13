"""Beneficiary, transfer and card endpoints for tests/fake_airwallex.py (installed by FakeAirwallex itself).

Strict where the reference is explicit: a beneficiary needs `nickname`, `transfer_methods` and bank details; a transfer
needs `request_id`, `reason`, `reference` and `transfer_currency`; a card limit is a JSON number. Faithful to the
reference's silences by being awkward on purpose:

* beneficiary create has NO request_id and does NOT deduplicate, so a client that repeats it makes a second record;
* the transfer create response carries only `id`, `status` and `request_id` (the fields the page lists), so the client
  must not depend on the rest;
* a card update REPLACES the whole `limits` array (the harsher of the two readings), so the client must send back the
  limits it is not changing;
* a repeated card `request_id` is "ignored": the fake returns the original card. That is an arbitrary choice.
"""

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import respx
from tessera_airwallex import _json
from tessera_core.domain import Currency

if TYPE_CHECKING:
    from fake_airwallex import FakeAirwallex

HOST = "api.sandbox.airwallex.com"
TRANSFER_REQUIRED = ("request_id", "reason", "reference", "transfer_currency")
CARD_REQUIRED = ("request_id", "cardholder_id", "created_by", "form_factor", "program", "authorization_controls")


class FakePayouts:
    def __init__(self, fake: "FakeAirwallex", router: respx.MockRouter) -> None:
        from fake_airwallex import error, reply, stamp

        self.fake, self._reply, self._error, self._stamp = fake, reply, error, stamp
        self.beneficiaries: dict[str, dict[str, Any]] = {}
        self.transfers: list[dict[str, Any]] = []
        self.cards: dict[str, dict[str, Any]] = {}
        self.card_requests: dict[str, str] = {}  # request_id -> card_id
        route = fake._route
        r = router
        r.post(path="/api/v1/beneficiaries/create", host=HOST).mock(side_effect=route(self._ben_create))
        r.get(path="/api/v1/beneficiaries", host=HOST).mock(side_effect=route(self._ben_list))
        r.get(path__regex=r"^/api/v1/beneficiaries/[^/]+$", host=HOST).mock(side_effect=route(self._ben_get))
        r.post(path="/api/v1/transfers/create", host=HOST).mock(side_effect=route(self._transfer_create))
        r.get(path="/api/v1/transfers", host=HOST).mock(side_effect=route(self._transfer_list))
        r.post(path="/api/v1/issuing/cards/create", host=HOST).mock(side_effect=route(self._card_create))
        r.get(path__regex=r"^/api/v1/issuing/cards/[^/]+$", host=HOST).mock(side_effect=route(self._card_get))
        r.post(path__regex=r"^/api/v1/issuing/cards/[^/]+/update$", host=HOST).mock(
            side_effect=route(self._card_update)
        )

    # ------------------------------------------------------------------ beneficiaries
    def _ben_create(self, request: httpx.Request) -> httpx.Response:
        if (denied := self.fake._auth(request)) is not None:
            return denied
        body = self.fake._body(request)
        details = (body.get("beneficiary") or {}).get("bank_details") or {}
        needed = ("account_currency", "account_name", "bank_country_code", "account_number")
        if not isinstance(body.get("nickname"), str) or not isinstance(body.get("transfer_methods"), list):
            return self._error(400, "validation_failed", "nickname and transfer_methods are required")
        if any(not details.get(k) for k in needed):
            return self._error(400, "validation_failed", "bank details are incomplete")
        ident = self.fake._id("ben")
        record = {
            "id": ident,
            "nickname": body["nickname"],
            "beneficiary": body["beneficiary"],
            "transfer_methods": body["transfer_methods"],
            "payer_entity_type": "COMPANY",
        }
        self.beneficiaries[ident] = record
        return self._reply(201, record)

    def _ben_list(self, request: httpx.Request) -> httpx.Response:
        if (denied := self.fake._auth(request)) is not None:
            return denied
        wanted = request.url.params.get("nick_name")
        items = [
            {"beneficiary": b["beneficiary"], "beneficiary_id": b["id"], "nickname": b["nickname"]}
            for b in self.beneficiaries.values()
            if wanted is None or b["nickname"] == wanted
        ]
        return self._reply(200, {"has_more": False, "items": items})

    def _ben_get(self, request: httpx.Request) -> httpx.Response:
        if (denied := self.fake._auth(request)) is not None:
            return denied
        record = self.beneficiaries.get(request.url.path.rsplit("/", 1)[1])
        return self._reply(200, record) if record else self._error(404, "not_found")

    # ------------------------------------------------------------------ transfers
    def _transfer_create(self, request: httpx.Request) -> httpx.Response:
        if (denied := self.fake._auth(request)) is not None:
            return denied
        body = self.fake._body(request)
        if any(not body.get(k) for k in TRANSFER_REQUIRED):
            return self._error(400, "field_required")
        request_id = body["request_id"]
        if not isinstance(request_id, str) or len(request_id) > 64:
            return self._error(400, "invalid_argument", "request_id must be at most 64 characters")
        if any(t["request_id"] == request_id for t in self.transfers):
            return self._error(400, "request_id_duplicate")
        if body.get("beneficiary_id") not in self.beneficiaries:
            return self._error(400, "validation_failed", "unknown beneficiary")
        if not isinstance(body.get("transfer_amount"), str):
            return self._error(400, "invalid_argument", "transfer_amount must be a string")
        if body.get("source_currency", body["transfer_currency"]) != body["transfer_currency"]:
            return self._error(400, "invalid_argument", "this fake does not convert")
        currency, amount = Currency(body["transfer_currency"]), Decimal(body["transfer_amount"])
        if amount <= 0:
            return self._error(400, "invalid_argument")
        if self.fake.balances[currency] < amount:
            return self._error(400, "insufficient_funds")
        self.fake.balances[currency] -= amount
        record = {
            "id": self.fake._id("tr"),
            "request_id": request_id,
            "beneficiary_id": body["beneficiary_id"],
            "transfer_amount": amount,
            "transfer_currency": currency.value,
            "reference": body["reference"],
            "reason": body["reason"],
            "status": "PROCESSING",
            "created_at": self._stamp(self.fake.clock()),
        }
        self.transfers.append(record)
        return self._reply(201, {"id": record["id"], "status": "PROCESSING", "request_id": request_id})

    def _transfer_list(self, request: httpx.Request) -> httpx.Response:
        if (denied := self.fake._auth(request)) is not None:
            return denied
        wanted = request.url.params.get("request_id")
        items = [t for t in reversed(self.transfers) if wanted is None or t["request_id"] == wanted]
        return self._reply(200, {"items": items})

    def set_transfer_status(self, transfer_id: str, status: str) -> None:
        next(t for t in self.transfers if t["id"] == transfer_id)["status"] = status

    # ------------------------------------------------------------------ cards
    def _card_create(self, request: httpx.Request) -> httpx.Response:
        if (denied := self.fake._auth(request)) is not None:
            return denied
        body = self.fake._body(request)
        if any(not body.get(k) for k in CARD_REQUIRED):
            return self._error(400, "field_required")
        limits = body["authorization_controls"].get("transaction_limits", {}).get("limits") or []
        if not limits or any(
            isinstance(x.get("amount"), bool) or not isinstance(x.get("amount"), int | Decimal) for x in limits
        ):
            return self._error(400, "invalid_argument", "limit amounts must be JSON numbers")
        if body["request_id"] in self.card_requests:  # "requests with the same request_id will be ignored"
            return self._reply(202, self.cards[self.card_requests[body["request_id"]]])
        card = {
            "card_id": self.fake._id("card"),
            "card_status": "ACTIVE",
            "nick_name": body.get("nick_name"),
            "form_factor": body["form_factor"],
            "authorization_controls": body["authorization_controls"],
            "created_at": self._stamp(self.fake.clock()),
        }
        self.cards[card["card_id"]] = card
        self.card_requests[body["request_id"]] = card["card_id"]
        return self._reply(202, card)

    def _card_get(self, request: httpx.Request) -> httpx.Response:
        if (denied := self.fake._auth(request)) is not None:
            return denied
        card = self.cards.get(request.url.path.rsplit("/", 1)[1])
        return self._reply(200, card) if card else self._error(404, "not_found")

    def _card_update(self, request: httpx.Request) -> httpx.Response:
        if (denied := self.fake._auth(request)) is not None:
            return denied
        card = self.cards.get(request.url.path.split("/")[-2])
        if card is None:
            return self._error(404, "not_found")
        sent = self.fake._body(request).get("authorization_controls", {}).get("transaction_limits")
        if not isinstance(sent, dict) or not sent.get("limits"):
            return self._error(400, "field_required", "limits are required")
        current = card["authorization_controls"].setdefault("transaction_limits", {})
        current.update(sent)  # `limits` is REPLACED, not merged
        return self._reply(200, card)


def dumps(payload: Any) -> str:
    return _json.dumps(payload)
