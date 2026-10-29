from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from tessera_core.domain import (
    ActionType,
    Balance,
    Currency,
    FxQuote,
    Invoice,
    Money,
    Payment,
    PaymentDirection,
    PolicyDecision,
    Proposal,
    Verdict,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def money(x: str, c: Currency = Currency.USD) -> Money:
    return Money(amount=x, currency=c)


def test_invoice_valid_and_defaults() -> None:
    inv = Invoice(
        id="i1",
        business_id="b1",
        customer_id="c1",
        amount=money("100"),
        issued_on=date(2026, 1, 1),
        due_on=date(2026, 1, 31),
    )
    assert inv.status.value == "OPEN" and inv.notes is None


def test_invoice_rejects_bad_dates_and_non_positive_amounts() -> None:
    base = {"id": "i1", "business_id": "b1", "customer_id": "c1"}
    with pytest.raises(ValidationError):
        Invoice(**base, amount=money("100"), issued_on=date(2026, 2, 1), due_on=date(2026, 1, 1))
    for bad in ("0", "-5"):
        with pytest.raises(ValidationError):
            Invoice(**base, amount=money(bad), issued_on=date(2026, 1, 1), due_on=date(2026, 1, 2))


def test_payment_requires_positive_amount() -> None:
    with pytest.raises(ValidationError):
        Payment(
            id="p1",
            business_id="b1",
            direction=PaymentDirection.OUTBOUND,
            amount=money("0"),
            counterparty_id="s1",
            paid_on=date(2026, 1, 1),
        )


def test_balance_currency_must_match_and_not_be_negative() -> None:
    assert Balance(currency=Currency.EUR, available=money("5", Currency.EUR))
    with pytest.raises(ValidationError):
        Balance(currency=Currency.EUR, available=money("5", Currency.USD))
    with pytest.raises(ValidationError):
        Balance(currency=Currency.USD, available=money("-1"))


def test_fx_quote_needs_two_currencies() -> None:
    with pytest.raises(ValidationError):
        FxQuote(
            quote_id="q",
            sell=money("10"),
            buy=money("9"),
            rate="0.9",
            expires_at=NOW,
        )


class TestProposal:
    def make(self, **overrides: object) -> Proposal:
        data: dict[str, object] = {
            "id": "p1",
            "action_type": ActionType.CONVERT,
            "params": {"sell": {"amount": "100.00", "currency": "EUR"}},
            "rationale": "because",
            "confidence": 0.7,
            "created_at": NOW,
        }
        data.update(overrides)
        return Proposal(**data)  # type: ignore[arg-type]

    def test_valid(self) -> None:
        assert self.make().action_type is ActionType.CONVERT

    @pytest.mark.parametrize("conf", [-0.1, 1.1, float("nan"), float("inf")])
    def test_confidence_must_be_a_probability(self, conf: float) -> None:
        with pytest.raises(ValidationError):
            self.make(confidence=conf)

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self.make(created_at=datetime(2026, 1, 1))

    def test_unknown_action_and_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self.make(action_type="WIRE_EVERYTHING")
        with pytest.raises(ValidationError):
            self.make(approved=True)


class TestPolicyDecision:
    def test_approve_may_have_no_reason(self) -> None:
        assert PolicyDecision(verdict=Verdict.APPROVE).reasons == ()

    @pytest.mark.parametrize("verdict", [Verdict.ESCALATE, Verdict.BLOCK])
    def test_non_approve_requires_a_reason(self, verdict: Verdict) -> None:
        with pytest.raises(ValidationError):
            PolicyDecision(verdict=verdict)
        d = PolicyDecision(verdict=verdict, reasons=["why"], rule_ids=["r1"])
        assert d.reasons == ("why",) and d.rule_ids == ("r1",)

    def test_unknown_verdict_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PolicyDecision(verdict="MAYBE", reasons=["x"])
