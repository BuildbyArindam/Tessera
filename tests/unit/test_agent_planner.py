"""Day 9: the planner, driven entirely by FakeLLM. No network, no real model."""

from __future__ import annotations

import json
from typing import Any

import pytest
from agent_helpers import (
    NOW,
    convert_item,
    first_open_invoice,
    item,
    make_mock,
    make_snapshot,
    mock_state,
    pay_item,
    reply,
)
from policy_helpers import full_context, sim_for
from tessera_agent import (
    FakeLLM,
    LLMError,
    LLMResponse,
    Planner,
    PlannerSnapshot,
    PlanResult,
    ToolCall,
    load_prompt,
    tool_call_reply,
)
from tessera_agent.planner import MAX_TOOL_CALLS, MAX_TOOL_ROUNDS
from tessera_core.domain import ActionType, Currency, Money, Verdict
from tessera_core.policy import ForecastConfidence, evaluate, load_ruleset
from test_agent_tools import digest

pytestmark = pytest.mark.usefixtures("no_network")

OBLIGATIONS = tool_call_reply("list_upcoming_obligations", {"days_ahead": 30})
SIMULATE = tool_call_reply(
    "simulate_proposal",
    {
        "action_type": "CONVERT",
        "amount": "3000.00",
        "currency": "USD",
        "to_currency": "INR",
        "obligation_id": "OBL-0001",
    },
    call_id="call-2",
)


@pytest.fixture(scope="module")
def snapshot() -> PlannerSnapshot:
    return make_snapshot()


def plan(snapshot: PlannerSnapshot, script: list[Any], **kw: Any) -> tuple[PlanResult, FakeLLM]:
    llm = FakeLLM(script, **kw)
    return Planner(llm).run(snapshot, now=NOW), llm


def good_run(snapshot: PlannerSnapshot) -> tuple[PlanResult, FakeLLM]:
    return plan(snapshot, [OBLIGATIONS, SIMULATE, reply(convert_item(evidence=("ev-1", "ev-2")))])


# ---- the happy path ---------------------------------------------------------------------------------
def test_tools_then_a_valid_reply_yields_one_proposal_with_a_full_record(snapshot: PlannerSnapshot) -> None:
    result, llm = good_run(snapshot)
    (proposal,) = result.proposals
    rec = result.record
    assert proposal.action_type is ActionType.CONVERT and proposal.created_at == NOW
    assert proposal.id.startswith("prp-") and proposal.params["obligation_id"] == "OBL-0001"
    assert proposal.params == {
        "amount": "3000.00",
        "currency": "USD",
        "to_currency": "INR",
        "obligation_id": "OBL-0001",
    }
    assert result.evidence == {proposal.id: ("ev-1", "ev-2")}
    assert (rec.outcome, rec.attempts, rec.llm_calls) == ("ok", 1, 3)
    assert rec.model == "fake-llm" and rec.prompt_hash == load_prompt().sha256 and rec.prompt_name == "planner_v1"
    assert [t.name for t in rec.tool_calls] == ["list_upcoming_obligations", "simulate_proposal"]
    assert rec.evidence_ids == ("ev-1", "ev-2") and len(rec.context_hash) == 64
    assert all(c.temperature == 0.0 for c in llm.calls)  # temperature 0 on every call
    assert all(c.system == load_prompt().text for c in llm.calls)
    results = llm.calls[2].messages[-1].tool_results  # what the model was shown for its second tool call
    assert json.loads(results[0].content)["evidence_id"] == "ev-2"


def test_the_same_state_gives_the_same_proposal_ids_and_a_different_state_does_not(snapshot: PlannerSnapshot) -> None:
    first, second = good_run(snapshot)[0], good_run(snapshot)[0]
    assert [p.id for p in first.proposals] == [p.id for p in second.proposals]
    changed = make_snapshot(balances=snapshot.balances[:2])
    other, _ = plan(changed, [OBLIGATIONS, SIMULATE, reply(convert_item(evidence=("ev-1", "ev-2")))])
    assert other.proposals[0].id != first.proposals[0].id and other.record.context_hash != first.record.context_hash


