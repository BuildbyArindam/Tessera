"""Day 13: each action type becomes the right port call, with the stored key, and replays are harmless."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from tessera_airwallex import MockAirwallex, UnknownError
from tessera_core.domain import Currency, Money
from tessera_worker.dispatch import CardRef, Retry, dispatch
from worker_helpers import DictDirectory, FlakyMock, balance

KEY = "tsr-test-key-0001"


def run(port: MockAirwallex, action: str, params: dict[str, Any], directory: DictDirectory | None = None,
        prior: dict[str, Any] | None = None, saved: list[dict[str, Any]] | None = None, key: str = KEY):  # type: ignore[no-untyped-def]  # fmt: skip
    return dispatch(port, action_type=action, params=params, business_id="B0", key=key,
                    directory=directory or DictDirectory(), prior=prior or {},
                    save=(saved if saved is not None else []).append)  # fmt: skip


CONVERT = {"amount": "1000.00", "currency": "EUR", "to_currency": "INR", "obligation_id": "OBL-0002"}


def test_a_conversion_saves_its_quote_first_then_executes() -> None:
    port, saved = FlakyMock(), []
    saved_before_call: list[int] = []
    port.on_call = lambda name: saved_before_call.append(len(saved))
    done = run(port, "CONVERT", CONVERT, saved=saved)
    assert done.state == "SETTLED" and done.external_id
    assert saved_before_call == [1]  # the quote id was handed to `save` before the conversion was sent
    assert balance(port, Currency.EUR) == Decimal("49000.00")


def test_replaying_a_conversion_uses_the_saved_quote_and_moves_money_once() -> None:
    port, saved = FlakyMock(), []
    first = run(port, "CONVERT", CONVERT, saved=saved)
    again = run(port, "CONVERT", CONVERT, prior=saved[0])
    assert again.external_id == first.external_id
    assert balance(port, Currency.EUR) == Decimal("49000.00")


def test_hold_and_flag_send_nothing() -> None:
    port = FlakyMock()
    assert run(port, "HOLD", CONVERT).state == "SETTLED"
    assert run(port, "FLAG_INVOICE", {"invoice_id": "INV-1", "reason": "other"}).state == "SETTLED"
    assert port.calls == []


PAY = {
    "amount": "500.00",
    "currency": "INR",
    "from_currency": "INR",
    "beneficiary_id": "BEN-001",
    "obligation_id": "OBL-1",
}


def _with_beneficiary() -> tuple[FlakyMock, DictDirectory]:
    port = FlakyMock()
    ref = port.create_beneficiary(name="Acme", currency=Currency.INR, account_identifier=SecretStr("123456"),
                                  idempotency_key="b-1")  # fmt: skip
    return port, DictDirectory({"BEN-001": ref.beneficiary_id})


def test_a_payout_is_submitted_not_settled_until_the_vendor_says_so() -> None:
    port, directory = _with_beneficiary()
    done = run(port, "PAY_SUPPLIER", PAY, directory)
    assert done.state == "SUBMITTED" and done.external_id
    assert balance(port, Currency.INR) == Decimal("999500.00")
    run(port, "PAY_SUPPLIER", PAY, directory)  # replay with the same key
    assert balance(port, Currency.INR) == Decimal("999500.00")


@pytest.mark.parametrize(
    ("params", "directory", "code"),
    [
        ({**PAY, "from_currency": "USD"}, None, "cross_currency_payout_unsupported"),
        (PAY, None, "beneficiary_not_in_directory"),
        ({**PAY, "amount": "-5"}, None, "invalid_params"),
        ({**PAY, "unexpected": "x"}, None, "invalid_params"),
    ],
)
def test_what_cannot_be_carried_out_as_stated_fails_before_anything_is_sent(
    params: dict[str, Any], directory: DictDirectory | None, code: str
) -> None:
    port = FlakyMock()
    done = run(port, "PAY_SUPPLIER", params, directory)
    assert (done.state, done.detail["error"]) == ("FAILED", code)
    assert port.calls == []


def test_a_vendor_refusal_is_a_failure_but_an_unknown_outcome_is_a_retry() -> None:
    port, directory = _with_beneficiary()
    too_much = run(port, "PAY_SUPPLIER", {**PAY, "amount": "99999999.00"}, directory)
    assert (too_much.state, too_much.detail["error"]) == ("FAILED", "InsufficientFundsError")
    port.fail_before = 1
    with pytest.raises(Retry):
        run(port, "PAY_SUPPLIER", PAY, directory)
    assert run(port, "PAY_SUPPLIER", PAY, directory).state == "SUBMITTED"


def test_topup_creates_a_card_when_the_vendor_has_none_and_raises_the_limit_when_it_has() -> None:
    port, saved = FlakyMock(), []
    topup = {"amount": "300.00", "currency": "USD", "vendor_id": "VEN-1"}
    made = run(port, "TOPUP_CARD", topup, saved=saved)
    assert made.state == "SETTLED" and made.detail["monthly_limit"] == "300.00"
    directory = DictDirectory(
        cards={"VEN-1": CardRef(made.external_id or "", Money(amount=Decimal("300"), currency=Currency.USD))}
    )
    raised = run(port, "TOPUP_CARD", {**topup, "amount": "200.00"}, directory, saved=saved, key="tsr-second")
    assert raised.detail["monthly_limit"] == "500.00"


def test_a_replayed_topup_does_not_add_the_amount_twice() -> None:
    port, saved = FlakyMock(), []
    first = run(port, "TOPUP_CARD", {"amount": "300.00", "currency": "USD", "vendor_id": "VEN-1"}, saved=saved)
    directory = DictDirectory(
        cards={"VEN-1": CardRef(first.external_id or "", Money(amount=Decimal("300"), currency=Currency.USD))}
    )
    topup = {"amount": "200.00", "currency": "USD", "vendor_id": "VEN-1"}
    saved.clear()
    run(port, "TOPUP_CARD", topup, directory, saved=saved, key="tsr-second")
    # a crash after the call, then a replay while the directory already shows the NEW limit: the saved target wins
    live = DictDirectory(
        cards={"VEN-1": CardRef(first.external_id or "", Money(amount=Decimal("500"), currency=Currency.USD))}
    )
    again = run(port, "TOPUP_CARD", topup, live, prior=saved[0], key="tsr-second")
    assert again.detail["monthly_limit"] == "500.00"


def test_unknown_error_from_the_port_is_never_swallowed_as_success() -> None:
    port = FlakyMock()
    port.fail_after = 1
    with pytest.raises(Retry):
        run(port, "CONVERT", CONVERT)
    assert isinstance(UnknownError("x"), Exception)
