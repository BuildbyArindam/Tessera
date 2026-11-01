"""Day 10: early_payment_gate. Found by the red team: a fooled model paying an approved supplier days early."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from policy_helpers import NOW, full_context, make_proposal
from tessera_core.domain import ActionType, Verdict
from tessera_core.policy import evaluate, load_ruleset

RULESET = load_ruleset()
TODAY = NOW.date()


def pay(**params: Any) -> Any:
    base = {"beneficiary_id": "sup_old", "obligation_id": "obl_1"}
    base.update(params)
    return make_proposal(ActionType.PAY_SUPPLIER, "500.00", "USD", **base)


def early(proposal: Any, **context: Any) -> tuple[str, str]:
    """(outcome, reason) of the early_payment_gate alone."""
    rule = next(r for r in RULESET.rules if r.rule_id == "early_payment_gate")
    result = rule.evaluate(proposal, full_context(**context))
    return result.outcome.value, result.reason


def test_a_payment_due_today_or_within_the_limit_passes() -> None:
    for days in (0, 1, 3):
        assert early(pay(), obligation_due_dates={"obl_1": TODAY + timedelta(days=days)})[0] == "PASS"


def test_one_day_past_the_limit_escalates_and_says_how_early() -> None:
    outcome, reason = early(pay(), obligation_due_dates={"obl_1": TODAY + timedelta(days=4)})
    assert outcome == "ESCALATE" and "4 days" in reason and "limit is 3" in reason


def test_a_late_payment_is_not_this_rules_concern() -> None:
    assert early(pay(), obligation_due_dates={"obl_1": TODAY - timedelta(days=20)})[0] == "PASS"


@pytest.mark.parametrize(
    "params", [{"obligation_id": None}, {"obligation_id": "obl_9"}, {"obligation_id": "not valid!"}]
)
def test_an_unnamed_unknown_or_malformed_obligation_escalates(params: dict[str, Any]) -> None:
    proposal = (
        pay(**params)
        if params["obligation_id"]
        else make_proposal(ActionType.PAY_SUPPLIER, "500.00", "USD", beneficiary_id="sup_old")
    )
    assert early(proposal)[0] == "ESCALATE"


def test_no_due_dates_at_all_escalates() -> None:
    assert early(pay(), obligation_due_dates=None)[0] == "ESCALATE"


def test_a_due_date_in_the_proposal_is_ignored() -> None:
    far = (TODAY + timedelta(days=30)).isoformat()
    proposal = pay(due_date=TODAY.isoformat(), due_on=TODAY.isoformat())
    assert early(proposal, obligation_due_dates={"obl_1": far})[0] == "ESCALATE"


def test_other_actions_are_ignored() -> None:
    for action in ("convert", "card"):
        proposal = __import__("policy_helpers").clean_proposals()[action]
        assert early(proposal, obligation_due_dates=None)[0] == "PASS"


def test_through_the_engine_an_early_payment_escalates_and_a_block_still_wins() -> None:
    context = full_context(obligation_due_dates={"obl_1": TODAY + timedelta(days=9)})
    assert evaluate(pay(), context, RULESET).verdict is Verdict.ESCALATE
    unapproved = pay(beneficiary_id="sup_unknown")
    assert evaluate(unapproved, context, RULESET).verdict is Verdict.BLOCK