def test_a_single_json_fence_is_accepted(snapshot: PlannerSnapshot) -> None:
    text = "```json\n" + reply(convert_item()) + "\n```"
    result, _ = plan(snapshot, [OBLIGATIONS, text])
    assert len(result.proposals) == 1


def test_an_empty_list_is_a_valid_answer_and_costs_no_retry(snapshot: PlannerSnapshot) -> None:
    result, llm = plan(snapshot, ['{"proposals": []}'])
    assert result.proposals == () and result.record.outcome == "ok_empty" and len(llm.calls) == 1


# ---- (a) invalid JSON: retry once, then an empty list, never a crash -------------------------------------
def test_invalid_json_retries_once_then_returns_an_empty_list(snapshot: PlannerSnapshot) -> None:
    result, llm = plan(snapshot, ["I would convert some USD MARKER-XYZ", "still not json MARKER-XYZ"])
    assert result.proposals == () and result.record.outcome == "invalid_output"
    assert (result.record.attempts, result.record.llm_calls) == (2, 2)
    retry = llm.calls[1]
    assert retry.tools == ()  # no tools on the retry
    assert "MARKER-XYZ" not in (retry.messages[-1].text or "")  # the failure note never echoes the output
    assert result.record.validation_errors and result.record.raw_output == "still not json MARKER-XYZ"


def test_a_corrected_retry_is_accepted(snapshot: PlannerSnapshot) -> None:
    result, llm = plan(snapshot, [OBLIGATIONS, "oops", reply(convert_item())])
    assert len(result.proposals) == 1 and (result.record.outcome, result.record.attempts) == ("ok", 2)
    assert "format:" in (llm.calls[2].messages[-1].text or "")


@pytest.mark.parametrize("silent", [LLMResponse(text=None), LLMResponse(text="")])
def test_a_reply_with_no_text_is_just_invalid(snapshot: PlannerSnapshot, silent: LLMResponse) -> None:
    result, _ = plan(snapshot, [silent, silent])
    assert result.proposals == () and result.record.outcome == "invalid_output"


# ---- (b) extra field / (c) free-text account number ----------------------------------------------------
def test_a_proposal_with_an_extra_field_is_rejected(snapshot: PlannerSnapshot) -> None:
    bad = reply(convert_item() | {"execute_immediately": True})
    result, _ = plan(snapshot, [OBLIGATIONS, bad, bad])
    assert result.proposals == () and any("extra_forbidden" in e for e in result.record.validation_errors)


def test_a_free_text_account_number_is_rejected_and_never_echoed_back(snapshot: PlannerSnapshot) -> None:
    iban = "GB82WEST12345698765432"
    pay = pay_item(iban)
    result, llm = plan(snapshot, [OBLIGATIONS, reply(pay), reply(pay)])
    assert result.proposals == () and result.record.outcome == "invalid_output"
    assert all(iban not in (m.text or "") for m in llm.calls[2].messages if m.role == "user")
    for field in ("account_number", "iban", "beneficiary_name"):  # and no field exists that could carry one
        extra = pay_item(**{field: "x"})
        assert plan(snapshot, [OBLIGATIONS, reply(extra), reply(extra)])[0].proposals == ()


# ---- references and evidence must be real ---------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        convert_item(obligation_id="OBL-7777"),
        item("PAY_SUPPLIER", {"amount": "5.00", "currency": "INR", "from_currency": "USD", "beneficiary_id": "BEN-777",
                              "obligation_id": "OBL-0001"}),
        item("TOPUP_CARD", {"amount": "5.00", "currency": "USD", "vendor_id": "VND-UNKNOWN"}),
        item("FLAG_INVOICE", {"invoice_id": "INV-NOPE-1", "reason": "other"}),
    ],
)  # fmt: skip
def test_an_id_the_planner_was_not_given_is_rejected(snapshot: PlannerSnapshot, bad: dict[str, Any]) -> None:
    result, _ = plan(snapshot, [OBLIGATIONS, reply(bad), reply(bad)])
    assert result.proposals == () and any("unknown_id" in e for e in result.record.validation_errors)


