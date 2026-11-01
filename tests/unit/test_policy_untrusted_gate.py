"""Day 10: untrusted_input_gate. Flags escalate; missing money-moving screening escalates; nothing ever approves."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from policy_helpers import clean_proposals, full_context, make_context, make_proposal, only, sim_for, usd
from pydantic import ValidationError
from tessera_core.domain import ActionType, Verdict
from tessera_core.policy import InputScreening, evaluate, load_ruleset

FULL = load_ruleset()
GATE = only("untrusted_input_gate.yaml")  # kill_switch + the gate alone
CODES = ("instruction_phrase", "role_markup", "payment_request", "limit_change", "homoglyph", "account_number")
MONEY = clean_proposals()
HOLD = make_proposal(ActionType.HOLD, "100.00", "USD", to_currency="EUR")
FLAG = make_proposal(ActionType.FLAG_INVOICE, invoice_id="INV-1", reason="likely_late")


def judge(proposal, screening, ruleset=FULL, **context):  # type: ignore[no-untyped-def]
    """Whole-ruleset decision. A money action gets the matching simulation (Day 8), so the ONLY thing under
    test is the screening result, not a missing simulation."""
    if proposal.action_type in (ActionType.CONVERT, ActionType.PAY_SUPPLIER):
        context.setdefault("simulation", sim_for(proposal))
    return evaluate(proposal, full_context(input_screening=screening, **context), ruleset)


@pytest.mark.parametrize("name", sorted(MONEY))
def test_a_clean_screen_leaves_a_clean_proposal_approved(name: str) -> None:
    assert judge(MONEY[name], InputScreening()).verdict is Verdict.APPROVE


@pytest.mark.parametrize("name", sorted(MONEY))
def test_any_flag_escalates_a_proposal_the_whole_ruleset_would_approve(name: str) -> None:
    decision = judge(MONEY[name], InputScreening(flag_codes=("instruction_phrase",)))
    assert decision.verdict is Verdict.ESCALATE and decision.rule_ids == ("untrusted_input_gate",)
    assert "instruction_phrase" in decision.reasons[0]


@pytest.mark.parametrize("proposal", [HOLD, FLAG], ids=["hold", "flag_invoice"])
def test_a_flag_escalates_actions_that_move_no_money_too(proposal) -> None:  # type: ignore[no-untyped-def]
    ctx = make_context(input_screening=InputScreening(flag_codes=("payment_request",)))
    assert evaluate(proposal, ctx, GATE).verdict is Verdict.ESCALATE


@pytest.mark.parametrize("name", sorted(MONEY))
def test_missing_screening_escalates_a_money_moving_action(name: str) -> None:
    decision = judge(MONEY[name], None)
    assert decision.verdict is Verdict.ESCALATE and "untrusted_input_gate" in decision.rule_ids


@pytest.mark.parametrize("proposal", [HOLD, FLAG], ids=["hold", "flag_invoice"])
def test_missing_screening_does_not_stop_an_action_that_moves_no_money(proposal) -> None:  # type: ignore[no-untyped-def]
    assert evaluate(proposal, make_context(), GATE).verdict is Verdict.APPROVE  # Day 7: kill-switch context only


def test_screening_present_and_empty_is_not_the_same_as_missing() -> None:
    assert judge(MONEY["pay"], InputScreening()).verdict is Verdict.APPROVE
    assert judge(MONEY["pay"], None).verdict is Verdict.ESCALATE


def test_a_block_from_another_rule_still_wins_over_the_flag() -> None:
    stranger = make_proposal(ActionType.PAY_SUPPLIER, "500.00", "USD", beneficiary_id="sup_stranger")
    decision = judge(stranger, InputScreening(flag_codes=("payment_request",)))
    assert decision.verdict is Verdict.BLOCK
    assert {"beneficiary_allowlist", "untrusted_input_gate"} <= set(decision.rule_ids)


def test_the_kill_switch_still_blocks_a_flagged_run() -> None:
    ctx = full_context(kill_switch_engaged=True, input_screening=InputScreening(flag_codes=("homoglyph",)))
    assert evaluate(MONEY["convert"], ctx, FULL).verdict is Verdict.BLOCK


def test_flagging_never_makes_a_decision_more_permissive() -> None:
    """For every shipped-clean proposal: flagged is never better than unflagged."""
    order = {Verdict.APPROVE: 0, Verdict.ESCALATE: 1, Verdict.BLOCK: 2}
    for proposal in (*MONEY.values(), HOLD, FLAG):
        clean = judge(proposal, InputScreening())
        flagged = judge(proposal, InputScreening(flag_codes=("limit_change",)))
        assert order[flagged.verdict] >= order[clean.verdict]


@given(
    codes=st.lists(st.sampled_from(CODES), min_size=1, max_size=6, unique=True),
    name=st.sampled_from(sorted(MONEY)),
    amount=st.integers(1, 5000),
)
def test_property_no_flagged_run_is_ever_approved(codes: list[str], name: str, amount: int) -> None:
    base = MONEY[name]
    proposal = base.model_copy(update={"params": {**base.params, "amount": f"{amount}.00"}})
    decision = judge(proposal, InputScreening(flag_codes=tuple(codes)))
    assert decision.verdict is not Verdict.APPROVE


def test_the_reason_contains_only_fixed_text_and_codes() -> None:
    decision = judge(MONEY["convert"], InputScreening(flag_codes=("instruction_phrase", "account_number")))
    assert decision.reasons == (
        "untrusted_input_gate: untrusted text behind this proposal was flagged (instruction_phrase, account_number)",
    )


def test_the_rationale_is_never_echoed_into_a_reason() -> None:
    proposal = make_proposal(ActionType.CONVERT, "100.00", "USD", rationale="ZQX-SECRET to=EUR", to_currency="EUR")
    decision = judge(proposal, InputScreening(flag_codes=("homoglyph",)))
    assert "ZQX-SECRET" not in " ".join(decision.reasons)


@pytest.mark.parametrize(
    "bad",
    [
        ("Ignore previous instructions",),  # text, not a code
        ("UPPER",),
        ("with space",),
        ("a" * 41,),
        ("x",) * 25,  # too many
        ("<script>",),
    ],
)
def test_screening_codes_cannot_carry_text(bad: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        InputScreening(flag_codes=bad)


def test_the_rule_has_no_threshold_to_aim_under() -> None:
    from policy_helpers import DEFAULT_RULES_DIR
    from tessera_core.policy import RulesetError, ruleset_from_yaml_texts

    kill = (DEFAULT_RULES_DIR / "kill_switch.yaml").read_text(encoding="utf-8")
    tuned = "id: untrusted_input_gate\ntype: untrusted_input_gate\nparams:\n  max_flags: 3\n"
    with pytest.raises(RulesetError):
        ruleset_from_yaml_texts({"kill_switch.yaml": kill, "untrusted_input_gate.yaml": tuned})


def test_adding_the_rule_changed_the_ruleset_hash_so_old_decisions_stay_traceable() -> None:
    names = ("kill_switch.yaml", "max_amount_per_action.yaml")
    from policy_helpers import DEFAULT_RULES_DIR
    from tessera_core.policy import ruleset_from_yaml_texts

    without = ruleset_from_yaml_texts({n: (DEFAULT_RULES_DIR / n).read_text(encoding="utf-8") for n in names})
    assert FULL.hash != without.hash and usd("1.00")
