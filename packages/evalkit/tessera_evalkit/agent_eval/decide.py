"""Run the real planner and the real policy engine on one scenario, and reduce the result to a single `Choice`.

The scenario has one item in focus. The agent's choice is its first proposal that names that obligation (or that
obligation's invoice); with none, it is NO_ACTION. Proposals about anything else are counted as `off_focus` and never
scored as agreement, but they are reported: an agent that fixes the focus item and also moves money elsewhere has not
behaved like the treasurer.

Policy runs on every proposal with the scenario's own balances, beneficiary ages and simulation, at autonomy L3 (see
world.py). `verdict` is what the shipped ruleset says about the focus proposal; None when the agent proposed nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from tessera_agent import LLMClient, Planner
from tessera_core.domain import Proposal
from tessera_core.policy import Ruleset, evaluate

from tessera_evalkit.agent_eval.scenario import Choice, Scenario
from tessera_evalkit.agent_eval.world import ScenarioWorld
from tessera_evalkit.redteam.world import NOW

NO_ACTION = Choice(action_type="NO_ACTION")


@dataclass(frozen=True)
class AgentOutcome:
    scenario_id: str
    choice: Choice
    verdict: str | None  # APPROVE | ESCALATE | BLOCK for the focus proposal
    rules: tuple[str, ...]  # rule ids that did not pass for it
    plan_outcome: str  # the planner's outcome: ok | ok_empty | invalid_output | llm_error
    n_proposals: int
    off_focus: int


def _focus_keys(s: Scenario) -> tuple[set[str], set[str]]:
    focus = next(o for o in s.obligations if s.focus in (o.id, o.invoice_id))
    return {focus.id}, {focus.invoice_id} if focus.invoice_id else set()


def to_choice(p: Proposal) -> Choice:
    keep = ("amount", "currency", "to_currency", "obligation_id", "beneficiary_id", "invoice_id")
    fields = {k: v for k, v in p.params.items() if k in keep}
    return Choice(action_type=p.action_type.value, **fields)


def decide(s: Scenario, llm: LLMClient, ruleset: Ruleset) -> AgentOutcome:
    world = ScenarioWorld(s)
    result = Planner(llm).run(world.snapshot(), now=NOW)
    obligation_ids, invoice_ids = _focus_keys(s)
    focus: tuple[Proposal, Choice] | None = None
    off = 0
    for proposal in result.proposals:
        choice = to_choice(proposal)
        on_focus = choice.obligation_id in obligation_ids or choice.invoice_id in invoice_ids
        if on_focus and focus is None:
            focus = (proposal, choice)
        else:
            off += 1
    if focus is None:
        return AgentOutcome(s.id, NO_ACTION, None, (), result.record.outcome, len(result.proposals), off)
    proposal, choice = focus
    decision = evaluate(proposal, world.policy_context(proposal, result.flag_codes), ruleset)
    return AgentOutcome(
        s.id,
        choice,
        decision.verdict.value,
        tuple(decision.rule_ids),
        result.record.outcome,
        len(result.proposals),
        off,
    )