def test_evidence_must_have_been_issued_in_this_run(snapshot: PlannerSnapshot) -> None:
    cites_nothing_real = reply(convert_item(evidence=("ev-9",)))
    result, _ = plan(snapshot, [OBLIGATIONS, cites_nothing_real, cites_nothing_real])
    assert result.proposals == () and any("unknown_evidence_id" in e for e in result.record.validation_errors)
    result, _ = plan(snapshot, [cites_nothing_real, cites_nothing_real])  # no tool was called at all
    assert result.proposals == ()


def test_a_failed_tool_call_is_not_evidence(snapshot: PlannerSnapshot) -> None:
    broken = tool_call_reply("get_forecast", {"currency": "BTC"})
    result, _ = plan(snapshot, [broken, reply(convert_item()), reply(convert_item())])
    assert result.record.evidence_ids == () and result.record.tool_calls[0].evidence_id is None
    assert result.proposals == ()


def test_more_than_five_proposals_is_invalid_not_truncated(snapshot: PlannerSnapshot) -> None:
    six = reply(*[convert_item(amount=f"{i}00.00") for i in range(1, 7)])
    result, _ = plan(snapshot, [OBLIGATIONS, six, six])
    assert result.proposals == ()
    five = reply(*[convert_item(amount=f"{i}00.00") for i in range(1, 6)])
    assert len(plan(snapshot, [OBLIGATIONS, five])[0].proposals) == 5


def test_the_same_action_twice_is_rejected(snapshot: PlannerSnapshot) -> None:
    dup = reply(convert_item(), convert_item())
    result, _ = plan(snapshot, [OBLIGATIONS, dup, dup])
    assert result.proposals == () and any("duplicate_proposal" in e for e in result.record.validation_errors)


# ---- fail safe ----------------------------------------------------------------------------------------------
def test_a_provider_failure_returns_an_empty_list_not_an_exception(snapshot: PlannerSnapshot) -> None:
    result, _ = plan(snapshot, [LLMError("provider call failed: APIError")])
    assert result.proposals == () and result.record.outcome == "llm_error"
    result, _ = plan(snapshot, ["not json", LLMError("down")])  # failure on the retry itself
    assert result.proposals == () and result.record.outcome == "llm_error"


def test_tool_rounds_are_bounded_and_tools_are_switched_off_for_the_last_one(snapshot: PlannerSnapshot) -> None:
    script = [tool_call_reply("get_forecast", {}, call_id=f"c{i}") for i in range(MAX_TOOL_ROUNDS + 1)]
    result, llm = plan(snapshot, [*script, '{"proposals": []}'])
    assert llm.calls[0].tools and llm.calls[MAX_TOOL_ROUNDS].tools == ()
    assert len(result.record.tool_calls) == MAX_TOOL_ROUNDS and result.record.outcome == "ok_empty"


def test_total_tool_calls_are_bounded(snapshot: PlannerSnapshot) -> None:
    burst = LLMResponse(tool_calls=tuple(ToolCall(f"c{i}", "get_forecast", {}) for i in range(3)))
    result, _ = plan(snapshot, [burst] * 5 + ['{"proposals": []}'])
    traces = result.record.tool_calls
    assert len(traces) == 15 and sum(t.evidence_id is not None for t in traces) == MAX_TOOL_CALLS
    assert all(t.evidence_id is None for t in traces[MAX_TOOL_CALLS:])


