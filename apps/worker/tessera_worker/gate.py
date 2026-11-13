"""The execution gate: what a FRESH policy verdict means for a proposal that was already approved.

Pure. Approval (by policy or by a human) is a statement about the world at that moment. Before anything is sent the
worker asks policy again with fresh context, and this function turns the new verdict into what happens next:

* fresh BLOCK                    -> BLOCK: stop. Nothing overrides a block, not even a human approval.
* fresh APPROVE                  -> APPROVE: go.
* fresh ESCALATE, human approved -> APPROVE only if the human's approval covered it: same ruleset, and every rule
                                    that escalates now also escalated when the human looked. Otherwise ESCALATE.
* fresh ESCALATE, no approval    -> ESCALATE: back to a human.

The last-but-one case exists because a human approves an ESCALATE precisely when a rule flagged it; without it the
same rule would flag it again at execution and the proposal could never run.
"""

from __future__ import annotations

from dataclasses import dataclass

from tessera_core.domain import PolicyDecision, Verdict


@dataclass(frozen=True)
class Covered:
    """What a human approval covered: the rules that escalated at that time, under one ruleset."""

    rule_ids: frozenset[str]
    ruleset_hash: str | None


def effective_verdict(fresh: PolicyDecision, covered: Covered | None) -> Verdict:
    if fresh.verdict is not Verdict.ESCALATE:
        return fresh.verdict
    if covered is None or covered.ruleset_hash is None or fresh.ruleset_hash != covered.ruleset_hash:
        return Verdict.ESCALATE
    if not fresh.rule_ids or not set(fresh.rule_ids) <= covered.rule_ids:
        return Verdict.ESCALATE
    return Verdict.APPROVE
