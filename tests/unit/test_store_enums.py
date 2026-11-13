"""Day 11: every CHECK list in the database equals the Python enum it mirrors, so the two cannot drift apart."""

from __future__ import annotations

from tessera_audit import EVENT_TYPE_VALUES, AuditEventType
from tessera_core.domain import ActionType, Currency, ExecutionState, InvoiceStatus, Verdict
from tessera_core.domain.entities import PaymentDirection
from tessera_store import models as m


def values(enum: type) -> tuple[str, ...]:
    return tuple(e.value for e in enum)  # type: ignore[attr-defined]


def test_database_value_lists_match_the_domain_enums() -> None:
    assert m.CURRENCIES == values(Currency)
    assert m.ACTION_TYPES == values(ActionType)
    assert m.VERDICTS == values(Verdict)
    assert m.INVOICE_STATUSES == values(InvoiceStatus)
    assert m.PAYMENT_DIRECTIONS == values(PaymentDirection)
    assert EVENT_TYPE_VALUES == values(AuditEventType)


def test_proposal_and_execution_statuses_match_the_lifecycle() -> None:
    from tessera_core.domain.lifecycle import PERSISTED_ACTION_STATES

    assert set(m.PROPOSAL_STATUSES) == set(PERSISTED_ACTION_STATES)
    assert m.EXECUTION_STATUSES == values(ExecutionState)


def test_the_brief_lists_ten_event_types() -> None:
    assert set(EVENT_TYPE_VALUES) == {
        "forecast_made", "proposal_created", "policy_decision", "approval_requested", "approved", "rejected",
        "execution_submitted", "execution_result", "webhook_received", "autonomy_changed",
    }  # fmt: skip