# ---- (e) tools cannot mutate state: the Airwallex mock and the snapshot are identical afterwards ----------------
def test_a_full_planner_run_changes_neither_the_mock_adapter_nor_the_snapshot() -> None:
    mock = make_mock()
    snap = make_snapshot(mock)
    before_mock, before_snap = mock_state(mock), digest(snap)
    calls = [
        tool_call_reply("get_forecast", {}, "a"),
        tool_call_reply("get_fx_range", {"pair": "USD/INR", "horizon_days": 30}, "b"),
        tool_call_reply("get_late_risk", {"invoice_ids": [first_open_invoice()]}, "c"),
        tool_call_reply("simulate_proposal", {"action_type": "HOLD", "amount": "1000.00", "currency": "EUR",
                                              "to_currency": "INR", "obligation_id": "OBL-0002"}, "d"),
        tool_call_reply("list_upcoming_obligations", {}, "e"),
    ]  # fmt: skip
    result, _ = plan(snap, [*calls, reply(convert_item(evidence=("ev-1", "ev-5")))])
    assert len(result.proposals) == 1 and len(result.record.tool_calls) == 5
    assert mock_state(mock) == before_mock and digest(snap) == before_snap


def test_a_different_prompt_file_changes_the_recorded_hash(snapshot: PlannerSnapshot) -> None:
    other = load_prompt().__class__(name="planner_test", text="x", sha256="0" * 64)
    result = Planner(FakeLLM(['{"proposals": []}']), prompt=other).run(snapshot, now=NOW)
    assert result.record.prompt_hash == "0" * 64 and result.record.prompt_name == "planner_test"


# ---- the planner proposes, the policy engine disposes --------------------------------------------------------------
def policy_context() -> Any:
    """Everything the shipped ruleset needs for USD, EUR and INR actions, with BEN-001 the only approved payee."""
    money = {
        c: Money(amount=a, currency=c)
        for c, a in ((Currency.USD, "50000"), (Currency.EUR, "20000"), (Currency.INR, "900000"))
    }
    rates = {"USD/INR": "83.00", "EUR/INR": "90.00"}
    return full_context(
        balances=money,
        beneficiary_allowlist=frozenset({"BEN-001"}),
        beneficiary_added_at={"BEN-001": NOW.replace(year=2025)},
        fx_reference_rate=rates,
        fx_quote_rate=rates,
        forecast_confidence={c: ForecastConfidence(relative_width="0.2") for c in money},
    )


def test_every_action_type_the_planner_emits_is_readable_by_the_real_policy_engine(snapshot: PlannerSnapshot) -> None:
    items = [
        convert_item(),
        item("HOLD", {"amount": "1000.00", "currency": "EUR", "to_currency": "INR", "obligation_id": "OBL-0002"}),
        pay_item(),
        item("TOPUP_CARD", {"amount": "100.00", "currency": "USD", "vendor_id": "VND-AWS"}),
        item("FLAG_INVOICE", {"invoice_id": first_open_invoice(), "reason": "likely_late"}),
    ]  # fmt: skip
    result, _ = plan(snapshot, [OBLIGATIONS, reply(*items)])
    assert [p.action_type for p in result.proposals] == [
        ActionType.CONVERT, ActionType.HOLD, ActionType.PAY_SUPPLIER, ActionType.TOPUP_CARD, ActionType.FLAG_INVOICE
    ]  # fmt: skip
    ruleset = load_ruleset()
    for proposal in result.proposals:
        ctx = policy_context().model_copy(update={"simulation": sim_for(proposal)})
        decision = evaluate(proposal, ctx, ruleset)
        assert "policy_error" not in " ".join(decision.reasons), (proposal.action_type, decision.reasons)


def test_a_fooled_planner_still_cannot_pay_an_unapproved_beneficiary(snapshot: PlannerSnapshot) -> None:
    """BEN-002 is a real id in the snapshot, so the planner accepts the proposal. Policy must not."""
    pay = pay_item("BEN-002")
    result, _ = plan(snapshot, [OBLIGATIONS, reply(pay)])
    (proposal,) = result.proposals
    ctx = policy_context().model_copy(update={"simulation": sim_for(proposal)})
    decision = evaluate(proposal, ctx, load_ruleset())
    assert decision.verdict is Verdict.BLOCK and "beneficiary_allowlist" in decision.rule_ids
