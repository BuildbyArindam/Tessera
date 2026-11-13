"""Turn an approved proposal into port calls. Only this module and the loop touch the AirwallexPort.

Each action maps to one vendor call, always with the idempotency key stored before the call:

  CONVERT       quote, then execute the quote (the quote id is saved first, so a restart executes the SAME quote)
  PAY_SUPPLIER  create_transfer to the vendor beneficiary the directory maps the proposal's beneficiary id to
  TOPUP_CARD    set_card_limits to (current limit + amount), or create_card when the vendor has none yet
  HOLD, FLAG_INVOICE  nothing is sent; the decision itself is the outcome

Params are re-validated with the planner's strict schemas, so a row edited after planning cannot smuggle in a field.
A proposal Tessera cannot carry out as stated (a cross-currency payout, an unmapped beneficiary) FAILS before anything
is sent: it is never widened into something the policy engine did not judge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import ValidationError as SchemaError
from tessera_agent.schemas import ConvertParams, PaySupplierParams, TopupCardParams
from tessera_airwallex import (
    AirwallexError,
    AirwallexPort,
    AuthError,
    IdempotencyConflictError,
    RateLimitedError,
    TransferStatus,
    UnknownError,
)
from tessera_core.domain import ActionType, Currency, Money

State = Literal["SUBMITTED", "SETTLED", "FAILED"]


@dataclass(frozen=True)
class CardRef:
    card_id: str
    monthly_limit: Money


class VendorDirectory(Protocol):
    """Tessera record id -> the vendor's id. Read-only: the worker never invents these, and no account number
    passes through a proposal. TODO(Day 14): back this with the onboarding records; tests use a dict."""

    def beneficiary(self, business_id: str, beneficiary_id: str) -> str | None: ...

    def card(self, business_id: str, vendor_id: str) -> CardRef | None: ...


@dataclass(frozen=True)
class Dispatched:
    state: State  # the state after the call
    external_id: str | None
    detail: dict[str, Any]


class Retry(Exception):  # noqa: N818  (a signal, not an error)
    """The outcome is unknown or nothing was sent. Keep the execution PENDING; the next cycle replays the same key."""


Saver = Callable[[dict[str, Any]], None]


def _failed(code: str) -> Dispatched:
    return Dispatched("FAILED", None, {"error": code})


def _money(amount: str, currency: Currency) -> Money:
    return Money(amount=Decimal(amount), currency=currency)


def dispatch(
    port: AirwallexPort,
    *,
    action_type: str,
    params: Mapping[str, Any],
    business_id: str,
    key: str,
    directory: VendorDirectory,
    prior: Mapping[str, Any],
    save: Saver,
) -> Dispatched:
    """Carry out one proposal. Raises Retry when the call should be replayed later; never raises on a vendor refusal."""
    try:
        return _run(port, action_type, params, business_id, key, directory, prior, save)
    except SchemaError:
        return _failed("invalid_params")
    except (AuthError, RateLimitedError, UnknownError) as exc:
        raise Retry(type(exc).__name__) from exc
    except IdempotencyConflictError:
        return _failed("idempotency_conflict")  # a key reused for a different request: a bug, never retried
    except AirwallexError as exc:
        return _failed(type(exc).__name__)  # the vendor refused (funds, validation, expiry): nothing happened


def _run(
    port: AirwallexPort,
    action_type: str,
    params: Mapping[str, Any],
    business_id: str,
    key: str,
    directory: VendorDirectory,
    prior: Mapping[str, Any],
    save: Saver,
) -> Dispatched:
    if action_type in (ActionType.HOLD, ActionType.FLAG_INVOICE):
        return Dispatched("SETTLED", None, {"sent": False})
    if action_type == ActionType.CONVERT:
        c = ConvertParams.model_validate(params)
        quote_id = prior.get("quote_id")
        if not isinstance(quote_id, str):
            quote = port.create_fx_quote(sell=_money(c.amount, c.currency), buy_currency=c.to_currency)
            quote_id = quote.quote_id
            save({"quote_id": quote_id})  # committed before executing: a restart runs this quote, not a new one
        done = port.execute_conversion(quote_id=quote_id, idempotency_key=key)
        return Dispatched("SETTLED", done.conversion_id, {"quote_id": quote_id})
    if action_type == ActionType.PAY_SUPPLIER:
        p = PaySupplierParams.model_validate(params)
        if p.from_currency != p.currency:
            return _failed("cross_currency_payout_unsupported")  # a conversion is its own, separately judged action
        vendor_id = directory.beneficiary(business_id, p.beneficiary_id)
        if vendor_id is None:
            return _failed("beneficiary_not_in_directory")
        transfer = port.create_transfer(
            beneficiary_id=vendor_id,
            amount=_money(p.amount, p.currency),
            reference=f"Tessera {p.obligation_id}",
            idempotency_key=key,
        )
        state: dict[TransferStatus, State] = {
            TransferStatus.SETTLED: "SETTLED",
            TransferStatus.FAILED: "FAILED",
            TransferStatus.SUBMITTED: "SUBMITTED",
        }
        return Dispatched(state[transfer.status], transfer.transfer_id, {"transfer_status": transfer.status.value})
    return _topup(port, params, business_id, key, directory, prior, save)


def _topup(
    port: AirwallexPort,
    params: Mapping[str, Any],
    business_id: str,
    key: str,
    directory: VendorDirectory,
    prior: Mapping[str, Any],
    save: Saver,
) -> Dispatched:
    t = TopupCardParams.model_validate(params)
    if isinstance(prior.get("target_limit"), str):  # replaying: use the limit decided the first time
        target = _money(prior["target_limit"], t.currency)
        stored = prior.get("card_id")
        card_id = stored if isinstance(stored, str) and stored else None
    else:
        card = directory.card(business_id, t.vendor_id)
        if card is not None and card.monthly_limit.currency != t.currency:
            return _failed("card_currency_mismatch")
        added = _money(t.amount, t.currency)
        target = added if card is None else Money(amount=card.monthly_limit.amount + added.amount, currency=t.currency)
        card_id = None if card is None else card.card_id
        save({"target_limit": str(target.amount), "card_id": card_id or ""})  # committed before the call
    if card_id:
        updated = port.set_card_limits(card_id=card_id, monthly_limit=target, idempotency_key=key)
    else:
        updated = port.create_card(label=t.vendor_id, monthly_limit=target, idempotency_key=key)
    return Dispatched("SETTLED", updated.card_id, {"monthly_limit": str(updated.monthly_limit.amount)})
